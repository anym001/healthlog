"""state_of_mind table

Revision ID: 0023_state_of_mind
Revises: 0022_app_settings_local_tz
Create Date: 2026-10-17

Apple Health "State of Mind" entries (HAE ``data.stateOfMind``): momentary
emotions and daily moods logged on the iPhone or Watch. Each entry carries a
stable UUID, a valence in [-1, 1] with Apple's seven-step classification, and
free label/association vocabularies (``anxious``, ``work``, ...). One row per
entry, keyed by HAE's id so replayed payloads upsert in place. Labels and
associations stay ``text[]`` rather than a lookup table: the vocabulary is
Apple's and may grow, and an unknown value must never reject a payload (§4.0).
DDL is idempotent (guarded by an inspector) so a partial/replayed migration is
safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_state_of_mind"
down_revision: str | None = "0022_app_settings_local_tz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "state_of_mind" not in inspector.get_table_names():
        op.create_table(
            "state_of_mind",
            sa.Column("hae_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            # 'momentary_emotion' | 'daily_mood' (kept open-ended, not an enum).
            sa.Column("kind", sa.Text(), nullable=True),
            sa.Column("valence", sa.Float(), nullable=True),
            sa.Column("valence_classification", sa.Text(), nullable=True),
            sa.Column("labels", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
            sa.Column("associations", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        )
        op.create_index("ix_state_of_mind_start_time", "state_of_mind", ["start_time"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "state_of_mind" in inspector.get_table_names():
        op.drop_table("state_of_mind")
