"""workout_trimp / daily_trimp SQL functions: one live TRIMP definition for Grafana.

Before this migration every TRIMP-consuming Grafana panel carried its own copy
of the Banister TRIMP pipeline — and the copies had drifted: the Training-load
panels scored workouts from the session *average HR* against a single 90-day
resting-HR median, while the Fitness & Form panels (PMC / ACWR) were
*sample-resolved* over ``workout_hr_samples`` with a per-day 28-day rolling
resting-HR median. Side by side on the consolidated Training dashboard the two
formulas produce visibly different numbers for the same day.

These two functions are the single live-SQL TRIMP source (same role
``sleep_metrics`` plays for sleep efficiency, migration 0009). They implement
the richer of the two variants — sample-resolved with average-HR fallback, per
``docs/workout-analysis.md`` §7:

  workout_trimp(p_tz, p_hr_max, p_since)
      One row per workout: (hae_id, day, canonical_type, trimp).
      * day buckets ``start_time`` in the caller's timezone ``p_tz``.
      * ``p_hr_max`` NULL means 'auto': max recorded workout HR clamped to
        160-210, falling back to 190 with no workouts (the dashboards pass
        ``NULLIF('${hr_max}','auto')`` straight through).
      * The HR-reserve baseline is a per-day 28-day rolling median of the
        measured resting HR (``daily_metrics``), falling back to the global
        median, then 60.
      * Sample-resolved TRIMP is rescaled from covered-sample seconds to the
        session duration; sessions without samples score their average HR over
        the full duration (identical value for steady sessions); sessions
        without either score 0.
      * ``p_since`` (optional) skips workouts bucketed before that day — a
        pruning knob for panels that only need a short window (the resting-HR
        baseline still reads the full ``daily_metrics`` history).

  daily_trimp(p_tz, p_hr_max, p_since)
      The same, summed per day (only days that have workouts; consumers
      0-fill gaps themselves where EWMAs need a dense series).

This intentionally stays distinct from the nightly analysis' profile-driven
TRIMP series (``daily_metrics``/findings, §3.1): the live functions work with
zero setup and react to the dashboard's HR-Max variable, the analysis snapshot
uses the configured profile. Plain SQL, no Timescale DDL — runs everywhere.

Revision ID: 0019_workout_trimp_functions
Revises: 0018_body_battery_tables
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_workout_trimp_functions"
down_revision: str | None = "0018_body_battery_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Banister TRIMP for one HR reading over `minutes`:
#   minutes * frac * 0.64 * e^(1.92 * frac),  frac = clamp((hr-rest)/(max-rest), 0..1)
_WORKOUT_TRIMP_SQL = """
CREATE OR REPLACE FUNCTION workout_trimp(
    p_tz text,
    p_hr_max numeric DEFAULT NULL,
    p_since date DEFAULT NULL
)
RETURNS TABLE (hae_id uuid, day date, canonical_type text, trimp double precision)
LANGUAGE sql STABLE AS
$fn$
WITH params AS (
    SELECT COALESCE(
        p_hr_max,
        LEAST(GREATEST((SELECT MAX(w.max_hr)::numeric FROM workouts w), 160), 210),
        190
    ) AS hr_max
),
rhr AS (
    SELECT dm.day, dm.vmin::numeric AS vmin
    FROM daily_metrics dm
    WHERE dm.metric = 'resting_heart_rate' AND dm.vmin IS NOT NULL
),
w AS (
    SELECT wo.hae_id,
           DATE(wo.start_time AT TIME ZONE p_tz) AS day,
           wo.canonical_type,
           wo.duration_s,
           wo.avg_hr
    FROM workouts wo
    WHERE p_since IS NULL OR DATE(wo.start_time AT TIME ZONE p_tz) >= p_since
),
base AS (
    SELECT d.day,
           COALESCE(
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r.vmin),
               (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY rhr.vmin) FROM rhr),
               60
           ) AS hr_rest
    FROM (SELECT DISTINCT w.day FROM w) d
    LEFT JOIN rhr r ON r.day BETWEEN d.day - 27 AND d.day
    GROUP BY d.day
),
iv AS (
    SELECT s.workout_hae_id AS hae_id,
           s.bpm,
           EXTRACT(EPOCH FROM (LEAD(s.ts) OVER (PARTITION BY s.workout_hae_id ORDER BY s.ts) - s.ts)) AS sec
    FROM workout_hr_samples s
    WHERE EXISTS (SELECT 1 FROM w WHERE w.hae_id = s.workout_hae_id)
),
sess AS (
    SELECT iv.hae_id,
           SUM(iv.sec) AS sec_total,
           SUM(
               (iv.sec / 60.0)
               * LEAST(1.0, GREATEST(0.0, (iv.bpm - b.hr_rest) / NULLIF(p.hr_max - b.hr_rest, 0)))
               * (0.64 * EXP(1.92 * LEAST(1.0, GREATEST(0.0, (iv.bpm - b.hr_rest) / NULLIF(p.hr_max - b.hr_rest, 0)))))
           ) AS trimp_raw
    FROM iv
    JOIN w ON w.hae_id = iv.hae_id
    JOIN base b ON b.day = w.day
    CROSS JOIN params p
    WHERE iv.sec IS NOT NULL AND iv.sec > 0
    GROUP BY iv.hae_id
)
SELECT w.hae_id,
       w.day,
       w.canonical_type,
       (CASE
            WHEN s.sec_total > 0 AND w.duration_s IS NOT NULL AND w.duration_s > 0
                THEN s.trimp_raw * (w.duration_s / s.sec_total)
            WHEN w.avg_hr IS NOT NULL AND w.duration_s IS NOT NULL AND w.duration_s > 0
                THEN (w.duration_s / 60.0)
                     * LEAST(1.0, GREATEST(0.0, (w.avg_hr - b.hr_rest) / NULLIF(p.hr_max - b.hr_rest, 0)))
                     * (0.64 * EXP(1.92 * LEAST(1.0, GREATEST(0.0,
                           (w.avg_hr - b.hr_rest) / NULLIF(p.hr_max - b.hr_rest, 0)))))
            ELSE 0
        END)::double precision AS trimp
FROM w
LEFT JOIN sess s ON s.hae_id = w.hae_id
JOIN base b ON b.day = w.day
CROSS JOIN params p
$fn$;
"""

_DAILY_TRIMP_SQL = """
CREATE OR REPLACE FUNCTION daily_trimp(
    p_tz text,
    p_hr_max numeric DEFAULT NULL,
    p_since date DEFAULT NULL
)
RETURNS TABLE (day date, trimp double precision)
LANGUAGE sql STABLE AS
$fn$
SELECT t.day, SUM(t.trimp) AS trimp
FROM workout_trimp(p_tz, p_hr_max, p_since) t
GROUP BY t.day
$fn$;
"""


def upgrade() -> None:
    op.execute(_WORKOUT_TRIMP_SQL)
    op.execute(_DAILY_TRIMP_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS daily_trimp(text, numeric, date)")
    op.execute("DROP FUNCTION IF EXISTS workout_trimp(text, numeric, date)")
