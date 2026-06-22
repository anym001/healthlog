"""recategorize recovery vitals from `sleep` to `vital`

Revision ID: 0012_recategorize_vitals
Revises: 0011_sleep_awakening_key
Create Date: 2026-06-22

heart_rate_variability, resting_heart_rate and respiratory_rate were seeded
under category ``sleep`` (they are measured mostly overnight). They are
cardiovascular/respiratory vital signs, so the Metrics Explorer surfaced HRV
under "sleep", which is misleading. This moves the three to ``vital`` to match
the updated ``app.registry.METRIC_REGISTRY``. The genuinely sleep-specific
metrics (sleeping wrist temperature, breathing disturbances, time in daylight)
stay under ``sleep``. Idempotent UPDATE keyed on the metric primary key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_recategorize_vitals"
down_revision: str | None = "0011_sleep_awakening_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECOVERY_VITALS = ("heart_rate_variability", "resting_heart_rate", "respiratory_rate")


def _set_category(target: str) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE metric_registry SET category = :cat WHERE metric = ANY(:metrics)"
        ),
        {"cat": target, "metrics": list(_RECOVERY_VITALS)},
    )


def upgrade() -> None:
    _set_category("vital")


def downgrade() -> None:
    _set_category("sleep")
