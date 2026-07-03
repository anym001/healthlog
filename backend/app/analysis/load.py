"""DB loaders for the analysis pipeline.

Each loader turns one query into the pandas object the pure helpers and finding
builders expect (a daily series or a per-session/per-day frame), on a complete
daily index so lag shifts stay calendar-correct.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from .pure import _daily_grid


def load_daily_series(db: Session, metric: str, agg: str, tz: str) -> pd.Series:
    """Daily value per local day for ``metric`` using its registry aggregate.

    ``COALESCE(vavg, qty)`` etc. handles both HAE shapes (only heart_rate fills
    Min/Avg/Max; everything else fills qty). Returned on a complete daily index
    with NaN for missing days (so lag shifts stay calendar-correct).
    """
    sql = text(
        """
        SELECT (time AT TIME ZONE :tz)::date AS day,
               CASE :agg
                 WHEN 'sum' THEN sum(qty)
                 WHEN 'avg' THEN avg(coalesce(vavg, qty))
                 WHEN 'min' THEN min(coalesce(vmin, qty))
                 WHEN 'max' THEN max(coalesce(vmax, qty))
               END AS value
        FROM metric_samples
        WHERE metric = :metric
        GROUP BY 1
        ORDER BY 1
        """
    )
    rows = db.execute(sql, {"tz": tz, "agg": agg, "metric": metric}).all()
    return _series_from_rows(rows)


def load_sleep_frame(db: Session, tz: str) -> pd.DataFrame:
    """Per wake-day sleep aggregates: durations, efficiency and bedtime offset."""
    rows = db.execute(
        text(
            """
            SELECT sleep_date, sleep_start, in_bed_start, in_bed_end,
                   total_sleep_h, deep_h, rem_h, in_bed_h
            FROM sleep_nightly
            WHERE sleep_date IS NOT NULL
            ORDER BY sleep_date
            """
        )
    ).all()
    if not rows:
        return pd.DataFrame()

    zone = ZoneInfo(tz)
    records = []
    for r in rows:
        in_bed_h = r.in_bed_h
        if r.in_bed_start is not None and r.in_bed_end is not None:
            in_bed_h = (r.in_bed_end - r.in_bed_start).total_seconds() / 3600.0
        bedtime = np.nan
        if r.sleep_start is not None:
            local = r.sleep_start.astimezone(zone)
            bedtime = local.hour + local.minute / 60.0
        records.append(
            {
                "day": pd.Timestamp(r.sleep_date),
                "total_sleep_h": r.total_sleep_h,
                "deep_h": r.deep_h,
                "rem_h": r.rem_h,
                "in_bed_h": in_bed_h,
                "bedtime": bedtime,
            }
        )
    df = pd.DataFrame.from_records(records).set_index("day")
    # sleep_nightly already yields one consolidated session per wake-day; the
    # groupby is a defensive no-op. Use max (not sum) so any stray duplicate
    # picks the most complete night instead of double-counting overlapping
    # API re-captures (see migration 0010).
    agg = df.groupby(level=0).agg(
        total_sleep_h=("total_sleep_h", "max"),
        deep_h=("deep_h", "max"),
        rem_h=("rem_h", "max"),
        in_bed_h=("in_bed_h", "max"),
        bedtime=("bedtime", "min"),
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        agg["efficiency"] = np.where(agg["in_bed_h"] > 0, agg["total_sleep_h"] / agg["in_bed_h"], np.nan)
    return agg


def load_workout_frame(db: Session, tz: str) -> pd.DataFrame:
    """One row per workout session, tagged with its local calendar day.

    Returns the raw per-session fields the workout aggregation needs; TRIMP and
    HR_max/HR_rest are computed downstream (pure helpers) because they depend on
    the profile and the measured resting-HR series, not just the row. Empty
    frame when there are no workouts.
    """
    rows = db.execute(
        text(
            """
            SELECT hae_id,
                   (start_time AT TIME ZONE :tz)::date AS day,
                   name, duration_s, active_energy_kcal, avg_hr, max_hr, intensity
            FROM workouts
            WHERE start_time IS NOT NULL
            ORDER BY start_time
            """
        ),
        {"tz": tz},
    ).all()
    if not rows:
        return pd.DataFrame()
    records = [
        {
            "hae_id": str(r.hae_id),
            "day": pd.Timestamp(r.day),
            "name": r.name,
            "duration_s": r.duration_s,
            "active_energy_kcal": r.active_energy_kcal,
            "avg_hr": r.avg_hr,
            "max_hr": r.max_hr,
            "intensity": r.intensity,
        }
        for r in rows
    ]
    return pd.DataFrame.from_records(records)


def load_workout_hr_samples(db: Session) -> dict[str, pd.DataFrame]:
    """Intra-workout HR samples grouped per workout (keyed by ``hae_id`` string).

    Each value is a frame with ``ts`` (sample time) and ``bpm`` columns, sorted
    by time. Empty dict when no workout carries an HR series. The samples feed
    zone-based (Edwards) TRIMP; zone boundaries are derived per run from HR_max,
    never stored.
    """
    rows = db.execute(text("SELECT workout_hae_id, ts, bpm FROM workout_hr_samples ORDER BY workout_hae_id, ts")).all()
    if not rows:
        return {}
    by_id: dict[str, list[tuple]] = {}
    for r in rows:
        by_id.setdefault(str(r.workout_hae_id), []).append((r.ts, float(r.bpm)))
    out: dict[str, pd.DataFrame] = {}
    for hid, pairs in by_id.items():
        frame = pd.DataFrame(pairs, columns=["ts", "bpm"])
        frame["ts"] = pd.to_datetime(frame["ts"])
        out[hid] = frame
    return out


def _series_from_rows(rows) -> pd.Series:
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r.day for r in rows])
    vals = [float(r.value) if r.value is not None else np.nan for r in rows]
    s = pd.Series(vals, index=idx, dtype="float64")
    return s.reindex(_daily_grid(s))


def _reindex_full(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty:
        return s
    return s.reindex(_daily_grid(s))
