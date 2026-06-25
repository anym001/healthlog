"""Operator data-quality audit over the live database.

``healthlog audit`` answers the questions that decide whether the nightly
findings can be trusted at all — *before* anyone reads a single one. The
analysis math is unit-tested against synthetic series (test_analysis.py); what
those tests cannot see is the real database: whether enough clean history has
actually accumulated, whether stored units drifted from their canonical form,
and whether the last pipeline run produced anything.

The scan is **read-only** and reports three things:

1. **Findings snapshot** — total and per-kind counts of the latest ``findings``
   batch, with the run timestamp. Zero findings is surfaced loudly: a silent
   pipeline is the first thing to notice.
2. **Coverage** — days of data per metric (from the ``daily_metrics`` view).
   Core metrics below ``analysis.min_overlap`` (the ~6-week floor before
   correlations are trustworthy) are flagged; core metrics with *no* data at
   all are surfaced explicitly.
3. **Unit anomalies** — any stored unit in ``metric_samples`` that differs from
   the metric's ``metric_registry.unit_canonical`` (the real-world case the
   ingest unit-guard exists for, e.g. energy arriving as kJ instead of kcal).

Usage (one-shot, typically via ``docker exec``)::

    healthlog audit

(equivalently ``python -m app.audit``.)

The pure builders (``build_coverage``, ``detect_unit_anomalies``,
``summarize_findings``) take plain rows and no DB, so they are unit-tested in
the default suite; ``run`` does the SQL and logging.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

from .config import get_settings
from .logging_config import configure_logging

log = logging.getLogger("healthlog.audit")


# ===========================================================================
# Pure data structures + builders (no DB, no I/O) — unit-tested.
# ===========================================================================


@dataclass
class MetricCoverage:
    metric: str
    tier: str | None
    days: int
    first_day: dt.date | None
    last_day: dt.date | None

    @property
    def span_days(self) -> int:
        """Calendar days spanned by the data (inclusive); 0 when no data."""
        if self.first_day is None or self.last_day is None:
            return 0
        return (self.last_day - self.first_day).days + 1

    @property
    def density(self) -> float:
        """Fraction of spanned days that actually carry a value (gap detector)."""
        return self.days / self.span_days if self.span_days else 0.0


@dataclass
class UnitAnomaly:
    metric: str
    expected: str
    seen: list[str]


@dataclass
class AuditReport:
    min_overlap: int
    coverage: list[MetricCoverage] = field(default_factory=list)
    findings_by_kind: dict[str, int] = field(default_factory=dict)
    unit_anomalies: list[UnitAnomaly] = field(default_factory=list)
    findings_last_run: dt.datetime | None = None

    @property
    def findings_total(self) -> int:
        return sum(self.findings_by_kind.values())

    @property
    def core_coverage(self) -> list[MetricCoverage]:
        return [c for c in self.coverage if c.tier == "core"]

    @property
    def core_below_overlap(self) -> list[MetricCoverage]:
        """Core metrics that have data but fewer days than ``min_overlap``."""
        return [c for c in self.core_coverage if 0 < c.days < self.min_overlap]

    @property
    def core_no_data(self) -> list[MetricCoverage]:
        return [c for c in self.core_coverage if c.days == 0]

    @property
    def core_ready(self) -> list[MetricCoverage]:
        """Core metrics meeting the correlation floor (>= min_overlap days)."""
        return [c for c in self.core_coverage if c.days >= self.min_overlap]


def build_coverage(
    rows: Iterable[tuple[str, int, dt.date | None, dt.date | None]],
    registry: dict[str, dict],
) -> list[MetricCoverage]:
    """Coverage per metric from ``daily_metrics`` rows ``(metric, days, min, max)``.

    Core metrics absent from the data entirely are added with ``days=0`` so a
    missing-but-expected metric is impossible to overlook. Sorted core-first,
    then by ascending days (the weakest coverage surfaces at the top).
    """
    seen: dict[str, MetricCoverage] = {}
    for metric, days, first_day, last_day in rows:
        spec = registry.get(metric) or {}
        seen[metric] = MetricCoverage(metric, spec.get("tier"), int(days), first_day, last_day)
    for metric, spec in registry.items():
        if spec.get("tier") == "core" and metric not in seen:
            seen[metric] = MetricCoverage(metric, "core", 0, None, None)
    return sorted(seen.values(), key=lambda c: (c.tier != "core", c.days, c.metric))


def detect_unit_anomalies(
    unit_rows: Iterable[tuple[str, str | None]],
    registry: dict[str, dict],
) -> list[UnitAnomaly]:
    """Stored units in ``metric_samples`` that diverge from the canonical unit.

    NULL/empty units and metrics whose registry has no ``unit_canonical`` (e.g.
    auto-registered stubs) are skipped — there is nothing to check against.
    """
    by_metric: dict[str, set[str]] = {}
    for metric, unit in unit_rows:
        if not unit:
            continue
        by_metric.setdefault(metric, set()).add(unit)
    anomalies: list[UnitAnomaly] = []
    for metric, units in sorted(by_metric.items()):
        canonical = (registry.get(metric) or {}).get("unit_canonical")
        if not canonical:
            continue
        bad = sorted(u for u in units if u != canonical)
        if bad:
            anomalies.append(UnitAnomaly(metric=metric, expected=canonical, seen=bad))
    return anomalies


def summarize_findings(rows: Iterable[tuple[str, int]]) -> dict[str, int]:
    return {kind: int(count) for kind, count in rows}


# ===========================================================================
# Reporting
# ===========================================================================


def _log_report(report: AuditReport) -> None:
    # --- 1. Findings snapshot ---------------------------------------------
    if report.findings_total == 0:
        log.warning(
            "findings: snapshot is EMPTY - the pipeline produced nothing. Run "
            "`healthlog analyze` and check there is enough history (see coverage below)"
        )
    else:
        when = report.findings_last_run.isoformat() if report.findings_last_run else "unknown"
        by_kind = ", ".join(f"{k}={v}" for k, v in sorted(report.findings_by_kind.items()))
        log.info("findings: %d total (last run %s) -> %s", report.findings_total, when, by_kind)

    # --- 2. Coverage -------------------------------------------------------
    core = report.core_coverage
    log.info(
        "coverage: %d/%d core metrics meet the %d-day correlation floor",
        len(report.core_ready),
        len(core),
        report.min_overlap,
    )
    for c in report.core_no_data:
        log.warning("coverage: core metric %r has NO data at all", c.metric)
    for c in report.core_below_overlap:
        log.warning(
            "coverage: core metric %r has only %d day(s) (< %d) - correlations not yet trustworthy",
            c.metric,
            c.days,
            report.min_overlap,
        )
    # Sparse coverage (gaps) among otherwise-ready core metrics is worth noting.
    for c in report.core_ready:
        if c.density < 0.8:
            log.info(
                "coverage: core metric %r is gappy - %d day(s) of values over a %d-day span (%.0f%%)",
                c.metric,
                c.days,
                c.span_days,
                c.density * 100,
            )

    # --- 3. Unit anomalies -------------------------------------------------
    if report.unit_anomalies:
        for a in report.unit_anomalies:
            log.warning(
                "unit: metric %r stores %s but canonical is %r - check the ingest unit-guard",
                a.metric,
                a.seen,
                a.expected,
            )
    else:
        log.info("unit: all stored units match their canonical unit")


# ===========================================================================
# CLI entry point (DB work lives here, kept out of the pure builders above).
# ===========================================================================


def add_arguments(parser: argparse.ArgumentParser) -> None:  # noqa: ARG001 - no options yet
    """No options yet; present for symmetry with the other CLI subcommands."""


def run(_args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    # Lazy imports so --help works without a configured DATABASE_URL, and the
    # heavy appconfig/yaml load only happens on a real run.
    from sqlalchemy import func, select, text

    from .appconfig import load_config
    from .database import SessionLocal
    from .models import Finding, MetricRegistry, MetricSample

    min_overlap = load_config(settings.config_file).analysis.min_overlap

    db = SessionLocal()
    try:
        registry: dict[str, dict] = {
            metric: {"tier": tier, "unit_canonical": unit}
            for metric, tier, unit in db.execute(
                select(MetricRegistry.metric, MetricRegistry.tier, MetricRegistry.unit_canonical)
            ).all()
        }

        coverage_rows = db.execute(
            text(
                "SELECT metric, count(*) AS days, min(day) AS first_day, max(day) AS last_day "
                "FROM daily_metrics GROUP BY metric"
            )
        ).all()

        unit_rows = db.execute(select(MetricSample.metric, MetricSample.unit).distinct()).all()

        finding_rows = db.execute(select(Finding.kind, func.count()).group_by(Finding.kind)).all()
        last_run = db.execute(select(func.max(Finding.computed_at))).scalar_one_or_none()
    finally:
        db.close()

    report = AuditReport(
        min_overlap=min_overlap,
        coverage=build_coverage(coverage_rows, registry),
        findings_by_kind=summarize_findings(finding_rows),
        unit_anomalies=detect_unit_anomalies(unit_rows, registry),
        findings_last_run=last_run,
    )
    _log_report(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.audit",
        description="Read-only data-quality audit: findings snapshot, coverage, unit anomalies.",
    )
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
