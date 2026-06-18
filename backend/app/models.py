"""SQLAlchemy ORM models.

The data model is deliberately metric-agnostic (see docs/PLAN.md §4.0): a new
metric needs no schema change, only a registry row. ``metric_samples`` carries
the metric name as a column and never gains per-metric columns.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RawIngest(Base):
    """Every incoming HAE payload, verbatim, before parsing (replayable)."""

    __tablename__ = "raw_ingest"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB)
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    # SHA-256 of the raw body; UNIQUE so identical re-posts are deduped.
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)


class MetricSample(Base):
    """One row per metric bucket. Hypertable on ``time`` in production."""

    __tablename__ = "metric_samples"
    __table_args__ = (UniqueConstraint("metric", "time", "source", name="uq_metric_samples"),)

    # No surrogate PK: a Timescale hypertable's unique index must include the
    # partition column. (metric, time, source) is the natural idempotency key.
    time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    # source may be empty or pipe-joined ("Apple Watch …|iPhone …"); never NULL.
    source: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    vmin: Mapped[float | None] = mapped_column(Float, nullable=True)
    vavg: Mapped[float | None] = mapped_column(Float, nullable=True)
    vmax: Mapped[float | None] = mapped_column(Float, nullable=True)
    n: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SleepSession(Base):
    """Sleep is an interval with phases; assigned to the wake-up day."""

    __tablename__ = "sleep_sessions"
    __table_args__ = (UniqueConstraint("sleep_start", "source", name="uq_sleep_sessions"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sleep_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    sleep_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    in_bed_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    in_bed_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(Text, default="")
    # HAE `date`: midnight of the wake-up day. Used to align sleep with the
    # daily metrics of the day you wake up.
    sleep_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    total_sleep_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    deep_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    core_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    rem_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    awake_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    asleep_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_bed_h: Mapped[float | None] = mapped_column(Float, nullable=True)


class Workout(Base):
    """Workout summary keyed by HAE's stable UUID. The intra-workout HR time
    series (when HAE attaches it) lands in ``workout_hr_samples``; other
    intra-workout series stay in the raw archive only."""

    __tablename__ = "workouts"

    hae_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # localised HAE name; normalised to a canonical type at analysis time (workout_types.py)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_indoor: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_energy_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_energy_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    hr_recovery: Mapped[float | None] = mapped_column(Float, nullable=True)
    intensity: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_up_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkoutHrSample(Base):
    """One intra-workout heart-rate sample (HAE ``heartRateData``).

    HAE ships these as ~per-minute buckets ({Min, Avg, Max} with a timestamp);
    we keep the representative ``Avg`` as ``bpm``. (workout_hae_id, ts) is the
    natural idempotency key so a replayed payload upserts rather than
    duplicates. Cascades with its workout. Used at analysis time to compute
    zone-based (Edwards) TRIMP — boundaries depend on HR_max, so they are
    derived per run, never frozen here."""

    __tablename__ = "workout_hr_samples"

    workout_hae_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workouts.hae_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    bpm: Mapped[float] = mapped_column(Float, nullable=False)


class MetricRegistry(Base):
    """Per-metric behaviour as data: canonical unit, daily aggregate, tier."""

    __tablename__ = "metric_registry"

    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)
    agg_default: Mapped[str | None] = mapped_column(String(8), nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tier: Mapped[str] = mapped_column(String(16), default="secondary")
    # True => created automatically by the tolerant ingest, awaiting curation.
    auto_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Finding(Base):
    """A statistical finding from the nightly pipeline (PLAN.md §4.7).

    Written as a fresh snapshot each run (the analysis deletes the previous
    batch). ``kind`` is one of: correlation, anomaly, trend, seasonality,
    recovery_alert, consistency. Fields not relevant to a kind stay NULL.
    """

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(16))
    metric_a: Mapped[str] = mapped_column(Text)
    metric_b: Mapped[str | None] = mapped_column(Text, nullable=True)  # correlation only
    lag_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coefficient: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_value_adj: Mapped[float | None] = mapped_column(Float, nullable=True)  # FDR (Benjamini-Hochberg)
    ref_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # anomaly / recovery_alert day
    window_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    severity: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # kind-specific extras
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
