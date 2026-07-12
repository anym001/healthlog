"""Intraday stress-proxy computation (ARCHITECTURE.md §4.9).

Derives a Garmin-style stress timeline + daily summary from the all-day
per-minute heart-rate buckets (elevation above the personal resting baseline,
workouts excluded, optionally HRV-modulated) and writes them into the dedicated
``stress_intraday`` / ``stress_daily`` tables. The math lives in ``pure.py``
(DB-free, unit-tested); this module is the DB glue: load the window, compute per
local day, and replace the window's rows idempotently.

Stress is a *proxy*: HAE does not export the beat-to-beat RR intervals a
Garmin/Firstbeat score needs, so the numbers track your own baseline over time
and are not comparable to a Garmin value.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session

from ..appconfig import ProfileConfig, StressConfig
from ..models import StressDaily, StressIntraday
from ..registry import METRIC_REGISTRY
from .constants import log
from .load import load_daily_series, load_intraday_hr, load_intraday_steps, load_workout_intervals
from .pure import resolve_hr_max, resolve_hr_rest, robust_z, stress_intraday_from_hr, summarize_stress_day


@dataclass
class StressResult:
    """Outcome of a stress computation run (for logging / notifications)."""

    days: int = 0
    buckets: int = 0


def hr_window_bounds(db: Session, tz: str, since_days: int | None) -> tuple[dt.datetime, dt.datetime, dt.date] | None:
    """Resolve the recompute window ``[start, end)`` and its first local day.

    Anchored on the newest heart-rate sample (not wall-clock now, so historical
    backfills recompute correctly). ``since_days=None`` means the full history.
    Returns ``None`` when there is no heart-rate data.
    """
    row = db.execute(
        text("SELECT min(time) AS lo, max(time) AS hi FROM metric_samples WHERE metric = 'heart_rate'")
    ).one()
    if row.hi is None:
        return None
    zone = ZoneInfo(tz)
    end = row.hi + dt.timedelta(seconds=1)
    if since_days is None:
        start = row.lo
        first_day = row.lo.astimezone(zone).date()
    else:
        last_day = row.hi.astimezone(zone).date()
        first_day = last_day - dt.timedelta(days=since_days - 1)
        start = dt.datetime.combine(first_day, dt.time.min, tzinfo=zone).astimezone(dt.UTC)
    return start, end, first_day


def _minute_cadence(steps: pd.Series) -> bool:
    """True when the step buckets are fine-grained enough for per-minute gating.

    An hourly-grouped export delivers one bucket per hour whose step total would
    spuriously gate the single co-timed HR bucket; gate only when the typical
    gap between step buckets is (near-)minute. Steps arrive in bursts (no bucket
    while sitting still), so the *median* gap is the cadence signal — a fine
    export has runs of consecutive minutes that dominate the median.
    """
    if len(steps) < 2:
        return False
    gaps = steps.index.to_series().diff().dt.total_seconds().dropna()
    return float(gaps.median()) <= 120.0


def compute_stress(
    db: Session,
    tz: str,
    cfg: StressConfig,
    profile: ProfileConfig,
    since_days: int | None,
) -> StressResult:
    """Compute the stress timeline + daily summary over the window and store it.

    ``since_days`` bounds the recompute window (``None`` = full history). The
    window's existing rows are replaced (delete + insert) so the pass is fully
    idempotent — a re-run reproduces identical rows.
    """
    if not cfg.enabled:
        return StressResult()

    bounds = hr_window_bounds(db, tz, since_days)
    if bounds is None:
        return StressResult()
    start, end, first_day = bounds

    hr = load_intraday_hr(db, start, end)
    if hr.empty:
        _replace_window(db, start, end, first_day, [], [])
        return StressResult()
    if hr.index.tz is None:
        hr.index = hr.index.tz_localize(dt.UTC)

    # Baselines from the whole history (cheap daily series), so a short window
    # still gets a stable trailing-median resting HR and HRV baseline.
    zone = ZoneInfo(tz)
    rhr = load_daily_series(db, "resting_heart_rate", METRIC_REGISTRY["resting_heart_rate"]["agg_default"], tz)
    hr_rest_series, hr_rest_default = resolve_hr_rest(rhr, profile)
    hr_max = resolve_hr_max(profile, load_daily_series(db, "heart_rate", "max", tz))
    hrv_daily = load_daily_series(db, "heart_rate_variability", "avg", tz)
    hrv_z_series = robust_z(hrv_daily.dropna()) if not hrv_daily.dropna().empty else pd.Series(dtype="float64")

    intervals = load_workout_intervals(db, start, end)

    # Step-based activity gating: only usable with per-minute step buckets — an
    # hourly step total would spuriously gate single buckets, so the gate
    # self-disables on coarse data (see _minute_cadence).
    steps = load_intraday_steps(db, start, end) if cfg.active_steps_per_min > 0 else pd.Series(dtype="float64")
    if not _minute_cadence(steps):
        steps = pd.Series(dtype="float64")
    elif steps.index.tz is None:
        steps.index = steps.index.tz_localize(dt.UTC)

    day_keys = hr.index.tz_convert(zone).date

    intraday_rows: list[dict] = []
    daily_rows: list[dict] = []
    for day, sub in hr.groupby(day_keys):
        day_ts = pd.Timestamp(day)
        hr_rest_day = float(hr_rest_series.get(day_ts, hr_rest_default)) if len(hr_rest_series) else hr_rest_default
        hrv_z_day = hrv_z_series.get(day_ts)
        hrv_z_day = float(hrv_z_day) if hrv_z_day is not None and not pd.isna(hrv_z_day) else None

        day_start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
        day_end = day_start + dt.timedelta(days=1)
        day_intervals = [(s, e) for (s, e) in intervals if e >= day_start and s < day_end]

        frame = stress_intraday_from_hr(
            sub,
            hr_rest_day,
            hr_max,
            workout_intervals=day_intervals,
            hrv_z=hrv_z_day,
            reserve_full=cfg.reserve_full,
            hrv_weight=cfg.hrv_weight,
            zone_low=cfg.zone_low,
            zone_medium=cfg.zone_medium,
            zone_high=cfg.zone_high,
            steps=steps,
            active_steps_per_min=cfg.active_steps_per_min,
        )
        for ts, stress, bpm, state in zip(
            frame.index, frame["stress"].to_numpy(), frame["hr"].to_numpy(), frame["state"].to_numpy(), strict=True
        ):
            intraday_rows.append(
                {
                    "ts": ts.to_pydatetime(),
                    "stress": int(stress) if stress is not None and not pd.isna(stress) else None,
                    "hr": float(bpm) if bpm is not None and not pd.isna(bpm) else None,
                    "state": state,
                }
            )

        summ = summarize_stress_day(frame)
        if summ["measured_min"] >= cfg.min_measured_min and summ["score"] is not None:
            daily_rows.append(
                {
                    "day": day,
                    "score": summ["score"],
                    "rest_min": summ["rest_min"],
                    "low_min": summ["low_min"],
                    "medium_min": summ["medium_min"],
                    "high_min": summ["high_min"],
                    "active_min": summ["active_min"],
                    "unmeasurable_min": summ["unmeasurable_min"],
                    "hrv_z": round(hrv_z_day, 4) if hrv_z_day is not None else None,
                }
            )

    _replace_window(db, start, end, first_day, intraday_rows, daily_rows)
    return StressResult(days=len(daily_rows), buckets=len(intraday_rows))


def _replace_window(
    db: Session,
    start: dt.datetime,
    end: dt.datetime,
    first_day: dt.date,
    intraday_rows: list[dict],
    daily_rows: list[dict],
) -> None:
    """Replace the window's rows: delete the range, then bulk-insert the fresh
    computation. Idempotent — the delete makes a re-run reproduce identical
    rows without stale leftovers (a day that dropped below the measured-minutes
    floor loses its old row)."""
    db.execute(delete(StressIntraday).where(StressIntraday.ts >= start, StressIntraday.ts < end))
    db.execute(delete(StressDaily).where(StressDaily.day >= first_day))
    if intraday_rows:
        db.execute(insert(StressIntraday), intraday_rows)
    if daily_rows:
        db.execute(insert(StressDaily), daily_rows)


def load_stress_daily(db: Session, since_days: int | None = None) -> pd.DataFrame:
    """Recent ``stress_daily`` rows as a frame (for the alert-finding builder).

    ``since_days`` filters to days at least that recent (relative to the latest
    stored day). Empty frame when the table is empty.
    """
    rows = db.execute(select(StressDaily).order_by(StressDaily.day)).scalars().all()
    if not rows:
        return pd.DataFrame()
    records = [
        {
            "day": pd.Timestamp(r.day),
            "score": r.score,
            "rest_min": r.rest_min,
            "low_min": r.low_min,
            "medium_min": r.medium_min,
            "high_min": r.high_min,
            "active_min": r.active_min,
            "unmeasurable_min": r.unmeasurable_min,
            "hrv_z": r.hrv_z,
        }
        for r in rows
    ]
    df = pd.DataFrame.from_records(records).set_index("day")
    if since_days is not None and not df.empty:
        cutoff = df.index.max() - pd.Timedelta(days=since_days)
        df = df[df.index >= cutoff]
    return df


def run_stress(db: Session, tz: str, cfg: StressConfig, profile: ProfileConfig, since_days: int | None) -> StressResult:
    """Compute + log the stress pass (thin wrapper used by run/CLI)."""
    result = compute_stress(db, tz, cfg, profile, since_days)
    log.info("stress: days=%d buckets=%d", result.days, result.buckets)
    return result
