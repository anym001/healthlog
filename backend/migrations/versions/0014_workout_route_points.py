"""workout_route_points table

Revision ID: 0014_workout_route_points
Revises: 0013_sleep_efficiency_fix
Create Date: 2026-06-30

Intra-workout GPS route (HAE ``route``), one row per recorded location, keyed
by the owning workout and the point timestamp. HAE only attaches this when the
operator enables "Include Route Data" *and* the workout was recorded outdoors
with GPS, so the table is sparse by nature. It is the data behind the Workout
Detail dashboard's geomap. Like ``workout_hr_samples`` the series lives in its
own table (not the cold raw archive) so a dashboard can read it directly, and
(workout_hae_id, ts) is the natural idempotency key. DDL is idempotent (guarded
by an inspector) so a partial/replayed migration is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_workout_route_points"
down_revision: str | None = "0013_sleep_efficiency_fix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workout_route_points" not in inspector.get_table_names():
        op.create_table(
            "workout_route_points",
            sa.Column(
                "workout_hae_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workouts.hae_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lon", sa.Float(), nullable=False),
            sa.Column("altitude_m", sa.Float(), nullable=True),
            sa.Column("speed_mps", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workout_route_points" in inspector.get_table_names():
        op.drop_table("workout_route_points")
