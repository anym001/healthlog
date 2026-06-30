"""Pydantic response schemas. The ingest *request* body is the raw HAE JSON
(loose, version-dependent) and is handled as a dict, not a strict model."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"


class IngestResponse(BaseModel):
    status: str  # "stored" | "duplicate"
    metric_rows: int = 0
    sleep_rows: int = 0
    workout_rows: int = 0
    unknown_metrics: int = 0
    flagged_units: int = 0  # values whose unit deviated with no known conversion
    implausible_values: int = 0  # values dropped for failing the plausibility envelope
    metric_new: int = 0
    sleep_new: int = 0
    workout_new: int = 0
