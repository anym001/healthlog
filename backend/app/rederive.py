"""Re-derive rows the parser learned to extract after they were archived.

The steady-state ingest path now extracts ``heartRateData`` into
``workout_hr_samples``, but workouts ingested *before* this feature only exist
as summaries: their raw payloads still sit in ``raw_ingest``, yet content-hash
dedup means a normal re-post is skipped, so the samples are never extracted.
This one-off command replays every archived payload through the same pure
parser and upserts *only* the HR samples — the owning workouts already exist,
and the (workout_hae_id, ts) key makes a re-run idempotent.

``rederive-state-of-mind`` does the same for Apple Health State of Mind
entries (``stateOfMind``), keyed by HAE's entry id, and ``rederive-sleep`` for
sleep sessions, e.g. unaggregated stage segments archived before the parser
folded them into nights (the sleep upsert never downgrades a fuller night).
``rederive-symptoms`` and ``rederive-medications`` replay Apple Health symptoms
and medication doses; they also refresh the canonical slugs after an extension
of ``health_vocab.py``. ``rederive-heart-notifications`` and ``rederive-ecg``
replay Apple Watch heart rate notifications and ECG summaries.

Usage (one-shot, typically via ``docker exec``)::

    healthlog rederive-workout-hr
    healthlog rederive-workout-hr --dry-run   # parse + report, no writes
    healthlog rederive-state-of-mind [--dry-run]
    healthlog rederive-sleep [--dry-run]
    healthlog rederive-symptoms [--dry-run]
    healthlog rederive-medications [--dry-run]
    healthlog rederive-heart-notifications [--dry-run]
    healthlog rederive-ecg [--dry-run]

(equivalently ``python -m app rederive-workout-hr`` or ``python -m app.rederive``.)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import ingest as ingest_svc
from .cli_support import bootstrap, db_session, module_main

log = logging.getLogger("healthlog.rederive")


@dataclass
class RederiveSummary:
    payloads: int = 0
    samples: int = 0
    failures: int = 0


def _replay(
    db: Session,
    extract: Callable[[ingest_svc.ParsedPayload], list[dict]],
    store: Callable[[Session, list[dict]], int],
    key: tuple[str, ...],
    dry_run: bool,
) -> RederiveSummary:
    """Replay every archived payload through the parser, upserting the rows
    ``extract`` picks out with ``store``.

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
            rows = extract(ingest_svc.parse_payload(payload))
            deduped = ingest_svc._dedupe(rows, key)
            summary.samples += len(deduped)
            if dry_run:
                continue
            if deduped:
                store(db, deduped)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad payload must not abort the run
            db.rollback()
            summary.failures += 1
            log.error("payload #%d -> FAILED: %s", summary.payloads, exc)
    return summary


def run_rederive(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its workout HR samples."""
    return _replay(
        db, lambda p: p.workout_hr_rows, ingest_svc.store_workout_hr_samples, ("workout_hae_id", "ts"), dry_run
    )


def run_rederive_state_of_mind(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its State of Mind entries."""
    return _replay(db, lambda p: p.state_of_mind_rows, ingest_svc.store_state_of_mind, ("hae_id",), dry_run)


def run_rederive_sleep(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its sleep sessions. The key keeps
    every parsed row (sleep_start included) so ``store_sleep`` itself picks the
    most complete capture per (sleep_end, source)."""
    return _replay(db, lambda p: p.sleep_rows, ingest_svc.store_sleep, ("sleep_end", "source", "sleep_start"), dry_run)


def run_rederive_symptoms(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its symptom entries."""
    return _replay(db, lambda p: p.symptom_rows, ingest_svc.store_symptoms, ("start_time", "name", "source"), dry_run)


def run_rederive_medications(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its medication dose events."""
    return _replay(
        db, lambda p: p.medication_rows, ingest_svc.store_medication_doses, ("medication", "dose_time"), dry_run
    )


def run_rederive_heart_notifications(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its heart rate notifications."""
    return _replay(
        db,
        lambda p: p.heart_notification_rows,
        ingest_svc.store_heart_notifications,
        ("start_time", "kind", "source"),
        dry_run,
    )


def run_rederive_ecg(db: Session, dry_run: bool = False) -> RederiveSummary:
    """Replay every archived payload, upserting its ECG summaries."""
    return _replay(db, lambda p: p.ecg_rows, ingest_svc.store_ecg_recordings, ("start_time", "source"), dry_run)


def _log_summary(summary: RederiveSummary, dry_run: bool, command: str = "rederive-workout-hr") -> None:
    log.info(
        "%s%s done: payloads=%d rows=%d failures=%d",
        "[dry-run] " if dry_run else "",
        command,
        summary.payloads,
        summary.samples,
        summary.failures,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="parse and report counts without writing")


def run(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run)
    return 1 if summary.failures else 0


def run_state_of_mind(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_state_of_mind(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-state-of-mind")
    return 1 if summary.failures else 0


def run_sleep(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_sleep(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-sleep")
    return 1 if summary.failures else 0


def run_symptoms(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_symptoms(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-symptoms")
    return 1 if summary.failures else 0


def run_medications(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_medications(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-medications")
    return 1 if summary.failures else 0


def run_heart_notifications(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_heart_notifications(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-heart-notifications")
    return 1 if summary.failures else 0


def run_ecg(args: argparse.Namespace) -> int:
    bootstrap()

    with db_session() as db:
        summary = run_rederive_ecg(db, dry_run=args.dry_run)

    _log_summary(summary, args.dry_run, "rederive-ecg")
    return 1 if summary.failures else 0


def main(argv: list[str] | None = None) -> int:
    return module_main(
        add_arguments,
        run,
        prog="python -m app.rederive",
        description="Re-derive intra-workout HR samples from the raw archive.",
        argv=argv,
    )


if __name__ == "__main__":
    sys.exit(main())
