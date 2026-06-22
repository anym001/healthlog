"""Idempotency: replaying overlapping windows must not create duplicates."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import func, select

from app.ingest import archive_raw, parse_payload, store
from app.models import MetricRegistry, MetricSample, RawIngest, SleepSession, Workout, WorkoutHrSample

_WID = "3213AD95-044D-4777-9D99-B473968262F1"


def _workout_hr_payload(avg_first: float = 104.5) -> dict:
    return {
        "data": {
            "workouts": [
                {
                    "id": _WID,
                    "name": "Outdoor Run",
                    "start": "2026-06-15 12:28:00 +0200",
                    "end": "2026-06-15 12:31:00 +0200",
                    "heartRateData": [
                        {"date": "2026-06-15 12:28:21 +0200", "Avg": avg_first},
                        {"date": "2026-06-15 12:29:21 +0200", "Avg": 111.0},
                    ],
                }
            ]
        }
    }


def _sleep_payload(*fragments) -> dict:
    """A sleep_analysis payload. Each fragment is (sleep_start, total_sleep_h,
    deep_h); all share one sleepEnd, mimicking HAE's overlapping API re-captures
    of a single night (same awakening, progressively later start)."""
    end = "2026-06-22 06:11:00 +0200"
    data = [
        {
            "date": "2026-06-22 00:00:00 +0200",
            "sleepStart": start,
            "sleepEnd": end,
            "deep": deep,
            "core": 4.0,
            "rem": 1.5,
            "awake": 0.2,
            "totalSleep": total,
            "source": "Apple Watch von Thomas",
        }
        for start, total, deep in fragments
    ]
    return {"data": {"metrics": [{"name": "sleep_analysis", "data": data}]}}


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_store_twice_no_duplicates(db, sample_payload):
    parsed = parse_payload(sample_payload)
    store(db, parsed)
    db.flush()
    first = _count(db, MetricSample)

    # Replay the same payload (idempotent upsert).
    store(db, parse_payload(sample_payload))
    db.flush()
    assert _count(db, MetricSample) == first
    assert _count(db, SleepSession) == 1
    assert _count(db, Workout) == 1


def test_upsert_updates_values(db, sample_payload):
    store(db, parse_payload(sample_payload))
    db.flush()

    # Same key (metric, time, source), changed value -> update, not insert.
    modified = json.loads(json.dumps(sample_payload))
    modified["data"]["metrics"][1]["data"][0]["qty"] = 9999  # step_count first bucket
    before = _count(db, MetricSample)
    store(db, parse_payload(modified))
    db.flush()
    assert _count(db, MetricSample) == before

    row = (
        db.execute(select(MetricSample).where(MetricSample.metric == "step_count").order_by(MetricSample.time))
        .scalars()
        .first()
    )
    assert row.qty == 9999


def test_unknown_metric_auto_registered_as_secondary_stub(db, sample_payload):
    store(db, parse_payload(sample_payload))
    db.flush()
    stub = db.get(MetricRegistry, "future_unknown_metric")
    assert stub is not None
    assert stub.tier == "secondary"
    assert stub.auto_registered is True
    assert stub.unit_canonical == "widgets"


def test_workout_hr_samples_idempotent_upsert(db):
    store(db, parse_payload(_workout_hr_payload()))
    db.flush()
    assert _count(db, WorkoutHrSample) == 2

    # Replay: same (workout_hae_id, ts) keys -> no duplicate rows.
    store(db, parse_payload(_workout_hr_payload()))
    db.flush()
    assert _count(db, WorkoutHrSample) == 2

    # Same keys, changed value -> upsert updates in place.
    store(db, parse_payload(_workout_hr_payload(avg_first=150.0)))
    db.flush()
    assert _count(db, WorkoutHrSample) == 2
    first = db.execute(select(WorkoutHrSample).order_by(WorkoutHrSample.ts)).scalars().first()
    assert first.bpm == 150.0
    assert first.workout_hae_id == uuid.UUID(_WID)


def test_sleep_overlapping_recaptures_collapse_to_most_complete(db):
    # One payload carrying the full night plus two partial re-captures (same end).
    store(
        db,
        parse_payload(
            _sleep_payload(
                ("2026-06-21 21:01:00 +0200", 8.5, 0.9),  # full night
                ("2026-06-22 00:27:00 +0200", 5.0, 0.0),  # partial
                ("2026-06-22 01:50:00 +0200", 3.0, 0.0),  # partial
            )
        ),
    )
    db.flush()
    assert _count(db, SleepSession) == 1
    row = db.execute(select(SleepSession)).scalars().one()
    assert row.total_sleep_h == 8.5  # the complete night, not 16.5 summed
    assert row.deep_h == 0.9


def test_sleep_partial_recapture_does_not_downgrade_stored_night(db):
    # The full night arrives first ...
    store(db, parse_payload(_sleep_payload(("2026-06-21 21:01:00 +0200", 8.5, 0.9))))
    db.flush()
    # ... a later partial push of the same awakening must not overwrite it.
    store(db, parse_payload(_sleep_payload(("2026-06-22 01:50:00 +0200", 3.0, 0.0))))
    db.flush()
    assert _count(db, SleepSession) == 1
    row = db.execute(select(SleepSession)).scalars().one()
    assert row.total_sleep_h == 8.5
    assert row.deep_h == 0.9


def test_sleep_fuller_recapture_upgrades_stored_night(db):
    # A partial arrives first, then the complete capture of the same awakening
    # (later push, earlier start) -> the stored row is upgraded in place.
    store(db, parse_payload(_sleep_payload(("2026-06-22 01:50:00 +0200", 3.0, 0.0))))
    db.flush()
    store(db, parse_payload(_sleep_payload(("2026-06-21 21:01:00 +0200", 8.5, 0.9))))
    db.flush()
    assert _count(db, SleepSession) == 1
    row = db.execute(select(SleepSession)).scalars().one()
    assert row.total_sleep_h == 8.5
    assert row.deep_h == 0.9


def test_sleep_distinct_periods_kept_separate(db):
    # A nap ends at a different time -> a distinct period, not a re-capture.
    store(db, parse_payload(_sleep_payload(("2026-06-21 21:01:00 +0200", 8.5, 0.9))))
    db.flush()
    nap = {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "data": [
                        {
                            "date": "2026-06-22 00:00:00 +0200",
                            "sleepStart": "2026-06-22 14:00:00 +0200",
                            "sleepEnd": "2026-06-22 14:40:00 +0200",
                            "totalSleep": 0.6,
                            "core": 0.6,
                            "source": "Apple Watch von Thomas",
                        }
                    ],
                }
            ]
        }
    }
    store(db, parse_payload(nap))
    db.flush()
    assert _count(db, SleepSession) == 2


def test_content_hash_dedup(db, sample_payload):
    body = json.dumps(sample_payload).encode()
    h = hashlib.sha256(body).digest()

    assert archive_raw(db, sample_payload, h, "127.0.0.1") is True
    db.flush()
    # Identical re-post: same hash -> not inserted again.
    assert archive_raw(db, sample_payload, h, "127.0.0.1") is False
    db.flush()
    assert _count(db, RawIngest) == 1
