"""Tests for :mod:`mirror_mirage.edge_aliases`."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import httpx
import pytest
import respx

from mirror_mirage import edge_aliases
from mirror_mirage.config import (
    Binding,
    CloudFrontCdn,
    Config,
    EdgeAliasesConfig,
    EdgeAliasRule,
    FastlyCdn,
    WatchConfig,
)
from mirror_mirage.edge_aliases import (
    EdgeAliasSync,
    build_edge_syncs,
    compute_mapping,
    diff_ops,
)
from mirror_mirage.providers.fastly_dictionary import DictionaryError, FastlyDictionaryClient

ISO = "almalinux/10.3/isos/x86_64"
IMG = "almalinux/10.3/cloud/x86_64/images"
DATED_IMG = "AlmaLinux-10-GenericCloud-10.3-20261116.x86_64.qcow2"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A /repo-like tree: ``10 -> 10.3``, latest links, and a nested vault watch."""
    root = tmp_path / "repo"
    (root / ISO).mkdir(parents=True)
    (root / ISO / "AlmaLinux-10.3-x86_64-dvd.iso").write_bytes(b"iso")
    (root / ISO / "AlmaLinux-10-latest-x86_64-dvd.iso").symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
    (root / ISO / "CHECKSUM").write_bytes(b"sums")
    (root / IMG).mkdir(parents=True)
    (root / IMG / DATED_IMG).write_bytes(b"img")
    (root / IMG / "AlmaLinux-10-GenericCloud-latest.x86_64.qcow2").symlink_to(DATED_IMG)
    (root / "almalinux" / "10.3" / "BaseOS").mkdir()
    (root / "almalinux" / "10").symlink_to("10.3")
    (root / "vault" / "8.3" / "isos").mkdir(parents=True)
    (root / "vault" / "8.3" / "isos" / "AlmaLinux-8.3-x86_64-dvd.iso").write_bytes(b"old")
    return root


@pytest.fixture
def watches(repo: Path) -> list[WatchConfig]:
    return [
        WatchConfig(
            path=repo,
            ignore=["**/.~tmp~/**"],
            bindings=[
                Binding(cdn="fastly_repo", url_prefix="https://repo.almalinux.org/"),
                Binding(cdn="cloudfront_repo", url_prefix="https://aws.repo.almalinux.org/"),
            ],
        ),
        WatchConfig(
            path=repo / "vault",
            bindings=[Binding(cdn="fastly_vault", url_prefix="https://vault.almalinux.org/")],
        ),
    ]


@pytest.fixture
def edge() -> EdgeAliasesConfig:
    return EdgeAliasesConfig(
        dictionary="mirage_aliases",
        rules=[
            EdgeAliasRule(name="isos", match="almalinux/*/isos/*/*.iso"),
            EdgeAliasRule(name="cloud-images", match="almalinux/*/cloud/*/images/*"),
        ],
        settle_seconds=0.05,
    )


REAL_ISO = "/almalinux/10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso"
REAL_IMG = f"/{IMG}/{DATED_IMG}"
EXPECTED = {
    "/almalinux/10.3/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso": REAL_ISO,
    "/almalinux/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso": REAL_ISO,
    "/almalinux/10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso": REAL_ISO,
    f"/{IMG}/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2": REAL_IMG,
    "/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2": REAL_IMG,
    f"/almalinux/10/cloud/x86_64/images/{DATED_IMG}": REAL_IMG,
}


