"""Keep a Fastly edge dictionary of symlink aliases in sync with the tree.

A file reachable through symlinks has several URLs, and a CDN caches each
one separately. For the rules configured under a Fastly CDN's
``edge_aliases``, this module publishes one dictionary item per alias URL
path, pointing at the real file's URL path::

    /almalinux/10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso
        -> /almalinux/10.3/isos/x86_64/AlmaLinux-10.3-x86_64-dvd.iso

A VCL snippet on the service rewrites ``req.url`` with that dictionary, so
every alias of a file is served from the real path's cache entry. Cached
objects then always correspond to real files, which the normal purge path
already keeps fresh, and retargeting a link is a dictionary update rather
than a purge.

Rules only say *which* URL paths to consider; the symlinks on disk say
where each one points. The mapping is recomputed from the filesystem on
every sync (glob expansion following symlinks, then ``realpath``), so it
needs no event history and cannot drift: a missed event is corrected at
the next periodic resync.

Sync model (:class:`EdgeAliasSync`): compute the full desired mapping, read
the live dictionary, apply the difference. Triggers are startup, any
symlink change, any purge target whose URL matches a rule (debounced by
``settle_seconds``), and ``resync_interval_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import quote, urlparse

from mirror_mirage.config import Config, EdgeAliasesConfig, FastlyCdn, WatchConfig
from mirror_mirage.filter import deepest_first, find_watch, matches_any_glob
from mirror_mirage.providers.base import WILDCARD_SUFFIX
from mirror_mirage.providers.fastly_dictionary import DictionaryError, FastlyDictionaryClient

_log = logging.getLogger("mirror_mirage.edge_aliases")

# Fastly edge dictionary limits.
MAX_KEY_LENGTH = 255
MAX_VALUE_LENGTH = 8000
# A ``**`` in a rule stops descending below this many directories.
_MAX_GLOB_DEPTH = 32
# Characters a URL path may contain unencoded. A path with anything else
# would need percent-encoding to match req.url.path, so it is skipped.
_SAFE_PATH_CHARS = "/-._~!$&'()*+,;=:@"
# Upper bound on how long a burst of changes can postpone a sync.
_MAX_SETTLE_SECONDS = 60.0
_INITIAL_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 300.0


@dataclass
class AliasMapping:
    """The dictionary items one CDN's rules produce, plus what was skipped."""

    entries: dict[str, str] = field(default_factory=dict)
    rule_of: dict[str, str] = field(default_factory=dict)
    skipped: Counter[str] = field(default_factory=Counter)
    examples: dict[str, str] = field(default_factory=dict)

    def skip(self, reason: str, path: Path | str) -> None:
        self.skipped[reason] += 1
        self.examples.setdefault(reason, str(path))


def compute_mapping(
    cdn_name: str, edge: EdgeAliasesConfig, watches: Sequence[WatchConfig]
) -> AliasMapping:
    """Expand every rule against the tree and return the alias mapping.

    For each watch binding of ``cdn_name``, each rule is matched against
    that binding's URL paths. A matching path becomes an item when it
    reaches a regular file through at least one symlink and that file is
    itself served through a binding of the same CDN. Paths that resolve to
    themselves (no symlink involved) are not aliases and are not counted.

    Blocking (walks directories); call it from a thread in async code.
    """
    ordered = deepest_first(watches)
    resolved_roots = deepest_first(
        w.model_copy(update={"path": Path(os.path.realpath(w.path))}) for w in watches
    )
    by_resolved_root = {Path(os.path.realpath(w.path)): w for w in watches}
    mapping = AliasMapping()

    for watch in watches:
        bindings = [b for b in watch.bindings if b.cdn == cdn_name]
        if not bindings:
            continue
        nested_roots = {w.path for w in watches if w.path != watch.path}
        real_root = Path(os.path.realpath(watch.path))
        for binding in bindings:
            base = _url_path_parts(binding.url_prefix)
            for rule in edge.rules:
                rel_pattern = _strip_base(rule.match.split("/"), base)
                if rel_pattern is None:
                    continue
                for alias in _expand(watch.path, rel_pattern, nested_roots):
                    owner = find_watch(ordered, alias)
                    if owner is None or owner[0].path != watch.path:
                        continue  # inside a nested watch; that watch's bindings own it
                    rel = owner[1]
                    if matches_any_glob(rel.parts, watch.ignore):
                        continue
                    real = Path(os.path.realpath(alias))
                    if real == real_root / rel:
                        continue  # no symlink on the way: not an alias
                    _add_entry(
                        mapping,
                        cdn_name=cdn_name,
                        rule=rule.name,
                        key=_url_path(binding.url_prefix, rel),
                        alias=alias,
                        real=real,
                        resolved_roots=resolved_roots,
                        by_resolved_root=by_resolved_root,
                    )
    return mapping


