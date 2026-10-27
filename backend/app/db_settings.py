"""Mirror runtime settings into the database for SQL-side consumers.

The analysis reads ``TZ`` from the environment, but SQL views and Grafana
queries cannot. This module writes such settings into ``app_settings``
(migration 0022) so the ``daily_metrics`` view and the dashboards bucket days
in the same zone as the analysis.

It runs at every container start, right after the schema migration (the s6
``migrate`` oneshot), so changing ``TZ`` in ``.env`` and recreating the
container is enough. Derived history (stress / Body Battery timelines,
findings) is recomputed with ``healthlog rederive-stress``,
``rederive-body-battery`` and ``analyze``.

Usage::

    python -m app.db_settings

An invalid ``TZ`` (not an IANA zone name) fails loudly instead of being
written: the view would otherwise error on every query.
"""

from __future__ import annotations

import logging
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .cli_support import bootstrap, db_session
from .models import AppSetting

log = logging.getLogger("healthlog.db_settings")

LOCAL_TZ_KEY = "local_tz"


def validate_tz(name: str) -> str:
    """Return ``name`` if it is a known IANA zone, else raise ``ValueError``."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"TZ={name!r} is not a valid IANA time zone (e.g. 'America/New_York')") from exc
    return name


def sync_db_settings(db: Session, local_tz: str) -> bool:
    """Upsert the settings SQL consumers need. Returns True if a value changed.

    Does not commit; the caller owns the transaction.
    """
    validate_tz(local_tz)
    current = db.execute(sa.select(AppSetting.value).where(AppSetting.key == LOCAL_TZ_KEY)).scalar_one_or_none()
    if current == local_tz:
        return False
    stmt = insert(AppSetting).values(key=LOCAL_TZ_KEY, value=local_tz)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": stmt.excluded.value, "updated_at": sa.func.now()},
        )
    )
    log.info("app_settings: local_tz %s -> %s", current, local_tz)
    return True


def main() -> int:
    settings = bootstrap()
    try:
        with db_session() as db:
            sync_db_settings(db, settings.local_tz)
            db.commit()
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
