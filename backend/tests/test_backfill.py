"""Bulk-backfill CLI: file discovery, idempotent re-runs, dry-run, bad files."""

from __future__ import annotations

import hashlib
import json

import pytest
from psycopg.errors import ProgramLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import backfill, cli
from app import ingest as ingest_svc
from app.backfill import collect_files, run_backfill, split_payload
from app.models import MetricSample, RawIngest, SleepSession, Workout


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload))


def test_backfill_imports_sample(db, sample_payload, tmp_path):
    f = tmp_path / "export.json"
    _write(f, sample_payload)

    summary = run_backfill(db, collect_files([f]))

    assert summary.files == 1
    assert summary.stored == 1
    assert summary.duplicates == 0
    assert summary.failures == 0
    assert summary.metric_rows == _count(db, MetricSample)
    assert _count(db, SleepSession) == 1
    assert _count(db, Workout) == 1


def test_backfill_rerun_is_idempotent(db, sample_payload, tmp_path):
    f = tmp_path / "export.json"
    _write(f, sample_payload)

    run_backfill(db, [f])
    metrics_after_first = _count(db, MetricSample)
    raw_after_first = _count(db, RawIngest)

    # Same bytes => content_hash dedup short-circuits before parsing.
    second = run_backfill(db, [f])
    assert second.stored == 0
    assert second.duplicates == 1
    assert _count(db, MetricSample) == metrics_after_first
    assert _count(db, RawIngest) == raw_after_first


def test_backfill_directory_collects_sorted_json(db, sample_payload, tmp_path):
    # Two distinct payloads (different content) => two raw rows, both stored.
    _write(tmp_path / "a.json", sample_payload)
    other = json.loads(json.dumps(sample_payload))
    other["data"]["metrics"][1]["data"][0]["qty"] = 12345
    _write(tmp_path / "b.json", other)
    (tmp_path / "ignore.txt").write_text("not json")

    files = collect_files([tmp_path])
    assert [p.name for p in files] == ["a.json", "b.json"]  # sorted, .txt excluded

    summary = run_backfill(db, files)
    assert summary.files == 2
    assert summary.stored == 2
    assert _count(db, RawIngest) == 2


def test_collect_files_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        collect_files(["/nonexistent/path/export.json"])


def test_dry_run_writes_nothing(db, sample_payload, tmp_path):
    f = tmp_path / "export.json"
    _write(f, sample_payload)

    summary = run_backfill(db, [f], dry_run=True)

    assert summary.metric_rows > 0  # reported
    assert _count(db, RawIngest) == 0  # but nothing persisted
    assert _count(db, MetricSample) == 0


def test_cli_wires_backfill_subcommand():
    args = cli.build_parser().parse_args(["backfill", "a.json", "b.json", "--dry-run"])
    assert args.func is backfill.run
    assert args.paths == ["a.json", "b.json"]
    assert args.dry_run is True


def test_cli_wires_analyze_subcommand():
    args = cli.build_parser().parse_args(["analyze"])
    assert args.func is cli._run_analyze


def test_bad_file_is_skipped_run_continues(db, sample_payload, tmp_path):
    (tmp_path / "broken.json").write_text("{ not valid json")
    _write(tmp_path / "good.json", sample_payload)

    summary = run_backfill(db, collect_files([tmp_path]))

    assert summary.failures == 1
    assert summary.stored == 1
    assert _count(db, MetricSample) > 0


