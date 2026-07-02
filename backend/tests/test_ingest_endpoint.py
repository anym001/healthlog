"""Ingest endpoint: auth, happy path, duplicate, daily view."""

from __future__ import annotations

import logging

from sqlalchemy import text


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"] == "dev"  # release builds stamp the tag here


def test_health_reports_503_when_db_unreachable(client):
    # Point the request's session at a database that cannot be reached (a
    # closed local port) so the readiness probe's SELECT 1 fails.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import get_db
    from app.main import app

    broken_engine = create_engine(
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )

    def broken_get_db():
        session = Session(bind=broken_engine)
        try:
            yield session
        finally:
            session.close()

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = broken_get_db
    try:
        r = client.get("/api/health")
    finally:
        app.dependency_overrides[get_db] = previous_override
        broken_engine.dispose()
    assert r.status_code == 503
    assert r.json()["detail"] == "Database unreachable."


def test_ingest_requires_token(client, sample_payload):
    r = client.post("/api/ingest", json=sample_payload)
    assert r.status_code == 401


def test_failed_auth_is_audit_logged(client):
    # Capture directly off the audit logger (the healthlog tree doesn't
    # propagate to root, so caplog would miss it).
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    audit_logger = logging.getLogger("healthlog.audit")
    audit_logger.addHandler(handler)
    try:
        r = client.post("/api/ingest", json={}, headers={"X-Ingest-Token": "wrong-token"})
    finally:
        audit_logger.removeHandler(handler)
    assert r.status_code == 401
    messages = [rec.getMessage() for rec in records]
    assert any(m.startswith("ingest.auth_failed ip=") for m in messages)
    assert not any("wrong-token" in m for m in messages)  # never log the token


def test_oversized_content_length_rejected_early(client, monkeypatch):
    from app import config

    monkeypatch.setenv("MAX_PAYLOAD_BYTES", "64")
    config.get_settings.cache_clear()
    try:
        r = client.post(
            "/api/ingest",
            content=b"x" * 200,  # Content-Length: 200 > 64
            headers={"X-Ingest-Token": "test-secret", "Content-Type": "application/json"},
        )
    finally:
        config.get_settings.cache_clear()
    assert r.status_code == 413


def test_oversized_chunked_body_cut_off_while_streaming(client, monkeypatch):
    from app import config

    monkeypatch.setenv("MAX_PAYLOAD_BYTES", "64")
    config.get_settings.cache_clear()

    def chunks():
        for _ in range(10):  # 200 bytes total, no Content-Length header
            yield b"y" * 20

    try:
        r = client.post(
            "/api/ingest",
            content=chunks(),
            headers={"X-Ingest-Token": "test-secret", "Content-Type": "application/json"},
        )
    finally:
        config.get_settings.cache_clear()
    assert r.status_code == 413


def test_metrics_endpoint_disabled_by_default(client):
    assert client.get("/metrics").status_code == 404


def test_metrics_endpoint_when_enabled(client, monkeypatch):
    from app import config

    monkeypatch.setenv("METRICS_ENABLED", "1")
    config.get_settings.cache_clear()
    try:
        r = client.get("/metrics")
    finally:
        config.get_settings.cache_clear()
    assert r.status_code == 200
    assert "healthlog_ingest_requests_total" in r.text
    assert "healthlog_last_ingest_timestamp_seconds" in r.text


def test_ingest_happy_path(client, sample_payload):
    r = client.post("/api/ingest", json=sample_payload, headers={"X-Ingest-Token": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stored"
    assert body["metric_rows"] == 7  # 2 hr + 2 steps + 1 energy + 1 resting + 1 unknown
    assert body["sleep_rows"] == 1
    assert body["workout_rows"] == 1
    assert body["unknown_metrics"] == 1
    # First ingest: every row is new.
    assert body["metric_new"] == 7
    assert body["sleep_new"] == 1
    assert body["workout_new"] == 1


def test_ingest_second_run_shows_updated_counts(client, sample_payload):
    # Simulate a HAE re-sync with the same data (overlapping window): the
    # second store sees all rows as updates (xmax != 0).
    headers = {"X-Ingest-Token": "test-secret"}
    import copy

    first = client.post("/api/ingest", json=sample_payload, headers=headers)
    assert first.json()["status"] == "stored"
    assert first.json()["metric_new"] == 7

    # Change the content hash so the raw-archive dedup does not short-circuit;
    # the per-row upserts still hit their unique constraints.
    payload2 = copy.deepcopy(sample_payload)
    payload2["_replay"] = True  # mutate so SHA-256 differs
    second = client.post("/api/ingest", json=payload2, headers=headers)
    body = second.json()
    assert body["status"] == "stored"
    assert body["metric_rows"] == 7
    assert body["metric_new"] == 0  # all rows already existed
    assert body["sleep_new"] == 0
    assert body["workout_new"] == 0


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
