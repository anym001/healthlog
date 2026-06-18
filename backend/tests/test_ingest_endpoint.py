"""Ingest endpoint: auth, happy path, duplicate, daily view."""

from __future__ import annotations

from sqlalchemy import text


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ingest_requires_token(client, sample_payload):
    r = client.post("/api/ingest", json=sample_payload)
    assert r.status_code == 401


def test_ingest_happy_path(client, sample_payload):
    r = client.post("/api/ingest", json=sample_payload, headers={"X-Ingest-Token": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stored"
    assert body["metric_rows"] == 7  # 2 hr + 2 steps + 1 energy + 1 resting + 1 unknown
    assert body["sleep_rows"] == 1
    assert body["workout_rows"] == 1
    assert body["unknown_metrics"] == 1


def test_ingest_duplicate_is_deduped(client, sample_payload):
    headers = {"X-Ingest-Token": "test-secret"}
    first = client.post("/api/ingest", json=sample_payload, headers=headers)
    assert first.json()["status"] == "stored"
    second = client.post("/api/ingest", json=sample_payload, headers=headers)
    assert second.json()["status"] == "duplicate"


def test_daily_view_buckets_in_local_tz(client, sample_payload, db_conn):
    client.post("/api/ingest", json=sample_payload, headers={"X-Ingest-Token": "test-secret"})
    rows = db_conn.execute(text("SELECT day, metric, sum FROM daily_metrics WHERE metric = 'step_count'")).all()
    # Both step buckets (08:00, 09:00 +0100) fall on the same local day.
    assert len(rows) == 1
    assert str(rows[0].day) == "2026-01-02"
    assert rows[0].sum == 2000


def test_daily_view_avg_coalesces_onto_qty(client, sample_payload, db_conn):
    # resting_heart_rate fills qty (no Min/Avg/Max) — after migration 0005 the
    # view's ``avg`` column must return the qty value via COALESCE, not NULL.
    client.post("/api/ingest", json=sample_payload, headers={"X-Ingest-Token": "test-secret"})
    rows = db_conn.execute(text("SELECT avg FROM daily_metrics WHERE metric = 'resting_heart_rate'")).all()
    assert len(rows) == 1
    assert rows[0].avg is not None  # pre-0005 this was NULL
    assert abs(rows[0].avg - 62.0) < 0.01  # resting_heart_rate qty=62 in fixture
