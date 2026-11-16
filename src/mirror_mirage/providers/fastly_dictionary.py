"""Client for the items of one Fastly edge dictionary.

Used by :mod:`mirror_mirage.edge_aliases` to read a dictionary's current
items and apply a diff to it. Dictionary items are versionless: changing
them takes effect within seconds and does not need a service version to be
cloned or activated. The dictionary itself must already exist on the
active version (creating it does need an activation, done once by hand).

API calls:

* ``GET /service/{service_id}`` — find the active version.
* ``GET /service/{service_id}/version/{version}/dictionary/{name}`` —
  resolve the dictionary's ID (stable across versions; cached).
* ``GET /service/{service_id}/dictionary/{id}/items`` — list items, paged.
* ``PATCH /service/{service_id}/dictionary/{id}/items`` — apply up to 1000
  ``upsert`` / ``delete`` operations per request.

The token needs ``global`` scope for the PATCH (Fastly has no narrower
scope that can write dictionary items); limit it to the services Mirror Mirage
manages.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx

from mirror_mirage.providers.fastly import _RETRYABLE_STATUS, FASTLY_API_BASE

_log = logging.getLogger("mirror_mirage.fastly_dictionary")

PAGE_SIZE = 100
BATCH_LIMIT = 1000
# Stop paging after this many pages even if the API keeps returning full
# pages of new keys; far above any dictionary Fastly allows.
_MAX_PAGES = 1000


class DictionaryError(Exception):
    """A dictionary API call failed. ``retryable`` is False for 4xx other than 429."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class FastlyDictionaryClient:
    """Reads and writes the items of one edge dictionary on one service."""

    def __init__(
        self,
        *,
        service_id: str,
        api_token: str,
        dictionary: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._service_id = service_id
        self._api_token = api_token
        self._dictionary = dictionary
        self._client = client
        self._own_client = client is None
        self._dictionary_id: str | None = None

    @property
    def dictionary(self) -> str:
        return self._dictionary

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def items(self) -> dict[str, str]:
        """Return every item in the dictionary as ``{key: value}``."""
        dictionary_id = await self._resolve_id()
        path = f"/service/{self._service_id}/dictionary/{dictionary_id}/items"
        items: dict[str, str] = {}
        for page in range(1, _MAX_PAGES + 1):
            resp = await self._request(
                "GET",
                path,
                params={"page": page, "per_page": PAGE_SIZE},
            )
            batch = cast(list[dict[str, Any]], resp.json())
            new = 0
            for item in batch:
                key = str(item["item_key"])
                if key not in items:
                    new += 1
                items[key] = str(item["item_value"])
            # A short page is the last one. A page with nothing new means
            # the API ignored the page parameter; stop rather than loop.
            if len(batch) < PAGE_SIZE or new == 0:
                break
        return items

    async def apply(self, ops: list[dict[str, str]]) -> None:
        """Apply ``upsert`` / ``delete`` operations in batches of 1000."""
        if not ops:
            return
        dictionary_id = await self._resolve_id()
        path = f"/service/{self._service_id}/dictionary/{dictionary_id}/items"
        for start in range(0, len(ops), BATCH_LIMIT):
            await self._request("PATCH", path, json={"items": ops[start : start + BATCH_LIMIT]})

    async def _resolve_id(self) -> str:
        if self._dictionary_id is not None:
            return self._dictionary_id
        resp = await self._request("GET", f"/service/{self._service_id}")
        service = cast(dict[str, Any], resp.json())
        version = next(
            (
                cast(int, v.get("number"))
                for v in cast(list[dict[str, Any]], service.get("versions", []))
                if v.get("active")
            ),
            None,
        )
        if version is None:
            raise DictionaryError(
                f"service {self._service_id!r} has no active version", retryable=False
            )
        resp = await self._request(
            "GET",
            f"/service/{self._service_id}/version/{version}/dictionary/{self._dictionary}",
            not_found=f"dictionary {self._dictionary!r} not found on active version {version}",
        )
        body = cast(dict[str, Any], resp.json())
        if body.get("write_only"):
            raise DictionaryError(
                f"dictionary {self._dictionary!r} is write-only; mirror-mirage must read "
                f"its items to compute a diff",
                retryable=False,
            )
        self._dictionary_id = str(body["id"])
        _log.info(
            "event=fastly_dictionary_resolved service_id=%s version=%d dictionary=%s id=%s",
            self._service_id,
            version,
            self._dictionary,
            self._dictionary_id,
        )
        return self._dictionary_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient()
        try:
            resp = await self._client.request(
                method,
                f"{FASTLY_API_BASE}{path}",
                params=params,
                json=json,
                headers={"Fastly-Key": self._api_token, "Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise DictionaryError(f"transport: {e}", retryable=True) from e
        if 200 <= resp.status_code < 300:
            return resp
        if resp.status_code == 404:
            # The cached dictionary ID may be stale (dictionary recreated).
            self._dictionary_id = None
            if not_found is not None:
                raise DictionaryError(not_found, retryable=False)
        raise DictionaryError(
            f"{method} {path}: HTTP {resp.status_code}",
            retryable=resp.status_code in _RETRYABLE_STATUS,
        )
