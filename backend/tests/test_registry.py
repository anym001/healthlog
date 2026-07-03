"""Pins the finalised metric-registry curation (Phase 0 close-out).

The registry is *data* (ARCHITECTURE.md §4.0/§4.5): a metric's canonical unit, daily
aggregate, category and tier live here, not in code. These tests guard that
the curated seed stays internally consistent and that the migration seeds the
table from it verbatim.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import MetricRegistry
from app.registry import METRIC_REGISTRY, SLEEP_METRIC, value_in_bounds

VALID_AGG = {"sum", "min", "max", "avg"}
VALID_CATEGORY = {"activity", "sleep", "vital", "mobility", "environment", "mindfulness", "nutrition"}
VALID_TIER = {"core", "secondary"}


def test_every_metric_is_fully_and_validly_curated():
    for metric, spec in METRIC_REGISTRY.items():
        assert spec["display_name"], f"{metric}: missing display_name"
        assert spec["unit_canonical"], f"{metric}: missing unit_canonical"
        assert spec["agg_default"] in VALID_AGG, f"{metric}: bad agg_default {spec['agg_default']!r}"
        assert spec["category"] in VALID_CATEGORY, f"{metric}: bad category {spec['category']!r}"
        assert spec["tier"] in VALID_TIER, f"{metric}: bad tier {spec['tier']!r}"


def test_sleep_metric_is_not_in_metric_registry():
    # sleep_analysis is routed to sleep_sessions, never metric_samples.
    assert SLEEP_METRIC not in METRIC_REGISTRY


def test_energy_metrics_are_canonical_kcal():
    # The unit guard converts HAE's kJ to these canonical kcal values.
    for metric in ("active_energy", "basal_energy_burned"):
        assert METRIC_REGISTRY[metric]["unit_canonical"] == "kcal"


def test_summing_metrics_use_sum_aggregate():
    # Counts/totals must sum, never average (ARCHITECTURE.md §11 aggregate semantics).
    for metric in ("step_count", "active_energy", "walking_running_distance", "flights_climbed"):
        assert METRIC_REGISTRY[metric]["agg_default"] == "sum"


def test_resting_heart_rate_uses_min_aggregate():
    assert METRIC_REGISTRY["resting_heart_rate"]["agg_default"] == "min"


def test_plausibility_bounds_are_well_ordered():
    # Where both bounds are set, the envelope must be non-empty (min <= max).
    for metric, spec in METRIC_REGISTRY.items():
        low, high = spec.get("value_min"), spec.get("value_max")
        if low is not None and high is not None:
            assert low <= high, f"{metric}: value_min {low} > value_max {high}"


def test_value_in_bounds_rejects_out_of_range_values():
    # heart_rate is bounded 20..250: a 0 reading and a 9999 spike are implausible.
    assert value_in_bounds("heart_rate", 60) is True
    assert value_in_bounds("heart_rate", 0) is False
    assert value_in_bounds("heart_rate", 9999) is False
    # step_count is non-negative (value_min only): a negative is implausible.
    assert value_in_bounds("step_count", 0) is True
    assert value_in_bounds("step_count", -5) is False
    assert value_in_bounds("step_count", 1_000_000) is True  # no upper bound


def test_value_in_bounds_is_permissive_when_nothing_to_check():
    # None, unknown metrics and metrics without bounds are all "plausible".
    assert value_in_bounds("heart_rate", None) is True
    assert value_in_bounds("not_a_real_metric", -999) is True


def test_cardio_recovery_promoted_to_core():
    # Curated up from an auto-registered stub: it joins the default pipeline.
    assert METRIC_REGISTRY["cardio_recovery"]["tier"] == "core"


def test_migration_seeds_registry_from_the_curated_dict(db):
    rows = {r.metric: r for r in db.execute(select(MetricRegistry)).scalars()}
    # Every curated metric is seeded (auto-registered stubs may add more).
    for metric, spec in METRIC_REGISTRY.items():
        row = rows.get(metric)
        assert row is not None, f"{metric} not seeded by migration"
        assert row.unit_canonical == spec["unit_canonical"]
        assert row.agg_default == spec["agg_default"]
        assert row.category == spec["category"]
        assert row.tier == spec["tier"]
        assert row.auto_registered is False
