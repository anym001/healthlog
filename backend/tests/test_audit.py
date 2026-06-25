"""Unit tests for the data-quality audit (app/audit.py).

Pure: the builders take plain rows + a registry dict, so no DB is needed and
they run in the default suite (mirrors test_diagnostics.py).
"""

from __future__ import annotations

import datetime as dt

from app.audit import (
    AuditReport,
    MetricCoverage,
    UnitAnomaly,
    build_coverage,
    detect_unit_anomalies,
    summarize_findings,
)

REGISTRY = {
    "heart_rate": {"tier": "core", "unit_canonical": "count/min"},
    "active_energy": {"tier": "core", "unit_canonical": "kcal"},
    "step_count": {"tier": "core", "unit_canonical": "count"},
    "walking_speed": {"tier": "secondary", "unit_canonical": "km/hr"},
}


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# --- Coverage ---------------------------------------------------------------


def test_build_coverage_maps_tier_and_span_and_density():
    rows = [("step_count", 30, _d("2026-05-01"), _d("2026-06-09"))]
    cov = {c.metric: c for c in build_coverage(rows, REGISTRY)}
    c = cov["step_count"]
    assert c.tier == "core"
    assert c.days == 30
    assert c.span_days == 40  # inclusive span 05-01..06-09
    assert abs(c.density - 30 / 40) < 1e-9


def test_build_coverage_surfaces_core_metric_with_no_data():
    # Only step_count has rows; the other core metrics must appear with days=0.
    rows = [("step_count", 50, _d("2026-01-01"), _d("2026-02-19"))]
    cov = {c.metric: c for c in build_coverage(rows, REGISTRY)}
    assert cov["heart_rate"].days == 0
    assert cov["heart_rate"].tier == "core"
    assert cov["heart_rate"].span_days == 0 and cov["heart_rate"].density == 0.0
    # secondary metrics with no data are NOT invented (would be noise).
    assert "walking_speed" not in cov


def test_build_coverage_sorts_core_first_then_weakest_days():
    rows = [
        ("walking_speed", 100, _d("2026-01-01"), _d("2026-04-10")),  # secondary, lots
        ("active_energy", 60, _d("2026-01-01"), _d("2026-03-01")),  # core
        ("step_count", 10, _d("2026-01-01"), _d("2026-01-10")),  # core, weakest
    ]
    order = [c.metric for c in build_coverage(rows, REGISTRY)]
    # core metrics come first, ascending by days; heart_rate (0 days) leads.
    assert order[0] == "heart_rate"  # core, no data
    assert order.index("step_count") < order.index("active_energy")
    assert order[-1] == "walking_speed"  # secondary always last


# --- Unit anomalies ---------------------------------------------------------


def test_detect_unit_anomalies_flags_divergent_unit():
    rows = [("active_energy", "kJ"), ("active_energy", "kcal"), ("step_count", "count")]
    anomalies = detect_unit_anomalies(rows, REGISTRY)
    assert anomalies == [UnitAnomaly(metric="active_energy", expected="kcal", seen=["kJ"])]


def test_detect_unit_anomalies_ignores_null_empty_and_unknown_metrics():
    rows = [
        ("step_count", None),  # null unit -> skip
        ("step_count", ""),  # empty unit -> skip
        ("step_count", "count"),  # matches canonical -> no anomaly
        ("mystery_metric", "widgets"),  # not in registry -> nothing to check
    ]
    assert detect_unit_anomalies(rows, REGISTRY) == []


def test_detect_unit_anomalies_skips_metric_without_canonical():
    registry = {"foo": {"tier": "secondary", "unit_canonical": None}}
    assert detect_unit_anomalies([("foo", "bar")], registry) == []


# --- Findings summary -------------------------------------------------------


def test_summarize_findings_builds_count_map():
    assert summarize_findings([("correlation", 5), ("anomaly", 2)]) == {
        "correlation": 5,
        "anomaly": 2,
    }


# --- Report properties ------------------------------------------------------


def _cov(metric, tier, days, first=None, last=None):
    return MetricCoverage(metric, tier, days, first, last)


def test_report_classifies_core_coverage_against_min_overlap():
    report = AuditReport(
        min_overlap=42,
        coverage=[
            _cov("a", "core", 0),  # no data
            _cov("b", "core", 20, _d("2026-01-01"), _d("2026-01-20")),  # below floor
            _cov("c", "core", 60, _d("2026-01-01"), _d("2026-03-01")),  # ready
            _cov("d", "secondary", 5),  # ignored for core checks
        ],
        findings_by_kind={"correlation": 3, "trend": 1},
    )
    assert report.findings_total == 4
    assert [c.metric for c in report.core_no_data] == ["a"]
    assert [c.metric for c in report.core_below_overlap] == ["b"]
    assert [c.metric for c in report.core_ready] == ["c"]
    assert {c.metric for c in report.core_coverage} == {"a", "b", "c"}


def test_report_findings_total_zero_when_empty():
    assert AuditReport(min_overlap=42).findings_total == 0
