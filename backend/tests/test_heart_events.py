"""Apple Watch heart rate notifications and ECG summaries (HAE
``heartRateNotifications`` / ``ecg``).

The fixture keeps the real HAE export structure of synthetic entries (times,
values, device name and identifier replaced, voltage series truncated).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select

from app.ingest import archive_raw, ingest_bytes, parse_payload, store
from app.models import EcgRecording, HeartRateNotification
from app.rederive import run_rederive_ecg, run_rederive_heart_notifications

FIXTURE = Path(__file__).parent / "fixtures" / "hae_heart_events.json"
EDT = dt.timezone(dt.timedelta(hours=-4))


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# --- parsing (pure) ---


def test_notification_is_summarised():
    first = parse_payload(_payload()).heart_notification_rows[0]
    assert first["start_time"] == dt.datetime(2026, 7, 8, 5, 10, tzinfo=EDT)
    assert first["kind"] == "low_heart_rate_notification"
    assert first["threshold"] == 40
    # source arrives as {name, identifier} in this section only.
    assert first["source"] == "Apple Watch"
    assert (first["hr_min"], first["hr_avg"], first["hr_max"], first["hr_samples"]) == (38, 39, 40, 3)


def test_ecg_is_a_summary_without_the_waveform():
    rows = parse_payload(_payload()).ecg_rows
    assert [r["classification"] for r in rows] == ["Sinus Rhythm", "Inconclusive Poor Recording"]
    assert rows[0]["average_heart_rate"] == 70
    # The declared sample count wins over the (possibly truncated) array length.
    assert rows[0]["voltage_samples"] == 15360
    assert all("voltage" not in key for row in rows for key in row if key != "voltage_samples")


def test_inconclusive_ecg_average_of_zero_is_null():
    assert parse_payload(_payload()).ecg_rows[1]["average_heart_rate"] is None


def test_entries_without_start_or_kind_are_counted_as_dropped():
    payload = {"data": {"heartRateNotifications": [{"start": "2026-07-08 05:10:00 -0400"}], "ecg": [{"end": "x"}]}}
    parsed = parse_payload(payload)
    assert parsed.heart_notification_rows == [] and parsed.ecg_rows == []
    assert [kind for _, kind in parsed.dropped_entries] == ["heart_rate_notification", "ecg"]


def test_notification_without_samples_keeps_the_event():
    payload = _payload()
    entry = payload["data"]["heartRateNotifications"][0]
    entry.pop("heartRateData")
    row = parse_payload({"data": {"heartRateNotifications": [entry]}}).heart_notification_rows[0]
    assert row["hr_samples"] is None and row["hr_avg"] is None


# --- storage ---


def test_store_is_idempotent(db):
    parsed = parse_payload(_payload())
    first = store(db, parsed)
    assert (first.heart_notification_new, first.ecg_new) == (2, 2)
    again = store(db, parse_payload(_payload()))
    assert (again.heart_notification_new, again.ecg_new) == (0, 0)
    assert _count(db, HeartRateNotification) == 2
    assert _count(db, EcgRecording) == 2


def test_ingest_endpoint_reports_the_counts(client):
    response = client.post("/api/ingest", json=_payload(), headers={"X-Ingest-Token": "test-secret"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["heart_notification_rows"], body["ecg_rows"]) == (2, 2)
    assert (body["heart_notification_new"], body["ecg_new"]) == (2, 2)


def test_rederive_fills_the_tables_from_the_archive(db):
    body = FIXTURE.read_bytes()
    archive_raw(db, json.loads(body), hashlib.sha256(body).digest(), None)
    assert _count(db, HeartRateNotification) == 0
    assert run_rederive_heart_notifications(db).samples == 2
    assert run_rederive_ecg(db).samples == 2
    assert _count(db, HeartRateNotification) == 2
    assert _count(db, EcgRecording) == 2
    # Re-running is a no-op.
    run_rederive_heart_notifications(db)
    assert _count(db, HeartRateNotification) == 2


def test_ingest_bytes_stores_heart_events(db):
    status, _ = ingest_bytes(db, FIXTURE.read_bytes())
    assert status == "stored"
    assert _count(db, EcgRecording) == 2
