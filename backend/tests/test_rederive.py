"""Re-derive: backfill workout_hr_samples from the raw archive (DB end-to-end)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import func, select

from app.ingest import archive_raw
from app.models import Workout, WorkoutHrSample
from app.rederive import run_rederive, run_rederive_workout_types

_WID = "3213AD95-044D-4777-9D99-B473968262F1"


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _payload() -> dict:
    return {
        "data": {
            "workouts": [
                {
                    "id": _WID,
                    "name": "Outdoor Run",
                    "start": "2026-06-15 12:28:00 +0200",
                    "end": "2026-06-15 12:31:00 +0200",
                    "heartRateData": [
                        {"date": "2026-06-15 12:28:21 +0200", "Avg": 104.5},
                        {"date": "2026-06-15 12:29:21 +0200", "Avg": 111.0},
                    ],
                }
            ]
        }
    }


def _archive_pre_feature_history(db, payload: dict) -> None:
    """Simulate a workout ingested before HR-sample extraction: the raw payload
    is archived and the workout summary exists, but no samples were stored."""
    body = json.dumps(payload).encode()
    archive_raw(db, payload, hashlib.sha256(body).digest(), None)
    db.add(
        Workout(
            hae_id=uuid.UUID(_WID),
            name="Outdoor Run",
            start_time=dt.datetime(2026, 6, 15, 10, 28, tzinfo=dt.UTC),
            duration_s=180.0,
            source="",
        )
    )
    db.flush()


def test_rederive_backfills_hr_samples_from_archive(db):
    _archive_pre_feature_history(db, _payload())
    assert _count(db, WorkoutHrSample) == 0

    summary = run_rederive(db)
    assert (summary.payloads, summary.samples, summary.failures) == (1, 2, 0)
    assert _count(db, WorkoutHrSample) == 2

    # Re-running is a no-op (idempotent upsert), not a duplication.
    again = run_rederive(db)
    assert again.samples == 2
    assert _count(db, WorkoutHrSample) == 2


def test_rederive_dry_run_writes_nothing(db):
    _archive_pre_feature_history(db, _payload())

    summary = run_rederive(db, dry_run=True)
    assert summary.payloads == 1 and summary.samples == 2 and summary.failures == 0
    assert _count(db, WorkoutHrSample) == 0  # dry run never writes


def _workout(db, name: str, canonical_type: str | None) -> uuid.UUID:
    hae_id = uuid.uuid4()
    db.add(
        Workout(
            hae_id=hae_id,
            name=name,
            canonical_type=canonical_type,
            start_time=dt.datetime(2026, 6, 15, 10, 28, tzinfo=dt.UTC),
            duration_s=1800.0,
            source="",
        )
    )
    db.flush()
    return hae_id


def test_rederive_workout_types_recomputes_stored_column(db):
    # Ingested before the map knew "Outdoor Cycling"; one custom name the
    # config maps; one stale value nothing maps any more; one already correct.
    cycling = [_workout(db, "Outdoor Cycling", None) for _ in range(2)]
    custom = _workout(db, "Kanu", None)
    stale = _workout(db, "Something Else", "old_type")
    correct = _workout(db, "Outdoor Run", "running")

    summary = run_rederive_workout_types(db, type_map={"Kanu": "paddling"})

    assert (summary.names, summary.changed_names, summary.workouts) == (4, 3, 4)
    types = dict(db.execute(select(Workout.hae_id, Workout.canonical_type)).all())
    assert [types[w] for w in cycling] == ["cycling", "cycling"]
    assert types[custom] == "paddling"
    assert types[stale] is None
    assert types[correct] == "running"

    again = run_rederive_workout_types(db, type_map={"Kanu": "paddling"})
    assert (again.changed_names, again.workouts) == (0, 0)


def test_rederive_workout_types_dry_run_writes_nothing(db):
    wid = _workout(db, "Indoor Cycling", None)

    summary = run_rederive_workout_types(db, dry_run=True)

    assert (summary.changed_names, summary.workouts) == (1, 1)
    stored = db.execute(select(Workout.canonical_type).where(Workout.hae_id == wid)).scalar_one()
    assert stored is None
