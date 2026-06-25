"""align daily_metrics view with the analysis COALESCE(vavg, qty)

Revision ID: 0005_daily_metrics_coalesce
Revises: 0004_workout_hr_samples
Create Date: 2026-06-17

The ``daily_metrics`` view (migration 0001) read ``avg(vavg)``/``min(vmin)``/
``max(vmax)``, while the analysis loader (``analysis.load_daily_series``) reads
``avg(coalesce(vavg, qty))`` etc. For the 29 of 30 metrics that fill ``qty``
(only ``heart_rate`` fills Min/Avg/Max), the view therefore returned NULL where
the analysis sees a real value — so Grafana dashboards and the pipeline disagreed
on the daily number (ARCHITECTURE.md §4.7).

This redefines the view to COALESCE onto ``qty`` exactly like the loader, so the
two sources line up. The ``sum`` column stays ``sum(qty)`` (a sum is qty-only by
design — Min/Avg/Max metrics are never summed). Column names/types are unchanged,
so CREATE OR REPLACE VIEW applies in place without dropping dependents.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_daily_metrics_coalesce"
down_revision: str | None = "0004_workout_hr_samples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match migration 0001 (and analysis.load_daily_series); the daily grid is
# the local calendar day, not UTC.
LOCAL_TZ = "Europe/Vienna"


def _create_view(avg_expr: str, min_expr: str, max_expr: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE VIEW daily_metrics AS
            SELECT (time AT TIME ZONE '{LOCAL_TZ}')::date AS day,
                   metric,
                   {avg_expr} AS avg,
                   {min_expr} AS vmin,
                   {max_expr} AS vmax,
                   sum(qty)   AS sum,
                   sum(n)     AS n
            FROM metric_samples
            GROUP BY 1, 2
            """
        )
    )


def upgrade() -> None:
    # COALESCE onto qty so the view matches analysis.load_daily_series.
    _create_view("avg(coalesce(vavg, qty))", "min(coalesce(vmin, qty))", "max(coalesce(vmax, qty))")


def downgrade() -> None:
    # Back to the qty-blind aggregates of migration 0001.
    _create_view("avg(vavg)", "min(vmin)", "max(vmax)")
