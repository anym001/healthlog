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
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from . import ingest as ingest_svc
from .appconfig import get_app_config
from .cli_support import bootstrap, db_session, module_main

log = logging.getLogger("healthlog.backfill")


@dataclass
class BackfillSummary:
    files: int = 0
    stored: int = 0
    duplicates: int = 0
    failures: int = 0
    metric_rows: int = 0
    sleep_rows: int = 0
    workout_rows: int = 0
    unknown_metrics: int = 0

    def add_stored(self, result: ingest_svc.StoreResult) -> None:
        """Fold one stored file's row counts into the running totals."""
        self.stored += 1
        self.metric_rows += result.metric_rows
        self.sleep_rows += result.sleep_rows
        self.workout_rows += result.workout_rows
        self.unknown_metrics += result.unknown_metrics

    def add_parsed(self, parsed: ingest_svc.ParsedPayload) -> None:
        """Fold one parsed-only file's row counts into the totals (dry-run)."""
        self.metric_rows += len(parsed.metric_rows)
        self.sleep_rows += len(parsed.sleep_rows)
        self.workout_rows += len(parsed.workout_rows)
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


def ingest_file(
    db: Session,
    path: Path,
    type_map: dict[str, str] | None = None,
) -> tuple[str, ingest_svc.StoreResult | None]:
    """Archive + parse + store a single file via the shared ingest pipeline.

    Identical to the HTTP endpoint's path (``ingest_svc.ingest_bytes``), so a
    file imported from disk behaves exactly like a posted payload. ``type_map``
    is threaded into workout-type normalisation. The commit is the caller's
    (``run_backfill`` commits per file). Returns ('stored'|'duplicate', result)."""
    return ingest_svc.ingest_bytes(db, path.read_bytes(), type_map=type_map)


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
                    "[dry-run] %s -> metrics=%d sleep=%d workouts=%d unknown=%d",
                    path.name,
                    len(parsed.metric_rows),
                    len(parsed.sleep_rows),
                    len(parsed.workout_rows),
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
                    "%s -> stored metrics=%d sleep=%d workouts=%d unknown=%d",
                    path.name,
                    result.metric_rows,
                    result.sleep_rows,
                    result.workout_rows,
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
        "| metric_rows=%d sleep_rows=%d workout_rows=%d unknown_metrics=%d",
        prefix,
        summary.files,
        summary.stored,
        summary.duplicates,
        summary.failures,
        summary.metric_rows,
        summary.sleep_rows,
        summary.workout_rows,
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
