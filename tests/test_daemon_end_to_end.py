"""End-to-end integration test.

Composes the real watcher → filter → mapper → batcher → queue pipeline
with mock CDN providers, runs an rsync-shaped push, and asserts that
each bound CDN sees exactly one batched purge containing the post-rename
URLs.

Skipped on non-Linux hosts (inotify is Linux-only).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from mirror_mirage.config import Config, EdgeAliasesConfig, EdgeAliasRule, FastlyCdn
from mirror_mirage.daemon import run as daemon_run
from mirror_mirage.providers.base import PurgeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify is Linux-only")


@dataclass
class RecordingProvider:
    """A provider that records every batch and always succeeds."""

    name: str
    batches: list[list[str]] = field(default_factory=list)

    async def purge(self, urls: list[str]) -> PurgeResult:
        self.batches.append(sorted(urls))
        return PurgeResult(ok=True, retryable=False, message="recorded")


def _simulate_rsync_push(mirror_tree: Path, files: list[str]) -> list[Path]:
    """Stage every file in .~tmp~/, then rename them into place."""
    rendered: list[Path] = []
    for rel in files:
        target = mirror_tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = target.parent / ".~tmp~"
        staging_dir.mkdir(exist_ok=True)
        staging_file = staging_dir / target.name
        staging_file.write_bytes(f"data:{rel}".encode())
        os.rename(staging_file, target)
        rendered.append(target)
    return rendered


async def _wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    """Poll ``condition`` every 50 ms until it holds or ``seconds`` pass."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _start_daemon(cfg: Config) -> asyncio.Task[None]:
    """Run the daemon in a task and return once it has logged ``event=ready``.

    The watcher adds every inotify watch before that line, so changes made
    after this returns are seen. A fixed sleep was flaky on slow CI runners:
    changes made before the watches existed were never reported.
    """
    ready = asyncio.Event()

    class ReadyHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("event=ready "):
                ready.set()

    logger = logging.getLogger("mirror_mirage.daemon")
    handler = ReadyHandler()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    task = asyncio.create_task(daemon_run(cfg))
    try:
        await _wait_for(lambda: ready.is_set() or task.done())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    if ready.is_set():
        return task
    if task.done():
        await task  # re-raises the startup error
        pytest.fail("daemon exited before logging event=ready")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    pytest.fail("daemon did not log event=ready within 10 s")


