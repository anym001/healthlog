"""workout_hr_samples table

Revision ID: 0004_workout_hr_samples
Revises: 0003_curate_metrics
Create Date: 2026-06-17

Intra-workout heart-rate time series (HAE ``heartRateData``), one row per
sample, keyed by the owning workout and the sample timestamp. This is the
prerequisite for zone-based (Edwards) TRIMP: the per-workout summary only keeps
``heartRate {min, avg, max}``, which cannot reveal time-in-zone. The series
stays in this dedicated table (not the cold raw archive) so the analysis reads
it directly; zone boundaries are computed at analysis time (they depend on the
config-/data-driven HR_max), never frozen here. DDL is idempotent (guarded by
an inspector) so a partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_workout_hr_samples"
down_revision: str | None = "0003_curate_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workout_hr_samples" not in inspector.get_table_names():
        op.create_table(
            "workout_hr_samples",
            sa.Column(
                "workout_hae_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workouts.hae_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("bpm", sa.Float(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workout_hr_samples" in inspector.get_table_names():
        op.drop_table("workout_hr_samples")
