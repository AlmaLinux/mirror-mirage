"""Common provider protocol and result type."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of a single :meth:`CdnProvider.purge` call.

    Attributes
    ----------
    ok
        True if every URL in the batch was accepted by the CDN.
    retryable
        Only meaningful when ``ok`` is False. When True, the queue worker
        will reschedule the job with exponential backoff. When False, the
        job is moved to ``failed`` status and requires operator action.
        For a result with ``per_url`` set, True means at least one URL
        can be retried.
    message
        Human-readable description of the outcome — surfaced in logs and
        in the ``failed`` job's recorded message.
    per_url
        Outcome for each input URL, for providers that make one API call
        per URL. When set, the queue worker retries only the URLs whose
        own outcome is retryable, logs the terminal ones as failed, and
        drops the ones that succeeded. When ``None`` (a provider that
        sends the batch as one API call), the outcome applies to the
        whole batch. Build it with :meth:`from_per_url`.
    """

    ok: bool
    retryable: bool
    message: str = ""
    per_url: Mapping[str, PurgeResult] | None = field(default=None, compare=False)

    @classmethod
    def from_per_url(cls, results: Mapping[str, PurgeResult]) -> PurgeResult:
        """Summarize per-URL outcomes into one batch result that keeps them."""
        failures = [r for r in results.values() if not r.ok]
        if not failures:
            return cls(ok=True, retryable=False, message="ok", per_url=dict(results))
        return cls(
            ok=False,
            retryable=any(r.retryable for r in failures),
            message=summarize_messages(failures),
            per_url=dict(results),
        )


def summarize_messages(results: Iterable[PurgeResult]) -> str:
    """Join result messages, collapsing repeats into a count.

    ``["HTTP 429", "HTTP 429", "HTTP 404"]`` becomes ``"2x HTTP 429; HTTP 404"``,
    so a 450-URL batch that hit the same error everywhere produces one
    short message instead of 450 copies.
    """
    counts = Counter(r.message for r in results)
    return "; ".join(msg if n == 1 else f"{n}x {msg}" for msg, n in counts.items())


class CdnProvider(Protocol):
    """Async CDN purge client."""

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Purge the given list of URLs from the CDN.

        Implementations may chunk ``urls`` internally if the CDN imposes
        per-call size limits (Fastly's 256-URL cap, for example). They
        must classify errors as retryable vs terminal per the rules in
        ``docs/cdn-providers.md``. Providers that purge URL by URL should
        return :meth:`PurgeResult.from_per_url` so that only the URLs
        that failed are retried.
        """
        ...
