"""HAE payload parser + idempotent storage.

``parse_payload`` is a pure function (no DB) returning normalised rows, so it
is unit-tested directly. ``store`` performs idempotent upserts via Postgres
``ON CONFLICT``. Unknown metrics are accepted, never rejected: they are stored
and auto-registered as ``secondary`` stubs (PLAN.md §4.0/§5).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import units
from .models import MetricRegistry, MetricSample, RawIngest, SleepSession, Workout
from .registry import METRIC_REGISTRY, SLEEP_METRIC
from .timeutil import parse_hae_datetime

log = logging.getLogger("healthlog.ingest")


@dataclass
class ParsedPayload:
    metric_rows: list[dict] = field(default_factory=list)
    sleep_rows: list[dict] = field(default_factory=list)
    workout_rows: list[dict] = field(default_factory=list)
    # Metrics seen in the payload that are absent from the registry seed.
    unknown_metrics: dict[str, str] = field(default_factory=dict)  # metric -> unit
    flagged_units: list[tuple[str, str]] = field(default_factory=list)  # (metric, unit)


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_metric(name: str, payload_unit: str | None, point: dict, out: ParsedPayload) -> None:
    ts = parse_hae_datetime(point.get("date"))
    if ts is None:
        return
    source = point.get("source") or ""

    # Two shapes: aggregated {Min,Avg,Max} (only heart_rate in practice) or
    # a single {qty}. Fill whichever the payload provides.
    has_minmax = any(k in point for k in ("Min", "Avg", "Max"))
    raw_unit = point.get("units") or payload_unit

    if has_minmax:
        norm_avg = units.normalise(name, raw_unit, _num(point.get("Avg")))
        if norm_avg.flagged:
            out.flagged_units.append((name, raw_unit or ""))
        out.metric_rows.append(
            {
                "time": ts,
                "metric": name,
                "source": source,
                "unit": norm_avg.unit,
                "qty": None,
                "vmin": units.normalise(name, raw_unit, _num(point.get("Min"))).value,
                "vavg": norm_avg.value,
                "vmax": units.normalise(name, raw_unit, _num(point.get("Max"))).value,
                "n": None,
            }
        )
    else:
        norm = units.normalise(name, raw_unit, _num(point.get("qty")))
        if norm.flagged:
            out.flagged_units.append((name, raw_unit or ""))
        out.metric_rows.append(
            {
                "time": ts,
                "metric": name,
                "source": source,
                "unit": norm.unit,
                "qty": norm.value,
                "vmin": None,
                "vavg": None,
                "vmax": None,
                "n": None,
            }
        )

    if name not in METRIC_REGISTRY:
        out.unknown_metrics.setdefault(name, raw_unit or "")


def _parse_sleep(point: dict, out: ParsedPayload) -> None:
    start = parse_hae_datetime(point.get("sleepStart"))
    if start is None:
        return
    date_field = parse_hae_datetime(point.get("date"))
    out.sleep_rows.append(
        {
            "sleep_start": start,
            "sleep_end": parse_hae_datetime(point.get("sleepEnd")),
            "in_bed_start": parse_hae_datetime(point.get("inBedStart")),
            "in_bed_end": parse_hae_datetime(point.get("inBedEnd")),
            "source": point.get("source") or "",
            "sleep_date": date_field.date() if date_field else None,
            "total_sleep_h": _num(point.get("totalSleep")),
            "deep_h": _num(point.get("deep")),
            "core_h": _num(point.get("core")),
            "rem_h": _num(point.get("rem")),
            "awake_h": _num(point.get("awake")),
            "asleep_h": _num(point.get("asleep")),
            "in_bed_h": _num(point.get("inBed")),
        }
    )


def _qty_of(obj) -> float | None:
    """HAE scalars come as {qty, units} dicts (or sometimes bare numbers)."""
    if isinstance(obj, dict):
        return _num(obj.get("qty"))
    return _num(obj)


def _energy_kcal(obj) -> float | None:
    """Workout energy ships in kJ; convert to the canonical kcal."""
    if not isinstance(obj, dict):
        return _num(obj)
    qty = _num(obj.get("qty"))
    if qty is None:
        return None
    unit = obj.get("units")
    if unit and unit != "kcal":
        converted = units.convert(qty, unit, "kcal")
        return converted if converted is not None else qty
    return qty


def _parse_workout(w: dict, out: ParsedPayload) -> None:
    raw_id = w.get("id")
    if not raw_id:
        return
    try:
        hae_id = uuid.UUID(str(raw_id))
    except ValueError:
        return
    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    out.workout_rows.append(
        {
            "hae_id": hae_id,
            "start_time": parse_hae_datetime(w.get("start")),
            "end_time": parse_hae_datetime(w.get("end")),
            "name": w.get("name"),
            "location": w.get("location"),
            "is_indoor": w.get("isIndoor"),
            "duration_s": _num(w.get("duration")),
            "total_energy_kcal": _energy_kcal(w.get("totalEnergy")),
            "active_energy_kcal": _energy_kcal(w.get("activeEnergyBurned")),
            "distance_km": _qty_of(w.get("distance")),
            "avg_hr": _qty_of(hr.get("avg")) if hr else _qty_of(w.get("avgHeartRate")),
            "max_hr": _qty_of(hr.get("max")) if hr else _qty_of(w.get("maxHeartRate")),
            "hr_recovery": _qty_of(w.get("heartRateRecovery"))
            if isinstance(w.get("heartRateRecovery"), dict)
            else None,
            "intensity": _qty_of(w.get("intensity")),
            "elevation_up_m": _qty_of(w.get("elevationUp")),
            "temperature_c": _qty_of(w.get("temperature")),
            "humidity_pct": _qty_of(w.get("humidity")),
            "source": w.get("source"),
        }
    )


def parse_payload(payload: dict) -> ParsedPayload:
    """Translate an HAE payload into normalised rows. Pure, DB-free."""
    out = ParsedPayload()
    data = payload.get("data") or {}

    for metric in data.get("metrics") or []:
        name = metric.get("name")
        if not name:
            continue
        unit = metric.get("units")
        points = metric.get("data") or []
        if name == SLEEP_METRIC:
            for point in points:
                _parse_sleep(point, out)
            continue
        for point in points:
            _parse_metric(name, unit, point, out)

    for w in data.get("workouts") or []:
        _parse_workout(w, out)

    return out


def _auto_register(db: Session, unknown: dict[str, str]) -> None:
    """Insert a secondary stub for each unknown metric (idempotent)."""
    for metric, unit in unknown.items():
        stmt = (
            pg_insert(MetricRegistry)
            .values(
                metric=metric,
                display_name=metric,
                unit_canonical=unit or None,
                agg_default="sum",
                category="unknown",
                tier="secondary",
                auto_registered=True,
            )
            .on_conflict_do_nothing(index_elements=["metric"])
        )
        db.execute(stmt)
        log.info("auto-registered unknown metric: %s (unit=%s)", metric, unit or "?")


@dataclass
class StoreResult:
    metric_rows: int = 0
    sleep_rows: int = 0
    workout_rows: int = 0
    unknown_metrics: int = 0


def store(db: Session, parsed: ParsedPayload) -> StoreResult:
    """Idempotent upsert of parsed rows. Safe to replay overlapping windows."""
    if parsed.unknown_metrics:
        _auto_register(db, parsed.unknown_metrics)

    if parsed.metric_rows:
        stmt = pg_insert(MetricSample).values(parsed.metric_rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_metric_samples",
            set_={
                "unit": stmt.excluded.unit,
                "qty": stmt.excluded.qty,
                "vmin": stmt.excluded.vmin,
                "vavg": stmt.excluded.vavg,
                "vmax": stmt.excluded.vmax,
                "n": stmt.excluded.n,
            },
        )
        db.execute(stmt)

    for row in parsed.sleep_rows:
        stmt = pg_insert(SleepSession).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_sleep_sessions",
            set_={k: getattr(stmt.excluded, k) for k in row if k not in ("sleep_start", "source")},
        )
        db.execute(stmt)

    for row in parsed.workout_rows:
        stmt = pg_insert(Workout).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["hae_id"],
            set_={k: getattr(stmt.excluded, k) for k in row if k != "hae_id"},
        )
        db.execute(stmt)

    for metric, unit in parsed.flagged_units:
        log.warning("unit mismatch for %s: incoming '%s' kept as-is (no known conversion)", metric, unit)

    return StoreResult(
        metric_rows=len(parsed.metric_rows),
        sleep_rows=len(parsed.sleep_rows),
        workout_rows=len(parsed.workout_rows),
        unknown_metrics=len(parsed.unknown_metrics),
    )


def archive_raw(db: Session, payload: dict, content_hash: bytes, source_ip: str | None) -> bool:
    """Insert the verbatim payload. Returns False if it's a duplicate
    (content_hash already present), in which case parsing should be skipped."""
    stmt = (
        pg_insert(RawIngest)
        .values(payload=payload, content_hash=content_hash, source_ip=source_ip)
        .on_conflict_do_nothing(index_elements=["content_hash"])
        .returning(RawIngest.id)
    )
    result = db.execute(stmt).first()
    return result is not None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
