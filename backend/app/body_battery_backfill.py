"""Re-derive the Body-Battery timeline from the stored stress history.

The nightly analysis only recomputes a trailing window
(``body_battery.window_days``), so after a bulk backfill — or a config change to
the Body-Battery model — the older days still carry the previous (or no)
computation. This one-off command recomputes the Body-Battery timeline + daily
summary over the full history (or a chosen trailing window) and replaces those
rows idempotently.

Body Battery integrates the ``stress_intraday`` timeline, so run it *after*
``rederive-stress --all`` when rebuilding history — it reads the (by then fresh)
stress rows.

Usage (one-shot, typically via ``docker exec``)::

    healthlog rederive-body-battery            # recompute the full history
    healthlog rederive-body-battery --days 30  # recompute only the trailing 30 days

(equivalently ``python -m app rederive-body-battery`` or
``python -m app.body_battery_backfill``.)
"""

from __future__ import annotations

import argparse
import logging
import sys

from .cli_support import bootstrap, db_session, module_main

log = logging.getLogger("healthlog.body_battery")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="recompute the full history (default)")
    group.add_argument("--days", type=int, metavar="N", help="recompute only the trailing N days")


def run(args: argparse.Namespace) -> int:
    settings = bootstrap()

    from .analysis.body_battery import run_body_battery
    from .appconfig import load_config

    cfg = load_config(settings.config_file)
    if not cfg.body_battery.enabled:
        log.warning("body_battery is disabled (config body_battery.enabled=false); nothing to do")
        return 0

    since_days = args.days  # None (full history) unless --days was given
    with db_session() as db:
        result = run_body_battery(db, settings.local_tz, cfg.body_battery, since_days)
        db.commit()

    log.info(
        "rederive-body-battery done (%s): days=%d buckets=%d",
        f"last {since_days}d" if since_days is not None else "full history",
        result.days,
        result.buckets,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return module_main(
        add_arguments,
        run,
        prog="python -m app.body_battery_backfill",
        description="Re-derive the Body-Battery timeline from the stored stress history.",
        argv=argv,
    )


if __name__ == "__main__":
    sys.exit(main())
