"""Pins the finalised metric-registry curation (Phase 0 close-out).

The registry is *data* (ARCHITECTURE.md §4.0/§4.5): a metric's canonical unit, daily
aggregate, category and tier live here, not in code. These tests guard that
the curated seed stays internally consistent and that the migration seeds the
table from it verbatim.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import MetricRegistry
from app.registry import METRIC_REGISTRY, SLEEP_METRIC

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
