"""Structured configuration from ``config.yaml``.

Two configuration homes, deliberately split (mirrors pocketlog-importer):

- **ENV** (``app/config.py:Settings``) — secrets and infrastructure:
  ``INGEST_SECRET``, ``DATABASE_URL``, ``TZ``, ``PUID``/``PGID``, ``LOG_*``,
  ``ANALYSIS_CRON``, ``NOTIFY_TOKEN`` …
- **YAML** (this module) — behaviour and profile that is structured, hand-edited
  and not secret: the physiological ``profile`` (for HR-based training load),
  the ``workouts`` mapping, and the analysis ``tunables``.

The file is optional: a missing ``config.yaml`` yields all-default values, so a
fresh container behaves exactly as before this module existed. Defaults here are
the single source of truth for the analysis tunables — ``app/analysis.py``
derives its module constants from :class:`AnalysisConfig`.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileConfig(BaseModel):
    """Physiological profile. Optional; sharpens HR-based training load (TRIMP).

    Consumed by the workout analysis (see ``docs/workout-analysis.md``); defined
    here so the value has a validated home. Without it, the analysis falls back
    to a data-driven HR_max and a generic TRIMP weighting.
    """

    model_config = ConfigDict(extra="forbid")

    # Birth year (not age) so HR_max tracks ageing automatically each run.
    birth_year: int | None = Field(default=None)
    sex: Literal["male", "female", "unspecified"] = "unspecified"
    # Optional overrides; normally HR_max is age-derived and HR_rest is measured.
    hr_max: int | None = Field(default=None, ge=120, le=230)
    hr_rest: int | None = Field(default=None, ge=25, le=120)

    @model_validator(mode="after")
    def _check(self) -> ProfileConfig:
        if self.birth_year is not None:
            age = dt.date.today().year - self.birth_year
            if not 10 <= age <= 120:
                raise ValueError(f"birth_year {self.birth_year} implies an implausible age ({age})")
        if self.hr_max is not None and self.hr_rest is not None and self.hr_max <= self.hr_rest:
            raise ValueError("hr_max must be greater than hr_rest")
        return self


class WorkoutConfig(BaseModel):
    """Workout-analysis knobs (consumed by the upcoming workout pipeline)."""

    model_config = ConfigDict(extra="forbid")

    load_metric: Literal["trimp", "energy", "both"] = "both"
    # Localised HAE workout name -> canonical type, for future type-split load.
    type_map: dict[str, str] = Field(default_factory=dict)
    # Zone-based (Edwards) TRIMP from the intra-workout HR series, as a parallel
    # series next to Banister. Self-gating: with no stored HR samples nothing is
    # emitted, so leaving this on is harmless when the export omits the series.
    edwards: bool = True


class AnalysisConfig(BaseModel):
    """Tunables for the nightly statistical pipeline.

    Defaults equal the values previously hard-coded in ``analysis.py``; changing
    them in ``config.yaml`` lets an operator retune without rebuilding the image.
    Structural periods (weekly=7, annual=365) are NOT exposed — they are domain
    constants, not knobs.
    """

    model_config = ConfigDict(extra="forbid")

    # Correlation
    max_lag: int = Field(default=3, ge=0, le=14)
    min_overlap: int = Field(default=42, ge=2)
    corr_keep_alpha: float = Field(default=0.10, gt=0.0, le=1.0)
    fdr_alpha: float = Field(default=0.05, gt=0.0, le=1.0)
    # Minimum non-zero (active) days each series must have within a pair's
    # overlap before the correlation is trusted. Guards 0-filled sparse series
    # (per-sport workout load): without it a mostly-zero series correlates on a
    # handful of coincidental active days. Continuous series (HR, sleep) are
    # never zero, so the guard never bites them. 0 disables it.
    corr_min_active: int = Field(default=10, ge=0)
    # Minimum absolute Spearman coefficient to report a correlation. With years
    # of daily data even a negligible effect clears the FDR gate (significance is
    # not relevance); this effect-size floor keeps only relationships of at least
    # moderate strength. 0 disables it.
    corr_min_abs: float = Field(default=0.3, ge=0.0, le=1.0)
    # De-trending needs a trustworthy long-run trend to subtract. A series whose
    # real observations cover less than this fraction of its calendar span is too
    # gappy: its STL trend is dominated by interpolation across the gaps, and
    # subtracting that manufactured trend produces spurious (de-trending-only)
    # correlations. Such series are dropped from correlation analysis. Near-daily
    # and 0-filled series sit well above the floor and are never affected. 0
    # disables the guard.
    corr_min_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    # Anomaly
    anomaly_window: int = Field(default=28, ge=2)
    anomaly_threshold: float = Field(default=3.5, gt=0.0)
    anomaly_recent_days: int = Field(default=14, ge=1)
    # Trend / seasonality
    trend_strength_min: float = Field(default=0.30, ge=0.0, le=1.0)
    seasonality_strength_min: float = Field(default=0.20, ge=0.0, le=1.0)
    # Recovery early-warning
    recovery_recent_days: int = Field(default=14, ge=1)
    recovery_z: float = Field(default=1.5, gt=0.0)
    recovery_sleep_z: float = Field(default=-1.0)
    # Consistency
    consistency_window: int = Field(default=28, ge=2)
    consistency_duration_std: float = Field(default=1.0, ge=0.0)
    consistency_bedtime_std: float = Field(default=1.0, ge=0.0)
    # Training load (ACWR = acute 7-day mean / chronic 28-day mean of workout
    # load); flagged only when the ratio leaves the safe band.
    acwr_high: float = Field(default=1.5, gt=0.0)
    acwr_low: float = Field(default=0.8, gt=0.0)
    # Minimum training days within the chronic window before an ACWR is trusted;
    # guards per-sport ratios for rarely-practised sports (a sparse series makes
    # the ratio jump on a single session).
    acwr_min_active_days: int = Field(default=8, ge=1, le=28)


NotifyEvent = Literal["ingest", "analysis", "findings"]


class NotifyConfig(BaseModel):
    """Push notifications (behaviour). The endpoint, event filter and verbosity
    live in YAML; the secret ``token`` comes from ``NOTIFY_TOKEN`` (never YAML)
    and is injected at load time.
    """

    # validate_assignment so the token injection in load_config is still checked.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Service selector. Only "gotify" is supported today (covers Gotify and the
    # Gotify-compatible PushBits). Extend this Literal and add a branch to
    # notify.build_notifier() to support a new service without touching callers.
    type: Literal["gotify"] = "gotify"
    url: str | None = None
    # Which sources may notify (subset of ingest/analysis/findings).
    events: list[NotifyEvent] = Field(default_factory=lambda: ["analysis", "findings"])
    # "problems" => failures + empty ingests + health alerts; "always" => also
    # routine OK summaries.
    level: Literal["problems", "always"] = "problems"
    verify_tls: bool = True
    # Secret, from NOTIFY_TOKEN at load time. Must NOT appear in config.yaml.
    token: str | None = None

    def event_set(self) -> set[str]:
        """The enabled notification sources as a set."""
        return set(self.events)


class NarrateConfig(BaseModel):
    """LLM narration via a local Ollama instance (behaviour, not secrets).

    Enabled by setting ``ollama_url``; everything else has a sensible default.
    The secret Ollama token (if any) is not supported here — Ollama's default
    setup has no auth, and the URL is infrastructure, not a secret.
    """

    model_config = ConfigDict(extra="forbid")

    # Base URL of the Ollama instance — required to use ``healthlog narrate``.
    # e.g. http://192.168.1.100:11434
    ollama_url: str | None = None
    # Ollama model identifier.
    model: str = "qwen2.5:14b"
    # Report language. "en" = English, "de" = German.
    language: Literal["de", "en"] = "en"
    # How far back to look for ref_date-based findings (anomaly, recovery_alert,
    # training_load). Time-independent kinds (correlation, trend, seasonality,
    # consistency) are always included — they represent the current state.
    lookback_days: int = Field(default=7, ge=1, le=90)
    # HTTP timeout in seconds for the Ollama call — generation can be slow.
    timeout_s: int = Field(default=300, ge=10, le=3600)
    # Layer-2 curation: cap the correlations passed to the LLM at this many,
    # highest report_priority first (cross-domain + effect size + lag); the rest
    # are summarised as a count so the report leads with the informative links
    # rather than expected/structural ones. 0 = no cap (pass them all).
    max_correlations: int = Field(default=15, ge=0, le=200)


class AppConfig(BaseModel):
    """Root of ``config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    workouts: WorkoutConfig = Field(default_factory=WorkoutConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    narrate: NarrateConfig = Field(default_factory=NarrateConfig)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate ``config.yaml``. A missing file yields all defaults.

    The notify ``token`` is never read from the file — it is injected from the
    ``NOTIFY_TOKEN`` environment variable (secrets stay in the environment).
    Raises ``ValueError`` on malformed YAML or schema violations so the caller
    can fail with a clean message instead of an opaque traceback.
    """
    p = Path(path)
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {p}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{p}: config root must be a mapping, got {type(data).__name__}")
    else:
        data = {}

    if isinstance(data.get("notify"), dict) and "token" in data["notify"]:
        raise ValueError("notify.token must not be set in config.yaml; use the NOTIFY_TOKEN environment variable")

    config = AppConfig.model_validate(data)
    config.notify.token = os.getenv("NOTIFY_TOKEN") or None
    return config


@lru_cache
def get_app_config() -> AppConfig:
    """Cached config from the path in ``Settings.config_file``."""
    from .config import get_settings

    return load_config(get_settings().config_file)
