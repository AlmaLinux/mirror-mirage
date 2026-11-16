"""Index of the symlinks under the watch roots, used to find alias paths.

A file in a mirror is often reachable under more than one path:

* through a file symlink, e.g. ``AlmaLinux-10-latest-x86_64-dvd.iso ->
  AlmaLinux-10.3-x86_64-dvd.iso``;
* through a directory symlink on one of its ancestors, e.g. ``10 -> 10.3``;
* or both, e.g. ``10/isos/x86_64/AlmaLinux-10-latest-x86_64-dvd.iso``.

Each of those paths is a separate URL and a separate CDN cache entry, but
inotify only reports the real path. :class:`SymlinkIndex` records every
symlink with its target so :meth:`SymlinkIndex.aliases` can answer "which
other paths reach this one?".

The watcher fills the index during its startup walk, and the daemon keeps
it current by passing every watcher event to :meth:`SymlinkIndex.observe`
before the filter runs.

Targets are stored lexically, one hop at a time:
``normpath(link.parent / readlink(link))``. Nothing is resolved with
``stat`` or ``realpath``, so a dangling link is indexed like any other,
and a link whose target passes through another link is handled by
following the chain in :meth:`SymlinkIndex.aliases`. A link with an
absolute target is only matched when the target is written with the same
path the watch uses (not, say, the device path behind a bind mount).
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path

_log = logging.getLogger("mirror_mirage.symlinks")

# Watcher events that can mean a symlink now exists at the path (``ln -s``
# gives IN_CREATE; an atomic replace by rsync or ``ln -sfn`` gives
# IN_MOVED_TO) and events that mean whatever was at the path is gone.
_APPEAR_MASKS = frozenset({"IN_CREATE", "IN_MOVED_TO"})
_GONE_MASKS = frozenset({"IN_DELETE", "IN_MOVED_FROM"})


class SymlinkIndex:
    """Forward (link → target) and reverse (target → links) maps of symlinks."""

    def __init__(self) -> None:
        self._target: dict[Path, Path] = {}
        self._links_to: dict[Path, set[Path]] = {}
        self._by_parent: dict[Path, set[Path]] = {}

    def __len__(self) -> int:
        return len(self._target)

    def __contains__(self, path: object) -> bool:
        return path in self._target

    def target(self, link: Path) -> Path | None:
        """Return the indexed one-hop target of ``link``, or ``None``."""
        return self._target.get(link)

    def links(self) -> Iterator[tuple[Path, Path]]:
        """Iterate over ``(link, target)`` pairs."""
        return iter(self._target.items())

    def record(self, link: Path) -> bool:
        """Read ``link`` from disk and index it. Return True if the index changed.

        If ``link`` is no longer a symlink (or cannot be read), it is
        forgotten instead.
        """
        try:
            raw = os.readlink(link)
        except OSError:
            return self.forget(link)
        target = Path(os.path.normpath(os.path.join(link.parent, raw)))
        old = self._target.get(link)
        if old == target:
            return False
        if old is not None:
            self._drop_reverse(link, old)
        self._target[link] = target
        self._links_to.setdefault(target, set()).add(link)
        self._by_parent.setdefault(link.parent, set()).add(link)
        return True

    def forget(self, path: Path) -> bool:
        """Remove ``path`` from the index if it is a known link."""
        old = self._target.pop(path, None)
        if old is None:
            return False
        self._drop_reverse(path, old)
        siblings = self._by_parent.get(path.parent)
        if siblings is not None:
            siblings.discard(path)
            if not siblings:
                del self._by_parent[path.parent]
        return True

    def forget_tree(self, directory: Path) -> int:
        """Remove every link at or below ``directory``. Return how many.

        Used when a directory is moved out of (or deleted from) a watch
        root: the links inside it produce no events of their own.
        """
        parents = [p for p in self._by_parent if p == directory or p.is_relative_to(directory)]
        removed = 0
        for parent in parents:
            for link in list(self._by_parent.get(parent, ())):
                removed += self.forget(link)
        return removed

    def observe(self, mask_name: str, path: Path) -> bool:
        """Update the index from one watcher event. Return True if it changed.

        ``mask_name`` is the symbolic inotify mask the watcher reports
        (``IN_CREATE``, ``IN_MOVED_TO``, ``IN_DELETE``, ...). Called for
        every event, before filtering, so links in paths the filter drops
        (for example when ``flush_on_delete`` is off) are still tracked.
        """
        if mask_name in _APPEAR_MASKS:
            if path.is_symlink():
                return self.record(path)
            return self.forget(path)
        if mask_name in _GONE_MASKS:
            return self.forget(path)
        return False

    def aliases(self, path: Path, *, limit: int) -> list[Path]:
        """Return the other paths that reach ``path`` through symlinks.

        Covers links to ``path`` itself, links to any of its ancestors
        (the path re-rooted under the link), and any chain of the two.
        ``path`` itself is not included. At most ``limit`` aliases are
        returned; hitting the limit is logged, since it usually means a
        symlink loop (e.g. ``a -> .``).
        """
        if limit <= 0:
            return []
        seen: set[Path] = {path}
        found: list[Path] = []
        work: deque[Path] = deque([path])
        while work:
            current = work.popleft()
            parts = current.parts
            for cut in range(len(parts), 0, -1):
                links = self._links_to.get(Path(*parts[:cut]))
                if not links:
                    continue
                rest = parts[cut:]
                for link in sorted(links):
                    alias = link.joinpath(*rest)
                    if alias in seen:
                        continue
                    seen.add(alias)
                    found.append(alias)
                    if len(found) >= limit:
                        _log.warning(
                            "event=alias_limit_reached path=%s limit=%d",
                            path,
                            limit,
                        )
                        return found
                    work.append(alias)
        return found

    def _drop_reverse(self, link: Path, target: Path) -> None:
        links = self._links_to.get(target)
        if links is None:
            return
        links.discard(link)
        if not links:
            del self._links_to[target]
