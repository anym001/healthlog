"""Operator diagnostics over the raw HAE archive.

``healthlog check-workout-hr`` answers one concrete question: does the stored
``raw_ingest`` payload actually carry an *intra-workout* heart-rate time series
(``heartRateData``)? That series is the prerequisite for zone-based (Edwards)
TRIMP — HAE only sometimes includes it, and the per-workout summary the
analysis uses today keeps just ``heartRate {min, avg, max}``. The scan is
read-only and reports what array-valued fields the workouts carry, so the
Edwards decision rests on real data rather than guesswork.

Usage (one-shot, typically via ``docker exec``)::

    healthlog check-workout-hr
    healthlog check-workout-hr --limit 200   # only the most recent N payloads

(equivalently ``python -m app.diagnostics``.)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median

from .cli_support import bootstrap, db_session, module_main

log = logging.getLogger("healthlog.diagnostics")

# The intra-workout heart-rate series HAE may attach to a workout object.
HR_SERIES_FIELD = "heartRateData"


@dataclass
class WorkoutHrReport:
    payloads: int = 0
    workouts: int = 0
    with_hr_series: int = 0
    # Every array-valued workout field seen (name -> how many workouts carry it
    # non-empty); reveals which intra-workout series HAE actually sends.
    array_fields: dict[str, int] = field(default_factory=dict)
    hr_series_len_min: int | None = None
    hr_series_len_max: int | None = None
    hr_series_len_median: int | None = None

    @property
    def edwards_feasible(self) -> bool:
        return self.with_hr_series > 0


def scan_workout_hr(payloads: Iterable[dict]) -> WorkoutHrReport:
    """Pure scan: tally intra-workout HR series across raw HAE payloads."""
    report = WorkoutHrReport()
    lengths: list[int] = []
    for payload in payloads:
        report.payloads += 1
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        for w in data.get("workouts") or []:
            if not isinstance(w, dict):
                continue
            report.workouts += 1
            for key, value in w.items():
                if isinstance(value, list) and value:
                    report.array_fields[key] = report.array_fields.get(key, 0) + 1
            hr = w.get(HR_SERIES_FIELD)
            if isinstance(hr, list) and hr:
                report.with_hr_series += 1
                lengths.append(len(hr))
    if lengths:
        report.hr_series_len_min = min(lengths)
        report.hr_series_len_max = max(lengths)
        report.hr_series_len_median = int(median(lengths))
    return report


def _log_report(report: WorkoutHrReport) -> None:
    log.info(
        "workout-hr scan: payloads=%d workouts=%d with_%s=%d",
        report.payloads,
        report.workouts,
        HR_SERIES_FIELD,
        report.with_hr_series,
    )
    if report.array_fields:
        fields = ", ".join(f"{k}={v}" for k, v in sorted(report.array_fields.items()))
        log.info("array-valued workout fields seen: %s", fields)
    else:
        log.info("no array-valued workout fields seen (only scalar summaries)")
    if report.edwards_feasible:
        log.info(
            "intra-workout HR series present (len min/median/max = %s/%s/%s) -> zone-based (Edwards) TRIMP is feasible",
            report.hr_series_len_min,
            report.hr_series_len_median,
            report.hr_series_len_max,
        )
    else:
        log.info(
            "no intra-workout HR series found -> zone-based (Edwards) TRIMP is NOT "
            "feasible from the current export; enable it in the HAE Workouts automation"
        )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="scan only the most recent N raw payloads (default: all)",
    )


def run(args: argparse.Namespace) -> int:
    bootstrap()

    # Lazy import so --help works without a configured DATABASE_URL.
    from sqlalchemy import select

    from .models import RawIngest

    with db_session() as db:
        stmt = select(RawIngest.payload).order_by(RawIngest.received_at.desc())
        if args.limit is not None:
            stmt = stmt.limit(args.limit)
        payloads = db.execute(stmt).scalars()
        report = scan_workout_hr(payloads)

    _log_report(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    return module_main(
        add_arguments,
        run,
        prog="python -m app.diagnostics",
        description="Check whether the raw HAE archive carries intra-workout HR series.",
        argv=argv,
    )


if __name__ == "__main__":
    sys.exit(main())
