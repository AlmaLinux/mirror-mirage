"""Tests for :mod:`mirror_mirage.filter`.

Verifies the rules in ``docs/architecture.md``:

* ``IN_MOVED_TO`` / ``IN_CLOSE_WRITE`` → ``INVALIDATE``.
* ``IN_CREATE`` / ``IN_MOVED_TO`` on a symlink → ``INVALIDATE_TREE`` for a
  directory (or dangling) link, ``INVALIDATE`` for a file link.
* ``IN_DELETE`` / ``IN_MOVED_FROM`` → ``DELETE``.
* ``IN_CREATE`` on a non-symlink, ``IN_OPEN``, and other non-relevant
  masks → dropped.
* Paths matching the watch's ``ignore`` globs are dropped.
* Paths outside every configured watch are dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mirror_mirage.config import Binding, WatchConfig
from mirror_mirage.filter import EventFilter, EventKind


@pytest.fixture
def watch(watch_factory) -> WatchConfig:
    return watch_factory()


@pytest.fixture
def filter_(watch: WatchConfig) -> EventFilter:
    return EventFilter([watch])


class TestNormalize:
    def test_moved_to_yields_invalidate(self, filter_: EventFilter, mirror_tree: Path) -> None:
        ev = filter_.normalize("IN_MOVED_TO", mirror_tree / "9/os/x86_64/repodata/repomd.xml")
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE
        assert ev.watch_root == mirror_tree

    def test_close_write_yields_invalidate(self, filter_: EventFilter, mirror_tree: Path) -> None:
        ev = filter_.normalize("IN_CLOSE_WRITE", mirror_tree / "9/os/x86_64/Packages/foo.rpm")
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE

    def test_delete_dropped_by_default(self, filter_: EventFilter, mirror_tree: Path) -> None:
        # flush_on_delete defaults to False, so IN_DELETE must not surface.
        ev = filter_.normalize("IN_DELETE", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        assert ev is None

    def test_moved_from_dropped_by_default(self, filter_: EventFilter, mirror_tree: Path) -> None:
        ev = filter_.normalize("IN_MOVED_FROM", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        assert ev is None

    def test_create_is_dropped(self, filter_: EventFilter, mirror_tree: Path) -> None:
        # IN_CREATE for files is uninteresting (we wait for CLOSE_WRITE);
        # directory creation is the watcher's concern, not the filter's.
        assert filter_.normalize("IN_CREATE", mirror_tree / "9/os/x86_64/new-thing") is None

    def test_open_is_dropped(self, filter_: EventFilter, mirror_tree: Path) -> None:
        assert filter_.normalize("IN_OPEN", mirror_tree / "9/os/x86_64/Packages/x.rpm") is None


class TestSymlinks:
    """A created or replaced symlink is classified by what it points at."""

    @pytest.mark.parametrize("mask", ["IN_MOVED_TO", "IN_CREATE"])
    def test_directory_link_yields_invalidate_tree(
        self, filter_: EventFilter, mirror_tree: Path, mask: str
    ) -> None:
        (mirror_tree / "10.3").mkdir()
        link = mirror_tree / "10"
        link.symlink_to("10.3")
        ev = filter_.normalize(mask, link)
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE_TREE
        assert ev.abs_path == link

    def test_dangling_link_yields_invalidate_tree(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        # rsync can create "10 -> 10.3" before 10.3 itself arrives.
        link = mirror_tree / "10"
        link.symlink_to("10.3")
        ev = filter_.normalize("IN_MOVED_TO", link)
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE_TREE

    def test_file_link_yields_plain_invalidate(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        iso = mirror_tree / "9/os/x86_64/AlmaLinux-9.9-x86_64-dvd.iso"
        iso.write_bytes(b"iso")
        link = mirror_tree / "9/os/x86_64/AlmaLinux-9-latest-x86_64-dvd.iso"
        link.symlink_to(iso.name)
        ev = filter_.normalize("IN_CREATE", link)
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE

    def test_create_of_regular_file_is_dropped(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        f = mirror_tree / "9/os/x86_64/Packages/new.rpm"
        f.write_bytes(b"x")
        assert filter_.normalize("IN_CREATE", f) is None

    def test_link_already_renamed_away_is_dropped(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        # rsync creates ".10.XXXXXX" then renames it to "10"; by the time
        # the IN_CREATE for the temp name is processed it no longer exists.
        assert filter_.normalize("IN_CREATE", mirror_tree / ".10.AbCdEf") is None

    def test_link_in_ignored_path_is_dropped(self, filter_: EventFilter, mirror_tree: Path) -> None:
        staging = mirror_tree / ".~tmp~"
        staging.mkdir()
        link = staging / "10"
        link.symlink_to("../9")
        assert filter_.normalize("IN_CREATE", link) is None


class TestIgnoreGlobs:
    def test_default_rsync_staging_ignored(self, filter_: EventFilter, mirror_tree: Path) -> None:
        # File inside an rsync --delay-updates staging dir.
        ev = filter_.normalize(
            "IN_CLOSE_WRITE",
            mirror_tree / "9/os/x86_64/repodata/.~tmp~/repomd.xml",
        )
        assert ev is None

    def test_post_rename_event_passes_through(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        # The same file, after rsync atomically renames it into place.
        ev = filter_.normalize("IN_MOVED_TO", mirror_tree / "9/os/x86_64/repodata/repomd.xml")
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE

    def test_custom_ignore_glob(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(ignore=["**/*.log"])
        f = EventFilter([watch])
        assert f.normalize("IN_CLOSE_WRITE", mirror_tree / "9/os/x86_64/rsync.log") is None
        assert f.normalize("IN_CLOSE_WRITE", mirror_tree / "9/os/x86_64/Packages/x.rpm") is not None


class TestFlushOnDelete:
    """Per-watch ``flush_on_delete`` controls whether DELETE events surface."""

    def test_delete_yields_event_when_opted_in(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory()
        watch = watch.model_copy(update={"flush_on_delete": True})
        f = EventFilter([watch])
        ev = f.normalize("IN_DELETE", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        assert ev is not None
        assert ev.kind is EventKind.DELETE

    def test_moved_from_yields_event_when_opted_in(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory().model_copy(update={"flush_on_delete": True})
        f = EventFilter([watch])
        ev = f.normalize("IN_MOVED_FROM", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        assert ev is not None
        assert ev.kind is EventKind.DELETE

    def test_invalidate_still_passes_when_opted_out(
        self, filter_: EventFilter, mirror_tree: Path
    ) -> None:
        # Sanity: dropping deletions must not affect modifications.
        ev = filter_.normalize("IN_MOVED_TO", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        assert ev is not None
        assert ev.kind is EventKind.INVALIDATE

    def test_per_watch_independence(self, mirror_tree: Path, tmp_path: Path) -> None:
        """Two watches with opposite toggles must each behave independently."""
        other_root = tmp_path / "srv" / "mirror" / "vault"
        other_root.mkdir(parents=True)
        watches = [
            WatchConfig(
                path=mirror_tree,
                ignore=[],
                flush_on_delete=False,
                bindings=[
                    Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/")
                ],
            ),
            WatchConfig(
                path=other_root,
                ignore=[],
                flush_on_delete=True,
                bindings=[Binding(cdn="fastly_main", url_prefix="https://vault.almalinux.org/")],
            ),
        ]
        f = EventFilter(watches)
        # Under mirror_tree: deletions dropped.
        assert f.normalize("IN_DELETE", mirror_tree / "9/x.rpm") is None
        # Under vault: deletions surface.
        assert f.normalize("IN_DELETE", other_root / "el7/x.rpm") is not None


class TestWatchResolution:
    def test_event_outside_any_watch(self, filter_: EventFilter, tmp_path: Path) -> None:
        assert filter_.normalize("IN_MOVED_TO", tmp_path / "elsewhere/file") is None

    def test_multiple_watches_resolve_correctly(self, mirror_tree: Path, tmp_path: Path) -> None:
        other_root = tmp_path / "srv" / "mirror" / "vault"
        other_root.mkdir(parents=True)
        watches = [
            WatchConfig(
                path=mirror_tree,
                ignore=["**/.~tmp~/**"],
                bindings=[
                    Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/")
                ],
            ),
            WatchConfig(
                path=other_root,
                ignore=["**/.~tmp~/**"],
                bindings=[Binding(cdn="fastly_main", url_prefix="https://vault.almalinux.org/")],
            ),
        ]
        f = EventFilter(watches)
        ev1 = f.normalize("IN_MOVED_TO", mirror_tree / "9/os/x86_64/Packages/x.rpm")
        ev2 = f.normalize("IN_MOVED_TO", other_root / "el7/somefile")
        assert ev1 is not None and ev1.watch_root == mirror_tree
        assert ev2 is not None and ev2.watch_root == other_root
