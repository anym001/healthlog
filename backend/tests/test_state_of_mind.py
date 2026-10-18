"""State of Mind (HAE ``stateOfMind``): parser, idempotent store, re-derive."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import func, select

from app.ingest import archive_raw, parse_payload, store
from app.models import StateOfMind
from app.rederive import run_rederive_state_of_mind

_ID_A = "0B6F3C1E-7A52-4C1D-9E0B-2F8A5D4C3B21"
_ID_B = "5D2E8F90-1B3C-4A6D-8E7F-9A0B1C2D3E4F"


def _entry(hae_id: str = _ID_A, **overrides) -> dict:
    entry = {
        "id": hae_id,
        "kind": "momentary_emotion",
        "start": "2026-08-03T18:20:00Z",
        "end": "2026-08-03T18:20:00Z",
        "valence": -0.4,
        "valenceClassification": "slightly_unpleasant",
        "labels": ["stressed", "worried"],
        "associations": ["work"],
    }
    entry.update(overrides)
    return entry


def _payload(*entries: dict) -> dict:
    return {"data": {"stateOfMind": list(entries)}}


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(StateOfMind)).scalar_one()


# --- parser (pure) ---


def test_entry_parsed_into_row():
    parsed = parse_payload(_payload(_entry()))
    assert len(parsed.state_of_mind_rows) == 1
    row = parsed.state_of_mind_rows[0]
    assert row["hae_id"] == uuid.UUID(_ID_A)
    assert row["start_time"] == dt.datetime(2026, 8, 3, 18, 20, tzinfo=dt.UTC)
    assert row["kind"] == "momentary_emotion"
    assert row["valence"] == -0.4
    assert row["valence_classification"] == "slightly_unpleasant"
    assert row["labels"] == ["stressed", "worried"]
    assert row["associations"] == ["work"]


def test_integer_valence_is_accepted():
    # HAE serialises the scale's endpoints as bare ints (-1, 0, 1).
    row = parse_payload(_payload(_entry(valence=1, kind="daily_mood"))).state_of_mind_rows[0]
    assert row["valence"] == 1.0 and row["kind"] == "daily_mood"


def test_entry_without_usable_id_or_start_is_dropped_and_counted():
    parsed = parse_payload(_payload(_entry(hae_id=""), _entry(hae_id="not-a-uuid"), _entry(_ID_B, start=None)))
    assert parsed.state_of_mind_rows == []
    assert len(parsed.dropped_state_of_mind) == 3


def test_out_of_range_valence_is_nulled_and_flagged_but_entry_kept():
    parsed = parse_payload(_payload(_entry(valence=4.2)))
    assert parsed.state_of_mind_rows[0]["valence"] is None
    assert parsed.state_of_mind_rows[0]["labels"] == ["stressed", "worried"]
    assert parsed.implausible == [("state_of_mind_valence", 4.2)]


def test_malformed_vocabularies_are_cleaned():
    row = parse_payload(_payload(_entry(labels=["calm", 3, None, ""], associations="work"))).state_of_mind_rows[0]
    assert row["labels"] == ["calm"]
    assert row["associations"] == []


def test_payload_without_state_of_mind_yields_nothing(sample_payload):
    assert parse_payload(sample_payload).state_of_mind_rows == []


# --- store (DB) ---


def test_store_is_idempotent_and_updates_in_place(db):
    first = store(db, parse_payload(_payload(_entry(), _entry(_ID_B, kind="daily_mood", valence=0.6))))
    assert (first.state_of_mind_rows, first.state_of_mind_new) == (2, 2)

    # An edited entry (same id) replayed in an overlapping window updates, never duplicates.
    again = store(db, parse_payload(_payload(_entry(valence=-0.8, labels=["anxious"]))))
    assert again.state_of_mind_new == 0
    assert _count(db) == 2
    row = db.get(StateOfMind, uuid.UUID(_ID_A))
    assert row.valence == -0.8 and row.labels == ["anxious"]


def test_duplicate_ids_within_one_payload_do_not_fail(db):
    result = store(db, parse_payload(_payload(_entry(), _entry(valence=0.2))))
    assert result.state_of_mind_new == 1
    assert db.get(StateOfMind, uuid.UUID(_ID_A)).valence == 0.2  # last wins


# --- re-derive from the raw archive (DB) ---


def _archive(db, payload: dict) -> None:
    body = json.dumps(payload).encode()
    archive_raw(db, payload, hashlib.sha256(body).digest(), None)
    db.flush()


def test_rederive_backfills_from_archive(db):
    _archive(db, _payload(_entry(), _entry(_ID_B)))
    assert _count(db) == 0

    summary = run_rederive_state_of_mind(db)
    assert (summary.payloads, summary.samples, summary.failures) == (1, 2, 0)
    assert _count(db) == 2

    assert run_rederive_state_of_mind(db).samples == 2
    assert _count(db) == 2  # re-run is a no-op


def test_rederive_dry_run_writes_nothing(db):
    _archive(db, _payload(_entry()))
    summary = run_rederive_state_of_mind(db, dry_run=True)
    assert summary.samples == 1
    assert _count(db) == 0