def _add_entry(
    mapping: AliasMapping,
    *,
    cdn_name: str,
    rule: str,
    key: str,
    alias: Path,
    real: Path,
    resolved_roots: Sequence[WatchConfig],
    by_resolved_root: dict[Path, WatchConfig],
) -> None:
    if not real.is_file():
        mapping.skip("not_a_file", alias)
        return
    owner = find_watch(resolved_roots, real)
    if owner is None:
        mapping.skip("outside_watches", alias)
        return
    real_watch = by_resolved_root[owner[0].path]
    real_rel = owner[1]
    if matches_any_glob(real_rel.parts, real_watch.ignore):
        mapping.skip("target_ignored", alias)
        return
    real_binding = next((b for b in real_watch.bindings if b.cdn == cdn_name), None)
    if real_binding is None:
        mapping.skip("target_not_on_cdn", alias)
        return
    value = _url_path(real_binding.url_prefix, real_rel)
    if quote(key, safe=_SAFE_PATH_CHARS) != key or quote(value, safe=_SAFE_PATH_CHARS) != value:
        mapping.skip("needs_url_encoding", alias)
        return
    if len(key) > MAX_KEY_LENGTH or len(value) > MAX_VALUE_LENGTH:
        mapping.skip("too_long", alias)
        return
    existing = mapping.entries.get(key)
    if existing is not None and existing != value:
        mapping.skip("conflict", alias)
        return
    mapping.entries[key] = value
    mapping.rule_of.setdefault(key, rule)


def diff_ops(live: dict[str, str], desired: dict[str, str]) -> list[dict[str, str]]:
    """Return the Fastly batch operations that turn ``live`` into ``desired``."""
    ops = [
        {"op": "upsert", "item_key": key, "item_value": value}
        for key, value in sorted(desired.items())
        if live.get(key) != value
    ]
    ops.extend({"op": "delete", "item_key": key} for key in sorted(live.keys() - desired.keys()))
    return ops


