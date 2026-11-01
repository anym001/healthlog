"""Keep the TimescaleDB extension in step with the database image.

A newer ``timescale/timescaledb`` image ships a newer extension binary, but the
database keeps the version it has installed until ``ALTER EXTENSION timescaledb
UPDATE`` runs (README "Updating the database image"). This module does that at
every container start, before the schema migration (the s6 ``migrate``
oneshot). Like migration 0027 for telemetry, it acts only when the app's
database role is a superuser (the compose default) and otherwise logs the
command to run by hand. On plain Postgres, or when the installed version is
already the newest the image offers, it does nothing.

The update has to be the first statement of a fresh session, before anything
loads the old extension library, so it runs on its own raw psycopg connection:
SQLAlchemy would send queries of its own on connect first.

A failed update is logged with the manual command and does not block the
start; the database keeps the version it had.

Usage::

    python -m app.db_extensions
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass

import psycopg
from sqlalchemy.engine import make_url

from .cli_support import bootstrap

log = logging.getLogger("healthlog.db_extensions")

EXTENSION = "timescaledb"
# The container and role names vary per setup, so only the psql part is given.
MANUAL_COMMAND = 'psql -X -d healthlog -c "ALTER EXTENSION timescaledb UPDATE;"'


@dataclass(frozen=True)
class ExtensionState:
    installed: str | None  # pg_extension.extversion; None when not installed
    available: str | None  # pg_available_extensions.default_version of the image
    is_superuser: bool


def _version_key(version: str) -> tuple[int, ...]:
    """``'2.31.0'`` -> ``(2, 31, 0)``; a suffix such as ``-dev`` is ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", 1)[0]))


def plan(state: ExtensionState) -> str:
    """What to do: ``absent``, ``current``, ``update``, ``manual`` (no superuser)
    or ``newer_installed`` (the database is ahead of the image, e.g. after
    rolling the image back; nothing to do, but worth a warning)."""
    if state.installed is None or state.available is None:
        return "absent"
    if _version_key(state.installed) == _version_key(state.available):
        return "current"
    if _version_key(state.installed) > _version_key(state.available):
        return "newer_installed"
    return "update" if state.is_superuser else "manual"


def _conninfo(database_url: str) -> str:
    """The SQLAlchemy URL (``postgresql+psycopg://…``) as a libpq URI."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def read_state(conninfo: str) -> ExtensionState:
    with psycopg.connect(conninfo, connect_timeout=5) as conn:
        installed, available, is_superuser = conn.execute(
            """
            SELECT
              (SELECT extversion FROM pg_extension WHERE extname = %(ext)s),
              (SELECT default_version FROM pg_available_extensions WHERE name = %(ext)s),
              (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
            """,
            {"ext": EXTENSION},
        ).fetchone()
    return ExtensionState(installed=installed, available=available, is_superuser=bool(is_superuser))


def update_extension(conninfo: str) -> None:
    # Fresh connection, nothing sent before the ALTER (see the module docstring).
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=5) as conn:
        conn.execute(f"ALTER EXTENSION {EXTENSION} UPDATE")


def run(database_url: str) -> str:
    """Check the extension and update it if needed; returns the ``plan`` action."""
    conninfo = _conninfo(database_url)
    state = read_state(conninfo)
    action = plan(state)
    if action == "update":
        try:
            update_extension(conninfo)
        except psycopg.Error as exc:
            log.error(
                "TimescaleDB %s -> %s: update failed (%s). Run it by hand: %s",
                state.installed,
                state.available,
                exc,
                MANUAL_COMMAND,
            )
            return "failed"
        log.info("TimescaleDB extension updated: %s -> %s", state.installed, state.available)
    elif action == "manual":
        log.warning(
            "TimescaleDB extension is %s, the image offers %s; updating it needs a superuser. Run as one: %s",
            state.installed,
            state.available,
            MANUAL_COMMAND,
        )
    elif action == "newer_installed":
        log.warning(
            "TimescaleDB extension %s is newer than the image's %s; use an image with at least %s",
            state.installed,
            state.available,
            state.installed,
        )
    return action


def main() -> int:
    settings = bootstrap()
    run(settings.database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
