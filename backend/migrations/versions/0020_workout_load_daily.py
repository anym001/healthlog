"""workout_load_daily table + findings_feed view.

Two consumers-of-the-nightly-run additions:

``workout_load_daily``
    The findings pipeline builds daily, profile-driven workout-load series —
    ``workout_trimp`` (Banister), ``workout_edwards`` (zone-based, from the
    intra-workout HR samples), ``workout_load`` (kcal), ``workout_duration``,
    ``workout_count``, ``workout_intensity`` and their per-sport children
    (``workout_trimp_running`` …) — but until now used them only internally and
    discarded them after the findings pass. This table persists that snapshot
    so Grafana can chart what only the nightly analysis can compute (the live
    ``workout_trimp`` SQL functions cover Banister, but not Edwards or the
    kcal/duration aggregates). One row per (series, day); each run deletes and
    rewrites the whole table (snapshot semantics like ``findings`` — past days
    legitimately change when the rolling resting-HR baseline or the resolved
    HR_max shifts). Plain table, no Timescale DDL: a handful of series over a
    few years stays tiny.

``findings_feed``
    Both dashboards' findings tables carried an identical CASE expression that
    renders a per-kind one-line detail out of ``findings.details``. The view
    owns that expression once (the consolidation role ``sleep_metrics`` plays
    for sleep efficiency); the panels now differ only in their WHERE clause.
    ``day`` is the finding's display date (``ref_date`` falling back to
    ``window_end``).

Revision ID: 0020_workout_load_daily
Revises: 0019_workout_trimp_functions
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_workout_load_daily"
down_revision: str | None = "0019_workout_trimp_functions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDINGS_FEED_SQL = """
CREATE OR REPLACE VIEW findings_feed AS
SELECT f.id,
       f.computed_at,
       COALESCE(f.ref_date, f.window_end) AS day,
       f.kind,
       f.metric_a,
       f.metric_b,
       f.lag_days,
       f.coefficient,
       f.severity,
       f.details,
       f.note,
       CASE f.kind
           WHEN 'anomaly' THEN
               ROUND((f.details->>'z')::numeric, 1)::text
               || 'σ (val: ' || ROUND((f.details->>'value')::numeric, 2)::text || ')'
           WHEN 'training_load' THEN
               'ACWR ' || ROUND((f.details->>'ratio')::numeric, 2)::text
           WHEN 'consistency' THEN
               ROUND((f.details->>'std_hours')::numeric, 2)::text || 'h std'
           WHEN 'recovery_alert' THEN
               'RHR z=' || ROUND((f.details->>'resting_heart_rate_z')::numeric, 1)::text
               || ' HRV z=' || ROUND((f.details->>'heart_rate_variability_z')::numeric, 1)::text
           WHEN 'correlation' THEN
               'r=' || ROUND(f.coefficient::numeric, 2)::text
               || ' lag=' || COALESCE(f.lag_days::text, '0') || 'd'
           ELSE ROUND(f.severity::numeric, 2)::text
       END AS detail
FROM findings f
"""


def upgrade() -> None:
    op.create_table(
        "workout_load_daily",
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("series", "day"),
    )
    op.execute(_FINDINGS_FEED_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS findings_feed")
    op.drop_table("workout_load_daily")
