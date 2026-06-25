"""Unit tests for the config.yaml loader (app/appconfig.py).

Pure: no DB, no numpy — just YAML + pydantic, so they run in the default suite.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.appconfig import AnalysisConfig, AppConfig, load_config


@pytest.fixture(autouse=True)
def _no_notify_token(monkeypatch):
    # load_config injects NOTIFY_TOKEN; clear it so tests are env-independent.
    monkeypatch.delenv("NOTIFY_TOKEN", raising=False)


def test_missing_file_yields_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg == AppConfig()
    assert cfg.analysis.max_lag == 3
    assert cfg.profile.sex == "unspecified"


def test_empty_file_yields_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("# only comments\n")
    assert load_config(p) == AppConfig()


def test_partial_override_keeps_other_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("analysis:\n  anomaly_threshold: 4.0\n  max_lag: 5\n")
    cfg = load_config(p)
    assert cfg.analysis.anomaly_threshold == 4.0
    assert cfg.analysis.max_lag == 5
    # untouched fields stay at their defaults
    assert cfg.analysis.min_overlap == 42
    assert cfg.analysis.fdr_alpha == 0.05


def test_profile_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("profile:\n  birth_year: 1990\n  sex: female\n  hr_max: 190\n")
    cfg = load_config(p)
    assert cfg.profile.birth_year == 1990
    assert cfg.profile.sex == "female"
    assert cfg.profile.hr_max == 190


def test_invalid_yaml_raises_valueerror(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("analysis: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_config(p)


def test_non_mapping_root_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(p)


def test_unknown_key_is_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("analysis:\n  max_lag: 3\n  typo_field: 1\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_out_of_range_is_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("analysis:\n  corr_keep_alpha: 5\n")  # must be in (0, 1]
    with pytest.raises(ValueError):
        load_config(p)


def test_implausible_birth_year_is_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(f"profile:\n  birth_year: {dt.date.today().year - 5}\n")  # age 5
    with pytest.raises(ValueError, match="implausible age"):
        load_config(p)


def test_hr_max_must_exceed_hr_rest(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("profile:\n  hr_max: 120\n  hr_rest: 120\n")  # equal => invalid
    with pytest.raises(ValueError, match="hr_max must be greater"):
        load_config(p)


def test_notify_defaults(tmp_path):
    n = load_config(tmp_path / "nope.yaml").notify
    assert n.url is None
    assert n.event_set() == {"analysis", "findings"}
    assert n.level == "problems"
    assert n.verify_tls is True
    assert n.token is None


def test_notify_behaviour_from_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("notify:\n  url: https://push.example.com\n  events: [ingest]\n  level: always\n  verify_tls: false\n")
    n = load_config(p).notify
    assert n.url == "https://push.example.com"
    assert n.event_set() == {"ingest"}
    assert n.level == "always"
    assert n.verify_tls is False


def test_notify_token_comes_from_env_not_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_TOKEN", "pb_secret")
    cfg = load_config(tmp_path / "nope.yaml")  # missing file, token still injected
    assert cfg.notify.token == "pb_secret"


def test_notify_token_in_yaml_is_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("notify:\n  url: https://push.example.com\n  token: leaked\n")
    with pytest.raises(ValueError, match="NOTIFY_TOKEN"):
        load_config(p)


def test_notify_unknown_event_is_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("notify:\n  events: [bogus]\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_narrate_defaults(tmp_path):
    n = load_config(tmp_path / "nope.yaml").narrate
    assert n.ollama_url is None
    assert n.model == "qwen2.5:14b"
    assert n.language == "en"
    assert n.lookback_days == 7
    assert n.timeout_s == 300


def test_narrate_config_from_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("narrate:\n  ollama_url: http://mac:11434\n  lookback_days: 14\n  language: de\n")
    n = load_config(p).narrate
    assert n.ollama_url == "http://mac:11434"
    assert n.lookback_days == 14
    assert n.language == "de"
    assert n.model == "qwen2.5:14b"  # default preserved


def test_narrate_invalid_language_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("narrate:\n  language: fr\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_narrate_unknown_key_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("narrate:\n  bogus_field: 1\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_analysis_defaults_are_stable():
    # These defaults are the single source of truth for analysis.py's module
    # constants; pin them so a change is deliberate.
    d = AnalysisConfig()
    assert (d.max_lag, d.min_overlap, d.corr_keep_alpha, d.fdr_alpha) == (3, 42, 0.10, 0.05)
    assert (d.corr_min_active, d.corr_min_abs, d.corr_min_coverage) == (10, 0.3, 0.5)
    assert (d.anomaly_window, d.anomaly_threshold, d.anomaly_recent_days) == (28, 3.5, 14)
    assert (d.trend_strength_min, d.seasonality_strength_min) == (0.30, 0.20)
    assert (d.recovery_recent_days, d.recovery_z, d.recovery_sleep_z) == (14, 1.5, -1.0)
    assert (d.consistency_window, d.consistency_duration_std, d.consistency_bedtime_std) == (28, 1.0, 1.0)
