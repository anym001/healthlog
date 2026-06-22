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

The special metric ``sleep_analysis`` is NOT listed here: it is routed to the
dedicated ``sleep_sessions`` table, not ``metric_samples``.
"""

from __future__ import annotations

from typing import TypedDict


class MetricSpec(TypedDict):
    display_name: str
    unit_canonical: str
    agg_default: str
    category: str  # activity | sleep | vital | mobility | environment | mindfulness | nutrition
    tier: str  # core | secondary


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
    },
    "active_energy": {
        "display_name": "Active Energy",
        "unit_canonical": "kcal",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
    },
    "apple_exercise_time": {
        "display_name": "Exercise Time",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
    },
    "walking_running_distance": {
        "display_name": "Walking + Running Distance",
        "unit_canonical": "km",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
    },
    "flights_climbed": {
        "display_name": "Flights Climbed",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
    },
    "physical_effort": {
        "display_name": "Physical Effort",
        "unit_canonical": "kcal/hr·kg",
        "agg_default": "avg",
        "category": "activity",
        "tier": "core",
    },
    "apple_stand_time": {
        "display_name": "Stand Time",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "activity",
        "tier": "core",
    },
    # --- core: sleep ------------------------------------------------------
    "apple_sleeping_wrist_temperature": {
        "display_name": "Sleeping Wrist Temperature",
        "unit_canonical": "degC",
        "agg_default": "avg",
        "category": "sleep",
        "tier": "core",
    },
    "breathing_disturbances": {
        "display_name": "Breathing Disturbances",
        "unit_canonical": "count",
        "agg_default": "sum",
        "category": "sleep",
        "tier": "core",
    },
    "time_in_daylight": {
        "display_name": "Time in Daylight",
        "unit_canonical": "min",
        "agg_default": "sum",
        "category": "sleep",
        "tier": "core",
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
    },
    "resting_heart_rate": {
        "display_name": "Resting Heart Rate",
        "unit_canonical": "count/min",
        "agg_default": "min",
        "category": "vital",
        "tier": "core",
    },
    "respiratory_rate": {
        "display_name": "Respiratory Rate",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
    },
    "heart_rate": {
        "display_name": "Heart Rate",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
    },
    "blood_oxygen_saturation": {
        "display_name": "Blood Oxygen Saturation",
        "unit_canonical": "%",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
    },
    "walking_heart_rate_average": {
        "display_name": "Walking Heart Rate Average",
        "unit_canonical": "count/min",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
    },
    "vo2_max": {
        "display_name": "VO2 Max",
        "unit_canonical": "ml/(kg·min)",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
    },
    "weight_body_mass": {
        "display_name": "Body Mass",
        "unit_canonical": "kg",
        "agg_default": "avg",
        "category": "vital",
        "tier": "core",
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
