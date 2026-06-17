"""Re-derive: backfill workout_hr_samples from the raw archive (DB end-to-end)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import func, select

from app.ingest import archive_raw
from app.models import Workout, WorkoutHrSample
from app.rederive import run_rederive

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
