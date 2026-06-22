"""sleep_nightly VIEW: one consolidated session per wake-up day.

Health Auto Export delivers sleep differently depending on the channel:

  * the manual export (used by the backfill) ships one aggregated
    ``sleep_analysis`` object per night -> one ``sleep_sessions`` row/night;
  * the REST API / automation push ships the same night several times as it
    accumulates, each push starting at a *later* ``sleepStart`` but sharing the
    same ``sleepEnd``. Because the natural key is ``(sleep_start, source)`` every
    push lands as its own row, so a night becomes several overlapping rows.

Those rows are not phase segments — they overlap, and only the earliest-starting
one (the largest ``total_sleep_h``) holds the full night incl. deep sleep; the
later ones are partial re-captures. Summing them double-counts (a night showed
as 13–15 h); plotting them stacks several bars on the same date in Grafana.

``sleep_nightly`` resolves this at read time by keeping exactly the most complete
row per ``sleep_date`` (largest ``total_sleep_h``, earliest ``sleep_start`` as
tie-break). It is non-destructive — every original row stays in
``sleep_sessions`` (and the raw payloads in ``raw_ingest``) — and it fixes all
consumers at once: the dashboards, the ``sleep_metrics`` efficiency view, and the
analysis pipeline (``load_sleep_frame``).

``sleep_metrics`` is rebuilt on top of ``sleep_nightly`` so its efficiency
calculation also sees one row per night. Its column list is unchanged, so
CREATE OR REPLACE VIEW applies in place.

Revision ID: 0010_sleep_nightly_view
Revises: 0009_sleep_metrics_view
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_sleep_nightly_view"
down_revision: str | None = "0009_sleep_metrics_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One row per wake-up day: the most complete session. Overlapping API re-captures
# of the same night share sleep_end and differ only by a later sleep_start, so the
# largest total_sleep_h is the full night; sleep_start ASC breaks exact ties.
_NIGHTLY_SQL = """
CREATE OR REPLACE VIEW sleep_nightly AS
SELECT DISTINCT ON (sleep_date)
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
    in_bed_h
FROM sleep_sessions
WHERE sleep_date IS NOT NULL
ORDER BY sleep_date, total_sleep_h DESC NULLS LAST, sleep_start ASC NULLS LAST;
"""

# sleep_metrics, identical columns to migration 0009 but sourced from the
# deduplicated sleep_nightly so efficiency is computed per night, not per row.
_METRICS_FROM_NIGHTLY = """
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
FROM {source};
"""


def upgrade() -> None:
    op.execute(_NIGHTLY_SQL)
    op.execute(_METRICS_FROM_NIGHTLY.format(source="sleep_nightly"))


def downgrade() -> None:
    # Restore the 0009 definition (reads sleep_sessions directly) before the
    # dependency it now relies on is dropped.
    op.execute(_METRICS_FROM_NIGHTLY.format(source="sleep_sessions"))
    op.execute("DROP VIEW IF EXISTS sleep_nightly;")
