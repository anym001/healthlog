"""Body-Battery (energy-reserve) computation (ARCHITECTURE.md §4.10).

Integrates the freshly computed intraday stress timeline (``stress_intraday``)
against recovery — stress and workouts drain the battery, calm rest and sleep
charge it, clamped to 0-100 — and writes the result into the dedicated
``body_battery_intraday`` / ``body_battery_daily`` tables. The math lives in
``pure.py`` (DB-free, unit-tested); this module is the DB glue: load the window's
stress timeline + sleep intervals (plus a warm-up margin before a bounded window,
so the neutral seed never reaches the stored rows), integrate once across the
whole window (the accumulator is continuous over day boundaries), summarise per
local day, and replace the window's rows idempotently.

Body Battery is a *proxy on a proxy*: it builds on the stress score, which HAE
cannot derive from beat-to-beat RR intervals, so the numbers track your own
baseline over time and are not comparable to a Garmin value. Integrator drift is
avoided by the self-correcting rate model — a night's sleep re-anchors the
battery toward 100, so the wake level is an emergent function of sleep quality,
not a hard-coded reset. This pass must run *after* the stress pass has flushed
its ``stress_intraday`` rows for the window.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from ..appconfig import BodyBatteryConfig
from ..models import BodyBatteryDaily, BodyBatteryIntraday
from .constants import BODY_BATTERY_WARMUP_DAYS, log
from .load import load_sleep_intervals, load_stress_intraday
from .pure import body_battery_timeline, summarize_body_battery_day
from .stress import _window_bounds


@dataclass
class BodyBatteryResult:
    """Outcome of a Body-Battery computation run (for logging / notifications)."""

    days: int = 0
    buckets: int = 0


def compute_body_battery(
    db: Session,
    tz: str,
    cfg: BodyBatteryConfig,
    since_days: int | None,
) -> BodyBatteryResult:
    """Compute the Body-Battery timeline + daily summary over the window and store it.

    ``since_days`` bounds the recompute window (``None`` = full history), resolved
    from the same heart-rate anchor as the stress pass so the two cover the same
    span. The window's existing rows are replaced (delete + insert) so the pass is
    fully idempotent — a re-run over the same stress timeline reproduces identical
    rows.

    A windowed recompute integrates ``BODY_BATTERY_WARMUP_DAYS`` extra days
    *before* the stored range: a day's last write happens on the run where it is
    the window's first day, so without the margin every archived day would keep
    the seed-influenced computation. The warm-up lets the nightly sleep re-anchor
    wash the seed out before the first stored bucket; only ``[start, end)`` is
    stored.
    """
    if not cfg.enabled:
        return BodyBatteryResult()

    bounds = _window_bounds(db, tz, since_days)
    if bounds is None:
        return BodyBatteryResult()
    start, end, first_day = bounds
    warm_start = start - dt.timedelta(days=BODY_BATTERY_WARMUP_DAYS) if since_days is not None else start

    intraday = load_stress_intraday(db, warm_start, end)
    if intraday.empty:
        _replace_window(db, start, end, first_day, [], [])
        return BodyBatteryResult()
    if intraday.index.tz is None:
        intraday.index = intraday.index.tz_localize(dt.UTC)

    sleep_intervals = load_sleep_intervals(db, warm_start, end)

    # Integrate once over the whole window (warm-up included): the battery is a
    # continuous accumulator, so unlike the stress summary it must not restart
    # each day.
    timeline = body_battery_timeline(
        intraday,
        sleep_intervals,
        neutral=cfg.neutral,
        charge_rate=cfg.charge_rate,
        drain_rate=cfg.drain_rate,
        sleep_charge_rate=cfg.sleep_charge_rate,
        active_drain_rate=cfg.active_drain_rate,
        seed_level=cfg.seed_level,
    )
    # Drop the warm-up rows — they only exist to settle the integrator.
    timeline = timeline[timeline.index >= start]
    if timeline.empty:
        _replace_window(db, start, end, first_day, [], [])
        return BodyBatteryResult()

    zone = ZoneInfo(tz)
    wake_by_day = _wake_ends_by_day(sleep_intervals, zone)

    intraday_rows = [
        {"ts": ts.to_pydatetime(), "level": int(round(float(level)))} for ts, level in timeline["level"].items()
    ]

    daily_rows: list[dict] = []
    for day, sub in timeline.groupby(timeline.index.tz_convert(zone).date):
        summ = summarize_body_battery_day(sub, wake_by_day.get(day))
        daily_rows.append(
            {
                "day": day,
                "wake_level": summ["wake_level"],
                "high_level": summ["high_level"],
                "low_level": summ["low_level"],
                "charged": summ["charged"],
                "drained": summ["drained"],
            }
        )

    _replace_window(db, start, end, first_day, intraday_rows, daily_rows)
    return BodyBatteryResult(days=len(daily_rows), buckets=len(intraday_rows))


def _wake_ends_by_day(sleep_intervals, zone: ZoneInfo) -> dict[dt.date, object]:
    """Map each local wake-day to the end of its *main* sleep (longest interval).

    The battery level at that timestamp is the day's ``wake_level`` — what you
    started the day with. A day with no sleep ending on it has no entry (→ no
    wake level).
    """
    best: dict[dt.date, tuple[float, object]] = {}
    for s_start, s_end, _eff in sleep_intervals:
        end_local = s_end.tz_convert(zone) if s_end.tzinfo is not None else s_end.tz_localize(dt.UTC).tz_convert(zone)
        day = end_local.date()
        duration = (s_end - s_start).total_seconds()
        prev = best.get(day)
        if prev is None or duration > prev[0]:
            best[day] = (duration, s_end)
    return {day: ts for day, (_dur, ts) in best.items()}


def _replace_window(
    db: Session,
    start: dt.datetime,
    end: dt.datetime,
    first_day: dt.date,
    intraday_rows: list[dict],
    daily_rows: list[dict],
) -> None:
    """Replace the window's rows: delete the range, then bulk-insert the fresh
    computation. Idempotent — the delete makes a re-run reproduce identical rows
    without stale leftovers."""
    db.execute(delete(BodyBatteryIntraday).where(BodyBatteryIntraday.ts >= start, BodyBatteryIntraday.ts < end))
    db.execute(delete(BodyBatteryDaily).where(BodyBatteryDaily.day >= first_day))
    if intraday_rows:
        db.execute(insert(BodyBatteryIntraday), intraday_rows)
    if daily_rows:
        db.execute(insert(BodyBatteryDaily), daily_rows)


def load_body_battery_daily(db: Session, since_days: int | None = None):
    """Recent ``body_battery_daily`` rows as a frame (for the alert-finding builder).

    ``since_days`` filters to days at least that recent (relative to the latest
    stored day). Empty frame when the table is empty.
    """
    import pandas as pd

    rows = db.execute(select(BodyBatteryDaily).order_by(BodyBatteryDaily.day)).scalars().all()
    if not rows:
        return pd.DataFrame()
    records = [
        {
            "day": pd.Timestamp(r.day),
            "wake_level": r.wake_level,
            "high_level": r.high_level,
            "low_level": r.low_level,
            "charged": r.charged,
            "drained": r.drained,
        }
        for r in rows
    ]
    df = pd.DataFrame.from_records(records).set_index("day")
    if since_days is not None and not df.empty:
        cutoff = df.index.max() - pd.Timedelta(days=since_days)
        df = df[df.index >= cutoff]
    return df


def run_body_battery(db: Session, tz: str, cfg: BodyBatteryConfig, since_days: int | None) -> BodyBatteryResult:
    """Compute + log the Body-Battery pass (thin wrapper used by run/CLI)."""
    result = compute_body_battery(db, tz, cfg, since_days)
    log.info("body_battery: days=%d buckets=%d", result.days, result.buckets)
    return result
