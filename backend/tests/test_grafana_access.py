"""grafana_ro's privileges reset to the dashboard relations (app.grafana_access).

That every dashboard query runs with exactly these grants is covered by
test_grafana_dashboards.py; this covers the reset itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.grafana_access import DASHBOARD_RELATIONS, role_exists, sync_grafana_grants

ROLE = "healthlog_grafana_access_test"


def _can_select(db, relation: str) -> bool:
    return db.execute(text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": ROLE, "t": relation}).scalar_one()


@pytest.fixture
def role(db):
    db.execute(text(f"CREATE ROLE {ROLE} NOLOGIN"))  # rolled back with the test
    return ROLE


def test_without_the_role_nothing_happens(db):
    assert not role_exists(db, ROLE)
    assert sync_grafana_grants(db, ROLE) is False


def test_grants_exactly_the_dashboard_relations(db, role):
    assert sync_grafana_grants(db, role) is True
    for relation in DASHBOARD_RELATIONS:
        assert _can_select(db, relation), relation
    assert not _can_select(db, "raw_ingest")
    assert not _can_select(db, "alembic_version")


def test_narrows_the_old_broad_grant(db, role):
    # What grafana/README.md used to have operators run.
    db.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"))
    db.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}"))
    assert _can_select(db, "raw_ingest")

    sync_grafana_grants(db, role)
    assert not _can_select(db, "raw_ingest")
    db.execute(text("CREATE TABLE grafana_access_probe (x int)"))
    assert not _can_select(db, "grafana_access_probe")  # the default privileges are gone


def test_restores_the_grant_on_a_recreated_view(db, role):
    # Migrations drop and recreate views (findings_feed in 0020/0021), which
    # drops explicit grants; the next start's sync puts them back.
    sync_grafana_grants(db, role)
    definition = db.execute(text("SELECT pg_get_viewdef('findings_feed')")).scalar_one()
    db.execute(text("DROP VIEW findings_feed"))
    db.execute(text(f"CREATE VIEW findings_feed AS {definition}"))
    assert not _can_select(db, "findings_feed")

    sync_grafana_grants(db, role)
    assert _can_select(db, "findings_feed")


def test_is_idempotent(db, role):
    assert sync_grafana_grants(db, role)
    assert sync_grafana_grants(db, role)
    assert _can_select(db, "daily_metrics")
