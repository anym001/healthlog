"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16

Creates the raw archive, the metric-agnostic sample table, sleep + workout
tables and the metric registry. Where TimescaleDB is available the sample
table is turned into a hypertable; on plain Postgres it stays a regular table
(so the test suite runs without the extension). A local-timezone daily view
exposes all aggregates per (day, metric).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Daily buckets are computed in this timezone (matches LOCAL_TZ default).
# Changing LOCAL_TZ later requires recreating the view in a follow-up migration.
LOCAL_TZ = "Europe/Vienna"


def upgrade() -> None:
    op.create_table(
        "raw_ingest",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("source_ip", postgresql.INET(), nullable=True),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_raw_ingest_hash"),
    )

    op.create_table(
        "metric_samples",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default=""),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("vmin", sa.Float(), nullable=True),
        sa.Column("vavg", sa.Float(), nullable=True),
        sa.Column("vmax", sa.Float(), nullable=True),
        sa.Column("n", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("time", "metric", "source"),
        sa.UniqueConstraint("metric", "time", "source", name="uq_metric_samples"),
    )

    op.create_table(
        "sleep_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sleep_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sleep_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_bed_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_bed_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default=""),
        sa.Column("sleep_date", sa.Date(), nullable=True),
        sa.Column("total_sleep_h", sa.Float(), nullable=True),
        sa.Column("deep_h", sa.Float(), nullable=True),
        sa.Column("core_h", sa.Float(), nullable=True),
        sa.Column("rem_h", sa.Float(), nullable=True),
        sa.Column("awake_h", sa.Float(), nullable=True),
        sa.Column("asleep_h", sa.Float(), nullable=True),
        sa.Column("in_bed_h", sa.Float(), nullable=True),
        sa.UniqueConstraint("sleep_start", "source", name="uq_sleep_sessions"),
    )

    op.create_table(
        "workouts",
        sa.Column("hae_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("is_indoor", sa.Boolean(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("total_energy_kcal", sa.Float(), nullable=True),
        sa.Column("active_energy_kcal", sa.Float(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("avg_hr", sa.Float(), nullable=True),
        sa.Column("max_hr", sa.Float(), nullable=True),
        sa.Column("hr_recovery", sa.Float(), nullable=True),
        sa.Column("intensity", sa.Float(), nullable=True),
        sa.Column("elevation_up_m", sa.Float(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
    )

    op.create_table(
        "metric_registry",
        sa.Column("metric", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("unit_canonical", sa.Text(), nullable=True),
        sa.Column("agg_default", sa.String(length=8), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False, server_default="secondary"),
        sa.Column("auto_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- TimescaleDB (optional) ------------------------------------------
    # Check availability first: attempting CREATE EXTENSION when the package is
    # absent errors and would abort the migration transaction. pg_available_
    # extensions always exists; on plain Postgres it simply has no timescaledb.
    conn = op.get_bind()
    available = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).first()
    if available:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        conn.execute(
            sa.text("SELECT create_hypertable('metric_samples', 'time', if_not_exists => TRUE)")
        )
        op.create_index("ix_metric_samples_metric_time", "metric_samples", ["metric", "time"])

    # --- Local-timezone daily aggregates (view) --------------------------
    op.execute(
        sa.text(
            f"""
            CREATE VIEW daily_metrics AS
            SELECT (time AT TIME ZONE '{LOCAL_TZ}')::date AS day,
                   metric,
                   avg(vavg) AS avg,
                   min(vmin) AS vmin,
                   max(vmax) AS vmax,
                   sum(qty)  AS sum,
                   sum(n)    AS n
            FROM metric_samples
            GROUP BY 1, 2
            """
        )
    )

    _seed_registry()


def _seed_registry() -> None:
    from app.registry import METRIC_REGISTRY

    rows = [
        {
            "metric": metric,
            "display_name": spec["display_name"],
            "unit_canonical": spec["unit_canonical"],
            "agg_default": spec["agg_default"],
            "category": spec["category"],
            "tier": spec["tier"],
            "auto_registered": False,
        }
        for metric, spec in METRIC_REGISTRY.items()
    ]
    if not rows:
        return
    registry = sa.table(
        "metric_registry",
        sa.column("metric", sa.Text),
        sa.column("display_name", sa.Text),
        sa.column("unit_canonical", sa.Text),
        sa.column("agg_default", sa.String),
        sa.column("category", sa.String),
        sa.column("tier", sa.String),
        sa.column("auto_registered", sa.Boolean),
    )
    op.bulk_insert(registry, rows)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS daily_metrics")
    op.drop_table("metric_registry")
    op.drop_table("workouts")
    op.drop_table("sleep_sessions")
    op.drop_table("metric_samples")
    op.drop_table("raw_ingest")
