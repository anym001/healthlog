"""Apple Health symptoms and medication doses (HAE ``symptoms`` / ``medications``).

The fixtures keep the real HAE export structure of synthetic test entries from a
German-locale device (timestamps shifted, medication renamed), so names,
severities and statuses arrive localised.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import health_vocab
from app.ingest import archive_raw, ingest_bytes, parse_payload, store
from app.models import MedicationDose, Symptom
from app.rederive import run_rederive_medications, run_rederive_symptoms

FIXTURES = Path(__file__).parent / "fixtures"
CEST = dt.timezone(dt.timedelta(hours=2))
# The symptom fixture logs every Apple Health symptom type once or more (44
# entries); four appetite-change answers share one minute, so they collapse
# onto one (start_time, name, source) key, last one wins.
SYMPTOMS_PARSED = 44
SYMPTOMS_STORED = 41


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# --- vocabulary (pure) ---


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("Kopfschmerzen", "headache"),
        ("Headache", "headache"),
        ("  husten ", "coughing"),
        ("Rückenschmerzen", "lower_back_pain"),
        ("Sinuscongestion", "sinus_congestion"),  # HAE's own spelling
        ("Brustschmerzen", "breast_pain"),
        ("Druckgefühl oder Schmerzen in der Brust", "chest_tightness_or_pain"),
        ("Schnelles Klopfen oder Flattern des Herzschlags", "rapid_pounding_or_fluttering_heartbeat"),
    ],
)
def test_symptom_names_map_across_languages(raw, slug):
    assert health_vocab.canonical_symptom(raw) == slug


def test_unknown_values_map_to_none_not_an_error():
    assert health_vocab.canonical_symptom("Schluckauf") is None
    assert health_vocab.canonical_severity("Unerträglich") is None
    assert health_vocab.canonical_medication_status("Vergessen") is None
    assert health_vocab.canonical_symptom(None) is None


# --- symptoms ---


def test_symptom_fixture_parses_with_canonical_slugs():
    rows = parse_payload(_fixture("hae_symptoms_de.json")).symptom_rows
    assert len(rows) == SYMPTOMS_PARSED
    # Every German name in the export maps, covering all 39 Apple symptom types.
    assert all(r["symptom"] is not None for r in rows)
    assert {r["symptom"] for r in rows} == set(health_vocab.BUILTIN_SYMPTOMS.values())
    assert ("Kopfschmerzen", "headache") in {(r["name"], r["symptom"]) for r in rows}
    assert {(r["severity_raw"], r["severity"]) for r in rows} == {
        ("Leicht", "mild"),
        ("Mäßig", "moderate"),
        ("Schwer", "severe"),
        ("Präsent", "present"),
        ("Nicht vorhanden", "not_present"),  # an explicit "no" is kept as a row
        ("Keine Änderung", "no_change"),  # appetite changes take a direction
        ("Erhöht", "increased"),
        ("Verringert", "decreased"),
    }
    assert all(r["source"] == "Health" and r["user_entered"] is True and r["cycle_start"] is False for r in rows)
    ranged = [r for r in rows if r["end_time"] != r["start_time"]]
    assert ranged and all(r["end_time"] > r["start_time"] for r in ranged)


def test_unknown_symptom_is_kept_with_raw_values():
    entry = {"name": "Schluckauf", "severity": "Unerträglich", "start": "2026-07-01 10:00:00 +0200"}
    row = parse_payload({"data": {"symptoms": [entry]}}).symptom_rows[0]
    assert (row["name"], row["symptom"], row["severity_raw"], row["severity"]) == (
        "Schluckauf",
        None,
        "Unerträglich",
        None,
    )
    assert row["source"] == ""


def test_entries_without_name_or_start_are_counted():
    parsed = parse_payload(
        {
            "data": {
                "symptoms": [{"name": "Husten"}, {"start": "2026-07-01 10:00:00 +0200"}],
                "medications": [{"displayText": "X", "start": "garbage"}],
            }
        }
    )
    assert parsed.symptom_rows == [] and parsed.medication_rows == []
    assert sorted(t for _, t in parsed.dropped_entries) == ["medication", "symptom", "symptom"]


# --- medications ---


def test_medication_fixture_parses_with_dose_keys():
    rows = parse_payload(_fixture("hae_medications_de.json")).medication_rows
    assert len(rows) == 5
    assert {r["medication"] for r in rows} == {"Testpraeparat A"}
    assert sorted(r["status"] for r in rows) == ["not_logged", "skipped", "taken", "taken", "taken"]
    scheduled = [r for r in rows if r["scheduled_time"] is not None]
    assert len(scheduled) == 1
    assert scheduled[0]["dose_time"] == scheduled[0]["scheduled_time"]  # keyed on the schedule
    assert scheduled[0]["scheduled_dosage"] == 1.0
    assert all(r["dose_time"] == r["start_time"] for r in rows if r["scheduled_time"] is None)
    assert all(r["codings"] == [] and r["is_archived"] is False and r["units"] == "count" for r in rows)


# --- store / ingest (DB) ---


def test_fixtures_ingest_end_to_end(db):
    for name in ("hae_symptoms_de.json", "hae_medications_de.json"):
        status, _ = ingest_bytes(db, (FIXTURES / name).read_bytes())
        assert status == "stored"
    assert (_count(db, Symptom), _count(db, MedicationDose)) == (SYMPTOMS_STORED, 5)


def test_store_is_idempotent(db):
    for _ in range(2):
        store(db, parse_payload(_fixture("hae_symptoms_de.json")))
        store(db, parse_payload(_fixture("hae_medications_de.json")))
    assert (_count(db, Symptom), _count(db, MedicationDose)) == (SYMPTOMS_STORED, 5)


def test_status_change_on_a_scheduled_dose_updates_in_place(db):
    dose = {
        "displayText": "Testpraeparat A",
        "start": "2026-07-01 08:05:00 +0200",
        "scheduledDate": "2026-07-01 08:00:00 +0200",
        "status": "Übersprungen",
    }
    store(db, parse_payload({"data": {"medications": [dose]}}))
    # Logged as taken later: a new logging time, but the same scheduled dose.
    store(
        db,
        parse_payload(
            {"data": {"medications": [{**dose, "start": "2026-07-01 09:30:00 +0200", "status": "Eingenommen"}]}}
        ),
    )
    rows = db.execute(select(MedicationDose)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "taken"
    assert rows[0].dose_time == dt.datetime(2026, 7, 1, 8, 0, tzinfo=CEST)


def test_replay_after_vocabulary_extension_fills_the_slug(db, monkeypatch):
    entry = {"name": "Schluckauf", "start": "2026-07-01 10:00:00 +0200", "source": "Health"}
    store(db, parse_payload({"data": {"symptoms": [entry]}}))
    assert db.execute(select(Symptom.symptom)).scalar_one() is None

    monkeypatch.setitem(health_vocab.BUILTIN_SYMPTOMS, "schluckauf", "hiccups")
    store(db, parse_payload({"data": {"symptoms": [entry]}}))
    assert db.execute(select(Symptom.symptom)).scalar_one() == "hiccups"
    assert _count(db, Symptom) == 1


def test_rederive_backfills_from_archive(db):
    for name in ("hae_symptoms_de.json", "hae_medications_de.json"):
        body = (FIXTURES / name).read_bytes()
        archive_raw(db, json.loads(body), hashlib.sha256(body).digest(), None)
    db.flush()

    assert run_rederive_symptoms(db, dry_run=True).samples == SYMPTOMS_STORED
    assert _count(db, Symptom) == 0
    assert (run_rederive_symptoms(db).samples, run_rederive_medications(db).samples) == (SYMPTOMS_STORED, 5)
    run_rederive_symptoms(db)
    run_rederive_medications(db)
    assert (_count(db, Symptom), _count(db, MedicationDose)) == (SYMPTOMS_STORED, 5)  # re-run is a no-op
