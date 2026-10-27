"""TimescaleDB telemetry is switched off by migration 0027 (ARCHITECTURE.md §12)."""

from __future__ import annotations

import pytest
from sqlalchemy import text


def test_timescaledb_telemetry_is_off(engine):
    # A fresh connection, so the reloaded instance-wide setting is visible.
    with engine.connect() as conn:
        level = conn.execute(text("SELECT current_setting('timescaledb.telemetry_level', true)")).scalar()
    if level is None:
        pytest.skip("TimescaleDB not loaded (plain Postgres)")
    assert level == "off"
