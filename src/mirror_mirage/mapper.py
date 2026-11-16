"""Translate filesystem events into per-CDN purge targets.

Each :class:`~mirror_mirage.filter.FsEvent` may fan out to multiple
:class:`PurgeTarget` instances — one per CDN binding declared on the
owning watch. The transform is::

    rel = event.abs_path.relative_to(event.watch_root)
    url = binding.url_prefix.rstrip("/") + "/" + rel.as_posix()

An ``INVALIDATE_TREE`` event (a directory symlink was retargeted) maps to
two targets per binding: the link's own URL and a wildcard covering
everything under it (``url + "/*"``). Providers decide how to carry out
the wildcard; see :data:`~mirror_mirage.providers.base.WILDCARD_SUFFIX`.

Alias paths. When given a :class:`~mirror_mirage.symlinks.SymlinkIndex`,
the mapper also emits targets for every other path that reaches the
event's path through symlinks (``10/...`` for a change under ``10.3/``,
``...-latest-...`` for a change to the file it points at). Each alias is
mapped through the bindings of the watch that owns *it*, so a link in one
watch pointing into another works, and the owning watch's ``ignore``
globs apply. Bindings with ``purge_aliases: false`` get only the real
path.

The mapper does no I/O. The symlink index it reads is in memory and is
kept current by the daemon, so the mapper stays unit-testable in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mirror_mirage.config import Binding, WatchConfig
from mirror_mirage.filter import EventKind, FsEvent, deepest_first, find_watch, matches_any_glob
from mirror_mirage.providers.base import WILDCARD_SUFFIX
from mirror_mirage.symlinks import SymlinkIndex

DEFAULT_MAX_ALIASES = 32


@dataclass(frozen=True)
class PurgeTarget:
    """A single (cdn_name, url) pair the batcher should coalesce."""

    cdn_name: str
    url: str


class Mapper:
    """Maps normalized filesystem events to per-CDN purge targets."""

    def __init__(
        self,
        watches: list[WatchConfig],
        *,
        symlinks: SymlinkIndex | None = None,
        max_aliases: int = DEFAULT_MAX_ALIASES,
    ) -> None:
        self._by_root = {w.path: list(w.bindings) for w in watches}
        self._watches = deepest_first(watches)
        self._symlinks = symlinks
        self._max_aliases = max_aliases

    def map(self, event: FsEvent) -> list[PurgeTarget]:
        """Return the :class:`PurgeTarget` list for the event.

        One target per binding, or two (URL and wildcard) for an
        ``INVALIDATE_TREE`` event, for the event's path and for each of
        its aliases. Returns an empty list if the event's ``watch_root``
        does not match any configured watch (which should not happen if
        the filter is used correctly upstream, but defensive coding is
        cheap here).
        """
        bindings = self._by_root.get(event.watch_root)
        if not bindings:
            return []
        rel = event.abs_path.relative_to(event.watch_root)
        targets = _targets(bindings, rel, event.kind)
        if self._symlinks is None:
            return targets
        for alias in self._symlinks.aliases(event.abs_path, limit=self._max_aliases):
            found = find_watch(self._watches, alias)
            if found is None:
                continue
            watch, alias_rel = found
            if matches_any_glob(alias_rel.parts, watch.ignore):
                continue
            alias_bindings = [b for b in watch.bindings if b.purge_aliases]
            targets.extend(_targets(alias_bindings, alias_rel, event.kind))
        return targets


def _targets(bindings: list[Binding], rel: Path, kind: EventKind) -> list[PurgeTarget]:
    rel_str = rel.as_posix()
    targets: list[PurgeTarget] = []
    for b in bindings:
        url = b.url_prefix.rstrip("/") + "/" + rel_str
        targets.append(PurgeTarget(cdn_name=b.cdn, url=url))
        if kind is EventKind.INVALIDATE_TREE:
            targets.append(PurgeTarget(cdn_name=b.cdn, url=url + WILDCARD_SUFFIX))
    return targets
