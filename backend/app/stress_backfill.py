"""Re-derive the stress timeline from stored heart-rate history.

The nightly analysis only recomputes a trailing window (``stress.window_days``),
so after a bulk backfill — or a config change to the stress model — the older
days still carry the previous (or no) computation. This one-off command
recomputes the stress timeline + daily summary over the full history (or a
chosen trailing window) and replaces those rows idempotently.

Usage (one-shot, typically via ``docker exec``)::

    healthlog rederive-stress            # recompute the full history
    healthlog rederive-stress --days 30  # recompute only the trailing 30 days

(equivalently ``python -m app rederive-stress`` or ``python -m app.stress_backfill``.)
"""

from __future__ import annotations

import argparse
import logging
import sys

from .cli_support import bootstrap, db_session, module_main

log = logging.getLogger("healthlog.stress")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="recompute the full history (default)")
    group.add_argument("--days", type=int, metavar="N", help="recompute only the trailing N days")


def run(args: argparse.Namespace) -> int:
    settings = bootstrap()

    from .analysis.stress import run_stress
    from .appconfig import load_config

    cfg = load_config(settings.config_file)
    if not cfg.stress.enabled:
        log.warning("stress is disabled (config stress.enabled=false); nothing to do")
        return 0

    since_days = args.days  # None (full history) unless --days was given
    with db_session() as db:
        result = run_stress(db, settings.local_tz, cfg.stress, cfg.profile, since_days)
        db.commit()

    log.info(
        "rederive-stress done (%s): days=%d buckets=%d",
        f"last {since_days}d" if since_days is not None else "full history",
        result.days,
        result.buckets,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return module_main(
        add_arguments,
        run,
        prog="python -m app.stress_backfill",
        description="Re-derive the stress timeline from stored heart-rate history.",
        argv=argv,
    )


if __name__ == "__main__":
    sys.exit(main())
