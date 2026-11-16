"""Fastly URL-purge provider.

Fastly does not expose a bulk URL purge endpoint — the API accepts one
URL per request via ``POST /purge/{host}{path}``. This provider issues
those calls in parallel with bounded concurrency.

(Fastly *does* support bulk purges via surrogate keys, but that requires
the origin service to emit ``Surrogate-Key`` headers and a different
config model. We stick with URL purges to match the daemon's
per-filesystem-path event model.)

Optionally, when ``purge_all_service_domains`` is True, the provider
calls Fastly's API at first use to discover every domain attached to
the service and expands each input URL across all of them — so one
filesystem event flushes the cache on every host the service serves.

See ``docs/cdn-providers.md`` for the full API contract and error
classification.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import httpx

from mirror_mirage.providers.base import PurgeResult

FASTLY_API_BASE = "https://api.fastly.com"
FASTLY_DEFAULT_CONCURRENCY = 10

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_log = logging.getLogger("mirror_mirage.fastly")


def _purge_endpoint(url: str) -> str:
    """Translate a target URL into Fastly's per-URL purge endpoint.

    Format: ``https://api.fastly.com/purge/{host}{path}[?{query}]``. The
    scheme of the target URL is dropped (Fastly infers it from the
    service config). Path and query are passed through as-is.
    """
    parsed = urlparse(url)
    suffix = parsed.netloc + parsed.path
    if parsed.query:
        suffix += "?" + parsed.query
    return f"{FASTLY_API_BASE}/purge/{suffix}"


def _domain_variants(url: str, domains: list[str]) -> list[str]:
    """Return ``url`` plus a sibling URL for each domain in ``domains``.

    The URL is emitted as-is, plus one copy per domain with the netloc
    swapped. Deduplicates so a URL whose host already appears in
    ``domains`` is only purged once.
    """
    parsed = urlparse(url)
    variants: list[str] = []
    for host in [parsed.netloc, *domains]:
        replaced = urlunparse(parsed._replace(netloc=host))
        if replaced not in variants:
            variants.append(replaced)
    return variants


def _combine_variants(variants: list[str], results: Mapping[str, PurgeResult]) -> PurgeResult:
    """Fold the outcomes of one input URL's domain variants into one outcome.

    The input URL is done only when every variant succeeded. If any
    variant can be retried, the whole input URL is retried (re-purging
    its other variants is harmless), so a terminal error on one domain
    never blocks the retry of another. Failure messages name the host.
    """
    if len(variants) == 1:
        return results[variants[0]]
    failures = [(v, results[v]) for v in variants if not results[v].ok]
    if not failures:
        return PurgeResult(ok=True, retryable=False, message="ok")
    return PurgeResult(
        ok=False,
        retryable=any(r.retryable for _, r in failures),
        message="; ".join(f"{urlparse(v).netloc}: {r.message}" for v, r in failures),
    )


class FastlyProvider:
    """Purges URLs via Fastly's per-URL purge API."""

    def __init__(
        self,
        *,
        service_id: str,
        api_token: str,
        client: httpx.AsyncClient | None = None,
        concurrency: int = FASTLY_DEFAULT_CONCURRENCY,
        purge_all_service_domains: bool = False,
    ) -> None:
        """Create a provider for a single Fastly service.

        Parameters
        ----------
        service_id
            Fastly service ID this binding maps to. Used both for
            operator-facing logging and (when ``purge_all_service_domains``
            is True) for the domain-discovery API call.
        api_token
            Token sent in the ``Fastly-Key`` header on every request.
            Needs ``purge_select`` scope for purges; needs ``global:read``
            (or wider) too when ``purge_all_service_domains`` is True.
        client
            Optional injected ``httpx.AsyncClient`` for testing. When
            omitted, the provider creates and owns its own client.
        concurrency
            Maximum in-flight purge requests at any moment.
        purge_all_service_domains
            When True, on first use the provider fetches every domain
            attached to ``service_id`` and expands each input URL across
            all of them. Useful when the same Fastly service serves the
            mirror content under multiple hostnames (e.g.
            ``repo.example.org`` and ``mirror.example.org``). Discovery
            happens once per daemon lifetime; restart to pick up domains
            added later.
        """
        self._service_id = service_id
        self._api_token = api_token
        self._client = client
        self._own_client = client is None
        self._sem = asyncio.Semaphore(concurrency)
        self._purge_all_service_domains = purge_all_service_domains
        self._domains: list[str] | None = None
        self._domains_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _api_get(self, path: str) -> tuple[httpx.Response | None, PurgeResult | None]:
        """GET a Fastly API path with the configured token.

        Returns ``(response, None)`` on success or ``(None, error)`` on
        transport failure. HTTP status is the caller's responsibility.
        """
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{FASTLY_API_BASE}{path}",
                headers={
                    "Fastly-Key": self._api_token,
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            return None, PurgeResult(ok=False, retryable=True, message=f"transport: {e}")
        return resp, None

    async def _ensure_domains(self) -> tuple[list[str], PurgeResult | None]:
        """Return the list of domains attached to this service.

        Lazy + cached: the first call hits Fastly's API; subsequent
        calls reuse the result. On failure, the cache is left empty so
        the next purge tries again — eventually picking up a fixed
        token or a recovered API.
        """
        async with self._domains_lock:
            if self._domains is not None:
                return self._domains, None

            # 1. Find the active version.
            resp, err = await self._api_get(f"/service/{self._service_id}")
            if err is not None:
                return [], err
            assert resp is not None
            if not 200 <= resp.status_code < 300:
                retryable = resp.status_code in _RETRYABLE_STATUS
                return [], PurgeResult(
                    ok=False,
                    retryable=retryable,
                    message=f"domain discovery: HTTP {resp.status_code}",
                )
            service = cast(dict[str, Any], resp.json())
            versions = cast(list[dict[str, Any]], service.get("versions", []))
            active_version: int | None = None
            for v in versions:
                if v.get("active"):
                    active_version = cast(int, v.get("number"))
                    break
            if active_version is None:
                return [], PurgeResult(
                    ok=False,
                    retryable=False,
                    message=(
                        f"domain discovery: service {self._service_id!r} has no active version"
                    ),
                )

            # 2. List domains on the active version.
            resp, err = await self._api_get(
                f"/service/{self._service_id}/version/{active_version}/domain"
            )
            if err is not None:
                return [], err
            assert resp is not None
            if not 200 <= resp.status_code < 300:
                retryable = resp.status_code in _RETRYABLE_STATUS
                return [], PurgeResult(
                    ok=False,
                    retryable=retryable,
                    message=f"domain discovery: HTTP {resp.status_code}",
                )
            raw_domains = cast(list[dict[str, Any]], resp.json())
            domains: list[str] = [str(d["name"]) for d in raw_domains if "name" in d]
            self._domains = domains
            _log.info(
                "event=fastly_domains_discovered service_id=%s version=%d count=%d domains=%s",
                self._service_id,
                active_version,
                len(domains),
                ",".join(domains),
            )
            return domains, None

    async def _purge_one(self, url: str) -> PurgeResult:
        async with self._sem:
            client = await self._get_client()
            endpoint = _purge_endpoint(url)
            try:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Fastly-Key": self._api_token,
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as e:
                return PurgeResult(ok=False, retryable=True, message=f"transport: {e}")
            if 200 <= resp.status_code < 300:
                return PurgeResult(ok=True, retryable=False, message="ok")
            if resp.status_code in _RETRYABLE_STATUS:
                return PurgeResult(
                    ok=False,
                    retryable=True,
                    message=f"HTTP {resp.status_code}",
                )
            return PurgeResult(
                ok=False,
                retryable=False,
                message=f"HTTP {resp.status_code}",
            )

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Issue one ``POST /purge/{host}{path}`` per URL, bounded by ``concurrency``.

        If ``purge_all_service_domains`` was set, each input URL is
        first expanded into one variant per domain attached to the
        service, and every distinct variant is purged once.

        The result carries each *input* URL's outcome in ``per_url``, so
        the queue worker retries only the URLs that hit a retryable
        error. With domain expansion, an input URL's outcome combines
        all of its variants (see :func:`_combine_variants`).
        """
        if not urls:
            return PurgeResult(ok=True, retryable=False, message="empty")

        if self._purge_all_service_domains:
            domains, err = await self._ensure_domains()
            if err is not None:
                return err
            variants = {url: _domain_variants(url, domains) for url in urls}
        else:
            variants = {url: [url] for url in urls}

        # Purge each distinct URL once, even if several inputs expand to it.
        targets = list(dict.fromkeys(v for vs in variants.values() for v in vs))
        results = dict(
            zip(targets, await asyncio.gather(*(self._purge_one(t) for t in targets)), strict=True)
        )
        return PurgeResult.from_per_url(
            {url: _combine_variants(vs, results) for url, vs in variants.items()}
        )