def test_split_payload_chunks_metrics_and_keeps_sleep_whole():
    payload = {
        "data": {
            "metrics": [
                {"name": "step_count", "units": "count", "data": [{"qty": i} for i in range(5)]},
                {"name": "sleep_analysis", "units": "hr", "data": [{"n": i} for i in range(5)]},
                {"name": "empty", "units": "x", "data": []},
            ],
            "workouts": [{"id": i} for i in range(3)],
        }
    }

    pieces = list(split_payload(payload, chunk_points=2, chunk_items=2))

    steps = [p["data"]["metrics"][0] for p in pieces if p["data"].get("metrics", [{}])[0].get("name") == "step_count"]
    assert [len(m["data"]) for m in steps] == [2, 2, 1]
    assert all(m["units"] == "count" for m in steps)
    sleep = [p for p in pieces if p["data"].get("metrics", [{}])[0].get("name") == "sleep_analysis"]
    assert len(sleep) == 1 and len(sleep[0]["data"]["metrics"][0]["data"]) == 5
    assert [len(p["data"]["workouts"]) for p in pieces if "workouts" in p["data"]] == [2, 1]
    assert pieces == list(split_payload(payload, chunk_points=2, chunk_items=2))  # deterministic


def test_oversized_file_is_split_and_rerun_is_idempotent(db, sample_payload, tmp_path, monkeypatch):
    f = tmp_path / "export.json"
    _write(f, sample_payload)
    whole = ingest_svc.parse_payload(sample_payload)

    monkeypatch.setattr(backfill, "WHOLE_MAX_BYTES", 0)
    monkeypatch.setattr(backfill, "SPLIT_CHUNK_POINTS", 1)

    first = run_backfill(db, [f])
    assert first.stored == 1 and first.failures == 0
    assert first.metric_rows == len(whole.metric_rows)
    assert _count(db, MetricSample) == len(whole.metric_rows)
    assert _count(db, SleepSession) == 1
    assert _count(db, Workout) == 1
    assert _count(db, RawIngest) > 1  # one archive row per piece

    raw_after_first = _count(db, RawIngest)
    second = run_backfill(db, [f])
    assert second.stored == 0 and second.duplicates == 1
    assert _count(db, RawIngest) == raw_after_first


def test_file_rejected_whole_falls_back_to_split(db, sample_payload, tmp_path, monkeypatch):
    f = tmp_path / "export.json"
    _write(f, sample_payload)
    whole_hash = hashlib.sha256(f.read_bytes()).digest()
    real_archive = ingest_svc.archive_raw

    def archive_raw(db_, payload, content_hash, source_ip):
        stored = real_archive(db_, payload, content_hash, source_ip)
        if content_hash == whole_hash:  # mimic Postgres rejecting the oversized jsonb
            raise OperationalError("INSERT", {}, ProgramLimitExceeded("total size of jsonb array elements"))
        return stored

    monkeypatch.setattr(ingest_svc, "archive_raw", archive_raw)
    monkeypatch.setattr(backfill, "SPLIT_CHUNK_POINTS", 1)

    summary = run_backfill(db, [f])

    assert summary.stored == 1 and summary.failures == 0
    assert summary.metric_rows == len(ingest_svc.parse_payload(sample_payload).metric_rows)
    hashes = db.execute(select(RawIngest.content_hash)).scalars().all()
    assert len(hashes) > 1 and whole_hash not in hashes  # whole attempt rolled back, pieces kept


def test_other_db_error_on_whole_attempt_is_not_split(db, sample_payload, tmp_path, monkeypatch):
    f = tmp_path / "export.json"
    _write(f, sample_payload)

    def archive_raw(*_):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(ingest_svc, "archive_raw", archive_raw)

    summary = run_backfill(db, [f])

    assert summary.failures == 1 and summary.stored == 0
    assert _count(db, RawIngest) == 0


def test_summary_counts_state_of_mind_rows(db, sample_payload, tmp_path):
    entry = {
        "id": "0B6F3C1E-7A52-4C1D-9E0B-2F8A5D4C3B21",
        "kind": "daily_mood",
        "start": "2026-08-03T18:20:00Z",
        "valence": 0.4,
        "labels": ["calm"],
    }
    payload = {**sample_payload, "data": {**sample_payload["data"], "stateOfMind": [entry]}}
    f = tmp_path / "export.json"
    _write(f, payload)

    assert run_backfill(db, [f], dry_run=True).state_of_mind_rows == 1
    assert run_backfill(db, [f]).state_of_mind_rows == 1
