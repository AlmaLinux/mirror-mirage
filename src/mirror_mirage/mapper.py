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

The mapper is a pure function over its config. It performs no I/O and
holds no mutable state, which makes it trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from mirror_mirage.config import WatchConfig
from mirror_mirage.filter import EventKind, FsEvent
from mirror_mirage.providers.base import WILDCARD_SUFFIX


@dataclass(frozen=True)
class PurgeTarget:
    """A single (cdn_name, url) pair the batcher should coalesce."""

    cdn_name: str
    url: str


class Mapper:
    """Maps normalized filesystem events to per-CDN purge targets."""

    def __init__(self, watches: list[WatchConfig]) -> None:
        self._by_root = {w.path: list(w.bindings) for w in watches}

    def map(self, event: FsEvent) -> list[PurgeTarget]:
        """Return the :class:`PurgeTarget` list for the event's watch bindings.

        One target per binding, or two (URL and wildcard) for an
        ``INVALIDATE_TREE`` event. Returns an empty list if the event's
        ``watch_root`` does not match any configured watch (which should
        not happen if the filter is used correctly upstream, but
        defensive coding is cheap here).
        """
        bindings = self._by_root.get(event.watch_root)
        if not bindings:
            return []
        rel = event.abs_path.relative_to(event.watch_root).as_posix()
        targets: list[PurgeTarget] = []
        for b in bindings:
            url = b.url_prefix.rstrip("/") + "/" + rel
            targets.append(PurgeTarget(cdn_name=b.cdn, url=url))
            if event.kind is EventKind.INVALIDATE_TREE:
                targets.append(PurgeTarget(cdn_name=b.cdn, url=url + WILDCARD_SUFFIX))
        return targets
