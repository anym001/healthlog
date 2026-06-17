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


def test_content_hash_dedup(db, sample_payload):
    body = json.dumps(sample_payload).encode()
    h = hashlib.sha256(body).digest()

    assert archive_raw(db, sample_payload, h, "127.0.0.1") is True
    db.flush()
    # Identical re-post: same hash -> not inserted again.
    assert archive_raw(db, sample_payload, h, "127.0.0.1") is False
    db.flush()
    assert _count(db, RawIngest) == 1