class TestComputeMapping:
    def test_isos_and_cloud_images(self, watches, edge) -> None:
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert mapping.entries == EXPECTED
        assert not mapping.skipped
        assert mapping.rule_of[f"/{IMG}/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2"] == (
            "cloud-images"
        )
        assert set(mapping.entries).isdisjoint(mapping.entries.values())

    def test_cdn_without_bindings_gets_nothing(self, watches, edge) -> None:
        assert compute_mapping("fastly_other", edge, watches).entries == {}

    def test_url_prefix_with_path(self, repo: Path, edge) -> None:
        watch = WatchConfig(
            path=repo / "almalinux",
            bindings=[
                Binding(cdn="fastly_repo", url_prefix="https://repo.almalinux.org/almalinux/")
            ],
        )
        mapping = compute_mapping("fastly_repo", edge, [watch])
        assert mapping.entries == EXPECTED

    def test_rule_not_under_binding_path_ignored(self, repo: Path) -> None:
        watch = WatchConfig(
            path=repo / "almalinux",
            bindings=[
                Binding(cdn="fastly_repo", url_prefix="https://repo.almalinux.org/almalinux/")
            ],
        )
        edge = EdgeAliasesConfig(
            dictionary="d", rules=[EdgeAliasRule(name="other", match="vault/*/isos/*.iso")]
        )
        assert compute_mapping("fastly_repo", edge, [watch]).entries == {}

    def test_paths_in_nested_watch_skipped(self, repo: Path, watches) -> None:
        (repo / "vault" / "8.3" / "isos" / "latest.iso").symlink_to("AlmaLinux-8.3-x86_64-dvd.iso")
        edge = EdgeAliasesConfig(
            dictionary="d", rules=[EdgeAliasRule(name="vault", match="vault/*/isos/*.iso")]
        )
        # The link is under the vault watch, which binds fastly_vault, not fastly_repo.
        assert compute_mapping("fastly_repo", edge, watches).entries == {}

    def test_target_outside_watches(self, repo: Path, tmp_path: Path, watches, edge) -> None:
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "x.iso").write_bytes(b"x")
        (repo / ISO / "outside.iso").symlink_to(tmp_path / "elsewhere" / "x.iso")
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert mapping.skipped["outside_watches"] == 2  # via 10.3/ and via 10/
        assert mapping.entries == EXPECTED

    def test_target_served_by_other_cdn(self, repo: Path, watches, edge) -> None:
        (repo / ISO / "vaulted.iso").symlink_to(
            "../../../../vault/8.3/isos/AlmaLinux-8.3-x86_64-dvd.iso"
        )
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert mapping.skipped["target_not_on_cdn"] == 2
        assert mapping.entries == EXPECTED

    def test_link_to_directory(self, repo: Path, watches, edge) -> None:
        (repo / ISO / "dir.iso").symlink_to("../../BaseOS")
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert mapping.skipped["not_a_file"] == 2

    def test_dangling_link_not_an_entry(self, repo: Path, watches, edge) -> None:
        (repo / ISO / "gone.iso").symlink_to("missing.iso")
        assert compute_mapping("fastly_repo", edge, watches).entries == EXPECTED

    def test_hidden_and_ignored_names_not_matched(self, repo: Path, watches, edge) -> None:
        (repo / ISO / ".tmp.iso").symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
        (repo / ISO / ".~tmp~").mkdir()
        (repo / ISO / ".~tmp~" / "x.iso").symlink_to("../AlmaLinux-10.3-x86_64-dvd.iso")
        assert compute_mapping("fastly_repo", edge, watches).entries == EXPECTED

    def test_name_needing_url_encoding_skipped(self, repo: Path, watches, edge) -> None:
        (repo / ISO / "with space.iso").symlink_to("AlmaLinux-10.3-x86_64-dvd.iso")
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert mapping.skipped["needs_url_encoding"] == 2
        assert mapping.entries == EXPECTED

    def test_double_star_with_symlink_loop(self, repo: Path, watches) -> None:
        (repo / "almalinux" / "10.3" / "loop").symlink_to(".")
        edge = EdgeAliasesConfig(
            dictionary="d", rules=[EdgeAliasRule(name="all-isos", match="almalinux/**/*.iso")]
        )
        mapping = compute_mapping("fastly_repo", edge, watches)
        assert {k: v for k, v in EXPECTED.items() if k.endswith(".iso")}.items() <= (
            mapping.entries.items()
        )
        assert all(v == REAL_ISO for v in mapping.entries.values())


