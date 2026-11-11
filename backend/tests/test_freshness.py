"""Data-freshness check (app.freshness): the pure rules, the queries, and the
hourly alert/all-clear cycle with its marker."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app import config, freshness, notify
from app.freshness import alert_marker, assess, decide, read_alert, read_timestamps, write_alert

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)


def _ago(hours: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=hours)


# --- assess / decide (pure) ------------------------------------------------


def test_fresh_data_is_not_overdue():
    f = assess(_ago(2), _ago(3), NOW, 24)
    assert not f.overdue and f.has_data


def test_no_payload_yet_never_counts_as_overdue():
    f = assess(None, None, NOW, 24)
    assert not f.has_data and not f.overdue
    assert decide(f, alerted=False) is None


def test_payloads_overdue():
    f = assess(_ago(25), _ago(25), NOW, 24)
    assert f.payloads_overdue and f.overdue


def test_payloads_arrive_but_no_new_data_point():
    f = assess(_ago(1), _ago(30), NOW, 24)
    assert not f.payloads_overdue and f.samples_overdue and f.overdue


def test_payloads_without_any_data_point_are_overdue():
    assert assess(_ago(1), None, NOW, 24).samples_overdue


def test_exactly_at_the_threshold_is_not_overdue():
    assert not assess(_ago(24), _ago(24), NOW, 24).overdue


@pytest.mark.parametrize(
    ("payload_age", "alerted", "expected"),
    [
        (30, False, "alert"),  # gap starts
        (30, True, None),  # alert already out: once per gap
        (1, True, "resume"),  # data again after an alert
        (1, False, None),  # routine
    ],
)
def test_decide(payload_age, alerted, expected):
    f = assess(_ago(payload_age), _ago(payload_age), NOW, 24)
    assert decide(f, alerted) == expected


def test_format_local_uses_the_configured_zone():
    assert freshness.format_local(NOW, "Europe/Vienna") == "2026-11-10 13:00"
    assert freshness.format_local(None, "Europe/Vienna") == "none"


# --- marker -------------------------------------------------------------------


def test_alert_marker_roundtrip_and_clear(tmp_path):
    marker = tmp_path / "state" / "freshness_alert"
    assert read_alert(marker) is None
    write_alert(marker, NOW)
    assert read_alert(marker) == NOW
    write_alert(marker, None)
    assert read_alert(marker) is None


def test_read_alert_tolerates_garbage(tmp_path):
    marker = tmp_path / "freshness_alert"
    marker.write_text("not a timestamp")
    assert read_alert(marker) is None


# --- queries ------------------------------------------------------------------


def _payload(db, received_at: dt.datetime) -> None:
    db.execute(
        text("INSERT INTO raw_ingest (received_at, payload, content_hash) VALUES (:t, '{}', :h)"),
        {"t": received_at, "h": received_at.isoformat().encode()},
    )


def _sample(db, time: dt.datetime) -> None:
    db.execute(
        text("INSERT INTO metric_samples (time, metric, source, qty) VALUES (:t, 'step_count', '', 1)"),
        {"t": time},
    )


def test_read_timestamps_takes_the_newest_and_ignores_future_points(db):
    assert read_timestamps(db, NOW) == (None, None)
    _payload(db, _ago(30))
    _payload(db, _ago(5))
    _sample(db, _ago(6))
    _sample(db, NOW + dt.timedelta(days=2))  # wrong phone clock
    assert read_timestamps(db, NOW) == (_ago(5), _ago(6))


# --- the hourly check ---------------------------------------------------------


class _Recorder:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[notify.Notification] = []

    def send(self, notification):
        self.sent.append(notification)
        return self.ok

    def close(self):
        pass


@pytest.fixture
def check(db, tmp_path, monkeypatch):
    """run_check against the test transaction, a temp config dir and a recorder."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notify:\n  url: https://push.example.com\n")
    monkeypatch.setenv("CONFIG_FILE", str(cfg))
    monkeypatch.setenv("NOTIFY_TOKEN", "pb_token")
    monkeypatch.setenv("TZ", "Europe/Vienna")
    config.get_settings.cache_clear()

    @contextmanager
    def test_session():
        yield db

    monkeypatch.setattr(freshness, "db_session", test_session)
    recorder = _Recorder()
    monkeypatch.setattr(notify, "build_notifier", lambda settings: recorder)
    yield recorder, tmp_path / "state" / "freshness_alert"
    config.get_settings.cache_clear()


def test_check_alerts_once_per_gap_then_sends_the_all_clear(db, check):
    recorder, marker = check
    _payload(db, _ago(30))
    _sample(db, _ago(31))

    freshness.run_check(NOW)
    assert [n.title for n in recorder.sent] == ["HealthLog: no new data"]
    assert recorder.sent[0].problem
    assert "2026-11-09 07:00" in recorder.sent[0].message  # last payload, local time
    assert read_alert(marker) == NOW

    freshness.run_check(NOW + dt.timedelta(hours=1))
    assert len(recorder.sent) == 1  # no repeat while the gap lasts

    _payload(db, NOW)
    _sample(db, NOW)
    freshness.run_check(NOW + dt.timedelta(hours=2))
    assert recorder.sent[-1].title == "HealthLog: data arriving again"
    assert read_alert(marker) is None


def test_check_retries_when_the_push_failed(db, check):
    recorder, marker = check
    recorder.ok = False
    _payload(db, _ago(30))
    _sample(db, _ago(30))
    freshness.run_check(NOW)
    assert len(recorder.sent) == 1 and read_alert(marker) is None
    recorder.ok = True
    freshness.run_check(NOW + dt.timedelta(hours=1))
    assert len(recorder.sent) == 2 and read_alert(marker) is not None


def test_check_is_quiet_with_the_source_disabled(db, check, tmp_path):
    recorder, marker = check
    (tmp_path / "config.yaml").write_text("notify:\n  url: https://push.example.com\n  events: [analysis]\n")
    _payload(db, _ago(30))
    freshness.run_check(NOW)
    assert recorder.sent == [] and not marker.exists()


def test_alert_marker_sits_next_to_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    config.get_settings.cache_clear()
    try:
        assert alert_marker(config.get_settings()) == tmp_path / "state" / "freshness_alert"
    finally:
        config.get_settings.cache_clear()
