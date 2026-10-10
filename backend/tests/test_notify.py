"""Unit tests for the push-notification module.

These are pure (no DB, no real network): the Gotify client is driven through an
httpx MockTransport, and the dispatchers are tested with ``build_notifier``
monkeypatched to a recorder so the event/level gating can be asserted directly.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

from app import notify
from app.appconfig import NotifyConfig
from app.notify import (
    MAX_ALERT_LINES,
    PRIORITY_INFO,
    PRIORITY_PROBLEM,
    AlertItem,
    GotifyNotifier,
    Notification,
    build_notifier,
    compose_analysis_crash_message,
    compose_analysis_run_message,
    compose_findings_message,
    compose_ingest_message,
    new_alerts,
    notify_analysis,
    notify_analysis_crash,
    notify_ingest,
)

_NOTE = Notification("t", "m", PRIORITY_INFO, False)


def _notify(**kw) -> NotifyConfig:
    base = {"url": "https://push.example.com", "token": "pb_token"}
    base.update(kw)
    return NotifyConfig(**base)


def _result(**kw):
    """Stand-in for analysis.AnalysisResult (duck-typed counters)."""
    counts = {
        "correlations": 0,
        "anomalies": 0,
        "trends": 0,
        "seasonality": 0,
        "recovery_alerts": 0,
        "consistency": 0,
        "training_load": 0,
        "training_status": 0,
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


def test_send_is_best_effort_on_any_exception():
    # The "never propagates" promise must hold beyond httpx errors — e.g. a
    # bug while building the request body may not crash the surrounding run.
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected serialization bug")

    assert _notifier(handler).send(_NOTE) is False


# --- build_notifier --------------------------------------------------------


def test_build_notifier_off_without_url():
    assert build_notifier(NotifyConfig(url=None)) is None


def test_build_notifier_requires_token():
    with pytest.raises(ValueError, match="NOTIFY_TOKEN"):
        build_notifier(NotifyConfig(url="https://push.example.com", token=None))


def test_build_notifier_default_type_is_gotify():
    cfg = NotifyConfig(url="https://push.example.com", token="t")
    notifier = build_notifier(cfg)
    assert isinstance(notifier, GotifyNotifier)
    notifier.close()


def test_build_notifier_explicit_gotify_type():
    cfg = NotifyConfig(type="gotify", url="https://push.example.com", token="t")
    notifier = build_notifier(cfg)
    assert isinstance(notifier, GotifyNotifier)
    notifier.close()


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


_D1 = dt.date(2026, 10, 8)
_D2 = dt.date(2026, 10, 9)


def _anomaly(metric="resting_heart_rate", day=_D2) -> AlertItem:
    return AlertItem("anomaly", metric, day, 4.2)


def test_compose_findings_message_skips_when_no_alerts():
    assert compose_findings_message([]) is None


def test_compose_findings_message_lists_new_alerts():
    note = compose_findings_message([_anomaly(), AlertItem("recovery_alert", "recovery", _D1, 2.0)])
    assert note is not None
    assert note.problem is True
    assert note.priority == PRIORITY_PROBLEM
    lines = note.message.splitlines()
    assert lines[0] == "new alerts: 2"
    # Newest first.
    assert lines[1] == "anomaly: resting_heart_rate (2026-10-09)"
    assert lines[2] == "recovery alert (2026-10-08)"


def test_compose_findings_message_reports_training_load_alone():
    note = compose_findings_message([AlertItem("training_load", "workout_load", _D2, 1.8)])
    assert note is not None
    assert "training load spike: workout_load" in note.message


def test_compose_findings_message_caps_lines():
    alerts = [_anomaly(f"m{i}") for i in range(MAX_ALERT_LINES + 3)]
    lines = compose_findings_message(alerts).message.splitlines()
    assert lines[0] == f"new alerts: {MAX_ALERT_LINES + 3}"
    assert len(lines) == MAX_ALERT_LINES + 2
    assert lines[-1] == "+3 more"


def test_new_alerts_drops_already_reported_days():
    # The same anomaly day stays in the recent window across runs: report once.
    previous = [_anomaly(day=_D1)]
    current = [_anomaly(day=_D1), _anomaly(day=_D2)]
    assert new_alerts(previous, current) == [_anomaly(day=_D2)]
    assert new_alerts(current, current) == []


def test_new_alerts_first_run_reports_everything():
    assert new_alerts([], [_anomaly()]) == [_anomaly()]


def test_new_alerts_training_load_is_keyed_by_state_not_date():
    # ref_date moves every run while the ACWR stays out of band: no repeat ...
    yesterday = [AlertItem("training_load", "workout_load", _D1, 1.7)]
    today = [AlertItem("training_load", "workout_load", _D2, 1.9)]
    assert new_alerts(yesterday, today) == []
    # ... but a flip from spike to detraining is new.
    flipped = [AlertItem("training_load", "workout_load", _D2, 0.6)]
    assert new_alerts(yesterday, flipped) == flipped


def test_compose_ingest_empty_message():
    note = compose_ingest_message("empty", 0, 0, 0)
    assert note.problem is True
    assert note.priority == PRIORITY_PROBLEM


def test_compose_ingest_stored_all_new():
    # All rows are fresh inserts: no "updated" suffix, just the count.
    note = compose_ingest_message("stored", 12, 1, 3, metric_new=12, sleep_new=1, workout_new=3)
    assert note.problem is False
    assert "metrics: 12" in note.message
    assert "sleep: 1" in note.message
    assert "workouts: 3" in note.message
    assert "updated" not in note.message


def test_compose_ingest_stored_with_updates():
    # Some rows already existed: show the new/updated split.
    note = compose_ingest_message("stored", 10, 1, 158, metric_new=0, sleep_new=0, workout_new=0)
    assert "metrics: 0 new, 10 updated" in note.message
    assert "sleep: 0 new, 1 updated" in note.message
    assert "workouts: 0 new, 158 updated" in note.message


def test_compose_ingest_stored_mixed():
    note = compose_ingest_message("stored", 10, 1, 5, metric_new=7, sleep_new=1, workout_new=2)
    assert "metrics: 7 new, 3 updated" in note.message
    assert "sleep: 1" in note.message  # all new, no suffix
    assert "workouts: 2 new, 3 updated" in note.message


# --- Dispatch: analysis + findings -----------------------------------------


def test_notify_analysis_problems_level_suppresses_clean_summary(recorder):
    notify_analysis(_notify(events=["analysis", "findings"], level="problems"), _result(correlations=4))
    assert recorder.sent == []


def test_notify_analysis_problems_level_still_sends_alerts(recorder):
    notify_analysis(_notify(events=["analysis", "findings"], level="problems"), _result(anomalies=2), [_anomaly()])
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: health alerts"


def test_notify_analysis_always_level_sends_summary_and_alerts(recorder):
    notify_analysis(_notify(events=["analysis", "findings"], level="always"), _result(anomalies=1), [_anomaly()])
    titles = [n.title for n in recorder.sent]
    assert titles == ["HealthLog: analysis OK", "HealthLog: health alerts"]


def test_notify_analysis_skips_alerts_already_reported(recorder):
    # The run still counts anomalies in its window, but none are new: no push.
    notify_analysis(_notify(events=["findings"], level="problems"), _result(anomalies=18), [])
    assert recorder.sent == []


def test_notify_analysis_respects_disabled_sources(recorder):
    # Only findings enabled: a clean run with no alerts sends nothing.
    notify_analysis(_notify(events=["findings"], level="always"), _result(correlations=3))
    assert recorder.sent == []


def test_notify_analysis_crash_gated_on_analysis_source(recorder):
    notify_analysis_crash(_notify(events=["findings"]), RuntimeError("boom"))
    assert recorder.sent == []
    notify_analysis_crash(_notify(events=["analysis"]), RuntimeError("boom"))
    assert len(recorder.sent) == 1
    assert recorder.sent[0].problem is True


# --- Dispatch: ingest ------------------------------------------------------


def test_notify_ingest_empty_is_a_problem_at_any_level(recorder):
    notify_ingest(_notify(events=["ingest"], level="problems"), metric_rows=0, sleep_rows=0, workout_rows=0)
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: empty ingest"


def test_notify_ingest_success_only_at_always_level(recorder):
    notify_ingest(_notify(events=["ingest"], level="problems"), metric_rows=10, sleep_rows=1, workout_rows=0)
    assert recorder.sent == []
    notify_ingest(_notify(events=["ingest"], level="always"), metric_rows=10, sleep_rows=1, workout_rows=0)
    assert len(recorder.sent) == 1
    assert recorder.sent[0].title == "HealthLog: data ingested"


def test_notify_ingest_disabled_source(recorder):
    notify_ingest(_notify(events=["analysis"]), metric_rows=0, sleep_rows=0, workout_rows=0)
    assert recorder.sent == []


# --- Best-effort: misconfiguration never raises ----------------------------


def test_dispatch_swallows_missing_token():
    # URL set, token missing => build_notifier raises ValueError; the dispatcher
    # must swallow it so a misconfigured notifier never breaks a run.
    cfg = NotifyConfig(url="https://push.example.com", token=None, events=["analysis"])
    notify_analysis_crash(cfg, RuntimeError("boom"))  # no exception


def test_dispatch_noop_when_notifications_off():
    cfg = NotifyConfig(url=None, events=["analysis", "findings"])
    notify_analysis(cfg, _result(anomalies=5))  # no exception, nothing sent
