"""Tests for :mod:`mirror_mirage.mapper`.

The mapper is a pure function: ``FsEvent → list[PurgeTarget]`` where each
binding on the event's owning watch produces one target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mirror_mirage.config import Binding, WatchConfig
from mirror_mirage.filter import EventKind, FsEvent
from mirror_mirage.mapper import Mapper, PurgeTarget
from mirror_mirage.symlinks import SymlinkIndex


def _ev(kind: EventKind, root: Path, rel: str) -> FsEvent:
    return FsEvent(kind=kind, abs_path=root / rel, watch_root=root)


class TestSingleBinding:
    def test_simple_path_joined_with_prefix(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        targets = mapper.map(
            _ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/repodata/repomd.xml")
        )
        assert targets == [
            PurgeTarget(
                cdn_name="fastly_main",
                url="https://repo.almalinux.org/almalinux/9/os/x86_64/repodata/repomd.xml",
            )
        ]

    def test_prefix_without_trailing_slash(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(
            bindings=[Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux")]
        )
        mapper = Mapper([watch])
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/Packages/x.rpm"))
        assert targets[0].url == "https://repo.almalinux.org/almalinux/9/os/x86_64/Packages/x.rpm"


class TestMultipleBindings:
    def test_fan_out(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(
            bindings=[
                Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/"),
                Binding(cdn="cloudfront_us", url_prefix="https://cdn-us.almalinux.org/almalinux/"),
                Binding(cdn="gcp_eu", url_prefix="https://cdn-eu.almalinux.org/almalinux/"),
            ],
        )
        mapper = Mapper([watch])
        targets = mapper.map(
            _ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/repodata/repomd.xml")
        )
        assert len(targets) == 3
        cdn_names = {t.cdn_name for t in targets}
        assert cdn_names == {"fastly_main", "cloudfront_us", "gcp_eu"}
        # All URLs should end with the same relative path.
        for t in targets:
            assert t.url.endswith("/9/os/x86_64/repodata/repomd.xml")


class TestInvalidateTree:
    def test_link_url_and_wildcard_per_binding(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(
            bindings=[
                Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/"),
                Binding(cdn="cloudfront_us", url_prefix="https://cdn-us.almalinux.org/almalinux"),
            ]
        )
        mapper = Mapper([watch])
        targets = mapper.map(_ev(EventKind.INVALIDATE_TREE, mirror_tree, "10"))
        assert targets == [
            PurgeTarget(cdn_name="fastly_main", url="https://repo.almalinux.org/almalinux/10"),
            PurgeTarget(cdn_name="fastly_main", url="https://repo.almalinux.org/almalinux/10/*"),
            PurgeTarget(cdn_name="cloudfront_us", url="https://cdn-us.almalinux.org/almalinux/10"),
            PurgeTarget(
                cdn_name="cloudfront_us", url="https://cdn-us.almalinux.org/almalinux/10/*"
            ),
        ]

    def test_plain_invalidate_has_no_wildcard(self, watch_factory, mirror_tree: Path) -> None:
        mapper = Mapper([watch_factory()])
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, "10"))
        assert [t.url for t in targets] == ["https://repo.almalinux.org/almalinux/10"]


class TestEdgeCases:
    def test_event_for_unknown_watch_returns_empty(
        self, watch_factory, tmp_path: Path, mirror_tree: Path
    ) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        # An event whose watch_root doesn't match any configured watch.
        bogus_root = tmp_path / "elsewhere"
        bogus_root.mkdir()
        targets = mapper.map(
            FsEvent(kind=EventKind.INVALIDATE, abs_path=bogus_root / "x", watch_root=bogus_root)
        )
        assert targets == []

    def test_deep_nested_path(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        deep = "9/os/x86_64/Packages/a/b/c/d/e/very-deep.rpm"
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, deep))
        assert targets[0].url.endswith("/" + deep)

    def test_delete_kind_also_mapped(self, watch_factory, mirror_tree: Path) -> None:
        # Mapper should not care about event kind — both kinds produce targets.
        watch = watch_factory()
        mapper = Mapper([watch])
        targets = mapper.map(_ev(EventKind.DELETE, mirror_tree, "9/os/x86_64/Packages/old.rpm"))
        assert len(targets) == 1

    def test_multiple_watches_routes_to_correct_one(
        self, mirror_tree: Path, tmp_path: Path
    ) -> None:
        other_root = tmp_path / "srv" / "mirror" / "vault"
        other_root.mkdir(parents=True)
        watches = [
            WatchConfig(
                path=mirror_tree,
                bindings=[
                    Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/")
                ],
            ),
            WatchConfig(
                path=other_root,
                bindings=[Binding(cdn="fastly_main", url_prefix="https://vault.almalinux.org/")],
            ),
        ]
        mapper = Mapper(watches)
        t1 = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/x.rpm"))
        t2 = mapper.map(_ev(EventKind.INVALIDATE, other_root, "el7/x.rpm"))
        assert t1[0].url.startswith("https://repo.almalinux.org/almalinux/")
        assert t2[0].url.startswith("https://vault.almalinux.org/")


ISO_DIR = "10.3/isos/x86_64"
REAL_ISO = f"{ISO_DIR}/AlmaLinux-10.3-x86_64-dvd.iso"
LATEST_ISO = f"{ISO_DIR}/AlmaLinux-10-latest-x86_64-dvd.iso"
FASTLY = "https://repo.almalinux.org/almalinux"
CLOUDFRONT = "https://cdn-us.almalinux.org/almalinux"


@pytest.fixture
def release_index(mirror_tree: Path) -> SymlinkIndex:
    """``10 -> 10.3`` and a ``-latest-`` ISO link, indexed."""
    (mirror_tree / ISO_DIR).mkdir(parents=True)
    (mirror_tree / REAL_ISO).write_bytes(b"iso")
    (mirror_tree / LATEST_ISO).symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
    (mirror_tree / "10").symlink_to("10.3")
    index = SymlinkIndex()
    index.record(mirror_tree / LATEST_ISO)
    index.record(mirror_tree / "10")
    return index


def _two_bindings(watch_factory, *, cloudfront_aliases: bool = True) -> WatchConfig:
    return watch_factory(
        bindings=[
            Binding(cdn="fastly_main", url_prefix=FASTLY + "/"),
            Binding(
                cdn="cloudfront_us",
                url_prefix=CLOUDFRONT + "/",
                purge_aliases=cloudfront_aliases,
            ),
        ]
    )


class TestAliases:
    def test_real_file_purges_every_alias(
        self, watch_factory, mirror_tree: Path, release_index: SymlinkIndex
    ) -> None:
        mapper = Mapper([_two_bindings(watch_factory)], symlinks=release_index)
        urls = {t.url for t in mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))}
        for base in (FASTLY, CLOUDFRONT):
            assert {
                f"{base}/10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
                f"{base}/10.3/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso",
                f"{base}/10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
                f"{base}/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso",
            } <= urls
        assert len(urls) == 8

    def test_purge_aliases_false_gets_real_path_only(
        self, watch_factory, mirror_tree: Path, release_index: SymlinkIndex
    ) -> None:
        watch = _two_bindings(watch_factory, cloudfront_aliases=False)
        mapper = Mapper([watch], symlinks=release_index)
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))
        assert [t.url for t in targets if t.cdn_name == "cloudfront_us"] == [
            f"{CLOUDFRONT}/10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso"
        ]
        assert len([t for t in targets if t.cdn_name == "fastly_main"]) == 4

    def test_no_index_means_no_aliases(
        self, watch_factory, mirror_tree: Path, release_index: SymlinkIndex
    ) -> None:
        mapper = Mapper([_two_bindings(watch_factory)])
        assert len(mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))) == 2

    def test_alias_in_ignored_path_skipped(
        self, watch_factory, mirror_tree: Path, release_index: SymlinkIndex
    ) -> None:
        staging = mirror_tree / ISO_DIR / ".~tmp~"
        staging.mkdir()
        (staging / "stale.iso").symlink_to("../AlmaLinux-10.3-x86_64-dvd.iso")
        release_index.record(staging / "stale.iso")
        mapper = Mapper([watch_factory()], symlinks=release_index)
        urls = [t.url for t in mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))]
        assert not any(".~tmp~" in u for u in urls)
        assert len(urls) == 4

    def test_alias_in_other_watch_uses_its_bindings(
        self, mirror_tree: Path, tmp_path: Path, release_index: SymlinkIndex
    ) -> None:
        vault = tmp_path / "srv" / "mirror" / "vault"
        vault.mkdir(parents=True)
        (vault / "linked.iso").symlink_to(mirror_tree / REAL_ISO)
        release_index.record(vault / "linked.iso")
        watches = [
            WatchConfig(path=mirror_tree, bindings=[Binding(cdn="fastly_main", url_prefix=FASTLY)]),
            WatchConfig(
                path=vault,
                bindings=[Binding(cdn="fastly_vault", url_prefix="https://vault.almalinux.org/")],
            ),
        ]
        mapper = Mapper(watches, symlinks=release_index)
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))
        expected = PurgeTarget(
            cdn_name="fastly_vault", url="https://vault.almalinux.org/linked.iso"
        )
        assert expected in targets

    def test_alias_outside_every_watch_skipped(
        self, watch_factory, mirror_tree: Path, tmp_path: Path, release_index: SymlinkIndex
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "linked.iso").symlink_to(mirror_tree / REAL_ISO)
        release_index.record(outside / "linked.iso")
        mapper = Mapper([watch_factory()], symlinks=release_index)
        urls = [t.url for t in mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))]
        assert len(urls) == 4
        assert not any("elsewhere" in u for u in urls)

    def test_tree_alias_gets_wildcard_too(self, watch_factory, mirror_tree: Path) -> None:
        """A dir link reached through another link purges both trees."""
        (mirror_tree / "10.3").mkdir()
        (mirror_tree / "10").symlink_to("10.3")
        (mirror_tree / "current").symlink_to("10")
        index = SymlinkIndex()
        index.record(mirror_tree / "10")
        index.record(mirror_tree / "current")
        mapper = Mapper([watch_factory()], symlinks=index)
        urls = [t.url for t in mapper.map(_ev(EventKind.INVALIDATE_TREE, mirror_tree, "10"))]
        assert urls == [
            f"{FASTLY}/10",
            f"{FASTLY}/10/*",
            f"{FASTLY}/current",
            f"{FASTLY}/current/*",
        ]

    def test_max_aliases_caps_expansion(
        self, watch_factory, mirror_tree: Path, release_index: SymlinkIndex
    ) -> None:
        mapper = Mapper([watch_factory()], symlinks=release_index, max_aliases=1)
        assert len(mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, REAL_ISO))) == 2
