"""CLI entrypoint.

Usage::

    mirror-mirage --config /etc/mirror-mirage/config.yaml
                  [--secrets /etc/mirror-mirage/secrets.yaml]
                  [--check] [--dry-run]
                  [--edge-aliases {show,diff} [--cdn NAME]]

* ``--check`` validates config and exits.
* ``--dry-run`` runs the daemon but skips real CDN API calls.
* ``--edge-aliases show`` prints the alias → real path items each Fastly
  CDN's ``edge_aliases`` rules produce from the current tree, and
  ``--edge-aliases diff`` compares them with the live dictionary. Both
  exit without starting the daemon; neither writes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from mirror_mirage import daemon, edge_aliases
from mirror_mirage import logging as mirror_mirage_logging
from mirror_mirage.config import Config, ConfigError, load
from mirror_mirage.watcher import InotifyLimitError


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mirror-mirage",
        description="Inotify-driven CDN cache-flush daemon for filesystem mirrors",
    )
    parser.add_argument("--config", required=True, help="path to config.yaml")
    parser.add_argument(
        "--secrets",
        help="path to secrets.yaml (default: secrets.yaml next to --config)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config and exit without starting the daemon",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log intended purges without calling any CDN API",
    )
    parser.add_argument(
        "--edge-aliases",
        choices=["show", "diff"],
        help=(
            "print the edge dictionary items the edge_aliases rules produce (show) "
            "or their difference from the live Fastly dictionary (diff), then exit"
        ),
    )
    parser.add_argument("--cdn", help="limit --edge-aliases to this CDN")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable DEBUG-level logging",
    )
    return parser.parse_args(argv)


async def _run(cfg: Config, *, dry_run: bool) -> None:
    task = asyncio.create_task(daemon.run(cfg, dry_run=dry_run), name="mirror-mirage-daemon")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def main(argv: list[str] | None = None) -> int:
    """Argument parsing, signal handling, and asyncio.run() entrypoint.

    Returns a process exit code suitable for sys.exit.
    """
    args = _parse_args(argv)
    mirror_mirage_logging.configure(level=logging.DEBUG if args.verbose else logging.INFO)

    config_path = Path(args.config)
    secrets_path = Path(args.secrets) if args.secrets else config_path.parent / "secrets.yaml"

    try:
        cfg = load(config_path, secrets_path)
    except ConfigError as e:
        print(f"mirror-mirage: config error: {e}", file=sys.stderr)
        return 1

    if args.check:
        print("mirror-mirage: config OK")
        return 0

    if args.edge_aliases:
        return asyncio.run(
            edge_aliases.report(
                cfg, mode=args.edge_aliases, cdn=args.cdn, out=sys.stdout, err=sys.stderr
            )
        )

    try:
        asyncio.run(_run(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
    except InotifyLimitError as e:
        print(f"mirror-mirage: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
