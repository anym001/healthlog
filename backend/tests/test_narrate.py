"""Unit tests for the narration module.

Pure tests — no DB, no real network. The Ollama client is driven through an
httpx MockTransport; context building and privacy scrubbing are tested with
synthetic findings dicts.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.narrate import (
    OllamaClient,
    _metric_domain,
    _pair_tier,
    _system_prompt,
    build_context,
    report_priority,
    scrub_details,
    write_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = dt.date(2026, 6, 22)
_COMPUTED_AT = dt.datetime(2026, 6, 22, 3, 30, tzinfo=dt.UTC)


def _finding(kind: str, **kw) -> dict:
    base: dict = {
        "kind": kind,
        "metric_a": "heart_rate_variability",
        "metric_a_label": "Herzfrequenzvariabilität",
        "metric_b": None,
        "metric_b_label": None,
        "lag_days": None,
        "coefficient": None,
        "p_value_adj": None,
        "ref_date": _TODAY,
        "window_start": None,
        "window_end": None,
        "severity": None,
        "details": None,
        "note": None,
        "computed_at": _COMPUTED_AT,
    }
    base.update(kw)
    return base


def _client(handler) -> OllamaClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaClient("http://mac:11434", "qwen2.5:14b", client=http)


# ---------------------------------------------------------------------------
# scrub_details — privacy boundary
# ---------------------------------------------------------------------------


def test_scrub_details_anomaly_removes_raw_value():
    result = scrub_details("anomaly", {"z": 3.9, "value": 72.5})
    assert result == {"z": 3.9}
    assert "value" not in result


def test_scrub_details_anomaly_none_returns_empty():
    assert scrub_details("anomaly", None) == {}


def test_scrub_details_anomaly_missing_z_returns_empty():
    assert scrub_details("anomaly", {"value": 72.5}) == {}


def test_scrub_details_recovery_alert_keeps_expected_keys():
    d = {"rhr_z": 1.8, "hrv_z": -2.1, "short_sleep": True, "extra": 99}
    result = scrub_details("recovery_alert", d)
    assert result == {"rhr_z": 1.8, "hrv_z": -2.1, "short_sleep": True}
    assert "extra" not in result


def test_scrub_details_recovery_alert_none_returns_empty():
    assert scrub_details("recovery_alert", None) == {}


def test_scrub_details_trend_passes_through():
    d = {"slope_per_day": 0.12, "strength": 0.45}
    assert scrub_details("trend", d) == d


def test_scrub_details_training_load_passes_through():
    d = {"ratio": 1.72, "acute": 85.3, "chronic": 49.6}
    assert scrub_details("training_load", d) == d


def test_scrub_details_unknown_kind_passes_through():
    d = {"foo": "bar"}
    assert scrub_details("future_kind", d) == d


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


def test_build_context_empty_findings():
    ctx = build_context([], 7, _TODAY)
    assert "–" in ctx  # each section should show the empty marker


def test_build_context_contains_report_date():
    ctx = build_context([], 7, _TODAY)
    assert str(_TODAY) in ctx


def test_build_context_contains_computed_at():
    findings = [_finding("anomaly", details={"z": 3.9, "value": 55.0}, severity=3.9)]
    ctx = build_context(findings, 7, _TODAY)
    assert "03:30" in ctx


def test_build_context_anomaly_shows_z_score():
    findings = [_finding("anomaly", details={"z": 3.9, "value": 55.0}, severity=3.9)]
    ctx = build_context(findings, 7, _TODAY)
    assert "3.90" in ctx


def test_build_context_anomaly_does_not_contain_raw_value():
    findings = [_finding("anomaly", details={"z": 3.9, "value": 72.5}, severity=3.9)]
    ctx = build_context(findings, 7, _TODAY)
    # 72.5 is the raw sensor value — must not appear
    assert "72.5" not in ctx


def test_build_context_uses_display_name_not_metric_key():
    findings = [_finding("anomaly", details={"z": 2.1}, severity=2.1)]
    ctx = build_context(findings, 7, _TODAY)
    assert "Herzfrequenzvariabilität" in ctx
    assert "heart_rate_variability" not in ctx


def test_build_context_recovery_alert_shows_z_scores_and_sleep():
    f = _finding(
        "recovery_alert",
        metric_a="recovery",
        metric_a_label="Erholung",
        details={"hrv_z": -2.1, "rhr_z": 1.8, "short_sleep": True},
    )
    ctx = build_context([f], 7, _TODAY)
    assert "HRV-z=-2.10" in ctx
    assert "RHR-z=1.80" in ctx
    assert "kurzer Schlaf" in ctx


def test_build_context_correlation_shows_lag_and_coef():
    f = _finding(
        "correlation",
        metric_a="heart_rate_variability",
        metric_a_label="Herzfrequenzvariabilität",
        metric_b="resting_heart_rate",
        metric_b_label="Ruhepuls",
        lag_days=1,
        coefficient=-0.62,
        p_value_adj=0.003,
        details={"n": 84},
        ref_date=None,
    )
    ctx = build_context([f], 7, _TODAY)
    assert "-0.620" in ctx
    assert "0.0030" in ctx
    assert "N=84" in ctx
    assert "Lag 1" in ctx or "lag 1" in ctx


def test_metric_domain_classification():
    assert _metric_domain("sleep_total_h") == "sleep"
    assert _metric_domain("respiratory_rate") == "autonomic"
    assert _metric_domain("heart_rate_variability") == "autonomic"
    assert _metric_domain("resting_heart_rate") == "hr_rate"
    assert _metric_domain("vo2_max") == "vital"
    assert _metric_domain("workout_load_yoga") == "activity"
    assert _metric_domain("physical_effort") == "activity"
    assert _metric_domain("step_count") == "activity"
    assert _metric_domain("time_in_daylight") == "env"
    assert _metric_domain(None) == "other"


def test_pair_tier_demotes_expected_and_promotes_cross_domain():
    # Expected / structural -> tier 0.
    assert _pair_tier("sleep", "sleep") == 0  # sleep architecture self-corr
    assert _pair_tier("hr_rate", "hr_rate") == 0  # average vs resting HR
    assert _pair_tier("hr_rate", "activity") == 0  # exercise raises average HR
    assert _pair_tier("env", "activity") == 0  # sunny days = more movement
    # Informative cross-subsystem -> tier 2.
    assert _pair_tier("sleep", "autonomic") == 2
    assert _pair_tier("activity", "sleep") == 2
    assert _pair_tier("autonomic", "hr_rate") == 2  # HRV vs RHR is a real signal
    assert _pair_tier("autonomic", "autonomic") == 2  # respiratory rate vs HRV
    # Neutral -> tier 1.
    assert _pair_tier("env", "sleep") == 1  # daylight vs sleep (seasonally confounded)


def test_report_priority_orders_cross_domain_over_structural():
    # A lagged cross-domain link must outrank a stronger same-subsystem pair.
    cross = report_priority("sleep_total_h", "respiratory_rate", -0.78, 2)
    arch = report_priority("sleep_total_h", "sleep_rem_h", 0.74, 0)
    trivial = report_priority("heart_rate", "workout_load", 0.41, 0)
    assert cross > arch > trivial
    # Within the same tier, the lag bonus breaks ties toward the directional one.
    assert report_priority("sleep_total_h", "vo2_max", 0.40, 3) > report_priority("sleep_total_h", "vo2_max", 0.40, 0)


def test_build_context_prioritises_and_caps_correlations():
    structural = [
        _finding(
            "correlation",
            metric_a="sleep_total_h",
            metric_a_label="Schlafdauer",
            metric_b="sleep_rem_h",
            metric_b_label="REM-Schlaf",
            lag_days=0,
            coefficient=0.74,
            p_value_adj=0.0,
            details={"n": 80},
            ref_date=None,
        ),
        _finding(
            "correlation",
            metric_a="heart_rate",
            metric_a_label="Herzfrequenz",
            metric_b="workout_load",
            metric_b_label="Trainingslast",
            lag_days=0,
            coefficient=0.41,
            p_value_adj=0.0,
            details={"n": 80},
            ref_date=None,
        ),
    ]
    gem = _finding(
        "correlation",
        metric_a="sleep_total_h",
        metric_a_label="Schlafdauer",
        metric_b="respiratory_rate",
        metric_b_label="Atemfrequenz",
        lag_days=2,
        coefficient=-0.78,
        p_value_adj=0.0,
        details={"n": 80},
        ref_date=None,
    )
    ctx = build_context([*structural, gem], 7, _TODAY, max_correlations=1)
    # The single kept correlation is the cross-domain gem, not a stronger structural pair.
    assert "Atemfrequenz" in ctx
    assert "REM-Schlaf" not in ctx
    # The dropped pairs are summarised as a count.
    assert "+2" in ctx


def test_build_context_note_appended():
    ctx = build_context([], 7, _TODAY, note="Focus on HRV.")
    assert "Focus on HRV." in ctx
    assert "NUTZERHINWEIS" in ctx or "USER NOTE" in ctx


def test_build_context_english_language():
    ctx = build_context([], 7, _TODAY, language="en")
    assert "ANOMALIES" in ctx
    assert "CORRELATIONS" in ctx
    assert "Health Report" in ctx


def test_build_context_english_note_label():
    ctx = build_context([], 7, _TODAY, note="Check training load.", language="en")
    assert "USER NOTE" in ctx


# ---------------------------------------------------------------------------
# _system_prompt
# ---------------------------------------------------------------------------


def test_system_prompt_de():
    p = _system_prompt("de")
    assert "deutsch" in p.lower() or "wochen" in p.lower()


def test_system_prompt_en():
    p = _system_prompt("en")
    assert "english" in p.lower() or "weekly" in p.lower()


def test_system_prompt_unknown_language_falls_back_to_de():
    p = _system_prompt("fr")
    p_de = _system_prompt("de")
    assert p == p_de


# ---------------------------------------------------------------------------
# OllamaClient
# ---------------------------------------------------------------------------


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"model": "qwen2.5:14b", "message": {"content": "Wochenbericht…"}},
    )


def test_ollama_posts_to_api_chat():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_handler(req)

    client = _client(handler)
    client.generate("sys", "usr")
    assert len(captured) == 1
    assert "/api/chat" in str(captured[0].url)


def test_ollama_posts_correct_body():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_handler(req)

    client = _client(handler)
    client.generate("my system", "my context")
    body = json.loads(captured[0].content)
    assert body["model"] == "qwen2.5:14b"
    assert body["stream"] is False
    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "my system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "my context"


def test_ollama_returns_response_content():
    client = _client(_ok_handler)
    result = client.generate("sys", "usr")
    assert result == "Wochenbericht…"


def test_ollama_raises_on_http_error():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.generate("sys", "usr")


def test_ollama_raises_valueerror_on_missing_content_key():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "qwen2.5:14b", "message": {}})

    client = _client(handler)
    with pytest.raises(ValueError, match="message.content"):
        client.generate("sys", "usr")


def test_ollama_raises_valueerror_on_missing_message_key():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"done": True})

    client = _client(handler)
    with pytest.raises(ValueError, match="message.content"):
        client.generate("sys", "usr")


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


def test_write_report_creates_directory_and_file(tmp_path):
    output_dir = tmp_path / "narration"
    path = write_report("Report text", output_dir, dt.date(2026, 6, 22))
    assert path.exists()
    assert path.name == "2026-06-22.md"
    assert path.read_text(encoding="utf-8") == "Report text"


def test_write_report_creates_nested_directory(tmp_path):
    output_dir = tmp_path / "config" / "narration"
    path = write_report("x", output_dir, _TODAY)
    assert path.exists()


def test_write_report_overwrites_existing(tmp_path):
    output_dir = tmp_path / "narration"
    write_report("first", output_dir, _TODAY)
    path = write_report("second", output_dir, _TODAY)
    assert path.read_text(encoding="utf-8") == "second"


def test_write_report_returns_path(tmp_path):
    path = write_report("x", tmp_path / "narration", _TODAY)
    assert str(_TODAY) in path.name
