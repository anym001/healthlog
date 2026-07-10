"""SQLAlchemy ORM models.

The data model is deliberately metric-agnostic (see docs/ARCHITECTURE.md §4.0): a new
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
    """Every incoming HAE payload, verbatim, before parsing (replayable).

    A TimescaleDB hypertable on ``received_at`` with a compression policy
    (migration 0016). The database PK is the composite (id, received_at) —
    hypertables need the partition column in every unique index — while the
    ORM keeps ``id`` as the logical key (it stays sequence-generated, hence
    unique in practice)."""

    __tablename__ = "raw_ingest"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB)
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    # SHA-256 of the raw body; indexed (not unique — hypertable) for the
    # SELECT-then-INSERT dedup in archive_raw.
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, index=True)


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
    """Sleep is an interval with phases; assigned to the wake-up day.

    Natural key is ``(sleep_end, source)`` — the awakening identity. Health Auto
    Export's REST API re-captures the same night several times, each starting at
    a later ``sleep_start`` but sharing the same ``sleep_end``; keying on the end
    collapses those re-captures (the most complete one wins at upsert) while
    keeping genuinely distinct sleep periods (e.g. a nap, with a different end)
    separate. NULLS NOT DISTINCT so a rare NULL ``sleep_end`` still de-dupes on
    replay instead of inserting a second row. See migration 0011.
    """

    __tablename__ = "sleep_sessions"
    __table_args__ = (
        UniqueConstraint("sleep_end", "source", name="uq_sleep_sessions", postgresql_nulls_not_distinct=True),
    )

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


class WorkoutTypeGroup(Base):
    """Maps canonical workout type slugs to display groups for Grafana.

    Populated by migration 0008; extend by inserting new rows — no code change
    needed. sort_order controls the series stacking order in bar charts.
    """

    __tablename__ = "workout_type_groups"

    canonical_type: Mapped[str] = mapped_column(Text, primary_key=True)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=99)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workout(Base):
    """Workout summary keyed by HAE's stable UUID. The intra-workout HR time
    series (when HAE attaches it) lands in ``workout_hr_samples``; other
    intra-workout series stay in the raw archive only."""

    __tablename__ = "workouts"

    hae_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Localised HAE name; canonical_type is the resolved slug (workout_types.py).
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_type: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class WorkoutRoutePoint(Base):
    """One intra-workout GPS location (HAE ``route``).

    HAE attaches this only for outdoor GPS workouts when "Include Route Data"
    is enabled, so the table is sparse. (workout_hae_id, ts) is the natural
    idempotency key so a replayed payload upserts rather than duplicates.
    Cascades with its workout. Read directly by the Workout Detail dashboard's
    geomap; not used by the analysis."""

    __tablename__ = "workout_route_points"

    workout_hae_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workouts.hae_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)


class StressIntraday(Base):
    """One intraday stress-proxy bucket (ARCHITECTURE.md §4.9).

    Derived per run from the all-day per-minute heart-rate buckets in
    ``metric_samples`` (elevation above the personal resting baseline, workouts
    excluded, optionally HRV-modulated) — a dedicated table, never written back
    into ``metric_samples`` (which stays a replayable mirror of the raw archive).
    ``ts`` is the bucket time; ``stress`` is 0-100 (NULL when the minute is
    inside a workout or has no HR sample); ``state`` is one of
    rest/low/medium/high/active/unmeasurable. Recomputed idempotently
    (upsert on ``ts``); the nightly run refreshes a trailing window,
    ``rederive-stress --all`` the full history."""

    __tablename__ = "stress_intraday"

    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    stress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(16))


class StressDaily(Base):
    """Per-local-day stress summary (ARCHITECTURE.md §4.9).

    The Garmin-style day view: an overall ``score`` (0-100, time-weighted mean of
    the measured non-active minutes) plus minutes-in-zone. A day with fewer than
    ``stress.min_measured_min`` measured minutes yields no row (a gap, not a
    zero). ``hrv_z`` records the day's HRV modulation input. Upserted on
    ``day``."""

    __tablename__ = "stress_daily"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rest_min: Mapped[int] = mapped_column(Integer, default=0)
    low_min: Mapped[int] = mapped_column(Integer, default=0)
    medium_min: Mapped[int] = mapped_column(Integer, default=0)
    high_min: Mapped[int] = mapped_column(Integer, default=0)
    active_min: Mapped[int] = mapped_column(Integer, default=0)
    unmeasurable_min: Mapped[int] = mapped_column(Integer, default=0)
    hrv_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BodyBatteryIntraday(Base):
    """One intraday Body-Battery bucket (ARCHITECTURE.md §4.10).

    Derived per run by integrating the ``stress_intraday`` timeline against
    recovery: stress and workouts drain the battery, calm rest and sleep charge
    it, clamped to 0-100. A dedicated table (like ``stress_intraday``), never
    written back into ``metric_samples``. ``ts`` is the bucket time; ``level``
    is the 0-100 reserve. Recomputed idempotently (upsert on ``ts``); the nightly
    run refreshes a trailing window, ``rederive-body-battery --all`` the full
    history."""

    __tablename__ = "body_battery_intraday"

    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BodyBatteryDaily(Base):
    """Per-local-day Body-Battery summary (ARCHITECTURE.md §4.10).

    Mirrors Garmin's day view: ``wake_level`` (battery at the end of the main
    sleep — what you started the day with), ``high_level`` / ``low_level`` (the
    day's peak and trough), ``charged`` / ``drained`` (total points gained / lost
    over the day). Upserted on ``day``."""

    __tablename__ = "body_battery_daily"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    wake_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    high_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    low_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charged: Mapped[float] = mapped_column(Float, default=0.0)
    drained: Mapped[float] = mapped_column(Float, default=0.0)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class _FindingColumns:
    """Columns shared by the current snapshot (``findings``) and the
    append-only archive (``findings_history``); declared once so the two
    tables cannot drift."""

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


# Shared column names, in order, for the snapshot -> history INSERT..SELECT.
FINDING_FIELDS: tuple[str, ...] = (
    "computed_at",
    "kind",
    "metric_a",
    "metric_b",
    "lag_days",
    "coefficient",
    "p_value",
    "p_value_adj",
    "ref_date",
    "window_start",
    "window_end",
    "severity",
    "details",
    "note",
)


class Finding(_FindingColumns, Base):
    """A statistical finding from the nightly pipeline (ARCHITECTURE.md §4.8).

    Written as a fresh snapshot each run (the analysis deletes the previous
    batch). ``kind`` is one of: correlation, anomaly, trend, seasonality,
    recovery_alert, consistency, training_load, stress, body_battery. Fields not
    relevant to a kind stay NULL.
    """

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class FindingHistory(_FindingColumns, Base):
    """Append-only archive of every findings snapshot (ARCHITECTURE.md §4.8).

    The nightly run copies its fresh snapshot here before the next run
    replaces ``findings``, so questions over time ("since when has the ACWR
    been warning?", "how many recovery alerts this month?") stay answerable.
    Rows share one ``computed_at`` per run — that timestamp is the run key.
    """

    __tablename__ = "findings_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
