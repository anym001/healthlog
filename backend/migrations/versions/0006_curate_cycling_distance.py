"""curate cycling_distance into the metric registry

Revision ID: 0006_curate_cycling_distance
Revises: 0005_daily_metrics_coalesce
Create Date: 2026-06-19

``cycling_distance`` was auto-registered as an ``unknown``/``secondary`` stub on
first ingest, so it surfaced under the "unknown" category in the dashboards.
It is now curated in ``app.registry.METRIC_REGISTRY`` (Cycling Distance, km,
sum, activity). This reconciles the curated dict with the table exactly like
migration 0003: an idempotent upsert on the ``metric`` primary key that
overwrites the stub (clearing ``auto_registered``) while leaving genuine
auto-registered stubs (metrics still absent from the dict) untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0006_curate_cycling_distance"
down_revision: str | None = "0005_daily_metrics_coalesce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_registry = sa.table(
    "metric_registry",
    sa.column("metric", sa.Text),
    sa.column("display_name", sa.Text),
    sa.column("unit_canonical", sa.Text),
    sa.column("agg_default", sa.String),
    sa.column("category", sa.String),
    sa.column("tier", sa.String),
    sa.column("auto_registered", sa.Boolean),
)


def upgrade() -> None:
    from app.registry import METRIC_REGISTRY

    spec = METRIC_REGISTRY["cycling_distance"]
    values = {
        "display_name": spec["display_name"],
        "unit_canonical": spec["unit_canonical"],
        "agg_default": spec["agg_default"],
        "category": spec["category"],
        "tier": spec["tier"],
        "auto_registered": False,
    }
    stmt = (
        pg_insert(_registry)
        .values(metric="cycling_distance", **values)
        .on_conflict_do_update(index_elements=["metric"], set_=values)
    )
    op.get_bind().execute(stmt)


def downgrade() -> None:
    # Metadata reconciliation only — no structural change to roll back.
    pass
