"""Bulk-backfill CLI: file discovery, idempotent re-runs, dry-run, bad files."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app import backfill, cli
from app.backfill import collect_files, run_backfill
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


def test_bad_file_is_skipped_run_continues(db, sample_payload, tmp_path):
    (tmp_path / "broken.json").write_text("{ not valid json")
    _write(tmp_path / "good.json", sample_payload)

    summary = run_backfill(db, collect_files([tmp_path]))

    assert summary.failures == 1
    assert summary.stored == 1
    assert _count(db, MetricSample) > 0
