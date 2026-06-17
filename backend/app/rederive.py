"""Re-derive intra-workout HR samples from the raw archive.

The steady-state ingest path now extracts ``heartRateData`` into
``workout_hr_samples``, but workouts ingested *before* this feature only exist
as summaries: their raw payloads still sit in ``raw_ingest``, yet content-hash
dedup means a normal re-post is skipped, so the samples are never extracted.
This one-off command replays every archived payload through the same pure
parser and upserts *only* the HR samples — the owning workouts already exist,
and the (workout_hae_id, ts) key makes a re-run idempotent.

Usage (one-shot, typically via ``docker exec``)::

    healthlog rederive-workout-hr
    healthlog rederive-workout-hr --dry-run   # parse + report, no writes

(equivalently ``python -m app rederive-workout-hr`` or ``python -m app.rederive``.)
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import ingest as ingest_svc
from .config import get_settings
from .logging_config import configure_logging

log = logging.getLogger("healthlog.rederive")


@dataclass
class RederiveSummary:
    payloads: int = 0
    samples: int = 0
    failures: int = 0


def run_rederive(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its workout HR samples.

    Each payload is committed on its own so a single bad one can't abort the
    whole run (the upsert + dedup make a re-run a no-op for what already landed).
    """
    from sqlalchemy import select

    from .models import RawIngest

    summary = RederiveSummary()
    # Fetch the ids first, then load one payload at a time: the per-payload
    # commit below would otherwise invalidate a streaming result cursor, and a
    # multi-year archive shouldn't all sit in memory at once.
    ids = db.execute(select(RawIngest.id).order_by(RawIngest.id)).scalars().all()
    for rid in ids:
        summary.payloads += 1
        try:
            payload = db.execute(select(RawIngest.payload).where(RawIngest.id == rid)).scalar_one()
            rows = ingest_svc.parse_payload(payload).workout_hr_rows
            if dry_run:
                summary.samples += len(ingest_svc._dedupe_workout_hr_rows(rows))
                continue
            if rows:
                summary.samples += ingest_svc.store_workout_hr_samples(db, rows)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad payload must not abort the run
            db.rollback()
            summary.failures += 1
            log.error("payload #%d -> FAILED: %s", summary.payloads, exc)
    return summary


def _log_summary(summary: RederiveSummary, dry_run: bool) -> None:
    log.info(
        "%srederive-workout-hr done: payloads=%d hr_samples=%d failures=%d",
        "[dry-run] " if dry_run else "",
        summary.payloads,
        summary.samples,
        summary.failures,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="parse and report counts without writing")


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    # Lazy import so --help works without a configured DATABASE_URL.
    from .database import SessionLocal

    db = SessionLocal()
    try:
        summary = run_rederive(db, dry_run=args.dry_run)
    finally:
        db.close()

    _log_summary(summary, args.dry_run)
    return 1 if summary.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.rederive",
        description="Re-derive intra-workout HR samples from the raw archive.",
    )
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
