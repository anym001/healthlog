"""sleep_metrics VIEW: pre-computed sleep efficiency and derived in-bed duration.

Consolidates the three-fallback efficiency calculation into a single view so
Grafana queries, the nightly analysis pipeline, and any future code all read
from one place instead of repeating the CASE logic.

Columns (all sleep_sessions columns plus):
  in_bed_h_calc   most precise available in-bed duration (hours):
                  1st: in_bed_h direct field from HAE
                  2nd: derived from in_bed_start / in_bed_end timestamps
                  3rd: asleep_h + awake_h sum
  efficiency_pct  sleep efficiency = total_sleep_h / in_bed_h_calc × 100,
                  rounded to 1 decimal; NULL when inputs are unavailable.

Extensibility: replace the view (or add a new one) whenever HAE exposes
additional sleep fields — no table migration needed.

Revision ID: 0009_sleep_metrics_view
Revises: 0008_workout_categories
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_sleep_metrics_view"
down_revision: str | None = "0008_workout_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW_SQL = """
CREATE OR REPLACE VIEW sleep_metrics AS
SELECT
    id,
    sleep_start,
    sleep_end,
    in_bed_start,
    in_bed_end,
    source,
    sleep_date,
    total_sleep_h,
    deep_h,
    core_h,
    rem_h,
    awake_h,
    asleep_h,
    in_bed_h,
    COALESCE(
        in_bed_h,
        EXTRACT(EPOCH FROM (in_bed_end - in_bed_start)) / 3600.0,
        COALESCE(asleep_h, 0) + COALESCE(awake_h, 0)
    ) AS in_bed_h_calc,
    CASE
        WHEN total_sleep_h IS NOT NULL
         AND COALESCE(
                 in_bed_h,
                 EXTRACT(EPOCH FROM (in_bed_end - in_bed_start)) / 3600.0,
                 COALESCE(asleep_h, 0) + COALESCE(awake_h, 0)
             ) > 0
        THEN ROUND(
            (total_sleep_h / COALESCE(
                 in_bed_h,
                 EXTRACT(EPOCH FROM (in_bed_end - in_bed_start)) / 3600.0,
                 COALESCE(asleep_h, 0) + COALESCE(awake_h, 0)
             ) * 100)::numeric,
            1
        )
        ELSE NULL
    END AS efficiency_pct
FROM sleep_sessions;
"""

_DROP_SQL = "DROP VIEW IF EXISTS sleep_metrics;"


def upgrade() -> None:
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
