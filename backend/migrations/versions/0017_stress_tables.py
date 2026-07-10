"""stress_intraday + stress_daily tables

Revision ID: 0017_stress_tables
Revises: 0016_raw_ingest_compression
Create Date: 2026-07-10

Derived intraday stress-proxy timeline and per-day summary (ARCHITECTURE.md
§4.9). Stress is computed from the all-day per-minute heart-rate buckets in
metric_samples (elevation above the personal resting baseline, workouts
excluded, optionally HRV-modulated). It is stored in dedicated tables — never
written back into metric_samples, which stays a replayable mirror of the raw
archive — mirroring the workout_hr_samples precedent. Both are recomputed
idempotently (upsert): the nightly run refreshes a trailing window,
`healthlog rederive-stress --all` the full history.

Plain indexed tables (like workout_hr_samples), not hypertables: the volume is
modest and the sliding-window upsert stays simple. DDL is idempotent (guarded by
an inspector) so a partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_stress_tables"
down_revision: str | None = "0016_raw_ingest_compression"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "stress_intraday" not in existing:
        op.create_table(
            "stress_intraday",
            sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("stress", sa.Integer(), nullable=True),
            sa.Column("hr", sa.Float(), nullable=True),
            sa.Column("state", sa.String(length=16), nullable=False),
        )

    if "stress_daily" not in existing:
        op.create_table(
            "stress_daily",
            sa.Column("day", sa.Date(), primary_key=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("rest_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("low_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("medium_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("high_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unmeasurable_min", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("hrv_z", sa.Float(), nullable=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "stress_daily" in existing:
        op.drop_table("stress_daily")
    if "stress_intraday" in existing:
        op.drop_table("stress_intraday")
