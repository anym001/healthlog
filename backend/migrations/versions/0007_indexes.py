"""Performance indexes for frequently queried columns.

Adds partial/covering indexes that eliminate sequential scans on the hot query
paths used by the Grafana dashboards and the nightly analysis pipeline. All
indexes use IF NOT EXISTS so the migration is safe to re-run and rolls back
cleanly.

Revision ID: 0007_indexes
Revises: 0006_curate_cycling_distance
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_indexes"
down_revision: str | None = "0006_curate_cycling_distance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # metric_samples: the dashboards always filter by metric first, then time DESC
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_metric_samples_metric_time "
        "ON metric_samples (metric, time DESC);"
    )
    # findings: nightly queries and Grafana filter by kind, then sort by date
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_findings_kind_ref_date "
        "ON findings (kind, ref_date DESC NULLS LAST);"
    )
    # workouts: dashboard time-range queries need fast start_time access
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workouts_start_time "
        "ON workouts (start_time DESC);"
    )
    # sleep_sessions: range queries keyed on the wake-up date
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sleep_sessions_sleep_date "
        "ON sleep_sessions (sleep_date DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_metric_samples_metric_time;")
    op.execute("DROP INDEX IF EXISTS ix_findings_kind_ref_date;")
    op.execute("DROP INDEX IF EXISTS ix_workouts_start_time;")
    op.execute("DROP INDEX IF EXISTS ix_sleep_sessions_sleep_date;")