class TestDiffOps:
    def test_upserts_then_deletes(self) -> None:
        live = {"/a": "/1", "/b": "/2", "/gone": "/3"}
        desired = {"/a": "/1", "/b": "/22", "/new": "/4"}
        assert diff_ops(live, desired) == [
            {"op": "upsert", "item_key": "/b", "item_value": "/22"},
            {"op": "upsert", "item_key": "/new", "item_value": "/4"},
            {"op": "delete", "item_key": "/gone"},
        ]

    def test_no_change(self) -> None:
        assert diff_ops({"/a": "/1"}, {"/a": "/1"}) == []


class FakeDictionary:
    """Stands in for :class:`FastlyDictionaryClient`."""

    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.data = dict(items or {})
        self.applied: list[list[dict[str, str]]] = []
        self.fail_next: Exception | None = None
        self.closed = False

    async def items(self) -> dict[str, str]:
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err
        return dict(self.data)

    async def apply(self, ops: list[dict[str, str]]) -> None:
        self.applied.append(ops)
        for op in ops:
            if op["op"] == "delete":
                self.data.pop(op["item_key"], None)
            else:
                self.data[op["item_key"]] = op["item_value"]

    async def aclose(self) -> None:
        self.closed = True


def _sync(watches, edge, client) -> EdgeAliasSync:
    return EdgeAliasSync(cdn_name="fastly_repo", edge=edge, watches=watches, client=client)


class TestEdgeAliasSync:
    async def test_first_sync_writes_everything_then_nothing(self, watches, edge) -> None:
        fake = FakeDictionary({"/stale": "/x"})
        sync = _sync(watches, edge, fake)
        assert await sync.sync_once() == len(EXPECTED) + 1
        assert fake.data == EXPECTED
        assert await sync.sync_once() == 0

    async def test_retarget_updates_items(self, repo: Path, watches, edge) -> None:
        fake = FakeDictionary()
        sync = _sync(watches, edge, fake)
        await sync.sync_once()
        (repo / ISO / "AlmaLinux-10.3-1-x86_64-dvd.iso").write_bytes(b"respin")
        latest = repo / ISO / "AlmaLinux-10-latest-x86_64-dvd.iso"
        latest.unlink()
        latest.symlink_to("AlmaLinux-10.3-1-x86_64-dvd.iso")
        await sync.sync_once()
        new_real = "/almalinux/10.3/isos/x86_64/AlmaLinux-10.3-1-x86_64-dvd.iso"
        assert fake.data["/almalinux/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso"] == new_real
        assert fake.data["/almalinux/10/isos/x86_64/AlmaLinux-10.3-1-x86_64-dvd.iso"] == new_real

    async def test_over_limit_writes_nothing(self, watches, edge) -> None:
        fake = FakeDictionary()
        sync = _sync(watches, edge.model_copy(update={"max_items": 2}), fake)
        with pytest.raises(DictionaryError, match="max_items"):
            await sync.sync_once()
        assert fake.applied == []

    async def test_dry_run_only_logs(self, watches, edge, caplog) -> None:
        sync = _sync(watches, edge, None)
        with caplog.at_level(logging.INFO, logger="mirror_mirage.edge_aliases"):
            assert await sync.sync_once() == 0
        assert "event=edge_aliases_dry_run" in caplog.text
        assert f"items={len(EXPECTED)}" in caplog.text

    def test_wants(self, watches, edge) -> None:
        sync = _sync(watches, edge, None)
        assert sync.wants(f"https://repo.almalinux.org{REAL_ISO}")
        assert sync.wants(f"https://repo.almalinux.org{REAL_IMG}")
        assert not sync.wants("https://repo.almalinux.org/almalinux/10.3/isos/x86_64/CHECKSUM")
        assert not sync.wants("https://repo.almalinux.org/almalinux/10/*")

    async def test_run_syncs_on_notify_and_survives_errors(
        self, watches, edge, monkeypatch
    ) -> None:
        monkeypatch.setattr(edge_aliases, "_INITIAL_BACKOFF_SECONDS", 0.01)
        fake = FakeDictionary()
        fake.fail_next = RuntimeError("boom")
        sync = _sync(watches, edge, fake)
        task = asyncio.create_task(sync.run())
        try:
            for _ in range(100):
                if fake.data:
                    break
                await asyncio.sleep(0.01)
            assert fake.data == EXPECTED  # recovered from the first failure
            fake.data["/injected"] = "/x"
            sync.notify()
            for _ in range(100):
                if "/injected" not in fake.data:
                    break
                await asyncio.sleep(0.02)
            assert "/injected" not in fake.data
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert fake.closed