class EdgeAliasSync:
    """Keeps one CDN's edge dictionary equal to :func:`compute_mapping`."""

    def __init__(
        self,
        *,
        cdn_name: str,
        edge: EdgeAliasesConfig,
        watches: Sequence[WatchConfig],
        client: FastlyDictionaryClient | None,
    ) -> None:
        """Create a sync loop. ``client=None`` is dry-run: compute and log only."""
        self._cdn_name = cdn_name
        self._edge = edge
        self._watches = list(watches)
        self._client = client
        self._patterns = [r.match for r in edge.rules]
        self._wake = asyncio.Event()
        self._synced_once = False

    def notify(self) -> None:
        """Request a sync once changes settle."""
        self._wake.set()

    def wants(self, url: str) -> bool:
        """Return True if ``url``'s path matches one of this CDN's rules."""
        path = urlparse(url).path
        if path.endswith(WILDCARD_SUFFIX):
            path = path[: -len(WILDCARD_SUFFIX)]
        parts = tuple(p for p in path.split("/") if p)
        return matches_any_glob(parts, self._patterns)

    async def run(self) -> None:
        """Sync at startup, on notification, and every ``resync_interval_seconds``.

        A failed sync is logged and retried with exponential backoff (5 s
        up to 5 min). It never raises: the purge pipeline runs in the same
        task group and must keep going whatever happens to the dictionary.
        """
        backoff = 0.0
        try:
            while True:
                self._wake.clear()
                try:
                    await self.sync_once()
                except Exception as e:
                    backoff = min(max(backoff * 2, _INITIAL_BACKOFF_SECONDS), _MAX_BACKOFF_SECONDS)
                    _log.error(
                        "event=edge_aliases_sync_failed cdn=%s retryable=%s retry_in_s=%.0f "
                        "error=%r",
                        self._cdn_name,
                        getattr(e, "retryable", True),
                        backoff,
                        str(e) if isinstance(e, DictionaryError) else repr(e),
                    )
                    await asyncio.sleep(backoff)
                    continue
                backoff = 0.0
                await self._wait_for_change()
        finally:
            if self._client is not None:
                await self._client.aclose()

    async def sync_once(self) -> int:
        """Compute the mapping and apply the difference. Return the op count.

        Raises :class:`DictionaryError` on API failure or when the mapping
        exceeds ``max_items`` (nothing is written in that case).
        """
        start = time.monotonic()
        mapping = await asyncio.to_thread(
            compute_mapping, self._cdn_name, self._edge, self._watches
        )
        skipped = " ".join(f"skipped_{r}={n}" for r, n in sorted(mapping.skipped.items()))
        if len(mapping.entries) > self._edge.max_items:
            raise DictionaryError(
                f"{len(mapping.entries)} items exceed max_items={self._edge.max_items}; "
                f"dictionary left unchanged",
                retryable=False,
            )
        if self._client is None:
            _log.info(
                "event=edge_aliases_dry_run cdn=%s dictionary=%s items=%d %s",
                self._cdn_name,
                self._edge.dictionary,
                len(mapping.entries),
                skipped,
            )
            return 0
        live = await self._client.items()
        ops = diff_ops(live, mapping.entries)
        await self._client.apply(ops)
        level = logging.INFO if ops or not self._synced_once else logging.DEBUG
        _log.log(
            level,
            "event=edge_aliases_synced cdn=%s dictionary=%s items=%d upserts=%d deletes=%d "
            "latency_ms=%.0f %s",
            self._cdn_name,
            self._edge.dictionary,
            len(mapping.entries),
            sum(op["op"] == "upsert" for op in ops),
            sum(op["op"] == "delete" for op in ops),
            (time.monotonic() - start) * 1000.0,
            skipped,
        )
        self._synced_once = True
        return len(ops)

    async def _wait_for_change(self) -> None:
        """Return after a notification has settled, or at the resync interval."""
        try:
            await asyncio.wait_for(self._wake.wait(), self._edge.resync_interval_seconds)
        except TimeoutError:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_SETTLE_SECONDS
        while True:
            self._wake.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), min(self._edge.settle_seconds, remaining))
            except TimeoutError:
                return


def build_edge_syncs(cfg: Config, *, dry_run: bool = False) -> dict[str, EdgeAliasSync]:
    """Create one :class:`EdgeAliasSync` per Fastly CDN with ``edge_aliases``."""
    syncs: dict[str, EdgeAliasSync] = {}
    for name, cdn in cfg.cdns.items():
        if not isinstance(cdn, FastlyCdn) or cdn.edge_aliases is None:
            continue
        client = None
        if not dry_run:
            client = FastlyDictionaryClient(
                service_id=cdn.service_id,
                api_token=cfg.credentials[name]["api_token"],
                dictionary=cdn.edge_aliases.dictionary,
            )
        syncs[name] = EdgeAliasSync(
            cdn_name=name, edge=cdn.edge_aliases, watches=cfg.watches, client=client
        )
    return syncs


