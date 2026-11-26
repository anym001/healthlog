"""Keep the read-only Grafana role's privileges to what the dashboards read.

``grafana/README.md`` has the operator create the ``grafana_ro`` login with
``CONNECT`` on the database and nothing else. This module grants it ``SELECT``
on exactly the relations the shipped dashboards query (``DASHBOARD_RELATIONS``)
and revokes everything else, including ``raw_ingest`` (the full HAE payload
archive) and any table a later migration adds.

It runs at every container start, right after the schema migration (the s6
``migrate`` oneshot), because explicit grants do not survive a migration that
drops and recreates a view (``findings_feed`` in 0020/0021, for example). The
old setup worked around that with ``ALTER DEFAULT PRIVILEGES``, which also
exposed every future table; this revokes those defaults too, so an existing
install is narrowed on its next start.

``tests/test_grafana_dashboards.py`` runs every dashboard query as a role with
exactly these grants, so a dashboard that reads a new relation fails there until
the relation is added below.

Usage::

    python -m app.grafana_access

Without a ``grafana_ro`` role it does nothing. A failure is logged and does not
block the start: ingest matters more than dashboards.
"""

from __future__ import annotations

import logging
import sys

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .cli_support import bootstrap, db_session

log = logging.getLogger("healthlog.grafana_access")

ROLE = "grafana_ro"

# Every table and view a query in grafana/dashboards/*.json reads (panels,
# variables and annotations), directly or through a SECURITY INVOKER function
# (workout_trimp / daily_trimp).
DASHBOARD_RELATIONS = (
    "app_settings",
    "body_battery_daily",
    "body_battery_intraday",
    "daily_metrics",
    "ecg_recordings",
    "findings",
    "findings_feed",
    "findings_history",
    "heart_rate_notifications",
    "metric_registry",
    "metric_samples",
    "sleep_metrics",
    "sleep_nightly",
    "state_of_mind",
    "stress_daily",
    "stress_intraday",
    "workout_hr_samples",
    "workout_load_daily",
    "workout_route_points",
    "workout_type_groups",
    "workouts",
)


def role_exists(db: Session, role: str = ROLE) -> bool:
    return db.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first() is not None


def sync_grafana_grants(db: Session, role: str = ROLE) -> bool:
    """Reset ``role``'s privileges in ``public`` to read-only on the dashboard relations.

    Returns False (and changes nothing) if the role does not exist. Does not
    commit; the caller owns the transaction, so the revoke and the grants land
    together.
    """
    if not role_exists(db, role):
        return False
    quote = db.get_bind().dialect.identifier_preparer.quote
    grantee = quote(role)
    relations = ", ".join(quote(name) for name in DASHBOARD_RELATIONS)
    # The defaults are per granting role: this clears the ones the current role
    # (the app's, which grafana/README.md used to set them as) has set.
    db.execute(sa.text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {grantee}"))
    db.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {grantee}"))
    db.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {grantee}"))
    db.execute(sa.text(f"GRANT SELECT ON {relations} TO {grantee}"))
    return True


def main() -> int:
    bootstrap()
    try:
        with db_session() as db:
            if sync_grafana_grants(db):
                db.commit()
                log.info("%s: SELECT on %d dashboard relations, nothing else", ROLE, len(DASHBOARD_RELATIONS))
            else:
                log.info("no %s role, skipping Grafana grants (see grafana/README.md)", ROLE)
    except sa.exc.SQLAlchemyError as exc:
        log.error("could not set %s's grants, Grafana panels may be empty: %s", ROLE, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
