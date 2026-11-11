"""Is new data still arriving? (``healthlog audit`` and the hourly check.)

When Health Auto Export stops sending (automation switched off, Background App
Refresh disabled, an expired token) nothing in HealthLog fails: no request
means no error, and the nightly analysis keeps reporting "OK" over old data.
This module measures the two timestamps that show it:

- **last new payload**: the newest ``raw_ingest.received_at``. A re-sent,
  byte-identical payload is discarded as a duplicate and not archived, so this
  is the last delivery that brought something new.
- **newest data point**: the newest ``metric_samples.time``. HAE can keep
  sending on schedule while the payloads hold no new readings (the Watch has
  not synced, the iPhone stayed locked).

Either older than ``freshness.max_age_hours`` counts as overdue. ``healthlog
audit`` reports both; the scheduler runs ``python -m app.freshness`` hourly,
which pushes one alert per gap (``freshness`` notification source) and an
all-clear once data arrives again. Whether the alert went out is kept in a
marker file next to ``config.yaml`` (like the analysis catch-up marker), and
written only after a delivered push, so a failed push is retried next hour.

``assess`` and ``decide`` are pure and unit-tested; ``read_timestamps`` and
``run_check`` do the SQL, the marker and the push.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .cli_support import bootstrap, db_session

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .config import Settings

log = logging.getLogger("healthlog.freshness")

# A data point stamped further ahead than this (a phone with a wrong clock)
# is ignored, so it can't hide a gap until its date has passed.
FUTURE_SLACK = dt.timedelta(hours=1)


@dataclass(frozen=True)
class Freshness:
    last_payload: dt.datetime | None
    newest_sample: dt.datetime | None
    checked_at: dt.datetime
    max_age: dt.timedelta

    @property
    def has_data(self) -> bool:
        return self.last_payload is not None

    @property
    def payloads_overdue(self) -> bool:
        return self.has_data and self.checked_at - self.last_payload > self.max_age

    @property
    def samples_overdue(self) -> bool:
        if not self.has_data:
            return False
        return self.newest_sample is None or self.checked_at - self.newest_sample > self.max_age

    @property
    def overdue(self) -> bool:
        return self.payloads_overdue or self.samples_overdue


def assess(
    last_payload: dt.datetime | None,
    newest_sample: dt.datetime | None,
    now: dt.datetime,
    max_age_hours: int,
) -> Freshness:
    return Freshness(last_payload, newest_sample, now, dt.timedelta(hours=max_age_hours))


def decide(freshness: Freshness, alerted: bool) -> str | None:
    """``"alert"`` when data became overdue and no alert went out yet,
    ``"resume"`` when it arrives again after one, else None. A fresh install
    without any payload never alerts."""
    if freshness.overdue and not alerted:
        return "alert"
    if not freshness.overdue and alerted and freshness.has_data:
        return "resume"
    return None


def format_local(when: dt.datetime | None, tz: str) -> str:
    return when.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") if when else "none"


def read_timestamps(db: Session, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    """(last new payload, newest data point). ORDER BY … LIMIT 1 rather than
    max(): on the hypertables it reads only the newest chunk."""
    from sqlalchemy import text

    last_payload = db.execute(text("SELECT received_at FROM raw_ingest ORDER BY received_at DESC LIMIT 1")).scalar()
    newest_sample = db.execute(
        text("SELECT time FROM metric_samples WHERE time <= :limit ORDER BY time DESC LIMIT 1"),
        {"limit": now + FUTURE_SLACK},
    ).scalar()
    return last_payload, newest_sample


# ---------------------------------------------------------------------------
# Alert marker: present while an alert for the current gap is out.
# ---------------------------------------------------------------------------


def alert_marker(settings: Settings) -> Path:
    return Path(settings.config_file).parent / "state" / "freshness_alert"


def read_alert(marker: Path) -> dt.datetime | None:
    """When the alert for the current gap was sent, or None (none out, or an
    unreadable marker: at worst one repeated alert)."""
    try:
        stamp = dt.datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)


def write_alert(marker: Path, when: dt.datetime | None) -> None:
    """Record the sent alert (``when``) or clear it (None); best-effort."""
    try:
        if when is None:
            marker.unlink(missing_ok=True)
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(when.isoformat())
    except OSError as exc:
        log.warning("could not update freshness marker %s: %s", marker, exc)


def run_check(now: dt.datetime | None = None) -> int:
    """The hourly check (``python -m app.freshness``): push an alert or the
    all-clear when ``decide`` says so. Does nothing, not even the queries,
    while notifications or the ``freshness`` source are off."""
    settings = bootstrap()
    from .appconfig import load_config
    from .notify import compose_data_resumed_message, compose_stale_message, notify_freshness

    cfg = load_config(settings.config_file)
    if not cfg.notify.url or "freshness" not in cfg.notify.event_set():
        log.debug("freshness check skipped: notifications or the freshness source are off")
        return 0

    now = now or dt.datetime.now(dt.UTC)
    with db_session() as db:
        last_payload, newest_sample = read_timestamps(db, now)
    freshness = assess(last_payload, newest_sample, now, cfg.freshness.max_age_hours)
    marker = alert_marker(settings)
    alerted_at = read_alert(marker)
    action = decide(freshness, alerted_at is not None)

    tz = settings.local_tz
    if action == "alert":
        message = compose_stale_message(
            max_age_hours=cfg.freshness.max_age_hours,
            last_payload=format_local(last_payload, tz),
            newest_sample=format_local(newest_sample, tz),
            payloads_overdue=freshness.payloads_overdue,
        )
        if notify_freshness(cfg.notify, message):
            write_alert(marker, now)
    elif action == "resume":
        message = compose_data_resumed_message(
            last_payload=format_local(last_payload, tz),
            newest_sample=format_local(newest_sample, tz),
            alerted_at=format_local(alerted_at, tz),
        )
        if notify_freshness(cfg.notify, message):
            write_alert(marker, None)
    log.log(
        logging.WARNING if freshness.overdue else logging.DEBUG,
        "freshness: last new payload %s, newest data point %s, %s",
        format_local(last_payload, tz),
        format_local(newest_sample, tz),
        f"overdue (> {cfg.freshness.max_age_hours} h)" if freshness.overdue else "ok",
    )
    return 0


def main() -> int:
    return run_check()


if __name__ == "__main__":
    sys.exit(main())
