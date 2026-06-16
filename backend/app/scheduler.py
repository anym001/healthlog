"""Nightly analysis scheduler.

Runs as its own process (s6 service), separate from uvicorn, so the heavy
analysis never blocks the ingest event loop. At the scheduled time it launches
the analysis as a *subprocess* (`python -m app.analysis`) so a crash in a C
extension can take down neither the scheduler nor uvicorn.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .logging_config import configure_logging

log = logging.getLogger("healthlog.scheduler")


def run_analysis() -> None:
    """Launch the analysis as an isolated subprocess (fault containment)."""
    log.info("nightly analysis trigger fired")
    try:
        subprocess.run([sys.executable, "-m", "app.analysis"], check=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        log.error("analysis subprocess failed: %s", exc)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    scheduler = BlockingScheduler(timezone=settings.local_tz)
    scheduler.add_job(
        run_analysis,
        CronTrigger(hour=settings.analysis_hour, minute=settings.analysis_minute),
        id="nightly_analysis",
    )
    log.info(
        "scheduler started: nightly analysis at %02d:%02d %s",
        settings.analysis_hour,
        settings.analysis_minute,
        settings.local_tz,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
