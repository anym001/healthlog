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

from .config import Settings, get_settings
from .logging_config import configure_logging

log = logging.getLogger("healthlog.scheduler")


def run_analysis() -> None:
    """Launch the analysis as an isolated subprocess (fault containment)."""
    log.info("nightly analysis trigger fired")
    try:
        subprocess.run([sys.executable, "-m", "app.analysis"], check=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        log.error("analysis subprocess failed: %s", exc)
        # The crashed subprocess can't notify about its own death; the scheduler
        # owns the crash alert (the success/findings notifications are sent from
        # inside the analysis run, which has the result).
        from .notify import notify_analysis_crash

        notify_analysis_crash(get_settings(), exc)


DEFAULT_CRON = "30 3 * * *"


def build_trigger(settings: Settings) -> CronTrigger:
    """Schedule from the ANALYSIS_CRON 5-field expression (in local_tz).
    An empty value falls back to the default daily 03:30."""
    cron = settings.analysis_cron.strip() or DEFAULT_CRON
    return CronTrigger.from_crontab(cron, timezone=settings.local_tz)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    scheduler = BlockingScheduler(timezone=settings.local_tz)
    scheduler.add_job(run_analysis, build_trigger(settings), id="nightly_analysis")
    log.info(
        "scheduler started: nightly analysis cron='%s' %s",
        settings.analysis_cron.strip() or DEFAULT_CRON,
        settings.local_tz,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
