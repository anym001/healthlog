"""reconcile metric registry with the curated dict

Revision ID: 0003_curate_metrics
Revises: 0002_findings
Create Date: 2026-06-16

The first full backfill auto-registered nine previously unseen metrics as
``unknown``/``secondary`` stubs. This reconciles every curated metric in
``app.registry.METRIC_REGISTRY`` with the table: it promotes ``cardio_recovery``
to ``core`` and gives the rest a proper display name / unit / category / tier
(clearing the ``auto_registered`` flag). Idempotent upsert on the ``metric``
primary key; rows *not* in the curated dict (genuine auto-registered stubs)
are left untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0003_curate_metrics"
down_revision: str | None = "0002_findings"
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

    bind = op.get_bind()
    for metric, spec in METRIC_REGISTRY.items():
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
        bind.execute(stmt)


def downgrade() -> None:
    # Metadata reconciliation only — no structural change to roll back.
    pass
