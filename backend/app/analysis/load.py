"""DB loaders for the analysis pipeline.

Each loader turns one query into the pandas object the pure helpers and finding
builders expect (a daily series or a per-session/per-day frame), on a complete
daily index so lag shifts stay calendar-correct.
"""

from __future__ import annotations

import datetime as dt
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
                   name, duration_s, active_energy_kcal, distance_km, avg_hr, max_hr, intensity
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
            "distance_km": r.distance_km,
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


def load_intraday_hr(db: Session, start: dt.datetime, end: dt.datetime) -> pd.Series:
    """Per-bucket representative heart rate in ``[start, end)`` (bpm).

    The all-day ``heart_rate`` buckets at their native (sub-daily) resolution —
    ``COALESCE(vavg, qty)`` handles both HAE shapes. Buckets from multiple
    sources at the same instant are averaged, so the index is unique (the
    downstream ``stress_intraday`` keys on ``ts`` alone). Index is the tz-aware
    bucket time; feeds the intraday stress model. Uses the ``(metric, time)`` index.
    """
    rows = db.execute(
        text(
            """
            SELECT time, avg(coalesce(vavg, qty)) AS bpm
            FROM metric_samples
            WHERE metric = 'heart_rate' AND time >= :start AND time < :end
              AND coalesce(vavg, qty) IS NOT NULL
            GROUP BY time
            ORDER BY time
            """
        ),
        {"start": start, "end": end},
    ).all()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([r.time for r in rows])
    return pd.Series([float(r.bpm) for r in rows], index=idx, dtype="float64")


def load_intraday_steps(db: Session, start: dt.datetime, end: dt.datetime) -> pd.Series:
    """Per-bucket step count in ``[start, end)``.

    The all-day ``step_count`` buckets at their native resolution; buckets from
    multiple sources at the same instant take the maximum (summing would double-
    count the same walk seen by watch *and* phone). Index is the tz-aware bucket
    time. Feeds the stress model's step-based activity gating — the caller must
    check the cadence is fine enough (per-minute) before using it, since an
    hourly step total would spuriously gate single buckets.
    """
    rows = db.execute(
        text(
            """
            SELECT time, max(qty) AS steps
            FROM metric_samples
            WHERE metric = 'step_count' AND time >= :start AND time < :end
              AND qty IS NOT NULL
            GROUP BY time
            ORDER BY time
            """
        ),
        {"start": start, "end": end},
    ).all()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([r.time for r in rows])
    return pd.Series([float(r.steps) for r in rows], index=idx, dtype="float64")


def load_workout_intervals(
    db: Session, start: dt.datetime, end: dt.datetime
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """``(start, end)`` intervals of workouts overlapping ``[start, end)``.

    ``end_time`` falls back to ``start_time + duration_s`` when absent. Used to
    exclude workout minutes from the stress timeline (Garmin's grey "active"
    band). Returns tz-aware Timestamp pairs, sorted by start.
    """
    rows = db.execute(
        text(
            """
            SELECT start_time,
                   coalesce(end_time, start_time + make_interval(secs => duration_s)) AS end_time
            FROM workouts
            WHERE start_time IS NOT NULL
              AND coalesce(end_time, start_time + make_interval(secs => duration_s), start_time) >= :start
              AND start_time < :end
            ORDER BY start_time
            """
        ),
        {"start": start, "end": end},
    ).all()
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for r in rows:
        if r.end_time is None:
            continue
        intervals.append((pd.Timestamp(r.start_time), pd.Timestamp(r.end_time)))
    return intervals


def load_stress_intraday(db: Session, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """The stored stress timeline in ``[start, end)`` as a frame.

    Reads the freshly computed ``stress_intraday`` rows (``ts``, ``stress``,
    ``state``) that the Body-Battery integrator drives off — so the body-battery
    pass must run *after* the stress pass has flushed. Index is the tz-aware
    bucket time; empty frame when the window has no rows.
    """
    rows = db.execute(
        text(
            """
            SELECT ts, stress, state
            FROM stress_intraday
            WHERE ts >= :start AND ts < :end
            ORDER BY ts
            """
        ),
        {"start": start, "end": end},
    ).all()
    idx = pd.DatetimeIndex([r.ts for r in rows], name="ts")
    return pd.DataFrame(
        {
            "stress": pd.array([r.stress for r in rows], dtype="object"),
            "state": [r.state for r in rows],
        },
        index=idx,
    )


def load_sleep_intervals(
    db: Session, start: dt.datetime, end: dt.datetime
) -> list[tuple[pd.Timestamp, pd.Timestamp, float]]:
    """``(sleep_start, sleep_end, efficiency)`` intervals overlapping ``[start, end)``.

    The asleep windows that charge the Body-Battery timeline. ``efficiency`` (total
    sleep / time in bed, clamped to ``(0, 1]``, defaulting to ``1.0`` when unknown)
    scales the sleep charge rate; ``sleep_end`` falls back to ``in_bed_end`` and
    the in-bed duration to the timestamp window (mirroring
    :func:`load_sleep_frame`). Returns tz-aware Timestamp triples, sorted by start.
    """
    rows = db.execute(
        text(
            """
            SELECT sleep_start,
                   coalesce(sleep_end, in_bed_end) AS sleep_end,
                   total_sleep_h, in_bed_h, in_bed_start, in_bed_end
            FROM sleep_sessions
            WHERE sleep_start IS NOT NULL
              AND coalesce(sleep_end, in_bed_end, sleep_start) >= :start
              AND sleep_start < :end
            ORDER BY sleep_start
            """
        ),
        {"start": start, "end": end},
    ).all()
    intervals: list[tuple[pd.Timestamp, pd.Timestamp, float]] = []
    for r in rows:
        if r.sleep_end is None:
            continue
        in_bed_h = r.in_bed_h
        if (in_bed_h is None or in_bed_h <= 0) and r.in_bed_start is not None and r.in_bed_end is not None:
            in_bed_h = (r.in_bed_end - r.in_bed_start).total_seconds() / 3600.0
        if in_bed_h and in_bed_h > 0 and r.total_sleep_h is not None:
            eff = float(np.clip(r.total_sleep_h / in_bed_h, 0.0, 1.0))
        else:
            eff = 1.0
        if eff <= 0:
            eff = 1.0
        intervals.append((pd.Timestamp(r.sleep_start), pd.Timestamp(r.sleep_end), eff))
    return intervals


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