async def report(
    cfg: Config,
    *,
    mode: Literal["show", "diff"],
    cdn: str | None,
    out: TextIO,
    err: TextIO,
) -> int:
    """CLI: print each CDN's mapping (``show``) or its diff against Fastly (``diff``)."""
    targets = {
        name: c
        for name, c in cfg.cdns.items()
        if isinstance(c, FastlyCdn) and c.edge_aliases is not None
    }
    if cdn is not None:
        if cdn not in targets:
            print(f"mirror-mirage: cdn {cdn!r} has no edge_aliases configured", file=err)
            return 1
        targets = {cdn: targets[cdn]}
    if not targets:
        print("mirror-mirage: no CDN has edge_aliases configured", file=err)
        return 1

    rc = 0
    for name, c in targets.items():
        edge = c.edge_aliases
        assert edge is not None
        mapping = compute_mapping(name, edge, cfg.watches)
        print(
            f"# cdn={name} dictionary={edge.dictionary} items={len(mapping.entries)} "
            f"max_items={edge.max_items}",
            file=out,
        )
        for reason, count in sorted(mapping.skipped.items()):
            print(f"# skipped {reason}={count} (e.g. {mapping.examples[reason]})", file=out)
        if len(mapping.entries) > edge.max_items:
            print(f"mirror-mirage: {name}: mapping exceeds max_items", file=err)
            rc = 1
        if mode == "show":
            for key in sorted(mapping.entries):
                print(f"{mapping.rule_of[key]}\t{key}\t{mapping.entries[key]}", file=out)
            continue
        client = FastlyDictionaryClient(
            service_id=c.service_id,
            api_token=cfg.credentials[name]["api_token"],
            dictionary=edge.dictionary,
        )
        try:
            live = await client.items()
        except DictionaryError as e:
            print(f"mirror-mirage: {name}: {e}", file=err)
            rc = 1
            continue
        finally:
            await client.aclose()
        added = changed = deleted = 0
        for op in diff_ops(live, mapping.entries):
            key = op["item_key"]
            if op["op"] == "delete":
                deleted += 1
                print(f"-\t{key}\t{live[key]}", file=out)
            elif key in live:
                changed += 1
                print(f"~\t{key}\t{live[key]} -> {op['item_value']}", file=out)
            else:
                added += 1
                print(f"+\t{key}\t{op['item_value']}", file=out)
        print(f"# {added} to add, {changed} to change, {deleted} to delete", file=out)
    return rc


def _url_path_parts(url_prefix: str) -> tuple[str, ...]:
    return tuple(p for p in urlparse(url_prefix).path.split("/") if p)


def _url_path(url_prefix: str, rel: Path) -> str:
    return urlparse(url_prefix).path.rstrip("/") + "/" + rel.as_posix()


def _strip_base(pattern: list[str], base: tuple[str, ...]) -> list[str] | None:
    """Drop the components of ``pattern`` that match the binding's URL path.

    Returns the watch-relative remainder, or ``None`` if the rule cannot
    match under this binding. ``**`` is not supported in the part of a
    rule that covers the ``url_prefix`` path.
    """
    if len(pattern) <= len(base):
        return None
    for part, component in zip(pattern, base, strict=False):
        if part == "**" or not fnmatchcase(component, part):
            return None
    return pattern[len(base) :]


def _expand(root: Path, parts: list[str], skip: set[Path]) -> Iterator[Path]:
    """Yield paths under ``root`` matching ``parts``, following symlinks.

    Hidden entries only match a component that itself starts with ``.``.
    Directories in ``skip`` (nested watch roots) are not entered. ``**``
    matches zero or more directories; a directory already on the current
    path (a symlink loop) is not entered again.
    """

    def walk(directory: Path, index: int, stack: tuple[str, ...]) -> Iterator[Path]:
        if index == len(parts):
            yield directory
            return
        part = parts[index]
        last = index == len(parts) - 1
        if part == "**":
            real = os.path.realpath(directory)
            if real in stack or len(stack) > _MAX_GLOB_DEPTH:
                return
            yield from walk(directory, index + 1, stack)
            for child in _children(directory, "*", skip):
                if child.is_dir():
                    yield from walk(child, index, (*stack, real))
            return
        for child in _children(directory, part, skip):
            if last:
                yield child
            elif child.is_dir():
                yield from walk(child, index + 1, stack)

    yield from walk(root, 0, ())


def _children(directory: Path, pattern: str, skip: set[Path]) -> Iterator[Path]:
    if not any(c in pattern for c in "*?["):
        child = directory / pattern
        if child not in skip and os.path.exists(child):
            yield child
        return
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for name in names:
        if name.startswith(".") and not pattern.startswith("."):
            continue
        if not fnmatchcase(name, pattern):
            continue
        child = directory / name
        if child not in skip:
            yield child
