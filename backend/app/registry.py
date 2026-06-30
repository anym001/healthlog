"""Metric registry seed.

The behaviour of a metric (canonical unit, default daily aggregate, category,
tier) is *data*, not code: this dict seeds the ``metric_registry`` table and is
also the in-process source of truth for unit normalisation at ingest time.

Adopting a new metric = adding a row here (or letting the tolerant ingest auto
register a ``secondary`` stub, see ``ingest.py``). No schema migration needed.

Tiers:
  core      -> participates in the correlation/anomaly/trend pipeline by default
  secondary -> captured and queryable, excluded from the default pipeline scan

agg_default: which daily aggregate is meaningful for this metric
  sum -> totals (steps, energy, distance)
  min -> daily minimum (resting heart rate)
  avg -> daily mean (HRV, SpO2, heart rate)
  max -> daily maximum

value_min / value_max (both optional): the plausibility envelope for a single
stored value in the metric's *canonical* unit. The ingest parser drops values
outside it (the raw payload is still archived verbatim, so nothing is lost and a
re-derive can recover them) instead of letting a spurious ``heart_rate = 0`` or a
negative ``step_count`` corrupt the series the nightly analysis runs on. Bounds
are deliberately generous sanity rails — non-negativity for cumulative/count
metrics and wide physiological ranges for vitals — not tight clinical limits,
because a metric's bucket granularity (per-minute vs. daily) varies.

The special metric ``sleep_analysis`` is NOT listed here: it is routed to the
dedicated ``sleep_sessions`` table, not ``metric_samples``.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class MetricSpec(TypedDict):
    display_name: str
    unit_canonical: str
    agg_default: str
    category: str  # activity | sleep | vital | mobility | environment | mindfulness | nutrition
    tier: str  # core | secondary
    # Plausibility envelope (canonical unit); see module docstring. Both optional:
    # a missing bound means "unbounded on that side".
    value_min: NotRequired[float]
    value_max: NotRequired[float]


# Confirmed against real Health Auto Export v2 payloads (curated, not exhaustive:
# unseen metrics are still accepted and auto-registered as secondary stubs).
METRIC_REGISTRY: dict[str, MetricSpec] = {
    # --- core: activity ---------------------------------------------------
    "step_count": {
        "display_name": "Steps",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "active_energy": {
        "display_name": "Active Energy",
        "unit_canonical": "kcal",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "apple_exercise_time": {
        "display_name": "Exercise Time",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "walking_running_distance": {
        "display_name": "Walking + Running Distance",
        "unit_canonical": "km",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "flights_climbed": {
        "display_name": "Flights Climbed",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "physical_effort": {
        "display_name": "Physical Effort",
        "unit_canonical": "kcal/hr·kg",
        "agg_default": "avg",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    "apple_stand_time": {
        "display_name": "Stand Time",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
        "value_min": 0,
    },
    # --- core: sleep ------------------------------------------------------
    "apple_sleeping_wrist_temperature": {
        "display_name": "Sleeping Wrist Temperature",
        "unit_canonical": "degC",
        "agg_default": "avg",
        "category": "sleep",
        "tier": "core",
        "value_min": 20,
        "value_max": 45,
    },
    "breathing_disturbances": {
        "display_name": "Breathing Disturbances",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "sleep",
        "tier": "core",
        "value_min": 0,
    },
    "time_in_daylight": {
        "display_name": "Time in Daylight",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "sleep",
        "tier": "core",
        "value_min": 0,
    },
    # --- core: vital ------------------------------------------------------
    # Recovery vitals: measured mostly overnight by the watch, but they are
    # cardiovascular/respiratory vital signs, so they live under `vital` (not
    # `sleep`). The Sleep dashboard still reads their columns directly.
    "heart_rate_variability": {
        "display_name": "Heart Rate Variability",
        "unit_canonical": "ms",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 0,
        "value_max": 1000,
    },
    "resting_heart_rate": {
        "display_name": "Resting Heart Rate",
        "unit_canonical": "count/min",
        "agg_default": "min",
        "category": "vital",
        "tier": "core",
        "value_min": 20,
        "value_max": 200,
    },
    "respiratory_rate": {
        "display_name": "Respiratory Rate",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 3,
        "value_max": 80,
    },
    "heart_rate": {
        "display_name": "Heart Rate",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 20,
        "value_max": 250,
    },
    "blood_oxygen_saturation": {
        "display_name": "Blood Oxygen Saturation",
        "unit_canonical": "%",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 50,
        "value_max": 100,
    },
    "walking_heart_rate_average": {
        "display_name": "Walking Heart Rate Average",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 20,
        "value_max": 250,
    },
    "vo2_max": {
        "display_name": "VO2 Max",
        "unit_canonical": "ml/(kg·min)",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 5,
        "value_max": 120,
    },
    "weight_body_mass": {
        "display_name": "Body Mass",
        "unit_canonical": "kg",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 2,
        "value_max": 500,
    },
    # --- secondary: mobility ---------------------------------------------
    "walking_speed": {
        "display_name": "Walking Speed",
        "unit_canonical": "km/hr",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "walking_step_length": {
        "display_name": "Walking Step Length",
        "unit_canonical": "cm",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "walking_asymmetry_percentage": {
        "display_name": "Walking Asymmetry",
        "unit_canonical": "%",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "walking_double_support_percentage": {
        "display_name": "Double Support",
        "unit_canonical": "%",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "stair_speed_up": {
        "display_name": "Stair Speed Up",
        "unit_canonical": "m/s",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "stair_speed_down": {
        "display_name": "Stair Speed Down",
        "unit_canonical": "m/s",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    "six_minute_walking_test_distance": {
        "display_name": "Six-Minute Walking Test Distance",
        "unit_canonical": "m",
        "agg_default": "avg",
        "category": "mobility",
        "tier": "secondary",
    },
    # --- secondary: activity / environment -------------------------------
    "basal_energy_burned": {
        "display_name": "Basal Energy",
        "unit_canonical": "kcal",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "apple_stand_hour": {
        "display_name": "Stand Hours",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "environmental_audio_exposure": {
        "display_name": "Environmental Audio Exposure",
        "unit_canonical": "dBASPL",
        "agg_default": "avg",
        "category": "environment",
        "tier": "secondary",
    },
    "headphone_audio_exposure": {
        "display_name": "Headphone Audio Exposure",
        "unit_canonical": "dBASPL",
        "agg_default": "avg",
        "category": "environment",
        "tier": "secondary",
    },
    # --- curated from the first full backfill's auto-registered unknowns ---
    # cardio_recovery (1-minute heart-rate recovery after exercise) is a genuine
    # cardio-fitness marker, so it joins the default pipeline as core.
    "cardio_recovery": {
        "display_name": "Cardio Recovery",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
        "value_min": 0,
        "value_max": 200,
    },
    "waist_circumference": {
        "display_name": "Waist Circumference",
        "unit_canonical": "cm",
        "agg_default": "avg",
        "category": "vital",
        "tier": "secondary",
    },
    "height": {
        "display_name": "Height",
        "unit_canonical": "m",
        "agg_default": "avg",
        "category": "vital",
        "tier": "secondary",
    },
    "atrial_fibrillation_burden": {
        "display_name": "AFib History",
        "unit_canonical": "%",
        "agg_default": "avg",
        "category": "vital",
        "tier": "secondary",
    },
    "swimming_distance": {
        "display_name": "Swimming Distance",
        "unit_canonical": "m",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "cycling_distance": {
        "display_name": "Cycling Distance",
        "unit_canonical": "km",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "swimming_stroke_count": {
        "display_name": "Swimming Strokes",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "handwashing": {
        "display_name": "Handwashing Duration",
        "unit_canonical": "s",
        "agg_default": "sum",
        "category": "activity",
        "tier": "secondary",
    },
    "mindful_minutes": {
        "display_name": "Mindful Minutes",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "mindfulness",
        "tier": "secondary",
    },
    "dietary_water": {
        "display_name": "Water Intake",
        "unit_canonical": "mL",
        "agg_default": "sum",
        "category": "nutrition",
        "tier": "secondary",
    },
}

# Routed to sleep_sessions, never metric_samples.
SLEEP_METRIC = "sleep_analysis"


def value_in_bounds(metric: str, value: float | None) -> bool:
    """True if ``value`` is within the metric's plausibility envelope.

    Pure (no DB): reads the in-process registry, the same source the ingest
    unit-guard uses. Treated as plausible — nothing to reject — when the value is
    ``None``, the metric is unknown, or the relevant bound is unset. Bounds are
    inclusive; the value must already be in the canonical unit (i.e. checked
    after ``units.normalise``). See the module docstring for the rationale.
    """
    if value is None:
        return True
    spec = METRIC_REGISTRY.get(metric)
    if spec is None:
        return True
    low = spec.get("value_min")
    high = spec.get("value_max")
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True
