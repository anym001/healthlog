"""sleep_sessions natural key -> (sleep_end, source): collapse API re-captures.

Migration 0010 (sleep_nightly view) fixed reads. This fixes ingest so the
overlapping re-captures never accumulate in the first place.

Health Auto Export's REST API re-captures the same night several times, each
push starting at a later ``sleepStart`` but sharing the same ``sleepEnd``. The
old natural key ``(sleep_start, source)`` therefore admitted every push as a new
row. The end of a sleep period (the awakening) is its stable identity: two
genuinely distinct periods (e.g. a nap) end at different times, while re-captures
of one night share an end. So the key becomes ``(sleep_end, source)`` and the
ingest upsert keeps the most complete capture (largest ``total_sleep_h``).

``NULLS NOT DISTINCT`` (Postgres 15+) keeps replay idempotent even for the rare
row without a ``sleep_end``: two NULL ends collapse instead of inserting twice.

Data integrity: the collapse step only removes rows that share an exact
``(sleep_end, source)`` with a kept, more complete row — i.e. partial
re-captures of the same awakening. Distinct sleep periods (different ``sleep_end``)
are preserved. Backfill nights (one row each, distinct ends) are untouched. The
raw payloads remain in ``raw_ingest`` regardless, so the collapse is replayable.

Revision ID: 0011_sleep_awakening_key
Revises: 0010_sleep_nightly_view
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_sleep_awakening_key"
down_revision: str | None = "0010_sleep_nightly_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep the most complete row per awakening; delete the partial re-captures.
# PARTITION BY groups NULL sleep_end together (matching NULLS NOT DISTINCT below).
_COLLAPSE_SQL = """
DELETE FROM sleep_sessions s
USING (
    SELECT id,
           row_number() OVER (
               PARTITION BY sleep_end, source
               ORDER BY total_sleep_h DESC NULLS LAST, sleep_start ASC NULLS LAST, id ASC
           ) AS rn
    FROM sleep_sessions
) d
WHERE s.id = d.id AND d.rn > 1;
"""


def upgrade() -> None:
    op.execute(_COLLAPSE_SQL)
    op.drop_constraint("uq_sleep_sessions", "sleep_sessions", type_="unique")
    op.execute(
        "ALTER TABLE sleep_sessions "
        "ADD CONSTRAINT uq_sleep_sessions UNIQUE NULLS NOT DISTINCT (sleep_end, source)"
    )


def downgrade() -> None:
    # Restores the key shape only; the collapsed partial rows are not resurrected
    # (they remain available in raw_ingest for a full re-derive if ever needed).
    op.drop_constraint("uq_sleep_sessions", "sleep_sessions", type_="unique")
    op.create_unique_constraint("uq_sleep_sessions", "sleep_sessions", ["sleep_start", "source"])
