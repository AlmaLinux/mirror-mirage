"""Tests for :mod:`mirror_mirage.symlinks`."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from mirror_mirage.symlinks import SymlinkIndex

ISO_DIR = "10.3/isos/x86_64"
REAL_ISO = f"{ISO_DIR}/AlmaLinux-10.3-x86_64-dvd.iso"
LATEST_ISO = f"{ISO_DIR}/AlmaLinux-10-latest-x86_64-dvd.iso"


@pytest.fixture
def release_tree(tmp_path: Path) -> Path:
    """``10 -> 10.3`` plus a ``-latest-`` ISO link next to the real ISO."""
    root = tmp_path / "almalinux"
    (root / ISO_DIR).mkdir(parents=True)
    (root / REAL_ISO).write_bytes(b"iso")
    (root / LATEST_ISO).symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
    (root / "10.3" / "BaseOS").mkdir()
    (root / "10.3" / "BaseOS" / "repomd.xml").write_bytes(b"<repomd/>")
    (root / "10").symlink_to("10.3")
    return root


def _index(*links: Path) -> SymlinkIndex:
    index = SymlinkIndex()
    for link in links:
        assert index.record(link)
    return index


class TestRecord:
    def test_relative_target_is_resolved_against_link_dir(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO)
        assert index.target(release_tree / LATEST_ISO) == release_tree / REAL_ISO

    def test_absolute_target_kept(self, tmp_path: Path) -> None:
        (tmp_path / "real").write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        assert _index(link).target(link) == tmp_path / "real"

    def test_dotdot_target_normalized(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        link = tmp_path / "a" / "link"
        link.symlink_to("../b/file")
        assert _index(link).target(link) == tmp_path / "b" / "file"

    def test_dangling_link_indexed(self, tmp_path: Path) -> None:
        link = tmp_path / "10"
        link.symlink_to("10.4")  # rsync can create the link before its target
        assert _index(link).target(link) == tmp_path / "10.4"

    def test_unchanged_link_reports_no_change(self, release_tree: Path) -> None:
        index = _index(release_tree / "10")
        assert index.record(release_tree / "10") is False

    def test_retarget_updates_reverse_map(self, release_tree: Path) -> None:
        index = _index(release_tree / "10")
        (release_tree / "10.4").mkdir()
        tmp = release_tree / ".10.tmp"
        tmp.symlink_to("10.4")
        os.replace(tmp, release_tree / "10")
        assert index.record(release_tree / "10") is True
        assert index.aliases(release_tree / "10.3" / "x", limit=10) == []
        assert index.aliases(release_tree / "10.4" / "x", limit=10) == [release_tree / "10" / "x"]

    def test_regular_file_is_forgotten(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO)
        (release_tree / LATEST_ISO).unlink()
        (release_tree / LATEST_ISO).write_bytes(b"now a file")
        assert index.record(release_tree / LATEST_ISO) is True
        assert release_tree / LATEST_ISO not in index


class TestAliases:
    def test_real_iso_reaches_all_four_paths(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO, release_tree / "10")
        aliases = index.aliases(release_tree / REAL_ISO, limit=10)
        assert set(aliases) == {
            release_tree / LATEST_ISO,
            release_tree / "10" / "isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
            release_tree / "10" / "isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso",
        }

    def test_latest_link_reaches_its_dir_alias(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO, release_tree / "10")
        assert index.aliases(release_tree / LATEST_ISO, limit=10) == [
            release_tree / "10" / "isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso"
        ]

    def test_file_under_dir_link_only(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO, release_tree / "10")
        assert index.aliases(release_tree / "10.3/BaseOS/repomd.xml", limit=10) == [
            release_tree / "10/BaseOS/repomd.xml"
        ]

    def test_unrelated_path_has_none(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO, release_tree / "10")
        assert index.aliases(release_tree / "9.7/BaseOS/repomd.xml", limit=10) == []

    def test_chain_of_file_links(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_bytes(b"x")
        (tmp_path / "b").symlink_to("a")
        (tmp_path / "c").symlink_to("b")
        index = _index(tmp_path / "b", tmp_path / "c")
        assert index.aliases(tmp_path / "a", limit=10) == [tmp_path / "b", tmp_path / "c"]

    def test_link_target_through_dir_link(self, release_tree: Path) -> None:
        """A link written as ``10/...`` still finds the real file via the ``10`` link."""
        link = release_tree / "current.iso"
        link.symlink_to("10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso")
        index = _index(release_tree / "10", link)
        assert link in index.aliases(release_tree / REAL_ISO, limit=10)

    def test_loop_is_bounded(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "loop").symlink_to(".")
        index = _index(tmp_path / "d" / "loop")
        with caplog.at_level(logging.WARNING, logger="mirror_mirage.symlinks"):
            aliases = index.aliases(tmp_path / "d" / "f", limit=5)
        assert len(aliases) == 5
        assert "event=alias_limit_reached" in caplog.text

    def test_zero_limit(self, release_tree: Path) -> None:
        index = _index(release_tree / "10")
        assert index.aliases(release_tree / REAL_ISO, limit=0) == []


class TestForget:
    def test_forget_link(self, release_tree: Path) -> None:
        index = _index(release_tree / "10")
        assert index.forget(release_tree / "10") is True
        assert index.forget(release_tree / "10") is False
        assert index.aliases(release_tree / REAL_ISO, limit=10) == []
        assert len(index) == 0

    def test_forget_tree_drops_links_below(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO, release_tree / "10")
        assert index.forget_tree(release_tree / "10.3") == 1
        assert release_tree / LATEST_ISO not in index
        assert release_tree / "10" in index


class TestObserve:
    def test_create_of_link_records(self, release_tree: Path) -> None:
        index = SymlinkIndex()
        assert index.observe("IN_CREATE", release_tree / "10") is True
        assert release_tree / "10" in index

    def test_moved_to_regular_file_forgets_link(self, release_tree: Path) -> None:
        index = _index(release_tree / LATEST_ISO)
        tmp = release_tree / ISO_DIR / ".tmp"
        tmp.write_bytes(b"replacement")
        os.replace(tmp, release_tree / LATEST_ISO)
        assert index.observe("IN_MOVED_TO", release_tree / LATEST_ISO) is True
        assert release_tree / LATEST_ISO not in index

    @pytest.mark.parametrize("mask", ["IN_DELETE", "IN_MOVED_FROM"])
    def test_removal_forgets(self, release_tree: Path, mask: str) -> None:
        index = _index(release_tree / "10")
        assert index.observe(mask, release_tree / "10") is True
        assert release_tree / "10" not in index

    def test_close_write_ignored(self, release_tree: Path) -> None:
        index = _index(release_tree / "10")
        assert index.observe("IN_CLOSE_WRITE", release_tree / REAL_ISO) is False
