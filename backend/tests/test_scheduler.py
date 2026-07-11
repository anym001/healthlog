"""Scheduler trigger from the ANALYSIS_CRON expression, catch-up state."""

from __future__ import annotations

import datetime as dt
import subprocess

from apscheduler.triggers.cron import CronTrigger

from app import config, scheduler
from app.scheduler import build_intraday_trigger, build_trigger, missed_run, read_last_run, write_last_run


def _settings(monkeypatch, **env) -> config.Settings:
    monkeypatch.delenv("ANALYSIS_CRON", raising=False)
    monkeypatch.delenv("INTRADAY_CRON", raising=False)
    monkeypatch.setenv("TZ", "Europe/Vienna")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()
    return config.get_settings()


def test_build_trigger_default_is_daily_0330(monkeypatch):
    try:
        trigger = build_trigger(_settings(monkeypatch))
        assert isinstance(trigger, CronTrigger)
        assert "hour='3'" in str(trigger)
        assert "minute='30'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


def test_build_trigger_uses_custom_cron(monkeypatch):
    try:
        trigger = build_trigger(_settings(monkeypatch, ANALYSIS_CRON="0 4 * * 1"))
        assert "hour='4'" in str(trigger)
        assert "minute='0'" in str(trigger)
        assert "day_of_week='1'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


def test_build_trigger_empty_falls_back_to_default(monkeypatch):
    try:
        trigger = build_trigger(_settings(monkeypatch, ANALYSIS_CRON="   "))
        assert "hour='3'" in str(trigger)
        assert "minute='30'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


def test_build_intraday_trigger_default_is_hourly_at_15(monkeypatch):
    try:
        trigger = build_intraday_trigger(_settings(monkeypatch))
        assert isinstance(trigger, CronTrigger)
        assert "minute='15'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


def test_build_intraday_trigger_off_disables(monkeypatch):
    try:
        assert build_intraday_trigger(_settings(monkeypatch, INTRADAY_CRON="off")) is None
        assert build_intraday_trigger(_settings(monkeypatch, INTRADAY_CRON="OFF")) is None
    finally:
        config.get_settings.cache_clear()


def test_build_intraday_trigger_empty_falls_back_to_default(monkeypatch):
    try:
        trigger = build_intraday_trigger(_settings(monkeypatch, INTRADAY_CRON="   "))
        assert "minute='15'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


def test_build_intraday_trigger_uses_custom_cron(monkeypatch):
    try:
        trigger = build_intraday_trigger(_settings(monkeypatch, INTRADAY_CRON="*/30 * * * *"))
        assert "minute='*/30'" in str(trigger)
    finally:
        config.get_settings.cache_clear()


# --- Catch-up: marker file + missed-slot detection --------------------------


def _daily_0330() -> CronTrigger:
    return CronTrigger.from_crontab("30 3 * * *", timezone="Europe/Vienna")


def test_missed_run_when_no_marker():
    now = dt.datetime(2026, 7, 2, 9, 0, tzinfo=dt.UTC)
    assert missed_run(_daily_0330(), None, now)


def test_missed_run_when_slot_passed_since_last_run():
    # Last success yesterday morning; today's 03:30 slot has passed unrun.
    last = dt.datetime(2026, 7, 1, 2, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 2, 9, 0, tzinfo=dt.UTC)
    assert missed_run(_daily_0330(), last, now)


def test_no_missed_run_after_todays_slot():
    # Last success this morning after the slot; next slot is tomorrow.
    last = dt.datetime(2026, 7, 2, 6, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 2, 9, 0, tzinfo=dt.UTC)
    assert not missed_run(_daily_0330(), last, now)


def test_last_run_marker_roundtrip(tmp_path):
    marker = tmp_path / "state" / "last_analysis"
    assert read_last_run(marker) is None  # missing file
    stamp = dt.datetime(2026, 7, 2, 1, 30, tzinfo=dt.UTC)
    write_last_run(marker, stamp)  # creates parent dirs
    assert read_last_run(marker) == stamp


def test_read_last_run_tolerates_garbage(tmp_path):
    marker = tmp_path / "last_analysis"
    marker.write_text("not a timestamp")
    assert read_last_run(marker) is None


def test_run_analysis_writes_marker_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: None)
    try:
        scheduler.run_analysis()
        assert read_last_run(tmp_path / "state" / "last_analysis") is not None
    finally:
        config.get_settings.cache_clear()


def test_run_analysis_failure_notifies_and_skips_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    config.get_settings.cache_clear()

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "app.analysis")

    crashes: list[Exception] = []
    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    from app import notify

    monkeypatch.setattr(notify, "notify_analysis_crash", lambda cfg, exc: crashes.append(exc))
    try:
        scheduler.run_analysis()
        assert len(crashes) == 1
        assert read_last_run(tmp_path / "state" / "last_analysis") is None
    finally:
        config.get_settings.cache_clear()


def test_run_analysis_survives_unexpected_error(tmp_path, monkeypatch):
    # run_analysis doubles as the startup catch-up call in main(); an escaping
    # OSError there would kill the scheduler before the nightly job is even
    # registered. It must swallow, notify, and skip the marker instead.
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    config.get_settings.cache_clear()

    def boom(*a, **k):
        raise OSError("spawn failed")

    crashes: list[Exception] = []
    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    from app import notify

    monkeypatch.setattr(notify, "notify_analysis_crash", lambda cfg, exc: crashes.append(exc))
    try:
        scheduler.run_analysis()  # must not raise
        assert len(crashes) == 1
        assert read_last_run(tmp_path / "state" / "last_analysis") is None
    finally:
        config.get_settings.cache_clear()


def test_run_analysis_survives_crashing_notifier(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    config.get_settings.cache_clear()

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "app.analysis")

    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    from app import notify

    def notify_boom(cfg, exc):
        raise RuntimeError("gotify config broken")

    monkeypatch.setattr(notify, "notify_analysis_crash", notify_boom)
    try:
        scheduler.run_analysis()  # even a broken crash alert must not propagate
        assert read_last_run(tmp_path / "state" / "last_analysis") is None
    finally:
        config.get_settings.cache_clear()
