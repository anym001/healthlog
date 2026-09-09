"""widen findings.kind for the weekly-summary kinds

Revision ID: 0021_widen_finding_kind
Revises: 0020_workout_load_daily

The weekly report kinds (``weekly_body_battery`` at 19 characters) no longer
fit the original ``VARCHAR(16)``; widen ``kind`` to 32 on both the snapshot
and the history table. ``findings_feed`` depends on the column, so the view is
dropped for the ALTER and recreated verbatim (same duplication pattern as
0013's ``sleep_metrics`` redefinition — the SQL below must match 0020). Plain
type widening otherwise — no data change, no Timescale specifics.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_widen_finding_kind"
down_revision: str | None = "0020_workout_load_daily"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Verbatim copy of migration 0020's findings_feed definition.
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
    op.execute("DROP VIEW IF EXISTS findings_feed")
    op.alter_column("findings", "kind", type_=sa.String(length=32), existing_nullable=False)
    op.alter_column("findings_history", "kind", type_=sa.String(length=32), existing_nullable=False)
    op.execute(_FINDINGS_FEED_SQL)


def downgrade() -> None:
    # Narrowing would fail on stored weekly kinds; delete them first.
    op.execute("DELETE FROM findings WHERE length(kind) > 16")
    op.execute("DELETE FROM findings_history WHERE length(kind) > 16")
    op.execute("DROP VIEW IF EXISTS findings_feed")
    op.alter_column("findings", "kind", type_=sa.String(length=16), existing_nullable=False)
    op.alter_column("findings_history", "kind", type_=sa.String(length=16), existing_nullable=False)
    op.execute(_FINDINGS_FEED_SQL)
