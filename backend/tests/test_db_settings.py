"""TZ mirrored into app_settings, and the daily_metrics view following it (migration 0022)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app.db_settings import LOCAL_TZ_KEY, sync_db_settings, validate_tz
from app.models import MetricSample

# 03:30 UTC on Jan 2 is 04:30 Jan 2 in Vienna but 22:30 Jan 1 in New York.
SAMPLE_TIME = dt.datetime(2026, 1, 2, 3, 30, tzinfo=dt.UTC)


def _local_tz(db) -> str | None:
    return db.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": LOCAL_TZ_KEY}).scalar()


def _view_day(db) -> str:
    return str(db.execute(text("SELECT day FROM daily_metrics WHERE metric = 'tz_probe'")).scalar_one())


def test_migration_seeds_the_previous_default(db):
    assert _local_tz(db) == "Europe/Vienna"


def test_sync_writes_and_reports_changes(db):
    assert sync_db_settings(db, "America/New_York") is True
    assert _local_tz(db) == "America/New_York"
    assert sync_db_settings(db, "America/New_York") is False  # unchanged -> no write


def test_sync_rejects_an_invalid_zone(db):
    with pytest.raises(ValueError, match="not a valid IANA time zone"):
        sync_db_settings(db, "Mars/Olympus_Mons")
    assert _local_tz(db) == "Europe/Vienna"


def test_validate_tz_accepts_iana_names():
    assert validate_tz("America/New_York") == "America/New_York"


def test_daily_view_buckets_in_the_configured_zone(db):
    db.add(MetricSample(time=SAMPLE_TIME, metric="tz_probe", source="", qty=1.0))
    db.flush()
    assert _view_day(db) == "2026-01-02"  # seeded Europe/Vienna

    sync_db_settings(db, "America/New_York")
    assert _view_day(db) == "2026-01-01"


def test_daily_view_falls_back_when_the_setting_is_missing(db):
    db.add(MetricSample(time=SAMPLE_TIME, metric="tz_probe", source="", qty=1.0))
    db.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": LOCAL_TZ_KEY})
    db.flush()
    assert _view_day(db) == "2026-01-02"  # Europe/Vienna fallback
