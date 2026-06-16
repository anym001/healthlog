"""Idempotency: replaying overlapping windows must not create duplicates."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select

from app.ingest import archive_raw, parse_payload, store
from app.models import MetricRegistry, MetricSample, RawIngest, SleepSession, Workout


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


def test_content_hash_dedup(db, sample_payload):
    body = json.dumps(sample_payload).encode()
    h = hashlib.sha256(body).digest()

    assert archive_raw(db, sample_payload, h, "127.0.0.1") is True
    db.flush()
    # Identical re-post: same hash -> not inserted again.
    assert archive_raw(db, sample_payload, h, "127.0.0.1") is False
    db.flush()
    assert _count(db, RawIngest) == 1
