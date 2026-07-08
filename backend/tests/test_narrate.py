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
        lambda _db, _lookback: [_finding("anomaly", details={"z": 3.9, "value": 72.5}, severity=3.9)],
    )
    monkeypatch.setattr(narrate_cli, "OllamaClient", _no_client)

    args = argparse.Namespace(
        lookback_days=None, output_dir=None, language=None, audience=None, note=None, dry_run=True
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
    assert "beim ersten Auftreten" in standard  # terms translated once
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
