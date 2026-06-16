"""HealthLog operator CLI (``healthlog`` console script).

A single entry point with subcommands, so operators type a short command
instead of a module path::

    healthlog backfill /config/import
    healthlog backfill --dry-run /config/import

New operator commands are added as further subparsers here. The installed
console script (see ``pyproject.toml``) maps ``healthlog`` to ``main``;
``python -m app`` resolves here too.
"""

from __future__ import annotations

import argparse
import sys

from . import backfill


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="healthlog", description="HealthLog operator CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    bf = sub.add_parser("backfill", help="bulk-import Apple Health history from disk")
    backfill.add_arguments(bf)
    bf.set_defaults(func=backfill.run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
