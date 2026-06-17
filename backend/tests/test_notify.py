"""Unit tests for the push-notification module.

These are pure (no DB, no real network): the Gotify client is driven through an
httpx MockTransport, and the dispatchers are tested with ``build_notifier``
monkeypatched to a recorder so the event/level gating can be asserted directly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import notify
from app.config import Settings
from app.notify import (
    PRIORITY_INFO,
    PRIORITY_PROBLEM,
    GotifyNotifier,
    Notification,
    build_notifier,
    compose_analysis_crash_message,
    compose_analysis_run_message,
    compose_findings_message,
    compose_ingest_message,
    notify_analysis,
    notify_analysis_crash,
    notify_ingest,
)

_NOTE = Notification("t", "m", PRIORITY_INFO, False)


def _settings(**kw) -> Settings:
    base = {"notify_url": "https://push.example.com", "notify_token": "pb_token"}
    base.update(kw)
    return Settings(**base)


def _result(**kw):
    """Stand-in for analysis.AnalysisResult (duck-typed counters)."""
    counts = {
        "correlations": 0,
        "anomalies": 0,
        "trends": 0,
        "seasonality": 0,
        "recovery_alerts": 0,
        "consistency": 0,
    }
    counts.update(kw)
    return SimpleNamespace(**counts)


def _notifier(handler) -> GotifyNotifier:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GotifyNotifier("https://push.example.com", "pb_token", client=http)


class _Recorder:
    """Stands in for GotifyNotifier in the dispatch tests."""

    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(notify, "build_notifier", lambda settings: rec)
    return rec


# --- GotifyNotifier (the wire format) --------------------------------------


def test_send_posts_gotify_message():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.url.params.get("token")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": 1})

    assert _notifier(handler).send(Notification("HealthLog: analysis OK", "anomalies: 0", PRIORITY_INFO, False))
    assert seen["path"] == "/message"
    assert seen["token"] == "pb_token"
    assert seen["body"] == {
        "title": "HealthLog: analysis OK",
        "message": "anomalies: 0",
        "priority": PRIORITY_INFO,
    }


def test_send_is_best_effort_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    assert _notifier(handler).send(_NOTE) is False


def test_send_is_best_effort_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert _notifier(handler).send(_NOTE) is False


# --- build_notifier --------------------------------------------------------


def test_build_notifier_off_without_url():
    assert build_notifier(Settings(notify_url="")) is None


def test_build_notifier_requires_token():
    with pytest.raises(ValueError, match="NOTIFY_TOKEN"):
        build_notifier(Settings(notify_url="https://push.example.com", notify_token=""))


# --- Composers -------------------------------------------------------------


def test_compose_analysis_run_message():
    note = compose_analysis_run_message(_result(correlations=3, anomalies=1))
    assert note.problem is False
    assert note.priority == PRIORITY_INFO
    assert "correlations: 3" in note.message


def test_compose_analysis_crash_message():
    note = compose_analysis_crash_message(RuntimeError("boom"))
    assert note.problem is True
    assert note.priority == PRIORITY_PROBLEM
    assert "RuntimeError: boom" in note.message


def test_compose_findings_message_skips_when_no_alerts():
    assert compose_findings_message(_result(correlations=5, trends=2)) is None


def test_compose_findings_message_reports_alerts():
    note = compose_findings_message(_result(anomalies=2, recovery_alerts=1))
    assert note is not None
    assert note.problem is True
    assert note.priority == PRIORITY_PROBLEM
    assert "anomalies: 2" in note.message
    assert "recovery alerts: 1" in note.message


def test_compose_ingest_messages():
    empty = compose_ingest_message("empty", 0, 0, 0)
    assert empty.problem is True
    assert empty.priority == PRIORITY_PROBLEM
    stored = compose_ingest_message("stored", 12, 1, 0)
    assert stored.problem is False
    assert "metrics: 12" in stored.message


# --- Dispatch: analysis + findings -----------------------------------------


def test_notify_analysis_problems_level_suppresses_clean_summary(recorder):
    notify_analysis(_settings(notify_events="analysis,findings", notify_level="problems"), _result(correlations=4))
    assert recorder.sent == []


def test_notify_analysis_problems_level_still_sends_alerts(recorder):
    notify_analysis(_settings(notify_events="analysis,findings", notify_level="problems"), _result(anomalies=2))
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: health alerts"


def test_notify_analysis_always_level_sends_summary_and_alerts(recorder):
    notify_analysis(_settings(notify_events="analysis,findings", notify_level="always"), _result(anomalies=1))
    titles = [n.title for n in recorder.sent]
    assert titles == ["HealthLog: analysis OK", "HealthLog: health alerts"]


def test_notify_analysis_respects_disabled_sources(recorder):
    # Only findings enabled: a clean run with no alerts sends nothing.
    notify_analysis(_settings(notify_events="findings", notify_level="always"), _result(correlations=3))
    assert recorder.sent == []


def test_notify_analysis_crash_gated_on_analysis_source(recorder):
    notify_analysis_crash(_settings(notify_events="findings"), RuntimeError("boom"))
    assert recorder.sent == []
    notify_analysis_crash(_settings(notify_events="analysis"), RuntimeError("boom"))
    assert len(recorder.sent) == 1
    assert recorder.sent[0].problem is True


# --- Dispatch: ingest ------------------------------------------------------


def test_notify_ingest_empty_is_a_problem_at_any_level(recorder):
    settings = _settings(notify_events="ingest", notify_level="problems")
    notify_ingest(settings, metric_rows=0, sleep_rows=0, workout_rows=0)
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: empty ingest"


def test_notify_ingest_success_only_at_always_level(recorder):
    s_problems = _settings(notify_events="ingest", notify_level="problems")
    notify_ingest(s_problems, metric_rows=10, sleep_rows=1, workout_rows=0)
    assert recorder.sent == []
    s_always = _settings(notify_events="ingest", notify_level="always")
    notify_ingest(s_always, metric_rows=10, sleep_rows=1, workout_rows=0)
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: data ingested"


def test_notify_ingest_disabled_source(recorder):
    notify_ingest(_settings(notify_events="analysis"), metric_rows=0, sleep_rows=0, workout_rows=0)
    assert recorder.sent == []


# --- Best-effort: misconfiguration never raises ----------------------------


def test_dispatch_swallows_missing_token():
    # URL set, token missing => build_notifier raises ValueError; the dispatcher
    # must swallow it so a misconfigured notifier never breaks a run.
    settings = Settings(notify_url="https://push.example.com", notify_token="", notify_events="analysis")
    notify_analysis_crash(settings, RuntimeError("boom"))  # no exception


def test_dispatch_noop_when_notifications_off():
    settings = Settings(notify_url="", notify_events="analysis,findings")
    notify_analysis(settings, _result(anomalies=5))  # no exception, nothing sent
