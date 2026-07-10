"""body_battery_intraday + body_battery_daily tables

Revision ID: 0018_body_battery_tables
Revises: 0017_stress_tables
Create Date: 2026-07-10

Derived Body-Battery (energy-reserve) timeline and per-day summary
(ARCHITECTURE.md §4.10). Body Battery integrates the intraday stress-proxy
timeline (stress_intraday) against recovery: stress and workouts drain it, calm
rest and sleep charge it, clamped to 0-100. Stored in dedicated tables — never
written back into metric_samples, which stays a replayable mirror of the raw
archive — mirroring the stress_intraday precedent. Both are recomputed
idempotently (upsert): the nightly run refreshes a trailing window,
`healthlog rederive-body-battery --all` the full history.

Plain indexed tables (like stress_intraday), not hypertables: the volume is
modest and the sliding-window upsert stays simple. DDL is idempotent (guarded by
an inspector) so a partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_body_battery_tables"
down_revision: str | None = "0017_stress_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "body_battery_intraday" not in existing:
        op.create_table(
            "body_battery_intraday",
            sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("level", sa.Integer(), nullable=True),
        )

    if "body_battery_daily" not in existing:
        op.create_table(
            "body_battery_daily",
            sa.Column("day", sa.Date(), primary_key=True),
            sa.Column("wake_level", sa.Integer(), nullable=True),
            sa.Column("high_level", sa.Integer(), nullable=True),
            sa.Column("low_level", sa.Integer(), nullable=True),
            sa.Column("charged", sa.Float(), nullable=False, server_default="0"),
            sa.Column("drained", sa.Float(), nullable=False, server_default="0"),
            sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "body_battery_daily" in existing:
        op.drop_table("body_battery_daily")
    if "body_battery_intraday" in existing:
        op.drop_table("body_battery_intraday")
