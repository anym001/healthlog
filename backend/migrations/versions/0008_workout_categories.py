"""Workout type groups table and canonical_type column on workouts.

Creates ``workout_type_groups``: a look-up table that maps each canonical type
slug (running, cycling, …) to a Grafana-friendly display group (Cardio,
Cycling, …) with a stable sort order. The table is the single source of truth
for how workouts are grouped in Grafana, decoupling dashboards from fragile
ILIKE patterns.

Adds ``canonical_type TEXT`` to ``workouts`` and back-fills it for all existing
rows by calling the same ``canonical_workout_type()`` resolver used at ingest
time, so historical data is available immediately after the migration.

An index on ``workouts(canonical_type)`` is added as well to support the GROUP
BY in the "Load by Sport" dashboard query.

Extensibility guide:
  • New workout type: add a row to BUILTIN_WORKOUT_TYPES in workout_types.py
    and insert a row into workout_type_groups (or update this migration's seed).
  • New display group: insert a row into workout_type_groups with a new
    group_name; sort_order controls the series stacking order in Grafana.
  • Operator overrides: workouts.type_map in config.yaml maps custom localised
    names to canonical slugs at ingest time — no migration needed.

Revision ID: 0008_workout_categories
Revises: 0007_indexes
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_workout_categories"
down_revision: str | None = "0007_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (canonical_type, group_name, sort_order)
# sort_order controls series stacking order in Grafana — lower = bottom of stack.
# Add new canonical types here; the group_name appears as the series label.
_GROUPS: list[tuple[str, str, int]] = [
    ("running", "Cardio", 10),
    ("elliptical", "Cardio", 11),
    ("rowing", "Cardio", 12),
    ("hiit", "Cardio", 13),
    ("mixed_cardio", "Cardio", 14),
    ("stair_stepper", "Cardio", 15),
    ("walking", "Walking", 20),
    ("hiking", "Walking", 21),
    ("cycling", "Cycling", 30),
    ("strength", "Strength", 40),
    ("core", "Strength", 41),
    ("swimming", "Swimming", 50),
    ("yoga", "Mind & Body", 60),
    ("pilates", "Mind & Body", 61),
    ("dance", "Mind & Body", 62),
    ("cooldown", "Mind & Body", 63),
]

_wtg = sa.table(
    "workout_type_groups",
    sa.column("canonical_type", sa.Text),
    sa.column("group_name", sa.Text),
    sa.column("sort_order", sa.Integer),
)

_workouts = sa.table(
    "workouts",
    sa.column("hae_id", sa.Text),
    sa.column("name", sa.Text),
    sa.column("canonical_type", sa.Text),
)


def upgrade() -> None:
    op.create_table(
        "workout_type_groups",
        sa.Column("canonical_type", sa.Text, primary_key=True),
        sa.Column("group_name", sa.Text, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="99"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    bind = op.get_bind()
    for canonical_type, group_name, sort_order in _GROUPS:
        bind.execute(
            _wtg.insert().values(
                canonical_type=canonical_type,
                group_name=group_name,
                sort_order=sort_order,
            )
        )

    op.add_column("workouts", sa.Column("canonical_type", sa.Text, nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workouts_canonical_type "
        "ON workouts (canonical_type);"
    )

    # Back-fill canonical_type for all existing workout rows using the same
    # Python resolver that the ingest pipeline uses going forward.
    from app.workout_types import canonical_workout_type

    rows = bind.execute(sa.select(_workouts.c.hae_id, _workouts.c.name)).fetchall()
    for hae_id, name in rows:
        ct = canonical_workout_type(name)
        if ct is not None:
            bind.execute(
                _workouts.update()
                .where(_workouts.c.hae_id == hae_id)
                .values(canonical_type=ct)
            )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_workouts_canonical_type;")
    op.drop_column("workouts", "canonical_type")
    op.drop_table("workout_type_groups")