class TestEndToEnd:
    async def test_rsync_push_produces_one_batch_per_cdn(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        # Install recording providers in place of the real ones.
        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        # Some implementations may import build_providers at module load.
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = await _start_daemon(basic_config)
        try:
            files = [
                "9/os/x86_64/repodata/repomd.xml",
                "9/os/x86_64/repodata/primary.xml.gz",
                "9/os/x86_64/Packages/foo-1.0-1.x86_64.rpm",
            ]
            _simulate_rsync_push(mirror_tree, files)

            # Wait for the quiet window to elapse and the queue worker
            # to drain at least one batch per CDN.
            await _wait_for(lambda: all(r.batches for r in recorders.values()))
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Each CDN should have received exactly one batch of three URLs.
        for name, rec in recorders.items():
            assert len(rec.batches) == 1, (
                f"{name}: expected 1 batch, got {len(rec.batches)} ({rec.batches!r})"
            )
            batch = rec.batches[0]
            assert len(batch) == 3
            # All three of the rsync'd files should be present.
            for rel in [
                "9/os/x86_64/repodata/repomd.xml",
                "9/os/x86_64/repodata/primary.xml.gz",
                "9/os/x86_64/Packages/foo-1.0-1.x86_64.rpm",
            ]:
                assert any(rel in url for url in batch), (
                    f"{name}: {rel} missing from batch {batch!r}"
                )

        # And the URLs in each CDN's batch must use that CDN's prefix.
        assert all(
            url.startswith("https://repo.almalinux.org/almalinux/")
            for url in recorders["fastly_main"].batches[0]
        )
        assert all(
            url.startswith("https://cdn-us.almalinux.org/almalinux/")
            for url in recorders["cloudfront_us"].batches[0]
        )

    async def test_symlink_flip_purges_link_tree_not_target(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        """Retargeting ``10 -> 10.2`` to ``10.3`` purges ``10`` and ``10/*`` only."""
        for version in ("10.2", "10.3"):
            (mirror_tree / version / "BaseOS").mkdir(parents=True)
            (mirror_tree / version / "BaseOS" / "repomd.xml").write_bytes(version.encode())
        (mirror_tree / "10").symlink_to("10.2")

        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = await _start_daemon(basic_config)
        try:
            # The way rsync and `ln -sfn` do it: new link, renamed over the old.
            tmp = mirror_tree / ".10.AbCdEf"
            tmp.symlink_to("10.3")
            os.replace(tmp, mirror_tree / "10")
            await _wait_for(lambda: all(r.batches for r in recorders.values()))
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert recorders["fastly_main"].batches == [
            [
                "https://repo.almalinux.org/almalinux/10",
                "https://repo.almalinux.org/almalinux/10/*",
            ]
        ]
        assert recorders["cloudfront_us"].batches == [
            [
                "https://cdn-us.almalinux.org/almalinux/10",
                "https://cdn-us.almalinux.org/almalinux/10/*",
            ]
        ]

    async def test_unlink_then_relink_purges_link_tree(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        """``rm 10 && ln -s 10.3 10``: the new link's creation triggers the purge."""
        for version in ("10.2", "10.3"):
            (mirror_tree / version).mkdir()
        (mirror_tree / "10").symlink_to("10.2")

        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = await _start_daemon(basic_config)
        try:
            (mirror_tree / "10").unlink()
            await asyncio.sleep(0.05)
            (mirror_tree / "10").symlink_to("10.3")
            await _wait_for(lambda: all(r.batches for r in recorders.values()))
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert recorders["fastly_main"].batches == [
            [
                "https://repo.almalinux.org/almalinux/10",
                "https://repo.almalinux.org/almalinux/10/*",
            ]
        ]
        assert recorders["cloudfront_us"].batches == [
            [
                "https://cdn-us.almalinux.org/almalinux/10",
                "https://cdn-us.almalinux.org/almalinux/10/*",
            ]
        ]

    async def test_staging_writes_do_not_purge(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        """Writing into .~tmp~ without renaming must not produce purges."""
        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = await _start_daemon(basic_config)
        try:
            # Write into staging but never rename out.
            staging = mirror_tree / "9/os/x86_64/repodata/.~tmp~"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "repomd.xml").write_bytes(b"<repomd/>")
            # Wait long enough for any spurious flush to occur.
            await asyncio.sleep(0.5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        for name, rec in recorders.items():
            assert rec.batches == [], (
                f"{name}: unexpected batches from staging-only writes: {rec.batches!r}"
            )


async def _run_until_batches(
    cfg: Config,
    monkeypatch,
    action: Callable[[], None],
    *,
    start_when: Callable[[], bool] | None = None,
    stop_when: Callable[[], bool] | None = None,
) -> dict[str, RecordingProvider]:
    """Start the daemon, run ``action``, and wait for each CDN's first batch.

    ``start_when`` must hold before ``action`` runs, and ``stop_when`` must
    hold (as well as every CDN having a batch) before the daemon is stopped.
    """
    recorders = {
        "fastly_main": RecordingProvider("fastly_main"),
        "cloudfront_us": RecordingProvider("cloudfront_us"),
    }

    def fake_build(cfg, *, dry_run=False):
        return dict(recorders)

    monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
    monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)
    task = await _start_daemon(cfg)
    try:
        if start_when is not None:
            assert await _wait_for(start_when), "start_when never held"
        action()
        await _wait_for(
            lambda: (
                all(r.batches for r in recorders.values()) and (stop_when is None or stop_when())
            )
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return recorders


class TestSymlinkAliases:
    ISO_DIR = "10.3/isos/x86_64"

    def _release(self, mirror_tree: Path) -> Path:
        iso_dir = mirror_tree / self.ISO_DIR
        iso_dir.mkdir(parents=True)
        (iso_dir / "AlmaLinux-10.3-x86_64-dvd.iso").write_bytes(b"first spin")
        (iso_dir / "AlmaLinux-10-latest-x86_64-dvd.iso").symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
        (mirror_tree / "10").symlink_to("10.3")
        return iso_dir

    async def test_respin_purges_every_alias(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        iso_dir = self._release(mirror_tree)

        def respin() -> None:
            _simulate_rsync_push(mirror_tree, [f"{self.ISO_DIR}/AlmaLinux-10.3-x86_64-dvd.iso"])

        recorders = await _run_until_batches(basic_config, monkeypatch, respin)
        assert iso_dir.exists()
        assert recorders["fastly_main"].batches == [
            sorted(
                f"https://repo.almalinux.org/almalinux/{p}"
                for p in (
                    "10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
                    "10.3/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso",
                    "10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
                    "10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso",
                )
            )
        ]
        assert len(recorders["cloudfront_us"].batches[0]) == 4

    async def test_latest_retarget_purges_link_and_its_alias(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        iso_dir = self._release(mirror_tree)
        (iso_dir / "AlmaLinux-10.3-1-x86_64-dvd.iso").write_bytes(b"second spin")

        def retarget() -> None:
            tmp = iso_dir / ".latest.AbCdEf"
            tmp.symlink_to("AlmaLinux-10.3-1-x86_64-dvd.iso")
            os.replace(tmp, iso_dir / "AlmaLinux-10-latest-x86_64-dvd.iso")

        recorders = await _run_until_batches(basic_config, monkeypatch, retarget)
        assert recorders["fastly_main"].batches == [
            [
                "https://repo.almalinux.org/almalinux/10.3/isos/x86_64/"
                "AlmaLinux-10-latest-x86_64-dvd.iso",
                "https://repo.almalinux.org/almalinux/10/isos/x86_64/"
                "AlmaLinux-10-latest-x86_64-dvd.iso",
            ]
        ]


class _FakeDictionaryClient:
    """Replaces FastlyDictionaryClient; one shared dict per test."""

    data: ClassVar[dict[str, str]] = {}

    def __init__(self, **_kwargs) -> None:
        pass

    async def items(self) -> dict[str, str]:
        return dict(self.data)

    async def apply(self, ops: list[dict[str, str]]) -> None:
        for op in ops:
            if op["op"] == "delete":
                self.data.pop(op["item_key"], None)
            else:
                self.data[op["item_key"]] = op["item_value"]

    async def aclose(self) -> None:
        pass


class TestEdgeAliases:
    async def test_latest_retarget_updates_dictionary(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        iso_dir = mirror_tree / "10.3/isos/x86_64"
        iso_dir.mkdir(parents=True)
        (iso_dir / "AlmaLinux-10.3-x86_64-dvd.iso").write_bytes(b"spin 1")
        (iso_dir / "AlmaLinux-10.3-1-x86_64-dvd.iso").write_bytes(b"spin 2")
        (iso_dir / "AlmaLinux-10-latest-x86_64-dvd.iso").symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
        (mirror_tree / "10").symlink_to("10.3")

        edge = EdgeAliasesConfig(
            dictionary="mirage_aliases",
            rules=[EdgeAliasRule(name="isos", match="almalinux/*/isos/*/*.iso")],
            settle_seconds=0.05,
        )
        fastly = basic_config.cdns["fastly_main"]
        assert isinstance(fastly, FastlyCdn)
        cfg = basic_config.model_copy(
            update={
                "cdns": {
                    **basic_config.cdns,
                    "fastly_main": fastly.model_copy(update={"edge_aliases": edge}),
                }
            }
        )
        monkeypatch.setattr(_FakeDictionaryClient, "data", {})
        monkeypatch.setattr(
            "mirror_mirage.edge_aliases.FastlyDictionaryClient", _FakeDictionaryClient
        )
        latest_key = "/almalinux/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso"

        def retarget() -> None:
            assert _FakeDictionaryClient.data[latest_key] == (
                "/almalinux/10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso"
            )
            tmp = iso_dir / ".latest.AbCdEf"
            tmp.symlink_to("AlmaLinux-10.3-1-x86_64-dvd.iso")
            os.replace(tmp, iso_dir / "AlmaLinux-10-latest-x86_64-dvd.iso")

        new_target = "/almalinux/10.3/isos/x86_64/AlmaLinux-10.3-1-x86_64-dvd.iso"
        await _run_until_batches(
            cfg,
            monkeypatch,
            retarget,
            # The startup sync has published the first spin.
            start_when=lambda: latest_key in _FakeDictionaryClient.data,
            stop_when=lambda: _FakeDictionaryClient.data.get(latest_key) == new_target,
        )
        assert _FakeDictionaryClient.data[latest_key] == new_target
