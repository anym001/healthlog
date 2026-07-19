"""Unit tests for the narration module.

Mostly pure — no real network. The Ollama client is driven through an httpx
MockTransport; context building and privacy scrubbing are tested with
synthetic findings dicts. Only the loader test touches the test DB (it
exercises the findings query itself).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

import httpx
import pytest

from app.narrate import (
    OllamaClient,
    _metric_domain,
    _pair_tier,
    _system_prompt,
    add_arguments,
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
# scrub_details — all fields pass through (local LLM, no privacy scrubbing)
# ---------------------------------------------------------------------------


def test_scrub_details_anomaly_passes_all_fields():
    d = {"z": 3.9, "value": 72.5, "global_z": 2.8}
    assert scrub_details("anomaly", d) == d


def test_scrub_details_anomaly_none_returns_empty():
    assert scrub_details("anomaly", None) == {}


def test_scrub_details_anomaly_value_only_passes_through():
    assert scrub_details("anomaly", {"value": 72.5}) == {"value": 72.5}


def test_scrub_details_recovery_alert_passes_all_fields():
    d = {"resting_heart_rate_z": 1.8, "heart_rate_variability_z": -2.1, "short_sleep": True, "extra": 99}
    assert scrub_details("recovery_alert", d) == d


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


def test_build_context_anomaly_contains_raw_value():
    findings = [_finding("anomaly", details={"z": 3.9, "value": 72.5}, severity=3.9)]
    ctx = build_context(findings, 7, _TODAY)
    # raw sensor value is included for local-LLM context
    assert "72.5" in ctx


def test_build_context_training_status_section():
    findings = [
        _finding(
            "training_status",
            metric_a="workout_trimp",
            metric_a_label="Training Load (TRIMP)",
            severity=0.17,
            note="productive training (moderate negative form)",
            details={
                "ctl": 42.3,
                "atl": 49.5,
                "tsb": -7.2,
                "tsb_pct": -0.1702,
                "zone": "productive",
                "ctl_days": 42,
                "atl_days": 7,
                "ctl_ago": 38.1,
                "ctl_trend": "rising",
                "ctl_trend_days": 28,
            },
        )
    ]
    ctx = build_context(findings, 7, _TODAY)
    assert "TRAININGSZUSTAND" in ctx
    assert "CTL=42.3" in ctx and "ATL=49.5" in ctx and "TSB=-7.2" in ctx
    assert "-17% CTL" in ctx
    assert "produktives Training" in ctx
    assert "steigend" in ctx

    ctx_en = build_context(findings, 7, _TODAY, language="en")
    assert "TRAINING STATUS" in ctx_en
    assert "productive training" in ctx_en and "rising" in ctx_en


def test_build_context_training_status_empty_shows_placeholder():
    ctx = build_context([], 7, _TODAY)
    assert "TRAININGSZUSTAND" in ctx


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
        details={"heart_rate_variability_z": -2.1, "resting_heart_rate_z": 1.8, "short_sleep": True},
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


def test_build_context_user_note_survives_recovery_alert_with_note():
    """A recovery alert carrying its own note must not overwrite the user note.

    Regression: the recovery section once rebound the ``note`` parameter, so a
    user note was clobbered (or wrongly fabricated) whenever recovery alerts
    were present.
    """
    alert = _finding(
        "recovery_alert",
        details={"heart_rate_variability_z": -2.1},
        note="alert-specific note",
    )
    ctx = build_context([alert], 7, _TODAY, note="Focus on HRV.")
    assert "Focus on HRV." in ctx  # the user note, not the alert note
    note_section = ctx.split("NUTZERHINWEIS")[-1].split("USER NOTE")[-1]
    assert "alert-specific note" not in note_section


def test_build_context_no_user_note_means_no_note_section():
    """Without a user note there is no note section, even when alerts have notes."""
    alert = _finding("recovery_alert", details={"heart_rate_variability_z": -2.1}, note="alert note")
    ctx = build_context([alert], 7, _TODAY)
    assert "NUTZERHINWEIS" not in ctx and "USER NOTE" not in ctx


def test_build_context_english_language():
    ctx = build_context([], 7, _TODAY, language="en")
    assert "ANOMALIES" in ctx
    assert "CORRELATIONS" in ctx
    assert "Health Report" in ctx


def test_build_context_english_note_label():
    ctx = build_context([], 7, _TODAY, note="Check training load.", language="en")
    assert "USER NOTE" in ctx


# ---------------------------------------------------------------------------
# --dry-run — render the context without calling Ollama
# ---------------------------------------------------------------------------


def test_dry_run_flag_parsing():
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    assert parser.parse_args([]).dry_run is False
    assert parser.parse_args(["--dry-run"]).dry_run is True


def test_run_dry_run_renders_context_without_ollama(tmp_path, monkeypatch, capsys):
    """A dry run prints the findings context, needs no ollama_url, and never
    constructs the client — the deterministic 'data -> report' bridge."""
    import app.database as database_mod
    import app.narrate.cli as narrate_cli
    from app.appconfig import AppConfig

    class _Settings:
        log_level = "INFO"
        log_format = "text"
        config_file = str(tmp_path / "config.yaml")

    class _DB:
        def close(self):
            pass

    def _no_client(*_a, **_k):
        raise AssertionError("OllamaClient must not be constructed in a dry run")

    # run() lives in the narrate.cli submodule; patch the names it looks up there.
    # Default AppConfig has narrate.ollama_url = None — the dry run must still work.
    # bootstrap() (settings + logging) is the shared CLI entry; stub it to the fake settings.
    monkeypatch.setattr(narrate_cli, "bootstrap", lambda: _Settings())
    monkeypatch.setattr(narrate_cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(database_mod, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(
        narrate_cli,
        "load_findings",
        lambda _db, _lookback, report="status": [_finding("anomaly", details={"z": 3.9, "value": 72.5}, severity=3.9)],
    )
    monkeypatch.setattr(narrate_cli, "OllamaClient", _no_client)

    args = argparse.Namespace(
        lookback_days=None,
        output_dir=None,
        language=None,
        audience=None,
        max_words=None,
        note=None,
        dry_run=True,
        report=None,
    )
    rc = narrate_cli.run(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "ANOMALIES" in out or "ANOMALIEN" in out  # context was rendered
    assert "3.90" in out  # z-score present
    assert "72.5" in out  # raw sensor value included for local-LLM context


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


def test_system_prompt_every_audience_keeps_safety_rules():
    # The safety rules are invariant: every language x audience combination
    # must contain them - the audience only changes the explanation style.
    markers = {
        "de": ("keine erfundenen Zahlen", "Keine medizinischen Diagnosen", "Berichtsstruktur"),
        "en": ("do not invent numbers", "Do not make medical diagnoses", "Report structure"),
    }
    for lang, needles in markers.items():
        for audience in ("simple", "standard", "expert"):
            p = _system_prompt(lang, audience)
            for needle in needles:
                assert needle in p, (lang, audience, needle)


def test_system_prompt_audience_styles_differ():
    simple = _system_prompt("de", "simple")
    standard = _system_prompt("de", "standard")
    expert = _system_prompt("de", "expert")
    assert len({simple, standard, expert}) == 3
    assert "erscheinen im Text gar nicht" in simple  # no jargon at all
    # standard is two-tier: consumer-fitness terms used directly, statistics
    # and model terms still translated on first use.
    assert "ohne Klammer-Erklärung" in standard
    assert "beim ersten Auftreten" in standard
    assert "ohne Erklärung verwenden" in expert  # jargon allowed


def test_system_prompt_default_audience_is_standard():
    assert _system_prompt("en") == _system_prompt("en", "standard")
    # An unknown audience value falls back to standard instead of failing.
    assert _system_prompt("en", "phd") == _system_prompt("en", "standard")


def test_system_prompt_injects_max_words():
    assert "Maximal 700 Wörter" in _system_prompt("de")
    assert "Maximum 450 words" in _system_prompt("en", "simple", max_words=450)


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


# ---------------------------------------------------------------------------
# load_findings (DB-backed: the lookback window itself)
# ---------------------------------------------------------------------------


def test_load_findings_lookback_uses_local_day(db):
    # The cutoff must be the *local* day (ref_date is written in local_tz by
    # the analysis), not the DB server's CURRENT_DATE, which typically runs
    # on UTC and would shift the window around local midnight.
    from zoneinfo import ZoneInfo

    from app.config import get_settings
    from app.models import Finding
    from app.narrate import load_findings

    today = dt.datetime.now(ZoneInfo(get_settings().local_tz)).date()
    old = today - dt.timedelta(days=30)
    db.add(Finding(kind="anomaly", metric_a="resting_heart_rate", ref_date=today, severity=3.0))
    db.add(Finding(kind="anomaly", metric_a="resting_heart_rate", ref_date=old, severity=3.0))
    db.add(Finding(kind="correlation", metric_a="step_count", metric_b="resting_heart_rate"))
    db.flush()

    rows = load_findings(db, 7)
    anomaly_dates = [r["ref_date"] for r in rows if r["kind"] == "anomaly"]
    assert anomaly_dates == [today]  # the 30-day-old anomaly is outside the window
    assert any(r["kind"] == "correlation" for r in rows)  # standing analyses always included


# ---------------------------------------------------------------------------
# OllamaClient transport retry
# ---------------------------------------------------------------------------


def test_client_retries_once_on_transport_error(monkeypatch):
    from app.narrate import client as client_mod

    monkeypatch.setattr(client_mod, "_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"message": {"content": "Report"}})

    assert _client(handler).generate("sys", "ctx") == "Report"
    assert calls["n"] == 2


def test_client_gives_up_after_one_retry(monkeypatch):
    from app.narrate import client as client_mod

    monkeypatch.setattr(client_mod, "_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("still down", request=request)

    with pytest.raises(httpx.ConnectError):
        _client(handler).generate("sys", "ctx")
    assert calls["n"] == 2  # exactly one retry, then propagate


def test_client_does_not_retry_http_status_errors():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="model exploded")

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).generate("sys", "ctx")
    assert calls["n"] == 1  # a real answer from a reachable server: no retry


# ---------------------------------------------------------------------------
# Hand-wired display labels (series without a metric_registry row)
# ---------------------------------------------------------------------------


def test_handwired_labels_for_registry_less_series():
    from app.narrate.loader import _handwired_label

    assert _handwired_label("workout_trimp") == "Training Load (TRIMP)"
    assert _handwired_label("workout_trimp_running") == "Training Load (TRIMP) — Running"
    assert _handwired_label("workout_load_cycling") == "Training Load — Cycling"
    assert _handwired_label("workout_edwards") == "Training Load (Edwards TRIMP)"
    assert _handwired_label("sleep_total_h") == "Total Sleep"
    assert _handwired_label("bedtime") == "Bedtime"
    assert _handwired_label("step_count") is None  # registry metrics are not ours


def test_load_findings_labels_handwired_series(db):
    # workout/sleep series have no metric_registry row; the loader must still
    # deliver a display label instead of leaking the snake_case key into prose.
    from zoneinfo import ZoneInfo

    from app.config import get_settings
    from app.models import Finding
    from app.narrate import load_findings

    today = dt.datetime.now(ZoneInfo(get_settings().local_tz)).date()
    db.add(Finding(kind="training_load", metric_a="workout_trimp_running", ref_date=today, severity=1.6))
    db.flush()

    rows = load_findings(db, 7)
    row = next(r for r in rows if r["kind"] == "training_load")
    assert row["metric_a_label"] == "Training Load (TRIMP) — Running"


# ---------------------------------------------------------------------------
# Stress section + system prompt
# ---------------------------------------------------------------------------


def test_build_context_stress_shows_score_and_zone():
    f = _finding(
        "stress",
        metric_a="stress",
        metric_a_label="Stress",
        severity=69.0,
        note="elevated daily stress",
        details={"score": 69.0, "high_min": 360, "rest_min": 180, "hrv_z": -1.66},
    )
    ctx = build_context([f], 7, _TODAY, language="en")
    assert "=== STRESS ===" in ctx
    assert "score=69 (medium)" in ctx
    assert "high-stress min=360" in ctx
    assert "HRV-z=-1.66" in ctx


def test_build_context_stress_german():
    f = _finding(
        "stress",
        metric_a="stress",
        metric_a_label="Stress",
        severity=80.0,
        details={"score": 80.0, "high_min": 120},
    )
    ctx = build_context([f], 7, _TODAY, language="de")
    assert "Score=80 (hoch)" in ctx
    assert "Hochstress-Min=120" in ctx


def test_build_context_stress_empty_placeholder():
    ctx = build_context([], 7, _TODAY)
    assert "=== STRESS ===" in ctx  # section always rendered, with a placeholder


def test_system_prompt_documents_stress_both_languages():
    assert "Stress-Score" in _system_prompt("de")
    assert "Stress score" in _system_prompt("en")


def test_build_context_body_battery_shows_levels_en():
    f = _finding(
        "body_battery",
        metric_a="body_battery",
        metric_a_label="Body Battery",
        severity=12.0,
        note="low energy reserve",
        details={"low_level": 12, "high_level": 55, "wake_level": 40, "charged": 30.0, "drained": 70.0},
    )
    ctx = build_context([f], 7, _TODAY, language="en")
    assert "=== BODY BATTERY ===" in ctx
    assert "low=12" in ctx
    assert "wake=40" in ctx
    assert "drained=70" in ctx


def test_build_context_body_battery_german():
    f = _finding(
        "body_battery",
        metric_a="body_battery",
        metric_a_label="Body Battery",
        severity=8.0,
        details={"low_level": 8, "wake_level": 25},
    )
    ctx = build_context([f], 7, _TODAY, language="de")
    assert "Tief=8" in ctx
    assert "Weckstand=25" in ctx


def test_build_context_body_battery_empty_placeholder():
    ctx = build_context([], 7, _TODAY)
    assert "=== BODY BATTERY ===" in ctx  # section always rendered, with a placeholder


def test_system_prompt_documents_body_battery_both_languages():
    assert "Body Battery" in _system_prompt("de")
    assert "Body Battery" in _system_prompt("en")


# ---------------------------------------------------------------------------
# Week/month-overview sections (narrate --report weekly|monthly)
# ---------------------------------------------------------------------------


def _weekly_training_finding() -> dict:
    return _finding(
        "weekly_training",
        metric_a="workout_trimp",
        metric_a_label="Training Load (TRIMP)",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "sessions": 4,
            "duration_h": 4.5,
            "distance_km": 52.3,
            "energy_kcal": 2100,
            "load": 350.0,
            "prev": {"sessions": 3, "duration_h": 2.9, "distance_km": 30.0, "energy_kcal": 1500, "load": 250.0},
            "baseline_load": 280.0,
            "per_sport": [
                {
                    "sport": "running",
                    "sessions": 2,
                    "duration_h": 2.0,
                    "distance_km": 22.3,
                    "energy_kcal": 1200,
                    "load": 200.0,
                },
                {
                    "sport": "cycling",
                    "sessions": 2,
                    "duration_h": 2.5,
                    "distance_km": 30.0,
                    "energy_kcal": 900,
                    "load": 150.0,
                },
            ],
        },
    )


def test_build_context_weekly_training_section():
    ctx = build_context([_weekly_training_finding()], 7, _TODAY, report="weekly")
    assert "=== WOCHE: TRAINING ===" in ctx
    assert "4 Einheiten" in ctx
    assert "Last=350 (Training Load (TRIMP))" in ctx
    assert "Vorwoche: 3 Einheiten" in ctx
    assert "4-Wochen-Schnitt Last=280/Woche" in ctx
    assert "– Running: 2 Einheiten" in ctx
    assert "52.3 km" in ctx


def test_build_context_weekly_sections_absent_without_flag():
    # The same findings render nothing in status mode: the overview blocks
    # are only assembled for the weekly/monthly reports.
    ctx = build_context([_weekly_training_finding()], 7, _TODAY, report="status")
    assert "WOCHE" not in ctx
    ctx_en = build_context([_weekly_training_finding()], 7, _TODAY, language="en", report="status")
    assert "WEEK:" not in ctx_en


def test_build_context_weekly_placeholders_when_no_data():
    ctx = build_context([], 7, _TODAY, report="weekly")
    for header in (
        "=== WOCHE: TRAINING ===",
        "=== WOCHE: SCHLAF ===",
        "=== WOCHE: STRESS ===",
        "=== WOCHE: BODY BATTERY ===",
        "=== WOCHE: VITALWERTE (vs. 28-Tage-Baseline) ===",
        "=== WOCHE: AKTIVITÄT ===",
        "=== FITNESS-MARKER ===",
    ):
        assert header in ctx


def test_build_context_weekly_sleep_formats_bedtime_clock():
    f = _finding(
        "weekly_sleep",
        metric_a="sleep_total_h",
        metric_a_label="Total Sleep",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "nights": 7,
            "avg_total_h": 7.25,
            "avg_deep_h": 1.1,
            "avg_rem_h": 1.5,
            "deep_pct": 15.2,
            "rem_pct": 20.7,
            "avg_efficiency": 0.92,
            "avg_bedtime": 23.17,
            "prev": {"nights": 6, "avg_total_h": 6.8, "avg_efficiency": 0.9},
        },
    )
    ctx = build_context([f], 7, _TODAY, report="weekly")
    assert "Ø Schlaf=7.25h" in ctx
    assert "Ø Zubettgehen=23:10" in ctx
    assert "Effizienz=92%" in ctx
    assert "Vorwoche: Ø 6.80h, Effizienz 90%" in ctx


def test_build_context_weekly_stress_and_battery_sections():
    stress = _finding(
        "weekly_stress",
        metric_a="stress",
        metric_a_label="Stress",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "days": 7,
            "avg_score": 38.0,
            "high_min": 45,
            "medium_min": 120,
            "peak_day": "2026-06-20",
            "peak_score": 62.0,
            "calm_day": "2026-06-17",
            "calm_score": 21.0,
            "prev": {"days": 7, "avg_score": 42.0, "high_min": 80},
        },
    )
    battery = _finding(
        "weekly_body_battery",
        metric_a="body_battery",
        metric_a_label="Body Battery",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "days": 7,
            "avg_wake": 78.0,
            "avg_low": 25.0,
            "avg_high": 88.0,
            "avg_charged": 68.0,
            "avg_drained": 70.0,
            "min_low": 12.0,
            "min_low_day": "2026-06-19",
            "prev": {"days": 7, "avg_wake": 74.0, "avg_low": 22.0},
        },
    )
    ctx = build_context([stress, battery], 7, _TODAY, report="weekly")
    assert "Ø Score=38 (niedrig)" in ctx
    assert "Spitzentag=2026-06-20 (Score 62)" in ctx
    assert "Vorwoche: Ø 42, Hochstress 80 min" in ctx
    assert "Ø Weckstand=78" in ctx
    assert "Tiefstwert=12 (2026-06-19)" in ctx
    assert "Vorwoche: Weckstand 74, Tief 22" in ctx


def test_build_context_weekly_vitals_activity_markers():
    vitals = _finding(
        "weekly_vitals",
        metric_a="resting_heart_rate",
        metric_a_label="Resting Heart Rate",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "week_mean": 52.1,
            "baseline_mean": 50.3,
            "delta": 1.8,
            "delta_pct": 3.6,
            "week_days": 7,
            "baseline_days": 28,
            "unit": "count/min",
        },
    )
    activity = _finding(
        "weekly_activity",
        metric_a="step_count",
        metric_a_label="Steps",
        window_start=dt.date(2026, 6, 16),
        window_end=dt.date(2026, 6, 22),
        details={
            "total": 68500.0,
            "daily_avg": 9786.0,
            "days": 7,
            "prev_total": 61200.0,
            "baseline_weekly": 64000.0,
            "unit": "count",
        },
    )
    marker = _finding(
        "fitness_markers",
        metric_a="vo2_max",
        metric_a_label="VO2 Max",
        details={
            "latest": 43.2,
            "latest_date": "2026-06-20",
            "prev": 42.8,
            "prev_date": "2026-05-18",
            "delta": 0.4,
            "unit": "ml/(kg·min)",
        },
    )
    ctx = build_context([vitals, activity, marker], 7, _TODAY, report="weekly")
    assert "Resting Heart Rate: Wochenmittel=52.1 count/min, Baseline=50.3 count/min, Δ=+1.8, +3.6%" in ctx
    assert "Steps: gesamt=68500.0 count, Ø 9786.0/Tag, Vorwoche 61200.0, 4-Wochen-Schnitt 64000.0/Woche" in ctx
    assert "VO2 Max: 43.2 ml/(kg·min) am 2026-06-20 (Δ=+0.40 vs. 2026-05-18)" in ctx


def test_build_context_weekly_english_labels():
    ctx = build_context([_weekly_training_finding()], 7, _TODAY, language="en", report="weekly")
    assert "=== WEEK: TRAINING ===" in ctx
    assert "4 sessions" in ctx
    assert "previous week: 3 sessions" in ctx
    assert "4-week average load=280/week" in ctx


def test_system_prompt_weekly_swaps_structure():
    daily = _system_prompt("de")
    weekly = _system_prompt("de", report="weekly")
    assert "Wochenübersicht" not in daily
    assert "Wochenübersicht" in weekly
    assert "Wochenbilanz" in weekly
    assert "Fitness-Marker" in weekly
    # The safety rules survive in both modes.
    assert "keine erfundenen Zahlen" in weekly

    weekly_en = _system_prompt("en", report="weekly")
    assert "Week overview" in weekly_en
    assert "Week review" in weekly_en


def test_load_findings_weekly_kinds_only_with_flag(db):
    from app.models import Finding
    from app.narrate import load_findings

    db.add(Finding(kind="weekly_training", metric_a="workout_trimp", ref_date=dt.date(2026, 6, 22)))
    db.add(Finding(kind="fitness_markers", metric_a="vo2_max", ref_date=dt.date(2026, 6, 20)))
    db.add(Finding(kind="correlation", metric_a="step_count", metric_b="resting_heart_rate"))
    db.flush()

    daily_kinds = {r["kind"] for r in load_findings(db, 7)}
    assert "weekly_training" not in daily_kinds
    assert "fitness_markers" not in daily_kinds
    assert "correlation" in daily_kinds

    weekly_kinds = {r["kind"] for r in load_findings(db, 7, report="weekly")}
    assert {"weekly_training", "fitness_markers", "correlation"} <= weekly_kinds
    # The hand-wired label map applies to the weekly kinds too.
    rows = load_findings(db, 7, report="weekly")
    training = next(r for r in rows if r["kind"] == "weekly_training")
    assert training["metric_a_label"] == "Training Load (TRIMP)"


# ---------------------------------------------------------------------------
# Monthly report (build_context report="monthly", prompts, loader, filenames)
# ---------------------------------------------------------------------------


def _monthly_training_finding() -> dict:
    return _finding(
        "monthly_training",
        metric_a="workout_trimp",
        metric_a_label="Training Load (TRIMP)",
        window_start=dt.date(2026, 5, 26),
        window_end=dt.date(2026, 6, 22),
        details={
            "sessions": 14,
            "duration_h": 16.5,
            "distance_km": 182.0,
            "energy_kcal": 8400,
            "load": 990.0,
            "prev": {"sessions": 12, "duration_h": 14.0, "distance_km": 150.0, "energy_kcal": 7000, "load": 850.0},
            "baseline_load": 880.0,
            "weeks": [
                {"start": "2026-05-26", "end": "2026-06-01", "sessions": 3, "load": 220.0},
                {"start": "2026-06-02", "end": "2026-06-08", "sessions": 4, "load": 260.0},
                {"start": "2026-06-09", "end": "2026-06-15", "sessions": 5, "load": 330.0},
                {"start": "2026-06-16", "end": "2026-06-22", "sessions": 2, "load": 180.0},
            ],
            "per_sport": [
                {"sport": "running", "sessions": 8, "duration_h": 8.0, "distance_km": 80.0, "load": 600.0},
            ],
        },
    )


def test_build_context_monthly_training_section():
    ctx = build_context([_monthly_training_finding()], 28, _TODAY, report="monthly")
    assert "=== MONAT: TRAINING ===" in ctx
    assert "14 Einheiten" in ctx
    assert "Vormonat: 12 Einheiten" in ctx
    assert "3-Monats-Schnitt Last=880/Monat" in ctx
    assert "Wochenverlauf: Last 220 → 260 → 330 → 180, Einheiten 3 → 4 → 5 → 2" in ctx
    assert "– Running: 8 Einheiten" in ctx


def test_build_context_monthly_training_english_labels():
    ctx = build_context([_monthly_training_finding()], 28, _TODAY, language="en", report="monthly")
    assert "=== MONTH: TRAINING ===" in ctx
    assert "previous month: 12 sessions" in ctx
    assert "3-month average load=880/month" in ctx
    assert "week by week: load 220 → 260 → 330 → 180, sessions 3 → 4 → 5 → 2" in ctx


def test_build_context_report_modes_keep_their_own_kinds():
    weekly_f = _finding("weekly_training", details={"sessions": 4, "duration_h": 4.0})
    monthly_f = _monthly_training_finding()
    # Weekly mode renders only weekly_*; the monthly finding stays invisible.
    ctx_weekly = build_context([weekly_f, monthly_f], 7, _TODAY, report="weekly")
    assert "=== WOCHE: TRAINING ===" in ctx_weekly
    assert "MONAT" not in ctx_weekly
    # Monthly mode renders only monthly_*.
    ctx_monthly = build_context([weekly_f, monthly_f], 28, _TODAY, report="monthly")
    assert "=== MONAT: TRAINING ===" in ctx_monthly
    assert "WOCHE:" not in ctx_monthly
    # The status report renders neither.
    ctx_status = build_context([weekly_f, monthly_f], 7, _TODAY)
    assert "WOCHE" not in ctx_status and "MONAT" not in ctx_status


def test_build_context_monthly_placeholders_when_no_data():
    ctx = build_context([], 28, _TODAY, report="monthly")
    for header in (
        "=== MONAT: TRAINING ===",
        "=== MONAT: SCHLAF ===",
        "=== MONAT: STRESS ===",
        "=== MONAT: BODY BATTERY ===",
        "=== MONAT: VITALWERTE (vs. 84-Tage-Baseline) ===",
        "=== MONAT: AKTIVITÄT ===",
        "=== FITNESS-MARKER ===",
    ):
        assert header in ctx


def test_build_context_monthly_vitals_and_activity():
    vitals = _finding(
        "monthly_vitals",
        metric_a="resting_heart_rate",
        metric_a_label="Resting Heart Rate",
        window_start=dt.date(2026, 5, 26),
        window_end=dt.date(2026, 6, 22),
        details={
            "month_mean": 52.1,
            "baseline_mean": 50.3,
            "delta": 1.8,
            "delta_pct": 3.6,
            "month_days": 28,
            "baseline_days": 84,
            "unit": "count/min",
            "weeks": [
                {"start": "2026-05-26", "end": "2026-06-01", "mean": 51.0},
                {"start": "2026-06-02", "end": "2026-06-08", "mean": 52.0},
                {"start": "2026-06-09", "end": "2026-06-15", "mean": 52.5},
                {"start": "2026-06-16", "end": "2026-06-22", "mean": 53.0},
            ],
        },
    )
    activity = _finding(
        "monthly_activity",
        metric_a="step_count",
        metric_a_label="Steps",
        window_start=dt.date(2026, 5, 26),
        window_end=dt.date(2026, 6, 22),
        details={
            "total": 280000.0,
            "daily_avg": 10000.0,
            "days": 28,
            "prev_total": 250000.0,
            "baseline_monthly": 260000.0,
            "unit": "count",
            "weeks": [
                {"start": "2026-05-26", "end": "2026-06-01", "total": 61000.0},
                {"start": "2026-06-02", "end": "2026-06-08", "total": 72000.0},
                {"start": "2026-06-09", "end": "2026-06-15", "total": 78000.0},
                {"start": "2026-06-16", "end": "2026-06-22", "total": 69000.0},
            ],
        },
    )
    ctx = build_context([vitals, activity], 28, _TODAY, report="monthly")
    assert "Resting Heart Rate: Monatsmittel=52.1 count/min, Baseline=50.3 count/min, Δ=+1.8, +3.6%" in ctx
    assert "Wochenverlauf: 51.0 → 52.0 → 52.5 → 53.0" in ctx
    assert "Steps: gesamt=280000.0 count, Ø 10000.0/Tag, Vormonat 250000.0, 3-Monats-Schnitt 260000.0/Monat" in ctx
    assert "Wochenverlauf: 61000 → 72000 → 78000 → 69000" in ctx


def test_build_context_monthly_fitness_markers_quarter_delta():
    marker = _finding(
        "fitness_markers",
        metric_a="vo2_max",
        metric_a_label="VO2 Max",
        details={
            "latest": 43.2,
            "latest_date": "2026-06-20",
            "prev": 42.8,
            "prev_date": "2026-05-18",
            "delta": 0.4,
            "prev_90d": 42.0,
            "prev_90d_date": "2026-03-15",
            "delta_90d": 1.2,
            "unit": "ml/(kg·min)",
        },
    )
    ctx = build_context([marker], 28, _TODAY, report="monthly")
    assert "VO2 Max: 43.2 ml/(kg·min) am 2026-06-20 (Δ=+0.40 vs. 2026-05-18, Δ90d=+1.20 vs. 2026-03-15)" in ctx
    # The weekly report keeps the shorter month-over-month view only.
    ctx_weekly = build_context([marker], 7, _TODAY, report="weekly")
    assert "Δ90d" not in ctx_weekly


def test_system_prompt_report_types():
    status = _system_prompt("de")
    weekly = _system_prompt("de", report="weekly")
    monthly = _system_prompt("de", report="monthly")
    # Distinct intros: the status check no longer calls itself a weekly report.
    assert "Status-Check" in status
    assert "Wochen-Gesundheitsbericht" not in status
    assert "Wochen-Gesundheitsbericht" in weekly
    assert "Monats-Gesundheitsbericht" in monthly
    # Monthly overview + structure.
    assert "Monatsübersicht" in monthly
    assert "Monatsbilanz" in monthly
    assert "Wochenverlauf" in monthly
    # The safety rules survive in every report type.
    for p in (status, weekly, monthly):
        assert "keine erfundenen Zahlen" in p
    # Unknown report types fall back to the status check.
    assert _system_prompt("de", report="quarterly") == status

    monthly_en = _system_prompt("en", report="monthly")
    assert "Month overview" in monthly_en
    assert "Month review" in monthly_en


def test_write_report_filename_per_report_type(tmp_path):
    day = dt.date(2026, 6, 22)
    assert write_report("s", tmp_path, day).name == "2026-06-22.md"
    assert write_report("s", tmp_path, day, "status").name == "2026-06-22.md"
    assert write_report("w", tmp_path, day, "weekly").name == "2026-06-22-weekly.md"
    assert write_report("m", tmp_path, day, "monthly").name == "2026-06-22-monthly.md"


def test_load_findings_monthly_kinds_only_with_monthly_report(db):
    from app.models import Finding
    from app.narrate import load_findings

    db.add(Finding(kind="monthly_training", metric_a="workout_trimp", ref_date=dt.date(2026, 6, 22)))
    db.add(Finding(kind="weekly_training", metric_a="workout_trimp", ref_date=dt.date(2026, 6, 22)))
    db.add(Finding(kind="fitness_markers", metric_a="vo2_max", ref_date=dt.date(2026, 6, 20)))
    db.flush()

    status_kinds = {r["kind"] for r in load_findings(db, 7)}
    assert not status_kinds & {"monthly_training", "weekly_training", "fitness_markers"}

    weekly_kinds = {r["kind"] for r in load_findings(db, 7, report="weekly")}
    assert "weekly_training" in weekly_kinds and "fitness_markers" in weekly_kinds
    assert "monthly_training" not in weekly_kinds

    monthly_kinds = {r["kind"] for r in load_findings(db, 7, report="monthly")}
    assert "monthly_training" in monthly_kinds and "fitness_markers" in monthly_kinds
    assert "weekly_training" not in monthly_kinds
    # The hand-wired label map applies to the monthly kinds too.
    rows = load_findings(db, 7, report="monthly")
    training = next(r for r in rows if r["kind"] == "monthly_training")
    assert training["metric_a_label"] == "Training Load (TRIMP)"


def test_report_flag_parsing():
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    assert parser.parse_args([]).report is None  # config decides (default: status)
    assert parser.parse_args(["--report", "weekly"]).report == "weekly"
    assert parser.parse_args(["--weekly"]).report == "weekly"
    assert parser.parse_args(["--monthly"]).report == "monthly"
    with pytest.raises(SystemExit):
        parser.parse_args(["--weekly", "--monthly"])  # mutually exclusive


def test_run_dry_run_monthly_applies_per_report_defaults(tmp_path, monkeypatch, capsys):
    """--monthly widens the alert lookback to the 28-day month window when
    neither --lookback-days nor narrate.lookback_days pins it."""
    import app.database as database_mod
    import app.narrate.cli as narrate_cli
    from app.appconfig import AppConfig

    class _Settings:
        log_level = "INFO"
        log_format = "text"
        config_file = str(tmp_path / "config.yaml")

    class _DB:
        def close(self):
            pass

    seen: dict = {}

    def _capture(_db, lookback, report="status"):
        seen["lookback"] = lookback
        seen["report"] = report
        return []

    monkeypatch.setattr(narrate_cli, "bootstrap", lambda: _Settings())
    monkeypatch.setattr(narrate_cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(database_mod, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(narrate_cli, "load_findings", _capture)

    args = argparse.Namespace(
        lookback_days=None,
        output_dir=None,
        language=None,
        audience=None,
        max_words=None,
        note=None,
        dry_run=True,
        report="monthly",
    )
    rc = narrate_cli.run(args)
    capsys.readouterr()

    assert rc == 0
    assert seen == {"lookback": 28, "report": "monthly"}
