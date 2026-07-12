"""The workout_trimp / daily_trimp SQL functions (migration 0019).

Grafana's Training dashboard reads all its TRIMP numbers through these
functions; verify them against an independent Python implementation of the
Banister formula on synthetic workouts.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid

import pytest
from sqlalchemy import text

from app.models import MetricSample, Workout, WorkoutHrSample

TZ = "Europe/Vienna"
HR_MAX = 190.0
HR_REST_DEFAULT = 60.0  # the functions' fallback when no resting HR is stored


def banister(minutes: float, hr: float, hr_rest: float = HR_REST_DEFAULT, hr_max: float = HR_MAX) -> float:
    frac = min(1.0, max(0.0, (hr - hr_rest) / (hr_max - hr_rest)))
    return minutes * frac * 0.64 * math.exp(1.92 * frac)


def approx(value: float):
    return pytest.approx(value, rel=1e-9)


def _workout(db, start: dt.datetime, **kw) -> uuid.UUID:
    w = Workout(hae_id=uuid.uuid4(), start_time=start, **kw)
    db.add(w)
    db.flush()
    return w.hae_id


def _samples(db, hae_id: uuid.UUID, start: dt.datetime, bpms: list[float], step_s: int = 60) -> None:
    for i, bpm in enumerate(bpms):
        db.add(WorkoutHrSample(workout_hae_id=hae_id, ts=start + dt.timedelta(seconds=i * step_s), bpm=bpm))
    db.flush()


def _trimp_by_id(db, hr_max: float | None = HR_MAX, since: dt.date | None = None) -> dict[uuid.UUID, float]:
    rows = db.execute(
        text("SELECT hae_id, day, trimp FROM workout_trimp(:tz, (:hr_max)::numeric, :since)"),
        {"tz": TZ, "hr_max": hr_max, "since": since},
    ).all()
    return {r.hae_id: r.trimp for r in rows}


def test_avg_hr_fallback_matches_banister(db):
    start = dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.UTC)
    wid = _workout(db, start, duration_s=1800, avg_hr=140.0)

    trimp = _trimp_by_id(db)[wid]
    assert trimp == approx(banister(30.0, 140.0))


def test_sample_resolved_rescales_to_session_duration(db):
    start = dt.datetime(2026, 7, 2, 10, 0, tzinfo=dt.UTC)
    # Samples cover 2 minutes (the last sample has no successor and is dropped);
    # the session lasts 4 minutes, so the sample TRIMP is rescaled by 2x.
    wid = _workout(db, start, duration_s=240, avg_hr=150.0)
    _samples(db, wid, start, bpms=[120.0, 180.0, 150.0])

    expected = (banister(1.0, 120.0) + banister(1.0, 180.0)) * (240 / 120)
    assert _trimp_by_id(db)[wid] == approx(expected)

    # Interval work costs more than the steady average-HR fallback would say.
    assert _trimp_by_id(db)[wid] > banister(4.0, 150.0)


def test_workout_without_hr_scores_zero(db):
    start = dt.datetime(2026, 7, 3, 10, 0, tzinfo=dt.UTC)
    wid = _workout(db, start, duration_s=1800, avg_hr=None)
    assert _trimp_by_id(db)[wid] == 0.0


def test_hr_max_auto_uses_clamped_max_recorded(db):
    start = dt.datetime(2026, 7, 4, 10, 0, tzinfo=dt.UTC)
    wid = _workout(db, start, duration_s=1800, avg_hr=140.0, max_hr=172.0)

    # p_hr_max NULL -> auto: max recorded workout HR (172, within the 160-210 clamp)
    trimp = _trimp_by_id(db, hr_max=None)[wid]
    assert trimp == approx(banister(30.0, 140.0, hr_max=172.0))


def test_resting_hr_baseline_uses_rolling_median(db):
    day = dt.date(2026, 7, 5)
    for offset, vmin in [(3, 50.0), (2, 52.0), (1, 54.0)]:
        db.add(
            MetricSample(
                time=dt.datetime.combine(day - dt.timedelta(days=offset), dt.time(4, 0), tzinfo=dt.UTC),
                metric="resting_heart_rate",
                source="test",
                unit="count/min",
                vmin=vmin,
                vavg=vmin,
                vmax=vmin,
            )
        )
    db.flush()

    start = dt.datetime.combine(day, dt.time(10, 0), tzinfo=dt.UTC)
    wid = _workout(db, start, duration_s=1800, avg_hr=140.0)

    trimp = _trimp_by_id(db)[wid]
    assert trimp == approx(banister(30.0, 140.0, hr_rest=52.0))


def test_since_prunes_older_workouts(db):
    old = _workout(db, dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC), duration_s=1800, avg_hr=140.0)
    new = _workout(db, dt.datetime(2026, 7, 6, 10, 0, tzinfo=dt.UTC), duration_s=1800, avg_hr=140.0)

    pruned = _trimp_by_id(db, since=dt.date(2026, 7, 1))
    assert new in pruned and old not in pruned


def test_day_buckets_in_requested_timezone(db):
    # 23:30 UTC is already the next day in Europe/Vienna (UTC+2 in July).
    start = dt.datetime(2026, 7, 7, 23, 30, tzinfo=dt.UTC)
    wid = _workout(db, start, duration_s=1800, avg_hr=140.0)

    row = db.execute(
        text("SELECT day FROM workout_trimp(:tz, (:hr_max)::numeric) WHERE hae_id = :wid"),
        {"tz": TZ, "hr_max": HR_MAX, "wid": wid},
    ).one()
    assert row.day == dt.date(2026, 7, 8)


def test_daily_trimp_sums_per_day(db):
    start = dt.datetime(2026, 7, 9, 8, 0, tzinfo=dt.UTC)
    _workout(db, start, duration_s=1800, avg_hr=140.0)
    _workout(db, start + dt.timedelta(hours=8), duration_s=3600, avg_hr=120.0)

    row = db.execute(
        text("SELECT trimp FROM daily_trimp(:tz, (:hr_max)::numeric) WHERE day = :day"),
        {"tz": TZ, "hr_max": HR_MAX, "day": dt.date(2026, 7, 9)},
    ).one()
    assert row.trimp == approx(banister(30.0, 140.0) + banister(60.0, 120.0))
