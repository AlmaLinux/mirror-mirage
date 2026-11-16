"""Top-level orchestrator.

Wires together :class:`~mirror_mirage.watcher.InotifyWatcher`,
:class:`~mirror_mirage.filter.EventFilter`, :class:`~mirror_mirage.mapper.Mapper`,
:class:`~mirror_mirage.batcher.Batcher`, :class:`~mirror_mirage.queue.PersistentQueue`,
and one provider worker per CDN. Handles graceful shutdown on SIGTERM /
SIGINT and emits ``sd_notify("READY=1")`` once the watcher is online.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from mirror_mirage.batcher import Batcher
from mirror_mirage.config import Config, RetryConfig
from mirror_mirage.edge_aliases import EdgeAliasSync, build_edge_syncs
from mirror_mirage.filter import EventFilter
from mirror_mirage.mapper import Mapper
from mirror_mirage.providers import build_providers
from mirror_mirage.providers.base import CdnProvider, PurgeResult, summarize_messages
from mirror_mirage.queue import PersistentQueue, exponential_backoff
from mirror_mirage.symlinks import SymlinkIndex
from mirror_mirage.watcher import InotifyWatcher

_log = logging.getLogger("mirror_mirage.daemon")

_WORKER_IDLE_POLL_SECONDS = 0.2
_URLS_LOG_LIMIT = 5


def _urls_field(urls: list[str], max_show: int = _URLS_LOG_LIMIT) -> str:
    """Render a URL list for a one-line log field.

    Small batches show every URL; larger batches show the first ``max_show``
    plus a ``+N more`` suffix so a single log line stays grep-friendly even
    for big rsync pushes. The full list always remains in the SQLite queue
    (``urls_json`` on the corresponding row in ``/var/lib/mirror-mirage/queue.db``).
    """
    if len(urls) <= max_show:
        return ",".join(urls)
    return ",".join(urls[:max_show]) + f",+{len(urls) - max_show}-more"


@dataclass(frozen=True)
class _Unpurged:
    """URLs from a failed attempt, split by what should happen to them next."""

    retry_urls: list[str]
    retry_message: str
    failed_urls: list[str]
    failed_message: str


def _split_unpurged(result: PurgeResult, urls: list[str]) -> _Unpurged:
    """Split a failed attempt's URLs into ones to retry and ones that failed for good.

    URLs the provider reports as purged are left out entirely. A result
    without ``per_url`` applies to the whole batch.
    """
    if result.per_url is None:
        if result.retryable:
            return _Unpurged(list(urls), result.message, [], "")
        return _Unpurged([], "", list(urls), result.message)
    retry = {u: r for u, r in result.per_url.items() if not r.ok and r.retryable}
    failed = {u: r for u, r in result.per_url.items() if not r.ok and not r.retryable}
    return _Unpurged(
        list(retry),
        summarize_messages(retry.values()),
        list(failed),
        summarize_messages(failed.values()),
    )


async def _queue_worker(
    queue: PersistentQueue,
    cdn_name: str,
    provider: CdnProvider,
    retry: RetryConfig,
) -> None:
    """Drain the persistent queue for one CDN."""
    while True:
        job = await queue.claim_ready(cdn_name)
        if job is None:
            await asyncio.sleep(_WORKER_IDLE_POLL_SECONDS)
            continue
        attempt = job.attempts + 1
        _log.info(
            "event=purge_attempt cdn=%s job_id=%d batch_size=%d attempt=%d",
            cdn_name,
            job.id,
            len(job.urls),
            attempt,
        )
        start = time.monotonic()
        try:
            result = await provider.purge(job.urls)
        except Exception as e:
            result = PurgeResult(ok=False, retryable=True, message=f"provider raised: {e}")
        latency_ms = (time.monotonic() - start) * 1000.0

        if result.ok:
            _log.info(
                "event=purge_ok cdn=%s job_id=%d batch_size=%d latency_ms=%.0f urls=%s",
                cdn_name,
                job.id,
                len(job.urls),
                latency_ms,
                _urls_field(job.urls),
            )
            await queue.mark_done(job.id)
            continue

        unpurged = _split_unpurged(result, job.urls)
        if unpurged.retry_urls and attempt < retry.max_attempts:
            if unpurged.failed_urls:
                # These URLs are dropped from the job, so this log line is
                # their only record: list them in full, not truncated.
                _log.error(
                    "event=purge_failed cdn=%s job_id=%d attempts=%d error=%r urls=%s",
                    cdn_name,
                    job.id,
                    attempt,
                    unpurged.failed_message,
                    ",".join(unpurged.failed_urls),
                )
            backoff = exponential_backoff(
                attempt,
                initial=retry.initial_backoff_seconds,
                ceiling=retry.max_backoff_seconds,
            )
            _log.warning(
                "event=purge_retry cdn=%s job_id=%d attempt=%d backoff_s=%.2f error=%r urls=%s",
                cdn_name,
                job.id,
                attempt,
                backoff,
                unpurged.retry_message,
                _urls_field(unpurged.retry_urls),
            )
            await queue.reschedule(
                job.id,
                attempts=attempt,
                next_attempt_at=time.time() + backoff,
                urls=unpurged.retry_urls,
            )
        else:
            failed_urls = unpurged.failed_urls + unpurged.retry_urls
            message = "; ".join(m for m in (unpurged.failed_message, unpurged.retry_message) if m)
            _log.error(
                "event=purge_failed cdn=%s job_id=%d attempts=%d error=%r urls=%s",
                cdn_name,
                job.id,
                attempt,
                message,
                _urls_field(failed_urls),
            )
            await queue.mark_failed(job.id, message, urls=failed_urls)


async def _pipeline(
    watcher: InotifyWatcher,
    filter_: EventFilter,
    mapper: Mapper,
    batcher: Batcher,
    symlinks: SymlinkIndex,
    edge_syncs: dict[str, EdgeAliasSync] | None = None,
) -> None:
    """Pump inotify events through filter → mapper → batcher.

    Every event updates the symlink index first, before the filter can
    drop it, so the mapper's alias lookups see the current links. A link
    change, or a purge target whose URL matches a CDN's edge alias rules,
    also asks that CDN's edge dictionary sync to run.
    """
    edge_syncs = edge_syncs or {}
    async for mask_name, abs_path, _watch_root in watcher:
        if symlinks.observe(mask_name, abs_path):
            for sync in edge_syncs.values():
                sync.notify()
        event = filter_.normalize(mask_name, abs_path)
        if event is None:
            continue
        for target in mapper.map(event):
            cdn_sync = edge_syncs.get(target.cdn_name)
            if cdn_sync is not None and cdn_sync.wants(target.url):
                cdn_sync.notify()
            await batcher.submit(target)


async def run(cfg: Config, *, dry_run: bool = False) -> None:
    """Run the daemon until canceled.

    Composes the pipeline described in :mod:`mirror_mirage.daemon`'s module
    docstring. Returns cleanly after draining in-flight buffers when the
    caller cancels the surrounding task.
    """
    queue = await PersistentQueue.open(cfg.daemon.queue_db)
    try:
        recovered = await queue.reset_in_flight()
        if recovered:
            _log.info("event=in_flight_recovered count=%d", recovered)

        providers = build_providers(cfg, dry_run=dry_run)
        batcher = Batcher(queue, cfg.daemon)
        symlinks = SymlinkIndex()
        mapper = Mapper(
            cfg.watches,
            symlinks=symlinks,
            max_aliases=cfg.daemon.max_aliases_per_event,
        )
        filter_ = EventFilter(cfg.watches)
        edge_syncs = build_edge_syncs(cfg, dry_run=dry_run)

        async with InotifyWatcher(cfg.watches, symlinks=symlinks) as watcher:
            _log.info(
                "event=ready dry_run=%s watches=%d cdns=%d symlinks=%d edge_alias_cdns=%d",
                dry_run,
                len(cfg.watches),
                len(providers),
                len(symlinks),
                len(edge_syncs),
            )
            async with asyncio.TaskGroup() as tg:
                for cdn_name, provider in providers.items():
                    tg.create_task(
                        _queue_worker(queue, cdn_name, provider, cfg.daemon.retry),
                        name=f"mirror-mirage-worker-{cdn_name}",
                    )
                for cdn_name, sync in edge_syncs.items():
                    tg.create_task(sync.run(), name=f"mirror-mirage-edge-aliases-{cdn_name}")
                tg.create_task(batcher.run(), name="mirror-mirage-batcher")
                tg.create_task(
                    _pipeline(watcher, filter_, mapper, batcher, symlinks, edge_syncs),
                    name="mirror-mirage-pipeline",
                )
    finally:
        await queue.close()
