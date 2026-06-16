"""Nightly analysis scheduler (Phase 1 skeleton).

Runs as its own process (s6 service), separate from uvicorn, so the heavy
analysis never blocks the ingest event loop. At the scheduled time it will
launch the analysis as a *subprocess* (Phase 3) so a crash in a C extension
cannot take down the scheduler. For now it logs a placeholder.
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
        # Phase 3 will implement `python -m app.analysis`. The module is not
        # present yet, so this is a no-op placeholder that fails softly.
        subprocess.run([sys.executable, "-c", "print('healthlog analysis placeholder')"], check=True)
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
