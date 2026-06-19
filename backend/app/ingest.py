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

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import literal_column

from . import units
from .models import MetricRegistry, MetricSample, RawIngest, SleepSession, Workout, WorkoutHrSample
from .registry import METRIC_REGISTRY, SLEEP_METRIC
from .timeutil import parse_hae_datetime
from .workout_types import canonical_workout_type

log = logging.getLogger("healthlog.ingest")


@dataclass
class ParsedPayload:
    metric_rows: list[dict] = field(default_factory=list)
    sleep_rows: list[dict] = field(default_factory=list)
    workout_rows: list[dict] = field(default_factory=list)
    # Intra-workout HR samples (heartRateData) -> workout_hr_samples.
    workout_hr_rows: list[dict] = field(default_factory=list)
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


def _hr_sample_bpm(point: dict) -> float | None:
    """Representative HR of one heartRateData sample.

    HAE ships per-minute buckets ({Min, Avg, Max}); the ``Avg`` is the
    representative rate. A bare ``{qty}`` shape is tolerated as a fallback.
    """
    if not isinstance(point, dict):
        return None
    return _num(point.get("Avg")) if point.get("Avg") is not None else _num(point.get("qty"))


def _parse_workout(w: dict, out: ParsedPayload, type_map: dict[str, str] | None = None) -> None:
    raw_id = w.get("id")
    if not raw_id:
        return
    try:
        hae_id = uuid.UUID(str(raw_id))
    except ValueError:
        return

    # Intra-workout HR time series (only sometimes present). Each usable sample
    # (timestamp + rate) becomes one workout_hr_samples row; (hae_id, ts) keeps
    # it idempotent across replayed payloads.
    hr_series = w.get("heartRateData")
    if isinstance(hr_series, list):
        for point in hr_series:
            if not isinstance(point, dict):
                continue
            try:
                ts = parse_hae_datetime(point.get("date"))
            except ValueError:
                ts = None  # a single malformed sample must not abort the payload
            bpm = _hr_sample_bpm(point)
            if ts is not None and bpm is not None:
                out.workout_hr_rows.append({"workout_hae_id": hae_id, "ts": ts, "bpm": bpm})

    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    name = w.get("name")
    out.workout_rows.append(
        {
            "hae_id": hae_id,
            "start_time": parse_hae_datetime(w.get("start")),
            "end_time": parse_hae_datetime(w.get("end")),
            "name": name,
            "canonical_type": canonical_workout_type(name, type_map),
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


def parse_payload(payload: dict, type_map: dict[str, str] | None = None) -> ParsedPayload:
    """Translate an HAE payload into normalised rows. Pure, DB-free.

    ``type_map`` is the operator's ``workouts.type_map`` from config.yaml:
    it maps custom localised workout names to canonical type slugs and is
    layered on top of the built-in map (config wins, built-in is the fallback).
    Pass ``None`` to use the built-in map only.
    """
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
        _parse_workout(w, out, type_map)

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
    workout_hr_rows: int = 0
    unknown_metrics: int = 0
    # New vs. updated counts from the Postgres xmax trick: xmax=0 means the
    # row was freshly inserted; xmax!=0 means an existing row was updated (i.e.
    # the payload contained data that was already in the DB).
    metric_new: int = 0
    sleep_new: int = 0
    workout_new: int = 0


# Postgres caps a single statement at 65535 bound parameters; a metric row has
# 9 columns, so a multi-year backfill (100k+ rows) must be inserted in chunks.
_METRIC_INSERT_BATCH = 2000


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _dedupe_metric_rows(rows: list[dict]) -> list[dict]:
    """Last-wins de-dup on the (metric, time, source) key so a single INSERT
    never upserts the same row twice ('ON CONFLICT ... affect row a second time')."""
    by_key: dict[tuple, dict] = {}
    for r in rows:
        by_key[(r["metric"], r["time"], r["source"])] = r
    return list(by_key.values())


def _dedupe_workout_hr_rows(rows: list[dict]) -> list[dict]:
    """Last-wins de-dup on (workout_hae_id, ts) — the natural PK — so a single
    INSERT never touches the same conflict target twice."""
    by_key: dict[tuple, dict] = {}
    for r in rows:
        by_key[(r["workout_hae_id"], r["ts"])] = r
    return list(by_key.values())


def store_workout_hr_samples(db: Session, rows: list[dict]) -> int:
    """Idempotent upsert of intra-workout HR samples. The owning workout must
    already be persisted (FK); within ``store`` this runs after the workout
    upsert. Returns the number of rows submitted. Shared with the re-derive CLI,
    which replays the raw archive into this table."""
    deduped = _dedupe_workout_hr_rows(rows)
    for chunk in _chunked(deduped, _METRIC_INSERT_BATCH):
        stmt = pg_insert(WorkoutHrSample).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["workout_hae_id", "ts"],
            set_={"bpm": stmt.excluded.bpm},
        )
        db.execute(stmt)
    return len(deduped)


_XMAX_IS_NEW = literal_column("(xmax = 0)")


def _count_new(cursor) -> int:
    """Count rows where xmax=0 (fresh insert, not a conflict-update)."""
    return sum(1 for (is_new,) in cursor if is_new)


def store(db: Session, parsed: ParsedPayload) -> StoreResult:
    """Idempotent upsert of parsed rows. Safe to replay overlapping windows.

    Uses the Postgres xmax trick (RETURNING xmax=0) to distinguish freshly
    inserted rows from conflict-updates so the caller can report new vs. updated
    counts in notifications.
    """
    if parsed.unknown_metrics:
        _auto_register(db, parsed.unknown_metrics)

    metric_new = 0
    for chunk in _chunked(_dedupe_metric_rows(parsed.metric_rows), _METRIC_INSERT_BATCH):
        stmt = pg_insert(MetricSample).values(chunk)
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
        ).returning(_XMAX_IS_NEW)
        metric_new += _count_new(db.execute(stmt))

    sleep_new = 0
    for row in parsed.sleep_rows:
        stmt = pg_insert(SleepSession).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_sleep_sessions",
            set_={k: getattr(stmt.excluded, k) for k in row if k not in ("sleep_start", "source")},
        ).returning(_XMAX_IS_NEW)
        sleep_new += _count_new(db.execute(stmt))

    workout_new = 0
    for row in parsed.workout_rows:
        stmt = pg_insert(Workout).values(**row)
        # canonical_type is only updated when the payload resolves it; a NULL
        # in a new payload must not overwrite an already-resolved value.
        update_set = {k: getattr(stmt.excluded, k) for k in row if k != "hae_id"}
        if "canonical_type" in update_set:
            update_set["canonical_type"] = sa.func.coalesce(
                stmt.excluded.canonical_type, Workout.__table__.c.canonical_type
            )
        stmt = stmt.on_conflict_do_update(
            index_elements=["hae_id"],
            set_=update_set,
        ).returning(_XMAX_IS_NEW)
        workout_new += _count_new(db.execute(stmt))

    # After the workouts (FK target) exist in this transaction.
    store_workout_hr_samples(db, parsed.workout_hr_rows)

    for metric, unit in parsed.flagged_units:
        log.warning("unit mismatch for %s: incoming '%s' kept as-is (no known conversion)", metric, unit)

    return StoreResult(
        metric_rows=len(parsed.metric_rows),
        sleep_rows=len(parsed.sleep_rows),
        workout_rows=len(parsed.workout_rows),
        workout_hr_rows=len(_dedupe_workout_hr_rows(parsed.workout_hr_rows)),
        unknown_metrics=len(parsed.unknown_metrics),
        metric_new=metric_new,
        sleep_new=sleep_new,
        workout_new=workout_new,
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