class TestBuildEdgeSyncs:
    def _cfg(self, watches) -> Config:
        return Config(
            cdns={
                "fastly_repo": FastlyCdn(
                    type="fastly",
                    service_id="svc",
                    edge_aliases=EdgeAliasesConfig(
                        dictionary="d", rules=[EdgeAliasRule(name="r", match="a/*")]
                    ),
                ),
                "fastly_vault": FastlyCdn(type="fastly", service_id="svc2"),
                "cloudfront_repo": CloudFrontCdn(type="cloudfront", distribution_id="E1"),
            },
            watches=watches,
            credentials={
                "fastly_repo": {"api_token": "t"},
                "fastly_vault": {"api_token": "t"},
                "cloudfront_repo": {},
            },
        )

    def test_only_cdns_with_edge_aliases(self, watches) -> None:
        syncs = build_edge_syncs(self._cfg(watches))
        assert list(syncs) == ["fastly_repo"]
        assert isinstance(syncs["fastly_repo"]._client, FastlyDictionaryClient)

    def test_dry_run_has_no_client(self, watches) -> None:
        syncs = build_edge_syncs(self._cfg(watches), dry_run=True)
        assert syncs["fastly_repo"]._client is None


SERVICE = {"versions": [{"number": 7, "active": True}]}


class TestReport:
    def _cfg(self, watches, edge) -> Config:
        return Config(
            cdns={"fastly_repo": FastlyCdn(type="fastly", service_id="svc", edge_aliases=edge)},
            watches=[watches[0].model_copy(update={"bindings": watches[0].bindings[:1]})],
            credentials={"fastly_repo": {"api_token": "t"}},
        )

    async def test_show(self, watches, edge) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = await edge_aliases.report(
            self._cfg(watches, edge), mode="show", cdn=None, out=out, err=err
        )
        assert rc == 0
        lines = out.getvalue().splitlines()
        assert lines[0] == (
            f"# cdn=fastly_repo dictionary=mirage_aliases items={len(EXPECTED)} max_items=1000"
        )
        assert (
            f"isos\t/almalinux/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso\t{REAL_ISO}"
            in lines
        )

    @respx.mock
    async def test_diff(self, watches, edge) -> None:
        respx.get("https://api.fastly.com/service/svc").mock(
            return_value=httpx.Response(200, json=SERVICE)
        )
        respx.get("https://api.fastly.com/service/svc/version/7/dictionary/mirage_aliases").mock(
            return_value=httpx.Response(200, json={"id": "dict1", "write_only": False})
        )
        live = [
            {
                "item_key": "/almalinux/10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso",
                "item_value": "/old",
            },
            {"item_key": "/gone", "item_value": "/x"},
        ]
        respx.get("https://api.fastly.com/service/svc/dictionary/dict1/items").mock(
            return_value=httpx.Response(200, json=live)
        )
        out, err = io.StringIO(), io.StringIO()
        rc = await edge_aliases.report(
            self._cfg(watches, edge), mode="diff", cdn="fastly_repo", out=out, err=err
        )
        assert rc == 0
        text = out.getvalue()
        assert "~\t/almalinux/10/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso\t/old -> " in text
        assert "-\t/gone\t/x" in text
        assert f"# {len(EXPECTED) - 1} to add, 1 to change, 1 to delete" in text

    async def test_unknown_cdn(self, watches, edge) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = await edge_aliases.report(
            self._cfg(watches, edge), mode="show", cdn="nope", out=out, err=err
        )
        assert rc == 1
        assert "has no edge_aliases" in err.getvalue()
