"""Nightly analysis scheduler.

Runs as its own process (s6 service), separate from uvicorn, so the heavy
analysis never blocks the ingest event loop. At the scheduled time it launches
the analysis as a *subprocess* (`python -m app.analysis`) so a crash in a C
extension can take down neither the scheduler nor uvicorn.

Three robustness guards around the cron slot:

- **Catch-up on start:** the timestamp of the last successful run is kept in a
  marker file under the config dir. If the most recent scheduled slot passed
  without a run (container was down at 03:30), the analysis runs at startup
  instead of silently skipping a day. The analysis is idempotent (snapshot
  replace), so a spurious extra run is harmless.
- **Misfire grace:** a slot that fires while the scheduler process is blocked
  still runs if it is less than an hour late (coalesced to one run).
- **Subprocess timeout:** a hung run (e.g. a DB lock) is killed after
  ``ANALYSIS_TIMEOUT_S`` and alerted like a crash, instead of wedging the
  scheduler forever.
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings, get_settings
from .logging_config import configure_logging

log = logging.getLogger("healthlog.scheduler")

DEFAULT_CRON = "30 3 * * *"

# A slot that fires late (scheduler blocked, clock jump) still runs within
# this window; older misfires collapse into the startup catch-up instead.
MISFIRE_GRACE_S = 3600

# Hard ceiling for one analysis run. Years of history finish in minutes, so
# hitting this means a wedged run (DB lock, hung connection), not a slow one.
ANALYSIS_TIMEOUT_S = 4 * 3600


def run_analysis() -> None:
    """Launch the analysis as an isolated subprocess (fault containment).

    Never raises: this runs both as the APScheduler job and as the startup
    catch-up call in ``main()`` — an escaping exception there would kill the
    scheduler process before the nightly job is even registered, silently
    disabling analysis until someone notices.
    """
    log.info("nightly analysis trigger fired")
    try:
        subprocess.run([sys.executable, "-m", "app.analysis"], check=True, timeout=ANALYSIS_TIMEOUT_S)
    except Exception as exc:
        log.error("analysis subprocess failed: %s", exc)
        # The crashed subprocess can't notify about its own death; the scheduler
        # owns the crash alert (the success/findings notifications are sent from
        # inside the analysis run, which has the result).
        from .appconfig import get_app_config
        from .notify import notify_analysis_crash

        try:
            notify_analysis_crash(get_app_config().notify, exc)
        except Exception:
            log.exception("could not send analysis-crash notification")
        return
    write_last_run(last_run_marker(get_settings()), dt.datetime.now(dt.UTC))


def build_trigger(settings: Settings) -> CronTrigger:
    """Schedule from the ANALYSIS_CRON 5-field expression (in local_tz).
    An empty value falls back to the default daily 03:30."""
    cron = settings.analysis_cron.strip() or DEFAULT_CRON
    return CronTrigger.from_crontab(cron, timezone=settings.local_tz)


# ---------------------------------------------------------------------------
# Catch-up state. The marker lives next to config.yaml (the one writable
# mount) rather than in the DB: a run that computes zero findings leaves the
# findings table empty, so table contents can't tell "ran" from "never ran".
# ---------------------------------------------------------------------------


def last_run_marker(settings: Settings) -> Path:
    return Path(settings.config_file).parent / "state" / "last_analysis"


def read_last_run(marker: Path) -> dt.datetime | None:
    """Timestamp of the last successful run, or None (missing/unreadable —
    either way the catch-up run is idempotent, so failing open is safe)."""
    try:
        stamp = dt.datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)


def write_last_run(marker: Path, when: dt.datetime) -> None:
    """Record a successful run; best-effort (an unwritable /config only costs
    an extra idempotent catch-up run on the next start)."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(when.isoformat())
    except OSError as exc:
        log.warning("could not write last-run marker %s: %s", marker, exc)


def missed_run(trigger: CronTrigger, last_run: dt.datetime | None, now: dt.datetime) -> bool:
    """True when a scheduled slot passed since ``last_run`` without a run
    (i.e. the first slot after the last successful run is already in the
    past). No marker at all also counts as missed."""
    if last_run is None:
        return True
    next_after_last = trigger.get_next_fire_time(None, last_run)
    return next_after_last is not None and next_after_last <= now


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    trigger = build_trigger(settings)

    last_run = read_last_run(last_run_marker(settings))
    if missed_run(trigger, last_run, dt.datetime.now(dt.UTC)):
        log.info(
            "catch-up: last successful analysis (%s) predates the most recent scheduled slot, running now",
            last_run.isoformat() if last_run else "never",
        )
        run_analysis()

    scheduler = BlockingScheduler(timezone=settings.local_tz)
    scheduler.add_job(
        run_analysis,
        trigger,
        id="nightly_analysis",
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_S,
    )
    log.info(
        "scheduler started: nightly analysis cron='%s' %s",
        settings.analysis_cron.strip() or DEFAULT_CRON,
        settings.local_tz,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
