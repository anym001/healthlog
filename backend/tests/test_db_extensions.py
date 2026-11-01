"""TimescaleDB extension check at container start (app.db_extensions)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db_extensions import ExtensionState, _conninfo, _version_key, plan, run


@pytest.mark.parametrize(
    ("installed", "available", "superuser", "expected"),
    [
        (None, None, True, "absent"),  # plain Postgres
        (None, "2.31.0", True, "absent"),  # available but never created
        ("2.31.0", "2.31.0", True, "current"),
        ("2.17.2", "2.31.0", True, "update"),
        ("2.17.2", "2.31.0", False, "manual"),
        ("2.31.0", "2.17.2", True, "newer_installed"),  # image rolled back
        ("2.9.0", "2.10.0", True, "update"),  # numeric, not string, order
    ],
)
def test_plan(installed, available, superuser, expected):
    assert plan(ExtensionState(installed, available, superuser)) == expected


def test_version_key_ignores_suffix():
    assert _version_key("2.31.0-dev") == (2, 31, 0)


def test_conninfo_drops_the_sqlalchemy_driver():
    url = "postgresql+psycopg://healthlog:s3cret@healthlog-db:5432/healthlog"
    assert _conninfo(url) == "postgresql://healthlog:s3cret@healthlog-db:5432/healthlog"


def test_run_against_the_test_database(engine, database_url):
    # A freshly created extension (CI's TimescaleDB) is the image's default
    # version, so there is nothing to update; plain Postgres has none.
    with engine.connect() as conn:
        has_ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).scalar()
    assert run(database_url) == ("current" if has_ext else "absent")
