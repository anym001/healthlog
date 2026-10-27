"""Pydantic response schemas. The ingest *request* body is the raw HAE JSON
(loose, version-dependent) and is handled as a dict, not a strict model."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "dev"  # release tag baked into the image; "dev" from source


class IngestResponse(BaseModel):
    status: str  # "stored" | "duplicate"
    metric_rows: int = 0
    sleep_rows: int = 0
    workout_rows: int = 0
    unknown_metrics: int = 0
    flagged_units: int = 0  # values whose unit deviated with no known conversion
    implausible_values: int = 0  # values dropped for failing the plausibility envelope
    dropped_workouts: int = 0  # workouts dropped for a missing/unparseable id
    dropped_sleep: int = 0  # sleep points/segments that could not become a night
    state_of_mind_rows: int = 0
    dropped_state_of_mind: int = 0  # State of Mind entries dropped for a missing id/start
    symptom_rows: int = 0
    medication_rows: int = 0
    heart_notification_rows: int = 0
    ecg_rows: int = 0
    dropped_entries: int = 0  # symptom/medication/heart entries dropped for a missing name/start
    metric_new: int = 0
    sleep_new: int = 0
    workout_new: int = 0
    state_of_mind_new: int = 0
    symptom_new: int = 0
    medication_new: int = 0
    heart_notification_new: int = 0
    ecg_new: int = 0
