"""findings table

Revision ID: 0002_findings
Revises: 0001_initial
Create Date: 2026-06-16

Holds the nightly statistical findings (ARCHITECTURE.md §4.8). Written as a fresh
snapshot each analysis run. DDL is idempotent (guarded by an inspector) so a
partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_findings"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "findings" not in inspector.get_table_names():
        op.create_table(
            "findings",
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

    existing = {ix["name"] for ix in inspector.get_indexes("findings")} if "findings" in inspector.get_table_names() else set()
    if "ix_findings_kind" not in existing:
        op.create_index("ix_findings_kind", "findings", ["kind"])
    if "ix_findings_ref_date" not in existing:
        op.create_index("ix_findings_ref_date", "findings", ["ref_date"])


def downgrade() -> None:
    op.drop_index("ix_findings_ref_date", table_name="findings")
    op.drop_index("ix_findings_kind", table_name="findings")
    op.drop_table("findings")
