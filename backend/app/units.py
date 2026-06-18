"""Unit guard: normalise an incoming value to the metric's canonical unit.

HAE ships a unit string per value and can localise units. The real export
delivers energy in ``kJ`` (not kcal), so we never trust the incoming unit
blindly: known conversions are applied, an unknown mismatch is flagged (and
stored with its original unit) rather than silently mis-recorded.

Pure functions, unit-tested without a database.
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import METRIC_REGISTRY

# Exact conversion factors keyed by (from_unit, to_unit).
_KCAL_PER_KJ = 0.2390057361
_CONVERSIONS: dict[tuple[str, str], float] = {
    ("kJ", "kcal"): _KCAL_PER_KJ,
    ("kcal", "kJ"): 1.0 / _KCAL_PER_KJ,
}


@dataclass(frozen=True)
class Normalised:
    value: float | None
    unit: str
    flagged: bool  # True => unit deviated and no conversion was known


def normalise(metric: str, incoming_unit: str | None, value: float | None) -> Normalised:
    """Return the value expressed in the metric's canonical unit.

    - Unknown metric: pass through unchanged (the caller auto-registers a stub
      whose canonical unit becomes this incoming unit).
    - Matching unit: pass through.
    - Known conversion: convert.
    - Unknown mismatch: keep original unit, mark ``flagged`` so the caller can
      log/surface it instead of corrupting the series.
    """
    spec = METRIC_REGISTRY.get(metric)
    if spec is None:
        return Normalised(value, incoming_unit or "", flagged=False)

    canonical = spec["unit_canonical"]
    if incoming_unit is None or incoming_unit == canonical:
        return Normalised(value, canonical, flagged=False)

    factor = _CONVERSIONS.get((incoming_unit, canonical))
    if factor is not None:
        converted = None if value is None else value * factor
        return Normalised(converted, canonical, flagged=False)

    return Normalised(value, incoming_unit, flagged=True)


def convert(value: float, from_unit: str, to_unit: str) -> float | None:
    """Direct conversion helper; None if the pair is unknown."""
    if from_unit == to_unit:
        return value
    factor = _CONVERSIONS.get((from_unit, to_unit))
    return None if factor is None else value * factor
