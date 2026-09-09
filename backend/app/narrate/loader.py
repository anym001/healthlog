"""The findings query: load the current snapshot for narration.

Recent, dated findings (anomalies, recovery alerts, training load) are bounded
by the lookback window; the standing analyses (correlations, trends,
seasonality, consistency, training status) are always included. Display names are joined from
the metric registry; the hand-wired series the analysis assembles itself
(workout load, sleep — no registry rows, see docs/workout-analysis.md) get
their labels from the fallback map below, so raw snake_case keys never reach
the narration prose.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_FINDINGS_SQL = """\
SELECT
    f.kind,
    f.metric_a,
    COALESCE(ra.display_name, f.metric_a) AS metric_a_label,
    f.metric_b,
    COALESCE(rb.display_name, f.metric_b) AS metric_b_label,
    f.lag_days,
    f.coefficient,
    f.p_value_adj,
    f.ref_date,
    f.window_start,
    f.window_end,
    f.severity,
    f.details,
    f.note,
    f.computed_at
FROM findings f
LEFT JOIN metric_registry ra ON ra.metric = f.metric_a
LEFT JOIN metric_registry rb ON rb.metric = f.metric_b
WHERE
    (
        f.kind IN ('anomaly', 'recovery_alert', 'training_load', 'stress', 'body_battery')
        AND f.ref_date >= :cutoff
    )
    OR f.kind IN ('correlation', 'trend', 'seasonality', 'consistency', 'training_status')
    OR (
        :include_weekly
        AND f.kind IN ('weekly_training', 'weekly_sleep', 'weekly_stress', 'weekly_body_battery',
                       'weekly_vitals', 'weekly_activity', 'fitness_markers')
    )
    OR (
        :include_monthly
        AND f.kind IN ('monthly_training', 'monthly_sleep', 'monthly_stress', 'monthly_body_battery',
                       'monthly_vitals', 'monthly_activity', 'fitness_markers')
    )
ORDER BY f.kind, f.ref_date DESC NULLS LAST, f.severity DESC NULLS LAST
"""


# Labels for series without a metric_registry row: the workout-load and sleep
# series are assembled by the analysis (not ingested), and a few finding kinds
# use symbolic metric names. Same Title-Case style as the registry display names.
_HANDWIRED_LABELS = {
    "workout_trimp": "Training Load (TRIMP)",
    "workout_load": "Training Load",
    "workout_edwards": "Training Load (Edwards TRIMP)",
    "sleep_total_h": "Total Sleep",
    "sleep_deep_h": "Deep Sleep",
    "sleep_rem_h": "REM Sleep",
    "sleep_efficiency": "Sleep Efficiency",
    "bedtime": "Bedtime",
    "recovery": "Recovery",
    "stress": "Stress",
    "body_battery": "Body Battery",
}
# Per-sport variants: "workout_trimp_running" -> "Training Load (TRIMP) — Running".
_HANDWIRED_PREFIXES = ("workout_trimp_", "workout_load_", "workout_edwards_")


def _handwired_label(key: str) -> str | None:
    """Display label for a hand-wired series key, or None when unknown."""
    if key in _HANDWIRED_LABELS:
        return _HANDWIRED_LABELS[key]
    for prefix in _HANDWIRED_PREFIXES:
        if key.startswith(prefix):
            sport = key[len(prefix) :].replace("_", " ").title()
            return f"{_HANDWIRED_LABELS[prefix.rstrip('_')]} — {sport}"
    return None


def load_findings(db: Session, lookback_days: int, report: str = "status") -> list[dict]:
    """Query the current findings snapshot, joining display names from the registry.

    The lookback cutoff is computed here in the configured local timezone:
    ``ref_date`` is a local-TZ day (ARCHITECTURE.md — daily buckets are local,
    not UTC), while the DB server typically runs on UTC, so Postgres'
    ``CURRENT_DATE`` would shift the window around local midnight.

    ``report`` selects the descriptive summary kinds: ``weekly`` adds the
    ``weekly_*`` kinds, ``monthly`` the ``monthly_*`` kinds (both include
    ``fitness_markers``). They are snapshot findings like ``training_status``,
    so the lookback cutoff does not apply to them; the default ``status``
    report leaves them all out.
    """
    from sqlalchemy import text

    today = dt.datetime.now(ZoneInfo(get_settings().local_tz)).date()
    cutoff = today - dt.timedelta(days=lookback_days)
    params = {"cutoff": cutoff, "include_weekly": report == "weekly", "include_monthly": report == "monthly"}
    rows = db.execute(text(_FINDINGS_SQL), params).mappings().all()
    result = []
    for row in rows:
        d = dict(row)
        # details comes back as a string from some drivers — parse if needed.
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                d["details"] = {}
        # Hand-wired series have no registry row, so the SQL COALESCE fell
        # back to the raw key — swap in the fallback label where we have one.
        for side in ("metric_a", "metric_b"):
            key = d.get(side)
            if key and d.get(f"{side}_label") == key:
                d[f"{side}_label"] = _handwired_label(key) or key
        result.append(d)
    return result
