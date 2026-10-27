"""curate caffeine and heart_rate_variability_rmssd into the metric registry

Revision ID: 0025_curate_caffeine_hrv_rmssd
Revises: 0024_symptoms_medications
Create Date: 2026-10-19

Both metrics reach real exports as single ``qty`` points, so ingest
auto-registered them as ``unknown``/``secondary`` stubs with ``agg_default =
'sum'``. For ``heart_rate_variability_rmssd`` that daily sum is meaningless (a
day with more readings looks like higher HRV); it is a per-reading value whose
daily mean counts, like ``heart_rate_variability`` (SDNN). Both are now curated
in ``app.registry.METRIC_REGISTRY``. Same idempotent upsert as 0006: it
overwrites the stubs (clearing ``auto_registered``) and leaves other
auto-registered stubs untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0025_curate_caffeine_hrv_rmssd"
down_revision: str | None = "0024_symptoms_medications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METRICS = ("caffeine", "heart_rate_variability_rmssd")

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

    for metric in _METRICS:
        spec = METRIC_REGISTRY[metric]
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
            .values(metric=metric, **values)
            .on_conflict_do_update(index_elements=["metric"], set_=values)
        )
        op.get_bind().execute(stmt)


def downgrade() -> None:
    # Metadata reconciliation only — no structural change to roll back.
    pass
