"""Scheduler trigger from the ANALYSIS_CRON expression (cron-only)."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from app import config
from app.scheduler import build_trigger


def _settings(monkeypatch, **env) -> config.Settings:
    monkeypatch.delenv("ANALYSIS_CRON", raising=False)
    monkeypatch.setenv("LOCAL_TZ", "Europe/Vienna")
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
