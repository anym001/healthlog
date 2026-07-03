"""findings_history table

Revision ID: 0015_findings_history
Revises: 0014_workout_route_points
Create Date: 2026-07-02

Append-only archive of every nightly findings snapshot (ARCHITECTURE.md §4.8).
`findings` stays a replace-per-run snapshot (all existing consumers keep
working unchanged); each run additionally copies its snapshot here so findings
remain queryable over time. DDL is idempotent (guarded by an inspector) so a
partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_findings_history"
down_revision: str | None = "0014_workout_route_points"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "findings_history" not in inspector.get_table_names():
        op.create_table(
            "findings_history",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("metric_a", sa.Text(), nullable=False),
            sa.Column("metric_b", sa.Text(), nullable=True),
            sa.Column("lag_days", sa.Integer(), nullable=True),
            sa.Column("coefficient", sa.Float(), nullable=True),
            sa.Column("p_value", sa.Float(), nullable=True),
            sa.Column("p_value_adj", sa.Float(), nullable=True),
            sa.Column("ref_date", sa.Date(), nullable=True),
            sa.Column("window_start", sa.Date(), nullable=True),
            sa.Column("window_end", sa.Date(), nullable=True),
            sa.Column("severity", sa.Float(), nullable=True),
            sa.Column("details", postgresql.JSONB(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
        )

    inspector = sa.inspect(bind)
    existing = {ix["name"] for ix in inspector.get_indexes("findings_history")}
    # computed_at is the run key (one timestamp per run); kind narrows the
    # typical "alert kinds over time" query.
    if "ix_findings_history_computed_at" not in existing:
        op.create_index("ix_findings_history_computed_at", "findings_history", ["computed_at"])
    if "ix_findings_history_kind" not in existing:
        op.create_index("ix_findings_history_kind", "findings_history", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_findings_history_kind", table_name="findings_history")
    op.drop_index("ix_findings_history_computed_at", table_name="findings_history")
    op.drop_table("findings_history")
