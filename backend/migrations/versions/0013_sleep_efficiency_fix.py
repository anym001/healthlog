"""sleep_metrics: treat in_bed_h = 0 as missing when computing efficiency

Revision ID: 0013_sleep_efficiency_fix
Revises: 0012_recategorize_vitals
Create Date: 2026-06-22

The efficiency denominator used ``COALESCE(in_bed_h, <timestamps>, asleep+awake)``.
Health Auto Export frequently stores ``in_bed_h = 0`` (and ``asleep_h = 0``)
while still delivering valid ``in_bed_start``/``in_bed_end`` timestamps. Because
COALESCE returns the first *non-NULL* value, a stored ``0`` short-circuited the
fallback to the timestamp window, so ``in_bed_h_calc`` came out as ``0`` and the
``CASE ... > 0`` guard forced ``efficiency_pct`` to NULL. Every night was blank,
so the "Sleep Efficiency" KPI on the Overview/Sleep dashboards showed nothing.

Fix: wrap the additive fallbacks in ``NULLIF(..., 0)`` so a stored zero is
treated as "missing" and the timestamp-derived in-bed duration is used. This
mirrors the analysis pipeline (``analysis.py:load_sleep_frame``), which already
prefers the timestamps over a stored ``in_bed_h``. Column list is unchanged, so
CREATE OR REPLACE VIEW applies in place.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_sleep_efficiency_fix"
down_revision: str | None = "0012_recategorize_vitals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# in_bed_h = 0 / asleep+awake = 0 mean "not reported"; NULLIF lets COALESCE fall
# through to the next candidate instead of locking the denominator at zero.
_IN_BED_H_CALC = """
    COALESCE(
        NULLIF(in_bed_h, 0),
        EXTRACT(EPOCH FROM (in_bed_end - in_bed_start)) / 3600.0,
        NULLIF(COALESCE(asleep_h, 0) + COALESCE(awake_h, 0), 0)
    )
"""

# Previous (0010) denominator: a stored 0 won the COALESCE and blanked efficiency.
_IN_BED_H_CALC_OLD = """
    COALESCE(
        in_bed_h,
        EXTRACT(EPOCH FROM (in_bed_end - in_bed_start)) / 3600.0,
        COALESCE(asleep_h, 0) + COALESCE(awake_h, 0)
    )
"""

_METRICS_SQL = """
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
    {calc} AS in_bed_h_calc,
    CASE
        WHEN total_sleep_h IS NOT NULL
         AND {calc} > 0
        THEN ROUND((total_sleep_h / {calc} * 100)::numeric, 1)
        ELSE NULL
    END AS efficiency_pct
FROM sleep_nightly;
"""


def upgrade() -> None:
    op.execute(_METRICS_SQL.format(calc=_IN_BED_H_CALC))


def downgrade() -> None:
    op.execute(_METRICS_SQL.format(calc=_IN_BED_H_CALC_OLD))
