"""The findings query: load the current snapshot for narration.

Recent, dated findings (anomalies, recovery alerts, training load) are bounded
by the lookback window; the standing analyses (correlations, trends,
seasonality, consistency) are always included. Display names are joined from the
metric registry so the context never leaks raw snake_case keys.
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
        f.kind IN ('anomaly', 'recovery_alert', 'training_load')
        AND f.ref_date >= :cutoff
    )
    OR f.kind IN ('correlation', 'trend', 'seasonality', 'consistency')
ORDER BY f.kind, f.ref_date DESC NULLS LAST, f.severity DESC NULLS LAST
"""


def load_findings(db: Session, lookback_days: int) -> list[dict]:
    """Query the current findings snapshot, joining display names from the registry.

    The lookback cutoff is computed here in the configured local timezone:
    ``ref_date`` is a local-TZ day (ARCHITECTURE.md — daily buckets are local,
    not UTC), while the DB server typically runs on UTC, so Postgres'
    ``CURRENT_DATE`` would shift the window around local midnight.
    """
    from sqlalchemy import text

    today = dt.datetime.now(ZoneInfo(get_settings().local_tz)).date()
    cutoff = today - dt.timedelta(days=lookback_days)
    rows = db.execute(text(_FINDINGS_SQL), {"cutoff": cutoff}).mappings().all()
    result = []
    for row in rows:
        d = dict(row)
        # details comes back as a string from some drivers — parse if needed.
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                d["details"] = {}
        result.append(d)
    return result
