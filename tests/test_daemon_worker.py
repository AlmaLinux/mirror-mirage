"""Tests for the per-CDN queue worker in :mod:`mirror_mirage.daemon`.

Drives :func:`~mirror_mirage.daemon._queue_worker` against a real
:class:`~mirror_mirage.queue.PersistentQueue` and a scripted provider, and
checks what each attempt sends and what ends up in the queue: only the
URLs that hit a retryable error are retried, URLs that failed for good
are logged and dropped, and URLs that succeeded are never purged again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from mirror_mirage.config import RetryConfig
from mirror_mirage.daemon import _queue_worker
from mirror_mirage.providers.base import PurgeResult
from mirror_mirage.queue import PersistentQueue

OK = PurgeResult(ok=True, retryable=False, message="ok")
GONE = PurgeResult(ok=False, retryable=False, message="HTTP 404")
BUSY = PurgeResult(ok=False, retryable=True, message="HTTP 429")

# Zero backoff so rescheduled jobs are claimable straight away.
FAST_RETRY = RetryConfig(max_attempts=3, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)


class ScriptedProvider:
    """Purges URL by URL; ``outcome(url, call_number)`` decides each result."""

    def __init__(self, outcome: Callable[[str, int], PurgeResult]) -> None:
        self._outcome = outcome
        self.batches: list[list[str]] = []

    async def purge(self, urls: list[str]) -> PurgeResult:
        self.batches.append(list(urls))
        call = len(self.batches)
        return PurgeResult.from_per_url({u: self._outcome(u, call) for u in urls})


class WholeBatchProvider:
    """Reports one outcome for the whole batch, like a single-call CDN API."""

    def __init__(self, results: list[PurgeResult]) -> None:
        self._results = results
        self.batches: list[list[str]] = []

    async def purge(self, urls: list[str]) -> PurgeResult:
        self.batches.append(list(urls))
        return self._results[len(self.batches) - 1]


@pytest.fixture
async def queue(tmp_path: Path):
    q = await PersistentQueue.open(tmp_path / "queue.db")
    yield q
    await q.close()


async def _job_row(queue: PersistentQueue, job_id: int) -> tuple[str, list[str], str | None]:
    cursor = await queue._conn.execute(
        "SELECT status, urls_json, message FROM jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return row[0], json.loads(row[1]), row[2]


async def _run_until_settled(
    queue: PersistentQueue, provider: object, job_id: int
) -> tuple[str, list[str], str | None]:
    """Run the worker until ``job_id`` is done or failed; return its row."""
    task = asyncio.create_task(_queue_worker(queue, "cdn", provider, FAST_RETRY))  # type: ignore[arg-type]
    try:
        for _ in range(500):
            row = await _job_row(queue, job_id)
            if row[0] in ("done", "failed"):
                return row
            await asyncio.sleep(0.01)
        raise AssertionError(f"job {job_id} never settled: {row}")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestPerUrlRetry:
    async def test_retries_only_retryable_urls(
        self, queue: PersistentQueue, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mirror_mirage.daemon")
        gone = [f"https://x/gone{i}" for i in range(6)]
        busy = ["https://x/busy1", "https://x/busy2"]
        urls = ["https://x/ok", *gone, *busy]

        def outcome(url: str, call: int) -> PurgeResult:
            if "gone" in url:
                return GONE
            if "busy" in url and call == 1:
                return BUSY
            return OK

        provider = ScriptedProvider(outcome)
        job_id = await queue.enqueue("cdn", urls)
        status, _, _ = await _run_until_settled(queue, provider, job_id)

        assert status == "done"
        # The second attempt covers only the rate-limited URLs.
        assert provider.batches == [urls, busy]

        failed_lines = [
            r.getMessage() for r in caplog.records if "event=purge_failed" in r.getMessage()
        ]
        assert len(failed_lines) == 1
        # Dropped URLs are listed in full: the log is their only record.
        assert f"urls={','.join(gone)}" in failed_lines[0]
        assert "error='6x HTTP 404'" in failed_lines[0]

        retry_lines = [
            r.getMessage() for r in caplog.records if "event=purge_retry" in r.getMessage()
        ]
        assert len(retry_lines) == 1
        assert f"urls={','.join(busy)}" in retry_lines[0]
        assert "error='2x HTTP 429'" in retry_lines[0]

    async def test_exhausted_retries_record_only_unpurged_urls(
        self, queue: PersistentQueue
    ) -> None:
        provider = ScriptedProvider(lambda url, call: BUSY if url.endswith("/b") else OK)
        job_id = await queue.enqueue("cdn", ["https://x/a", "https://x/b"])
        status, job_urls, message = await _run_until_settled(queue, provider, job_id)

        assert status == "failed"
        assert job_urls == ["https://x/b"]
        assert message == "HTTP 429"
        assert provider.batches == [
            ["https://x/a", "https://x/b"],
            ["https://x/b"],
            ["https://x/b"],
        ]

    async def test_terminal_only_fails_job_with_failed_urls(self, queue: PersistentQueue) -> None:
        provider = ScriptedProvider(lambda url, call: GONE if url.endswith("/b") else OK)
        job_id = await queue.enqueue("cdn", ["https://x/a", "https://x/b"])
        status, job_urls, message = await _run_until_settled(queue, provider, job_id)

        assert status == "failed"
        assert job_urls == ["https://x/b"]
        assert message == "HTTP 404"
        assert provider.batches == [["https://x/a", "https://x/b"]]

    async def test_whole_batch_result_retries_whole_batch(self, queue: PersistentQueue) -> None:
        provider = WholeBatchProvider(
            [PurgeResult(ok=False, retryable=True, message="Throttling"), OK]
        )
        urls = ["https://x/a", "https://x/b"]
        job_id = await queue.enqueue("cdn", urls)
        status, _, _ = await _run_until_settled(queue, provider, job_id)

        assert status == "done"
        assert provider.batches == [urls, urls]
