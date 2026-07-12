"""Lightweight intraday refresh: stress + Body Battery over the last two days.

The full nightly analysis (``run.py``) recomputes findings and a 90-day
stress/Body-Battery window — too heavy to run hourly. But HAE syncs deltas
every hour, so without an intraday pass the Stress dashboard only shows
*today* after the next nightly run. This module recomputes just the stress
timeline and its Body-Battery integration over a two-day window (yesterday +
today, so the pass is seamless across midnight) and nothing else: no findings,
no notifications. Cheap (a few thousand buckets) and idempotent, it is run
hourly by the scheduler as an isolated subprocess (``python -m
app.analysis.refresh``), schedule via ``INTRADAY_CRON``.

The Body-Battery warm-up margin (``BODY_BATTERY_WARMUP_DAYS``) applies here
too, so the short window still integrates from a settled level.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..appconfig import AppConfig, load_config
from ..cli_support import bootstrap, db_session
from .body_battery import run_body_battery
from .constants import log
from .stress import run_stress

# Yesterday + today: enough that the pass is continuous across midnight while
# staying trivially cheap. The nightly run still refreshes the full window.
REFRESH_WINDOW_DAYS = 2


def run_refresh(db: Session, tz: str, config: AppConfig) -> None:
    """Recompute the trailing two days of stress + Body Battery (flush only)."""
    run_stress(db, tz, config.stress, config.profile, REFRESH_WINDOW_DAYS)
    db.flush()
    run_body_battery(db, tz, config.body_battery, REFRESH_WINDOW_DAYS)


def main() -> int:
    settings = bootstrap()
    config = load_config(settings.config_file)
    if not (config.stress.enabled or config.body_battery.enabled):
        log.info("intraday refresh: stress and body_battery disabled; nothing to do")
        return 0

    with db_session() as db:
        try:
            run_refresh(db, settings.local_tz, config)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("intraday refresh failed")
            raise

    log.info("intraday refresh done (last %dd)", REFRESH_WINDOW_DAYS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
