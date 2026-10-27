"""Bulk backfill CLI: import a full Apple Health history from disk.

The HTTP ingest endpoint (``POST /api/ingest``) is the steady-state path for
the nightly HAE deltas, but a one-time full-history export spans years and
easily exceeds ``MAX_PAYLOAD_BYTES`` (and the proxy's request timeout). This
CLI sidesteps both: it reads the JSON file(s) HAE exported straight from the
filesystem and runs them through the *same* pipeline
(``archive_raw -> parse_payload -> store``), so behaviour is identical to the
endpoint and the result is idempotent — re-running is always safe.

Usage (one-shot, typically via ``docker exec``)::

    healthlog backfill /config/import/export.json
    healthlog backfill /config/import            # a directory of *.json
    healthlog backfill --dry-run /config/import  # parse + report, no writes

(equivalently ``python -m app backfill …`` or ``python -m app.backfill …``.)

Each file is committed on its own: a failure midway keeps already-imported
files persisted, and the dedup (``content_hash``) + upsert make a re-run a
no-op for what already landed.

A file too large to archive as one ``raw_ingest`` row (a high-resolution
export easily runs to hundreds of MB; Postgres caps a single ``jsonb`` value at
256 MB) is split into self-contained HAE payloads (``split_payload``: each
metric's samples in runs of ``SPLIT_CHUNK_POINTS``, other arrays in runs of
``SPLIT_CHUNK_ITEMS``), and each piece is ingested and committed on its own. The split is deterministic, so a
re-run hashes every piece identically and skips the ones that already landed.
Every file is first tried whole and only split when Postgres rejects it, so a
file that ever fit as one row keeps its original ``content_hash``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from psycopg.errors import ProgramLimitExceeded
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import ingest as ingest_svc
from .appconfig import get_app_config
from .cli_support import bootstrap, db_session, module_main
from .registry import SLEEP_METRIC

log = logging.getLogger("healthlog.backfill")

# Files above this size skip the whole-file attempt and go straight to the
# split: Postgres rejects any protocol message over 1 GB, so such a body can
# never be archived as one row.
WHOLE_MAX_BYTES = 1024 * 1024 * 1024
# Samples per metric piece / items per piece for the other ``data`` arrays
# (workouts, ecg, ...). ~100k compact samples is ~10 MB of JSON. Changing either
# changes every piece's content_hash, so already split files would be archived
# again on a re-run (the metric upserts stay idempotent; only raw_ingest grows).
SPLIT_CHUNK_POINTS = 100_000
SPLIT_CHUNK_ITEMS = 200


@dataclass
class BackfillSummary:
    files: int = 0
    stored: int = 0
    duplicates: int = 0
    failures: int = 0
    metric_rows: int = 0
    sleep_rows: int = 0
    workout_rows: int = 0
    state_of_mind_rows: int = 0
    symptom_rows: int = 0
    medication_rows: int = 0
    heart_notification_rows: int = 0
    ecg_rows: int = 0
    unknown_metrics: int = 0

    def add_stored(self, result: ingest_svc.StoreResult) -> None:
        """Fold one stored file's row counts into the running totals."""
        self.stored += 1
        self.metric_rows += result.metric_rows
        self.sleep_rows += result.sleep_rows
        self.workout_rows += result.workout_rows
        self.state_of_mind_rows += result.state_of_mind_rows
        self.symptom_rows += result.symptom_rows
        self.medication_rows += result.medication_rows
        self.heart_notification_rows += result.heart_notification_rows
        self.ecg_rows += result.ecg_rows
        self.unknown_metrics += result.unknown_metrics

    def add_parsed(self, parsed: ingest_svc.ParsedPayload) -> None:
        """Fold one parsed-only file's row counts into the totals (dry-run)."""
        self.metric_rows += len(parsed.metric_rows)
        self.sleep_rows += len(parsed.sleep_rows)
        self.workout_rows += len(parsed.workout_rows)
        self.state_of_mind_rows += len(parsed.state_of_mind_rows)
        self.symptom_rows += len(parsed.symptom_rows)
        self.medication_rows += len(parsed.medication_rows)
        self.heart_notification_rows += len(parsed.heart_notification_rows)
        self.ecg_rows += len(parsed.ecg_rows)
        self.unknown_metrics += len(parsed.unknown_metrics)


