"""app_settings table; daily_metrics view follows the configured TZ

Revision ID: 0022_app_settings_local_tz
Revises: 0021_widen_finding_kind
Create Date: 2026-10-15

The ``daily_metrics`` view (migrations 0001/0005) bucketed days with a
hard-coded ``Europe/Vienna``, while the analysis (``load_daily_series``) uses
the ``TZ`` setting. For any other ``TZ`` the view - and every Grafana panel
built on it - therefore disagreed with the pipeline about what a "day" is.

A view cannot read an environment variable, so the configured zone is stored
in a small key/value ``app_settings`` table, written from ``TZ`` at every
container start (``app.db_settings``, run by the s6 migrate oneshot), and the
view reads it. The seed is ``Europe/Vienna``, so behaviour is unchanged until
the first sync. Column names/types are unchanged, so CREATE OR REPLACE VIEW
applies in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_app_settings_local_tz"
down_revision: str | None = "0021_widen_finding_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pre-0022 hard-coded zone: the seed value and the fallback if the row is missing.
DEFAULT_TZ = "Europe/Vienna"


def _create_view(day_tz_expr: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE VIEW daily_metrics AS
            SELECT (time AT TIME ZONE {day_tz_expr})::date AS day,
                   metric,
                   avg(coalesce(vavg, qty)) AS avg,
                   min(coalesce(vmin, qty)) AS vmin,
                   max(coalesce(vmax, qty)) AS vmax,
                   sum(qty)   AS sum,
                   sum(n)     AS n
            FROM metric_samples
            GROUP BY 1, 2
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(sa.text(f"INSERT INTO app_settings (key, value) VALUES ('local_tz', '{DEFAULT_TZ}')"))
    # Uncorrelated scalar subquery: evaluated once per query, not per row.
    _create_view(f"coalesce((SELECT value FROM app_settings WHERE key = 'local_tz'), '{DEFAULT_TZ}')")


def downgrade() -> None:
    # Back to migration 0005's hard-coded zone before the table goes away.
    _create_view(f"'{DEFAULT_TZ}'")
    op.drop_table("app_settings")
