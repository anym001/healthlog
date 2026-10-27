"""switch TimescaleDB telemetry off for existing installs

Revision ID: 0027_timescaledb_telemetry_off
Revises: 0026_heart_notifications_ecg
Create Date: 2026-10-20

TimescaleDB ships with ``timescaledb.telemetry_level = basic``: it periodically
sends an anonymous usage report (versions, OS, table sizes, job stats) to
Timescale and fetches the latest version in return. That contradicts HealthLog's
"nothing leaves your network" (ARCHITECTURE.md §12). New installs get
``TIMESCALEDB_TELEMETRY=off`` from docker-compose.yml, but the image only reads
that on the first initdb, so this migration switches it off instance-wide for
databases created before.

``ALTER SYSTEM`` needs a superuser (the compose default, where the app connects
as ``POSTGRES_USER``) and cannot run inside a transaction, hence the autocommit
block. Without TimescaleDB (plain Postgres, e.g. the test suite) or without
superuser rights it does nothing and logs the manual command instead. Not
reverted on downgrade: re-enabling telemetry is never the safe default.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_timescaledb_telemetry_off"
down_revision: str | None = "0026_heart_notifications_ecg"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

MANUAL_COMMAND = "ALTER SYSTEM SET timescaledb.telemetry_level = 'off'; SELECT pg_reload_conf();"


def upgrade() -> None:
    conn = op.get_bind()
    # missing_ok=true: NULL when the TimescaleDB library isn't loaded.
    level = conn.execute(sa.text("SELECT current_setting('timescaledb.telemetry_level', true)")).scalar()
    if level is None or level == "off":
        return
    is_superuser = conn.execute(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar()
    if not is_superuser:
        log.warning(
            "TimescaleDB telemetry is '%s'; switching it off needs a superuser. Run as one: %s",
            level,
            MANUAL_COMMAND,
        )
        return
    with op.get_context().autocommit_block():
        conn.execute(sa.text("ALTER SYSTEM SET timescaledb.telemetry_level = 'off'"))
        conn.execute(sa.text("SELECT pg_reload_conf()"))


def downgrade() -> None:
    pass