def collect_files(paths: Iterable[str | Path], pattern: str = "*.json") -> list[Path]:
    """Expand the given paths into a sorted, de-duplicated list of JSON files.

    A directory contributes every file matching ``pattern`` (sorted, so the
    import order is deterministic); a file is taken as-is. Missing paths raise.
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise FileNotFoundError(p)
        candidates = sorted(p.glob(pattern)) if p.is_dir() else [p]
        for c in candidates:
            resolved = c.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(c)
    return files


def split_payload(
    payload: dict,
    chunk_points: int = SPLIT_CHUNK_POINTS,
    chunk_items: int = SPLIT_CHUNK_ITEMS,
) -> Iterator[dict]:
    """Split one HAE payload into smaller, self-contained HAE payloads. Pure.

    Every metric's ``data`` is cut into runs of ``chunk_points`` samples, one
    metric per piece (``sleep_analysis`` stays whole: its rows are consolidated
    per night within a single store call). Every other list under ``data``
    (workouts, ecg, ...) is cut into runs of ``chunk_items``; a workout is never
    split, so its HR samples and route stay with it. Non-list ``data`` keys and
    top-level keys besides ``data`` ride along on every piece. The output order
    is deterministic, so the same file always yields the same pieces."""
    data = payload.get("data") or {}
    envelope = {k: v for k, v in payload.items() if k != "data"}
    scalars = {k: v for k, v in data.items() if not isinstance(v, list)}

    def piece(key: str, items: list) -> dict:
        return {**envelope, "data": {**scalars, key: items}}

    for metric in data.get("metrics") or []:
        points = metric.get("data") or []
        size = len(points) if metric.get("name") == SLEEP_METRIC else chunk_points
        for start in range(0, max(len(points), 1), max(size, 1)):
            yield piece("metrics", [{**metric, "data": points[start : start + size]}])

    for key, value in data.items():
        if key == "metrics" or not isinstance(value, list):
            continue
        for start in range(0, len(value), chunk_items):
            yield piece(key, value[start : start + chunk_items])


def _merge_results(results: list[ingest_svc.StoreResult]) -> ingest_svc.StoreResult:
    """Sum the per-piece counters of a split file into one StoreResult."""
    merged = ingest_svc.StoreResult()
    for r in results:
        for f in fields(merged):
            setattr(merged, f.name, getattr(merged, f.name) + getattr(r, f.name))
    return merged


def ingest_file(
    db: Session,
    path: Path,
    type_map: dict[str, str] | None = None,
) -> tuple[str, ingest_svc.StoreResult | None]:
    """Archive + parse + store a single file via the shared ingest pipeline.

    Identical to the HTTP endpoint's path (``ingest_svc.ingest_bytes``), so a
    file imported from disk behaves exactly like a posted payload. ``type_map``
    is threaded into workout-type normalisation. Returns
    ('stored'|'duplicate', result).

    The file is first tried whole (inside a savepoint), as one transaction
    whose commit is the caller's (``run_backfill`` commits per file). Only if
    Postgres rejects it as too large (or it exceeds ``WHOLE_MAX_BYTES``) is it
    split (``split_payload``) and each piece ingested *and committed* here, so
    a failure midway keeps the pieces that landed; it counts as 'duplicate'
    only if every piece was already archived."""
    body = path.read_bytes()
    if len(body) <= WHOLE_MAX_BYTES:
        try:
            with db.begin_nested():
                return ingest_svc.ingest_bytes(db, body, type_map=type_map)
        except DBAPIError as exc:
            if not isinstance(exc.orig, ProgramLimitExceeded):
                raise
        log.info("%s: too large to archive as one row, splitting", path.name)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid JSON body.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    del body

    results: list[ingest_svc.StoreResult] = []
    n_pieces = 0
    for sub in split_payload(payload):
        n_pieces += 1
        piece_bytes = json.dumps(sub, separators=(",", ":"), ensure_ascii=False).encode()
        status, result = ingest_svc.ingest_bytes(db, piece_bytes, type_map=type_map)
        db.commit()
        if status == "stored":
            results.append(result)
    log.info("%s: split into %d piece(s), %d new", path.name, n_pieces, len(results))
    if not results:
        return "duplicate", None
    return "stored", _merge_results(results)


def run_backfill(
    db: Session,
    files: Sequence[Path],
    dry_run: bool = False,
    type_map: dict[str, str] | None = None,
) -> BackfillSummary:
    """Import every file, committing per file. ``dry_run`` parses + reports only."""
    summary = BackfillSummary(files=len(files))
    for path in files:
        try:
            if dry_run:
                parsed = ingest_svc.parse_payload(json.loads(path.read_bytes()), type_map=type_map)
                summary.add_parsed(parsed)
                log.info(
                    "[dry-run] %s -> metrics=%d sleep=%d workouts=%d state_of_mind=%d symptoms=%d medications=%d "
                    "heart_notifications=%d ecg=%d unknown=%d",
                    path.name,
                    len(parsed.metric_rows),
                    len(parsed.sleep_rows),
                    len(parsed.workout_rows),
                    len(parsed.state_of_mind_rows),
                    len(parsed.symptom_rows),
                    len(parsed.medication_rows),
                    len(parsed.heart_notification_rows),
                    len(parsed.ecg_rows),
                    len(parsed.unknown_metrics),
                )
                continue

            status, result = ingest_file(db, path, type_map=type_map)
            db.commit()
            if status == "duplicate":
                summary.duplicates += 1
                log.info("%s -> duplicate (already imported), skipped", path.name)
            else:
                summary.add_stored(result)
                log.info(
                    "%s -> stored metrics=%d sleep=%d workouts=%d state_of_mind=%d symptoms=%d medications=%d "
                    "heart_notifications=%d ecg=%d unknown=%d",
                    path.name,
                    result.metric_rows,
                    result.sleep_rows,
                    result.workout_rows,
                    result.state_of_mind_rows,
                    result.symptom_rows,
                    result.medication_rows,
                    result.heart_notification_rows,
                    result.ecg_rows,
                    result.unknown_metrics,
                )
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
            db.rollback()
            summary.failures += 1
            log.error("%s -> FAILED: %s", path.name, exc)
    return summary


def _log_summary(summary: BackfillSummary, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    log.info(
        "%sbackfill done: files=%d stored=%d duplicates=%d failures=%d "
        "| metric_rows=%d sleep_rows=%d workout_rows=%d state_of_mind_rows=%d symptom_rows=%d "
        "medication_rows=%d heart_notification_rows=%d ecg_rows=%d unknown_metrics=%d",
        prefix,
        summary.files,
        summary.stored,
        summary.duplicates,
        summary.failures,
        summary.metric_rows,
        summary.sleep_rows,
        summary.workout_rows,
        summary.state_of_mind_rows,
        summary.symptom_rows,
        summary.medication_rows,
        summary.heart_notification_rows,
        summary.ecg_rows,
        summary.unknown_metrics,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the backfill arguments on ``parser`` (shared by the CLI)."""
    parser.add_argument("paths", nargs="+", help="JSON file(s) or directory(ies) to import")
    parser.add_argument("--glob", default="*.json", help="glob for files inside a directory (default: *.json)")
    parser.add_argument("--dry-run", action="store_true", help="parse and report counts without writing")


def run(args: argparse.Namespace) -> int:
    """Execute a parsed backfill invocation. Returns a process exit code."""
    bootstrap()

    try:
        files = collect_files(args.paths, args.glob)
    except FileNotFoundError as exc:
        log.error("path does not exist: %s", exc)
        return 2
    if not files:
        log.error("no files matched (glob=%s)", args.glob)
        return 2

    log.info("backfill: %d file(s) to import%s", len(files), " (dry-run)" if args.dry_run else "")

    type_map = get_app_config().workouts.type_map
    with db_session() as db:
        summary = run_backfill(db, files, dry_run=args.dry_run, type_map=type_map)

    _log_summary(summary, args.dry_run)
    return 1 if summary.failures else 0


def main(argv: list[str] | None = None) -> int:
    return module_main(
        add_arguments,
        run,
        prog="python -m app.backfill",
        description="Bulk-import Apple Health history exported by Health Auto Export.",
        argv=argv,
    )


if __name__ == "__main__":
    sys.exit(main())
