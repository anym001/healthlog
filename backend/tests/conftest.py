"""Test fixtures.

Tests run against a real Postgres (DATABASE_URL, a TimescaleDB service in CI).
The schema is migrated once per session; each test runs inside a transaction
that is rolled back, with a SAVEPOINT so endpoint ``commit()`` calls stay
isolated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+psycopg://healthlog:healthlog@127.0.0.1:5432/healthlog"


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def engine(database_url):
    os.environ["DATABASE_URL"] = database_url
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(database_url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db_conn(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield conn
    trans.rollback()
    conn.close()


@pytest.fixture
def db(db_conn) -> Session:
    session = Session(bind=db_conn, join_transaction_mode="create_savepoint")
    yield session
    session.close()


@pytest.fixture
def client(db_conn, monkeypatch):
    """TestClient sharing the test transaction; endpoint commits stay isolated."""
    monkeypatch.setenv("INGEST_SECRET", "test-secret")
    # get_settings is cached; clear so the env override is picked up.
    from app import config

    config.get_settings.cache_clear()

    from app.database import get_db
    from app.main import app

    def override_get_db():
        session = Session(bind=db_conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    config.get_settings.cache_clear()


@pytest.fixture
def sample_payload() -> dict:
    import json

    return json.loads((Path(__file__).parent / "fixtures" / "hae_sample.json").read_text())
