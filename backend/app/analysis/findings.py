"""Finding builders and series assembly.

Builds the analysis series (core metrics + derived sleep + workout-load) and the
finding kinds (correlation, anomaly, trend, seasonality, recovery_alert,
consistency, training_load, training_status, stress, body_battery, plus the
descriptive weekly_*/monthly_* summaries and fitness_markers for the weekly and
monthly reports) on top of the pure helpers and DB loaders. The stress and body_battery findings are
alert-only; their continuous scores/timelines live in their own tables (see
``stress.py`` / ``body_battery.py``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, fields

import pandas as pd
from sqlalchemy.orm import Session

from ..appconfig import AnalysisConfig, BodyBatteryConfig, ProfileConfig, StressConfig, WorkoutConfig
from ..models import Finding
from ..registry import METRIC_REGISTRY
from ..workout_types import canonical_workout_type
from .body_battery import load_body_battery_daily
from .constants import (
    _DEFAULT_APP_CONFIG,
    _DEFAULTS,
    ACWR_ACUTE_DAYS,
    ACWR_CHRONIC_DAYS,
    ATL_DAYS,
    CTL_DAYS,
    CTL_TREND_LOOKBACK_DAYS,
    CTL_TREND_REL,
    MONTH_PERIOD,
    log,
)
from .load import (
    _reindex_full,
    load_daily_series,
    load_sleep_frame,
    load_workout_frame,
    load_workout_hr_samples,
)
from .pure import (
    Corr,
    Decomp,
    _component_strength,
    _global_robust_z,
    acute_chronic_ratio,
    aggregate_workout_daily,
    annual_seasonality,
    circular_bedtime_offset,
    decompose,
    edwards_trimp,
    fdr_adjust,
    fill_zero_within_span,
    latest_marker_delta,
    resolve_hr_max,
    resolve_hr_rest,
    robust_z,
    rolling_mad_anomalies,
    spearman_lag,
    training_status,
    trend_monotonicity,
    trend_slope,
    week_breakdown,
    weekly_baseline_delta,
    weekly_body_battery_summary,
    weekly_sessions_summary,
    weekly_sleep_summary,
    weekly_stress_summary,
    weekly_window,
)
from .stress import load_stress_daily


@dataclass
class AnalysisResult:
    correlations: int = 0
    anomalies: int = 0
    trends: int = 0
    seasonality: int = 0
    recovery_alerts: int = 0
    consistency: int = 0
    training_load: int = 0
    training_status: int = 0
    stress: int = 0
    body_battery: int = 0
    weekly_training: int = 0
    weekly_sleep: int = 0
    weekly_stress: int = 0
    weekly_body_battery: int = 0
    weekly_vitals: int = 0
    weekly_activity: int = 0
    monthly_training: int = 0
    monthly_sleep: int = 0
    monthly_stress: int = 0
    monthly_body_battery: int = 0
    monthly_vitals: int = 0
    monthly_activity: int = 0
    fitness_markers: int = 0

    def counts(self) -> list[tuple[str, int]]:
        """The (category, count) pairs in declaration order — the single source
        for ``total()`` and the run summary, so a new finding kind is added by
        declaring one field above."""
        return [(f.name, getattr(self, f.name)) for f in fields(self)]

    def total(self) -> int:
        return sum(count for _, count in self.counts())


def core_metrics() -> list[str]:
    return [m for m, spec in METRIC_REGISTRY.items() if spec["tier"] == "core"]


def build_workout_series(
    db: Session,
    tz: str,
    profile: ProfileConfig,
    workouts: WorkoutConfig,
    rhr: pd.Series | None,
) -> dict[str, pd.Series]:
    """Daily workout-load series (training-load features as 0-filled series).

    Workouts are events, so a training-free day is a real 0 (not a gap): every
    series is densified over its observed span with ``fill_zero_within_span``.
    ``workout_trimp`` (HR-based) and ``workout_load`` (kcal) run in parallel and
    are gated by ``workouts.load_metric``; duration/count always come along.

    An additional per-sport load series is emitted for each recognised type
    (``workout_trimp_running``, ``workout_load_cycling`` …) so a sport's lagged
    effect on recovery can be told apart from another's. Types are normalised by
    the built-in workout-type map (``workout_types.py``), extensible/overridable
    via ``workouts.type_map``. Unrecognised workouts still feed the type-agnostic
    aggregate; they just get no per-type series.

    When ``workouts.edwards`` is on and an intra-workout HR series is stored, a
    parallel zone-based series (``workout_edwards`` + per sport) is added next to
    the Banister ``workout_trimp``. It self-gates on the data: with no stored
    samples nothing is emitted. Empty when there are no workouts.
    """
    sessions = load_workout_frame(db, tz)
    if sessions.empty:
        return {}

    hr_max = resolve_hr_max(profile, sessions["max_hr"])
    hr_rest, hr_rest_default = resolve_hr_rest(rhr, profile)
    # Zone-based (Edwards) TRIMP per session, from the intra-workout HR series.
    # Self-gating: with no stored samples there is simply no edwards column, so
    # the aggregate stays 0 and no workout_edwards series is emitted.
    if workouts.edwards:
        hr_samples = load_workout_hr_samples(db)
        if hr_samples:
            sessions = sessions.assign(
                edwards=[
                    edwards_trimp(hr_samples.get(hid), hr_max, dur)
                    for hid, dur in zip(sessions["hae_id"], sessions["duration_s"], strict=True)
                ]
            )
    daily = aggregate_workout_daily(sessions, hr_rest, hr_rest_default, hr_max, profile.sex)
    if daily.empty:
        return {}

    out: dict[str, pd.Series] = {}
    # Type-agnostic aggregate (Iteration 1): load series + duration/count.
    for name, col in _load_columns(workouts.load_metric).items():
        out[name] = fill_zero_within_span(daily[col])
    _emit_edwards(out, daily, workouts.edwards)
    for name, col in {"workout_duration": "duration_h", "workout_count": "count"}.items():
        out[name] = fill_zero_within_span(daily[col])
    # Intensity is an average (NaN where absent), not a load total -> no 0-fill.
    intensity = _reindex_full(daily["intensity"])
    if not intensity.dropna().empty:
        out["workout_intensity"] = intensity

    # Per-sport load (Iteration 2): the built-in workout-type map normalises the
    # localised HAE name out of the box, with workouts.type_map layered on top.
    types = sessions["name"].map(lambda n: canonical_workout_type(n, workouts.type_map))
    for wtype in sorted(t for t in types if isinstance(t, str)):
        subset = sessions[types == wtype]
        sub_daily = aggregate_workout_daily(subset, hr_rest, hr_rest_default, hr_max, profile.sex)
        if sub_daily.empty:
            continue
        for name, col in _load_columns(workouts.load_metric).items():
            out[f"{name}_{wtype}"] = fill_zero_within_span(sub_daily[col])
        _emit_edwards(out, sub_daily, workouts.edwards, suffix=f"_{wtype}")

    # Drop any series that ended up empty (matching the core/sleep series; a
    # constant series is harmless downstream, so no std>0 guard).
    return {name: s for name, s in out.items() if not s.dropna().empty}


def _load_columns(load_metric: str) -> dict[str, str]:
    """Series-name -> aggregate-column for the load metrics enabled by config."""
    columns: dict[str, str] = {}
    if load_metric in ("trimp", "both"):
        columns["workout_trimp"] = "trimp"
    if load_metric in ("energy", "both"):
        columns["workout_load"] = "load"
    return columns


def _emit_edwards(out: dict[str, pd.Series], daily: pd.DataFrame, enabled: bool, suffix: str = "") -> None:
    """Append the zone-based ``workout_edwards`` series (0-filled) when enabled
    and the daily Edwards load is non-zero somewhere — an all-zero column means
    no HR series reached this scope, so there is nothing to add."""
    if not enabled or "edwards" not in daily.columns:
        return
    series = fill_zero_within_span(daily["edwards"])
    if (series > 0).any():
        out[f"workout_edwards{suffix}"] = series


def build_series(
    db: Session,
    tz: str,
    profile: ProfileConfig | None = None,
    workouts: WorkoutConfig | None = None,
    sleep: pd.DataFrame | None = None,
) -> dict[str, pd.Series]:
    """All analysis series: core metrics + derived sleep + workout-load series.

    ``sleep`` may be supplied by the caller (``run`` loads it once and shares it
    with the consistency pass) to avoid a second ``load_sleep_frame`` query.
    """
    profile = profile or _DEFAULT_APP_CONFIG.profile
    workouts = workouts or _DEFAULT_APP_CONFIG.workouts
    series: dict[str, pd.Series] = {}
    for metric in core_metrics():
        s = load_daily_series(db, metric, METRIC_REGISTRY[metric]["agg_default"], tz)
        if not s.dropna().empty:
            series[metric] = s

    if sleep is None:
        sleep = load_sleep_frame(db, tz)
    if not sleep.empty:
        mapping = {
            "sleep_total_h": "total_sleep_h",
            "sleep_deep_h": "deep_h",
            "sleep_rem_h": "rem_h",
            "sleep_efficiency": "efficiency",
        }
        for name, col in mapping.items():
            s = _reindex_full(sleep[col])
            if not s.dropna().empty:
                series[name] = s

    series.update(build_workout_series(db, tz, profile, workouts, series.get("resting_heart_rate")))
    return series


def _decompose_all(series: dict[str, pd.Series]) -> dict[str, Decomp | None]:
    """STL/MSTL decomposition of every series, computed once.

    Decomposition (MSTL(7, 365) over years of daily data) is the most expensive
    step in the pipeline; both the correlation de-trending and the
    trend/seasonality findings need it, so ``run`` computes it once here and
    threads the result through both passes instead of decomposing twice.

    A decomposition that blows up on one pathological series degrades to None
    for that series (the same as "too short to decompose") instead of aborting
    the whole run — every consumer already handles the None case.
    """
    out: dict[str, Decomp | None] = {}
    for name, s in series.items():
        try:
            out[name] = decompose(s)
        except Exception:
            log.exception("decomposition failed for %s; treating series as undecomposable", name)
            out[name] = None
    return out


def _detrended_series(s: pd.Series, decomp: Decomp) -> pd.Series:
    """``s`` with only its long-run trend removed (seasonality retained).

    This is the *previous* correlation basis, kept to stamp ``details.detr_coef``
    so a report can show how much of an old number lived in shared seasonality."""
    return s - decomp.trend.reindex(s.index)


def _residual_series(s: pd.Series, decomp: Decomp) -> pd.Series:
    """``s`` with both trend AND seasonal components removed (the STL residual),
    keeping real observations only. This is the correlation basis."""
    out = _detrended_series(s, decomp)
    for seasonal in decomp.seasonal.values():
        out = out - seasonal.reindex(s.index)
    return out


def _residual_for_correlation(
    series: dict[str, pd.Series], decomps: dict[str, Decomp | None] | None = None
) -> dict[str, pd.Series]:
    """Strip each metric's trend AND seasonal component, leaving the STL residual,
    so correlations measure pure day-to-day co-movement.

    Removing only the trend (the old basis) leaves seasonality in, so two metrics
    that merely share a weekly/annual rhythm — or that just trend together over
    the years — correlate spuriously. Validated on live data, ~two thirds of the
    de-trended findings collapsed to a ~0 residual once seasonality was also
    removed (see docs/ARCHITECTURE.md §4.8). Real observations only: the series
    keeps its complete daily index (NaN at gaps/edges) so lag shifts stay
    calendar-correct, but no interpolated point enters a correlation. A series too
    short to decompose is dropped.

    ``decomps`` reuses a precomputed decomposition cache when provided; absent
    one (e.g. a direct test call) each series is decomposed on the fly.
    """
    out: dict[str, pd.Series] = {}
    for name, s in series.items():
        decomp = decomps.get(name) if decomps is not None else decompose(s)
        if decomp is None:
            continue
        residual = _residual_series(s, decomp)
        if not residual.dropna().empty:
            out[name] = residual
    return out


# An "activity-volume" series measures *how much you moved or trained*, not a
# body state. Two of them correlating is structural, not a health insight — it
# just says the same activity was logged two ways. The family has two sub-groups:
#
#   * Workout-derived metrics: per-target training load — Banister TRIMP, active-
#     energy load, zone-based Edwards, plus session duration, count and intensity
#     (the type-agnostic aggregate, its per-sport children, one sport vs another).
#     Intensity is load-per-time off the same sessions, so it co-moves with load
#     by construction (intensity vs trimp/load/edwards ~ 0.65).
#   * Apple activity metrics: the move/exercise/stand ring and its raw drivers —
#     exercise minutes, stand time, active energy, steps, distance, flights.
#
# Correlating *within* this family is tautological, whether load-vs-load, ring-
# vs-ring (step_count vs walking_running_distance), or load-vs-ring
# (apple_exercise_time vs workout_duration ~ 0.9). What *is* informative pairs an
# activity-volume series with a body-state metric (recovery, sleep, vital) — e.g.
# workout_load_running vs resting_heart_rate, or workout_intensity vs sleep — and
# is kept.
_WORKOUT_LOAD_FAMILY = ("trimp", "load", "edwards", "duration", "count", "intensity")
_APPLE_ACTIVITY_METRICS = frozenset(
    {
        "apple_exercise_time",
        "apple_stand_time",
        "active_energy",
        "step_count",
        "walking_running_distance",
        "flights_climbed",
    }
)


def _is_workout_load_family(name: str) -> bool:
    """True for ``workout_{trimp,load,edwards,duration,count,intensity}[_sport]``."""
    return any(name == f"workout_{m}" or name.startswith(f"workout_{m}_") for m in _WORKOUT_LOAD_FAMILY)


def _is_activity_volume(name: str) -> bool:
    """True for any activity-volume series: a workout load-family metric or an
    Apple activity-ring metric (exercise/stand time, active energy, steps,
    distance, flights)."""
    return _is_workout_load_family(name) or name in _APPLE_ACTIVITY_METRICS


def _is_redundant_activity_pair(a: str, b: str) -> bool:
    """True when *both* names are activity-volume series: their correlation is
    training/movement composition, not a health relationship, so it is
    suppressed. An activity-volume series against any body-state metric
    (recovery, sleep, vital) is kept — that is where the value is."""
    return _is_activity_volume(a) and _is_activity_volume(b)


# --- Correlation prioritisation (layer 2) -----------------------------------
# Suppression removes structural pairs; the survivors still span a quality
# gradient. A lagged cross-subsystem link (training load -> next-day respiratory
# rate) is a real insight, while same-day pairs inside one subsystem (total vs
# deep sleep, average vs resting heart rate) are expected and only crowd the
# report. Each metric maps to a coarse physiological domain; a pair gets a tier
# from those domains. The tier is stored on the finding (``details.priority_tier``)
# so both the narration and Grafana rank by the same rule without re-deriving it.
_AUTONOMIC = frozenset({"respiratory_rate", "heart_rate_variability", "cardio_recovery"})
_HR_RATE = frozenset({"heart_rate", "resting_heart_rate", "walking_heart_rate_average"})
_VITAL = frozenset(
    {
        "vo2_max",
        "apple_sleeping_wrist_temperature",
        "weight_body_mass",
        "blood_oxygen_saturation",
        "body_temperature",
        "breathing_disturbances",
    }
)
_PHYSIO = frozenset({"sleep", "autonomic", "hr_rate", "vital", "activity"})


def _metric_domain(name: str | None) -> str:
    """Coarse physiological domain of a metric, for correlation prioritisation."""
    if not name:
        return "other"
    if name.startswith("sleep_"):
        return "sleep"
    if name in _AUTONOMIC:
        return "autonomic"
    if name in _HR_RATE:
        return "hr_rate"
    if name in _VITAL:
        return "vital"
    if name == "physical_effort" or _is_activity_volume(name):
        return "activity"
    if name == "time_in_daylight":
        return "env"
    return "other"


def _pair_tier(domain_a: str, domain_b: str) -> int:
    """Priority tier for a correlation between two metric domains.

    2 = informative cross-subsystem link, 1 = neutral, 0 = expected/structural
    (a subsystem correlated with itself, or a trivially-coupled pair: exercise
    raises average HR; sunny days mean more movement).
    """
    pair = frozenset({domain_a, domain_b})
    if domain_a == domain_b and domain_a in {"sleep", "hr_rate", "activity"}:
        return 0
    if pair == {"hr_rate", "activity"}:
        return 0
    if pair == {"env", "activity"}:
        return 0
    if domain_a in _PHYSIO and domain_b in _PHYSIO:
        return 2
    return 1


def report_priority(
    metric_a: str | None, metric_b: str | None, coefficient: float | None, lag_days: int | None
) -> float:
    """Rank score for a correlation finding (higher = lead the report).

    The domain-crossing tier dominates, then the effect size, then a small bonus
    for lagged (directional, causally plausible) relationships.
    """
    tier = _pair_tier(_metric_domain(metric_a), _metric_domain(metric_b))
    strength = abs(coefficient) if coefficient is not None else 0.0
    lag_bonus = 0.05 * min(lag_days or 0, 3)
    return tier * 10.0 + strength + lag_bonus


def _correlation_findings(
    series: dict[str, pd.Series],
    computed_at: dt.datetime,
    cfg: AnalysisConfig | None = None,
    decomps: dict[str, Decomp | None] | None = None,
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
    residual = _residual_for_correlation(series, decomps)
    names = list(residual)
    candidates: list[tuple[str, str, int, Corr]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if _is_redundant_activity_pair(a, b):
                continue  # both measure activity volume (load or Apple ring) — structural, not health
            ma = cfg.corr_min_active
            c0 = spearman_lag(residual[a], residual[b], 0, min_overlap=cfg.min_overlap, min_active=ma)
            if c0:
                candidates.append((a, b, 0, c0))
            for lag in range(1, cfg.max_lag + 1):
                cab = spearman_lag(residual[a], residual[b], lag, min_overlap=cfg.min_overlap, min_active=ma)
                if cab:
                    candidates.append((a, b, lag, cab))  # a leads b
                cba = spearman_lag(residual[b], residual[a], lag, min_overlap=cfg.min_overlap, min_active=ma)
                if cba:
                    candidates.append((b, a, lag, cba))  # b leads a

    if not candidates:
        return []
    adj = fdr_adjust([c.p for *_, c in candidates], alpha=cfg.fdr_alpha)

    # FDR sees every lag/direction we tested (multiple-testing honesty); for
    # presentation keep only the single strongest lag/direction per unordered
    # metric pair, so a slow pair isn't listed 5x across lags and directions.
    best: dict[frozenset[str], tuple[str, str, int, Corr, float]] = {}
    for (a, b, lag, c), p_adj in zip(candidates, adj, strict=True):
        if p_adj > cfg.corr_keep_alpha or abs(c.coef) < cfg.corr_min_abs:
            continue  # not significant, or too weak to be worth reporting
        key = frozenset((a, b))
        prev = best.get(key)
        if prev is None or abs(c.coef) > abs(prev[3].coef):
            best[key] = (a, b, lag, c, p_adj)

    findings = []
    for a, b, lag, c, p_adj in best.values():
        # The reported ``coefficient`` is the residual (trend + seasonal removed)
        # Spearman. Stamp two comparison coefficients at the same lag/direction for
        # transparency about what the de-seasonalising removed:
        #   * raw_coef  — nothing removed (shared trend + seasonal + day-to-day)
        #   * detr_coef — only the trend removed (the previous reporting basis)
        # When detr_coef is strong but the stored residual coefficient is ~0, the
        # old number lived in shared seasonality, not a real day-to-day link.
        raw = spearman_lag(series[a], series[b], lag, min_overlap=2, min_active=0)
        # Raw-corroboration guard: trust the residual correlation only if the raw
        # series show the same-signed relationship with at least a weak magnitude.
        # A strong residual with raw ~ 0 or opposite sign is an artefact of the
        # de-seasonalising (typically a sparse or derived metric whose residual is
        # decomposition noise), the mirror image of a shared-seasonality artefact
        # (caught above by the residual being ~ 0). A genuine link shows in both.
        if cfg.corr_raw_min_abs > 0 and (raw is None or raw.coef * c.coef <= 0 or abs(raw.coef) < cfg.corr_raw_min_abs):
            continue
        dec_a = decomps.get(a) if decomps is not None else decompose(series[a])
        dec_b = decomps.get(b) if decomps is not None else decompose(series[b])
        detr = None
        if dec_a is not None and dec_b is not None:
            detr = spearman_lag(
                _detrended_series(series[a], dec_a),
                _detrended_series(series[b], dec_b),
                lag,
                min_overlap=2,
                min_active=0,
            )
        details: dict[str, object] = {
            "n": c.n,
            "priority_tier": _pair_tier(_metric_domain(a), _metric_domain(b)),
        }
        if raw is not None:
            details["raw_coef"] = round(raw.coef, 4)
        if detr is not None:
            details["detr_coef"] = round(detr.coef, 4)
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="correlation",
                metric_a=a,
                metric_b=b,
                lag_days=lag,
                coefficient=round(c.coef, 4),
                p_value=c.p,
                p_value_adj=p_adj,
                window_start=c.start,
                window_end=c.end,
                severity=round(abs(c.coef), 4),
                details=details,
            )
        )
    return findings


def _dedupe_workout_anomalies(findings: list[Finding]) -> list[Finding]:
    """Collapse co-derived workout-load anomalies that share a day to the single
    strongest. ``workout_{trimp,load,edwards,duration,...}`` (and per-sport
    variants) are alternative measures of the *same* session, so one hard day
    otherwise fires several near-identical anomalies. Non-workout metrics are
    independent and never merged.
    """
    best_by_day: dict[dt.date, Finding] = {}
    kept: list[Finding] = []
    for f in findings:
        if not _is_workout_load_family(f.metric_a):
            kept.append(f)
            continue
        cur = best_by_day.get(f.ref_date)
        if cur is None or f.severity > cur.severity:
            best_by_day[f.ref_date] = f
    kept.extend(best_by_day.values())
    return kept


def _anomaly_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
    guard = cfg.anomaly_min_global_z > 0
    findings = []
    for name, s in series.items():
        s = s.dropna()
        if s.empty:
            continue
        cutoff = s.index.max() - pd.Timedelta(days=cfg.anomaly_recent_days)
        anomalies = rolling_mad_anomalies(s, window=cfg.anomaly_window, threshold=cfg.anomaly_threshold)
        for ts, row in anomalies.iterrows():
            if ts < cutoff:
                continue
            # Global corroboration: a day flagged against a calm recent window is
            # only an anomaly if it is also unusual against the series' own full
            # history. Drops window-only spikes (a hard workout after a taper,
            # already covered by training_load) while keeping genuine extremes.
            global_z = _global_robust_z(s, float(row["value"]))
            if guard and (global_z is None or abs(global_z) < cfg.anomaly_min_global_z):
                continue
            details = {"value": round(float(row["value"]), 4), "z": round(float(row["z"]), 4)}
            if global_z is not None:
                details["global_z"] = round(global_z, 4)
            findings.append(
                Finding(
                    computed_at=computed_at,
                    kind="anomaly",
                    metric_a=name,
                    ref_date=ts.date(),
                    severity=round(abs(float(row["z"])), 4),
                    details=details,
                )
            )
    return _dedupe_workout_anomalies(findings)


def _trend_and_seasonality_findings(
    series: dict[str, pd.Series],
    computed_at: dt.datetime,
    cfg: AnalysisConfig | None = None,
    decomps: dict[str, Decomp | None] | None = None,
) -> tuple[list, list]:
    cfg = cfg or _DEFAULTS
    trends, seasons = [], []
    for name, s in series.items():
        decomp = decomps.get(name) if decomps is not None else decompose(s)
        if decomp is None:
            continue
        start, end = decomp.trend.index.min().date(), decomp.trend.index.max().date()

        strength = _component_strength(decomp.trend, decomp.resid)
        # Strength only certifies the trend is smooth vs the residual, not that it
        # goes anywhere: a smooth meander (up then back) scores high yet has no
        # direction. Require the trend to also move consistently one way
        # (monotonicity, mirrors the other kinds' corroboration guards). A floor
        # of 0 disables the guard.
        monotonicity = trend_monotonicity(decomp.trend)
        trend_guard = cfg.trend_min_monotonicity > 0
        if strength >= cfg.trend_strength_min and (
            not trend_guard or (monotonicity is not None and monotonicity >= cfg.trend_min_monotonicity)
        ):
            slope = trend_slope(decomp.trend)
            details = {"slope_per_day": round(slope, 6), "strength": round(strength, 4)}
            if monotonicity is not None:
                details["monotonicity"] = round(monotonicity, 4)
            trends.append(
                Finding(
                    computed_at=computed_at,
                    kind="trend",
                    metric_a=name,
                    window_start=start,
                    window_end=end,
                    severity=round(strength, 4),
                    details=details,
                )
            )

        annual = annual_seasonality(decomp)
        # Strength alone over-fires: MSTL fits *some* annual component for every
        # series, so a high in-sample strength is necessary but not sufficient. A
        # genuine annual cycle also recurs year over year; require the seasonal
        # shape to be reproducible (mirrors the correlation raw-corroboration
        # guard, ARCHITECTURE.md §4.8). reproducibility is None when there are
        # fewer than two comparable years -> not trustworthy, dropped.
        reproducibility = annual["reproducibility"] if annual else None
        # A floor of 0 disables the guard (mirrors corr_raw_min_abs); otherwise the
        # shape must recur: reproducibility is not None and clears the floor.
        guard = cfg.seasonality_reproducibility_min > 0
        if (
            annual
            and annual["strength"] >= cfg.seasonality_strength_min
            and (not guard or (reproducibility is not None and reproducibility >= cfg.seasonality_reproducibility_min))
        ):
            seasons.append(
                Finding(
                    computed_at=computed_at,
                    kind="seasonality",
                    metric_a=name,
                    window_start=start,
                    window_end=end,
                    severity=round(annual["strength"], 4),
                    note=None if annual["phase_confident"] else "annual phase uncertain (peak and trough too close)",
                    details={
                        "strength": round(annual["strength"], 4),
                        "reproducibility": round(reproducibility, 4) if reproducibility is not None else None,
                        "amplitude": round(annual["amplitude"], 4),
                        "peak_month": annual["peak_month"],
                        "trough_month": annual["trough_month"],
                        "phase_confident": annual["phase_confident"],
                    },
                )
            )
    return trends, seasons


def _recovery_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
    rhr = series.get("resting_heart_rate")
    hrv = series.get("heart_rate_variability")
    if rhr is None or hrv is None:
        return []
    rhr_z = robust_z(rhr.dropna())
    hrv_z = robust_z(hrv.dropna())
    sleep_z = robust_z(series["sleep_total_h"].dropna()) if "sleep_total_h" in series else None

    paired = pd.concat([rhr_z.rename("rhr"), hrv_z.rename("hrv")], axis=1, join="inner").dropna()
    if paired.empty:
        return []
    cutoff = paired.index.max() - pd.Timedelta(days=cfg.recovery_recent_days)
    findings = []
    for ts, row in paired.iterrows():
        if ts < cutoff:
            continue
        if row["rhr"] >= cfg.recovery_z and row["hrv"] <= -cfg.recovery_z:
            short_sleep = bool(sleep_z is not None and ts in sleep_z.index and sleep_z.loc[ts] <= cfg.recovery_sleep_z)
            severity = (row["rhr"] - row["hrv"]) / 2.0
            findings.append(
                Finding(
                    computed_at=computed_at,
                    kind="recovery_alert",
                    metric_a="recovery",
                    ref_date=ts.date(),
                    severity=round(float(severity), 4),
                    note="low HRV and high resting HR" + (" with short sleep" if short_sleep else ""),
                    details={
                        "resting_heart_rate_z": round(float(row["rhr"]), 4),
                        "heart_rate_variability_z": round(float(row["hrv"]), 4),
                        "short_sleep": short_sleep,
                    },
                )
            )
    return findings


def _stress_findings(db: Session, computed_at: dt.datetime, cfg: StressConfig | None = None) -> list[Finding]:
    """High-stress days as alert-only findings (mirrors ``_recovery_findings``).

    The continuous stress score lives in ``stress_daily`` (for Grafana); this
    surfaces only the recent days whose score reaches ``cfg.alert_score`` as a
    ``kind="stress"`` finding, so the narration can mention them. A calm day
    produces no row.
    """
    cfg = cfg or StressConfig()
    if not cfg.enabled:
        return []
    daily = load_stress_daily(db, since_days=cfg.alert_recent_days)
    if daily.empty:
        return []
    findings: list[Finding] = []
    for ts, row in daily.iterrows():
        score = row["score"]
        if score is None or pd.isna(score) or score < cfg.alert_score:
            continue
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="stress",
                metric_a="stress",
                ref_date=ts.date(),
                severity=round(float(score), 4),
                note="elevated daily stress",
                details={
                    "score": round(float(score), 1),
                    "high_min": int(row["high_min"]),
                    "medium_min": int(row["medium_min"]),
                    "rest_min": int(row["rest_min"]),
                    "hrv_z": round(float(row["hrv_z"]), 4)
                    if row["hrv_z"] is not None and not pd.isna(row["hrv_z"])
                    else None,
                },
            )
        )
    return findings


def _body_battery_findings(
    db: Session, computed_at: dt.datetime, cfg: BodyBatteryConfig | None = None
) -> list[Finding]:
    """Low Body-Battery days as alert-only findings (mirrors ``_stress_findings``).

    The continuous 0-100 reserve lives in ``body_battery_daily`` (for Grafana);
    this surfaces only the recent days whose lowest level drops to at/below
    ``cfg.alert_level`` as a ``kind="body_battery"`` finding, so the narration can
    flag a day you ran the tank near empty. A day that stayed charged produces no
    row. ``severity`` is ``100 − low_level`` (the day's depletion), so higher =
    worse like every other finding kind; the raw level lives in ``details``.
    """
    cfg = cfg or BodyBatteryConfig()
    if not cfg.enabled:
        return []
    daily = load_body_battery_daily(db, since_days=cfg.alert_recent_days)
    if daily.empty:
        return []
    findings: list[Finding] = []
    for ts, row in daily.iterrows():
        low = row["low_level"]
        if low is None or pd.isna(low) or low > cfg.alert_level:
            continue
        wake = row["wake_level"]
        high = row["high_level"]
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="body_battery",
                metric_a="body_battery",
                ref_date=ts.date(),
                severity=round(100.0 - float(low), 4),
                note="low energy reserve",
                details={
                    "low_level": int(low),
                    "high_level": int(high) if high is not None and not pd.isna(high) else None,
                    "wake_level": int(wake) if wake is not None and not pd.isna(wake) else None,
                    "charged": round(float(row["charged"]), 1),
                    "drained": round(float(row["drained"]), 1),
                },
            )
        )
    return findings


def _consistency_findings(
    db: Session,
    tz: str,
    computed_at: dt.datetime,
    cfg: AnalysisConfig | None = None,
    sleep: pd.DataFrame | None = None,
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
    if sleep is None:
        sleep = load_sleep_frame(db, tz)
    if sleep.empty:
        return []
    findings = []

    duration = _reindex_full(sleep["total_sleep_h"]).dropna()
    if len(duration) >= cfg.consistency_window:
        std = float(duration.tail(cfg.consistency_window).std())
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="consistency",
                metric_a="sleep_total_h",
                window_start=duration.index[-cfg.consistency_window].date(),
                window_end=duration.index[-1].date(),
                severity=round(std, 4),
                note="irregular sleep duration" if std > cfg.consistency_duration_std else "stable sleep duration",
                details={"std_hours": round(std, 4), "threshold": cfg.consistency_duration_std},
            )
        )

    bedtime = circular_bedtime_offset(_reindex_full(sleep["bedtime"]).dropna())
    if len(bedtime) >= cfg.consistency_window:
        std = float(bedtime.tail(cfg.consistency_window).std())
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="consistency",
                metric_a="bedtime",
                window_start=bedtime.index[-cfg.consistency_window].date(),
                window_end=bedtime.index[-1].date(),
                severity=round(std, 4),
                note="irregular bedtime" if std > cfg.consistency_bedtime_std else "stable bedtime",
                details={"std_hours": round(std, 4), "threshold": cfg.consistency_bedtime_std},
            )
        )
    return findings


def _training_load_targets(series: dict[str, pd.Series]) -> list[str]:
    """Series to assess for ACWR: the type-agnostic aggregate plus one per sport.

    Each "family" contributes a single series, preferring TRIMP (HR-based, the
    better signal) over the kcal load. Returns e.g.
    ``["workout_trimp", "workout_trimp_cycling", "workout_trimp_running"]``.
    """
    targets: list[str] = []
    if "workout_trimp" in series:
        targets.append("workout_trimp")
    elif "workout_load" in series:
        targets.append("workout_load")
    sports = {
        key[len(prefix) :] for key in series for prefix in ("workout_trimp_", "workout_load_") if key.startswith(prefix)
    }
    for sport in sorted(sports):
        if f"workout_trimp_{sport}" in series:
            targets.append(f"workout_trimp_{sport}")
        elif f"workout_load_{sport}" in series:
            targets.append(f"workout_load_{sport}")
    return targets


def _active_days(s: pd.Series, window: int) -> int:
    """Training days (load > 0) within the trailing ``window`` of a dense series."""
    return int((s.dropna().tail(window) > 0).sum())


def _training_load_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    """ACWR on the daily workout load; flagged only when it leaves the safe band.

    Assessed on the type-agnostic aggregate and, when a type map produced them,
    on each per-sport series (``workout_trimp_running`` …) — preferring TRIMP
    over kcal per family. A ratio above ``acwr_high`` is a load spike (overload
    risk), below ``acwr_low`` is detraining; inside the band yields no finding
    (mirrors anomalies/recovery — only alerts are stored). A series with fewer
    than ``acwr_min_active_days`` training days in the chronic window is skipped,
    so a rarely-practised sport can't spike the ratio off a single session.
    """
    cfg = cfg or _DEFAULTS
    findings: list[Finding] = []
    for name in _training_load_targets(series):
        s = series[name]
        if _active_days(s, ACWR_CHRONIC_DAYS) < cfg.acwr_min_active_days:
            continue
        acwr = acute_chronic_ratio(s)
        if acwr is None:
            continue
        acute, chronic, ratio = acwr
        if cfg.acwr_low <= ratio <= cfg.acwr_high:
            continue
        note = (
            "training load spike (acute load high vs. chronic)"
            if ratio > cfg.acwr_high
            else "detraining (acute load low vs. chronic)"
        )
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="training_load",
                metric_a=name,
                ref_date=s.dropna().index.max().date(),
                severity=round(ratio, 4),
                note=note,
                details={
                    "acute": round(acute, 4),
                    "chronic": round(chronic, 4),
                    "ratio": round(ratio, 4),
                    "acute_days": ACWR_ACUTE_DAYS,
                    "chronic_days": ACWR_CHRONIC_DAYS,
                },
            )
        )
    return findings


# Zone slug -> note text. The zone is classified on TSB/CTL (scale-free; see
# AnalysisConfig.tsb_*), the note is the English one-liner stored on the finding.
_TSB_ZONE_NOTES = {
    "detraining": "form strongly positive (load well below fitness - base shrinking)",
    "fresh": "fresh / tapered (form positive)",
    "neutral": "neutral (load matches fitness)",
    "productive": "productive training (moderate negative form)",
    "overreaching_risk": "overreaching risk (deeply negative form)",
}


def _tsb_zone(tsb_pct: float, cfg: AnalysisConfig) -> str:
    """Classify the normalised form (TSB/CTL) into its descriptive zone."""
    if tsb_pct >= cfg.tsb_detraining_pct:
        return "detraining"
    if tsb_pct >= cfg.tsb_fresh_pct:
        return "fresh"
    if tsb_pct > -cfg.tsb_fresh_pct:
        return "neutral"
    if tsb_pct > cfg.tsb_overreach_pct:
        return "productive"
    return "overreaching_risk"


def _ctl_trend(ctl: float, ctl_ago: float | None) -> str | None:
    """Direction of the fitness base vs. CTL_TREND_LOOKBACK_DAYS earlier."""
    if ctl_ago is None:
        return None
    if ctl_ago <= 0:
        return "rising" if ctl > 0 else "flat"
    change = ctl / ctl_ago - 1.0
    if change >= CTL_TREND_REL:
        return "rising"
    if change <= -CTL_TREND_REL:
        return "falling"
    return "flat"


def _training_status_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    """One descriptive fitness/form snapshot per run (CTL/ATL/TSB) — a status
    finding, not an alert (docs/workout-analysis.md §5.2).

    Written every run like the consistency findings, so the narration can
    describe the training state ("productive, base rising") even when nothing
    is alert-worthy; the alerting role stays with the ACWR finding above.
    Assessed on the type-agnostic aggregate only (form is systemic, not
    per-sport), preferring TRIMP over the kcal load. Zones are classified on
    TSB normalised by CTL, so they are scale-free on the relative TRIMP
    estimate. Skipped with less than one CTL time constant (42 days) of
    history, where the EWMA is still warm-up dominated.
    """
    cfg = cfg or _DEFAULTS
    name = next((n for n in ("workout_trimp", "workout_load") if n in series), None)
    if name is None:
        return []
    status = training_status(series[name])
    if status is None:
        return []
    zone = _tsb_zone(status.tsb_pct, cfg)
    details: dict[str, object] = {
        "ctl": round(status.ctl, 4),
        "atl": round(status.atl, 4),
        "tsb": round(status.tsb, 4),
        "tsb_pct": round(status.tsb_pct, 4),
        "zone": zone,
        "ctl_days": CTL_DAYS,
        "atl_days": ATL_DAYS,
    }
    trend = _ctl_trend(status.ctl, status.ctl_ago)
    if trend is not None:
        details["ctl_ago"] = round(status.ctl_ago, 4)
        details["ctl_trend"] = trend
        details["ctl_trend_days"] = CTL_TREND_LOOKBACK_DAYS
    return [
        Finding(
            computed_at=computed_at,
            kind="training_status",
            metric_a=name,
            ref_date=series[name].dropna().index.max().date(),
            severity=round(abs(status.tsb_pct), 4),
            note=_TSB_ZONE_NOTES[zone],
            details=details,
        )
    ]


# --- Weekly summaries (descriptive status findings, docs/ARCHITECTURE.md §4.8) ---
# Written every run like training_status/consistency — they give the weekly
# report its descriptive backbone ("how the week actually went") even when no
# alert fired. The narration includes them only in --weekly mode.

_WEEKLY_VITALS_METRICS = ("resting_heart_rate", "heart_rate_variability")
_WEEKLY_ACTIVITY_METRICS = ("step_count", "active_energy", "apple_exercise_time", "time_in_daylight")
_FITNESS_MARKER_METRICS = ("vo2_max", "cardio_recovery", "weight_body_mass")
_MARKER_LONG_GAP_DAYS = 90  # the fitness markers' ~quarter comparison (monthly report)


def series_anchor(series: dict[str, pd.Series]) -> pd.Timestamp | None:
    """The last day holding any data across all series — the shared "current
    week" anchor, so a lagging single metric can't shift its own window while a
    lagging export can't produce an empty week."""
    dates = [s.dropna().index.max() for s in series.values() if not s.dropna().empty]
    return max(dates) if dates else None


def _opt_round(value: float | None, ndigits: int = 2) -> float | None:
    return round(value, ndigits) if value is not None else None


def _volume_details(totals: dict) -> dict:
    """Round a ``weekly_sessions_summary`` totals dict for the finding JSON."""
    return {
        "sessions": totals["sessions"],
        "duration_h": round(totals["duration_h"], 2),
        "distance_km": round(totals["distance_km"], 1),
        "energy_kcal": round(totals["energy_kcal"]),
    }


def _weekly_training_findings(
    db: Session,
    tz: str,
    series: dict[str, pd.Series],
    computed_at: dt.datetime,
    workouts: WorkoutConfig,
    anchor: pd.Timestamp | None = None,
) -> list[Finding]:
    """One descriptive training-week snapshot: session volume plus load totals.

    Session counts/duration/distance/kcal come from the workouts table
    (``weekly_sessions_summary``); the load totals reuse the daily load series
    the pipeline already built, preferring TRIMP over the kcal load — the same
    preference the ACWR/status findings apply. A week without any workout is a
    real zero, not an omission; only a DB without workouts yields no finding.
    """
    sessions = load_workout_frame(db, tz)
    if sessions.empty:
        return []
    types = sessions["name"].map(lambda n: canonical_workout_type(n, workouts.type_map))
    summary = weekly_sessions_summary(sessions, types, anchor=anchor)
    if summary is None:
        return []

    load_name = next((n for n in ("workout_trimp", "workout_load") if n in series), None)
    details: dict = {
        **_volume_details(summary["current"]),
        "prev": _volume_details(summary["previous"]),
        "per_sport": [{"sport": s["sport"], **_volume_details(s)} for s in summary["per_sport"]],
    }
    if load_name is not None:
        ww = weekly_window(series[load_name], agg="sum", anchor=anchor)
        if ww is not None:
            # Outside the series' 0-filled span means "no workouts" — a real 0.
            details["load"] = round(ww.value if ww.value is not None else 0.0, 1)
            details["prev"]["load"] = round(ww.prev_value if ww.prev_value is not None else 0.0, 1)
            details["baseline_load"] = _opt_round(ww.baseline_value, 1)
        for sport_entry in details["per_sport"]:
            sport_series = series.get(f"{load_name}_{sport_entry['sport']}")
            if sport_series is not None:
                sw = weekly_window(sport_series, agg="sum", anchor=anchor)
                if sw is not None and sw.value is not None:
                    sport_entry["load"] = round(sw.value, 1)
    return [
        Finding(
            computed_at=computed_at,
            kind="weekly_training",
            metric_a=load_name or "workout_trimp",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _weekly_sleep_findings(sleep: pd.DataFrame, computed_at: dt.datetime) -> list[Finding]:
    """One descriptive sleep-week snapshot: averages the alert kinds never show."""
    summary = weekly_sleep_summary(sleep)
    if summary is None:
        return []

    def _stats_details(stats: dict | None) -> dict | None:
        if stats is None:
            return None
        return {
            "nights": stats["nights"],
            "avg_total_h": _opt_round(stats["avg_total_h"]),
            "avg_deep_h": _opt_round(stats["avg_deep_h"]),
            "avg_rem_h": _opt_round(stats["avg_rem_h"]),
            "deep_pct": _opt_round(stats["deep_pct"], 1),
            "rem_pct": _opt_round(stats["rem_pct"], 1),
            "avg_efficiency": _opt_round(stats["avg_efficiency"], 3),
            "avg_bedtime": _opt_round(stats["avg_bedtime"]),
        }

    details = _stats_details(summary["current"]) or {}
    details["prev"] = _stats_details(summary["previous"])
    return [
        Finding(
            computed_at=computed_at,
            kind="weekly_sleep",
            metric_a="sleep_total_h",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _weekly_stress_findings(db: Session, computed_at: dt.datetime, cfg: StressConfig | None = None) -> list[Finding]:
    """One descriptive stress-week snapshot off ``stress_daily`` (every day has
    a score there — the alert finding only ever shows the bad days)."""
    cfg = cfg or StressConfig()
    if not cfg.enabled:
        return []
    daily = load_stress_daily(db, since_days=14)  # current + previous week
    summary = weekly_stress_summary(daily)
    if summary is None:
        return []
    cur = summary["current"]
    details: dict = {
        "days": cur["days"],
        "avg_score": round(cur["avg_score"], 1),
        "high_min": cur["high_min"],
        "medium_min": cur["medium_min"],
        "peak_day": cur["peak_day"].isoformat(),
        "peak_score": round(cur["peak_score"], 1),
        "calm_day": cur["calm_day"].isoformat(),
        "calm_score": round(cur["calm_score"], 1),
    }
    prev = summary["previous"]
    details["prev"] = (
        {"days": prev["days"], "avg_score": round(prev["avg_score"], 1), "high_min": prev["high_min"]}
        if prev is not None
        else None
    )
    return [
        Finding(
            computed_at=computed_at,
            kind="weekly_stress",
            metric_a="stress",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _weekly_body_battery_findings(
    db: Session, computed_at: dt.datetime, cfg: BodyBatteryConfig | None = None
) -> list[Finding]:
    """One descriptive Body-Battery week snapshot off ``body_battery_daily``
    (mirrors ``_weekly_stress_findings``)."""
    cfg = cfg or BodyBatteryConfig()
    if not cfg.enabled:
        return []
    daily = load_body_battery_daily(db, since_days=14)  # current + previous week
    summary = weekly_body_battery_summary(daily)
    if summary is None:
        return []
    cur = summary["current"]
    details: dict = {
        "days": cur["days"],
        "avg_wake": _opt_round(cur["avg_wake"], 1),
        "avg_low": _opt_round(cur["avg_low"], 1),
        "avg_high": _opt_round(cur["avg_high"], 1),
        "avg_charged": _opt_round(cur["avg_charged"], 1),
        "avg_drained": _opt_round(cur["avg_drained"], 1),
    }
    if "min_low" in cur:
        details["min_low"] = round(cur["min_low"], 1)
        details["min_low_day"] = cur["min_low_day"].isoformat()
    prev = summary["previous"]
    details["prev"] = (
        {"days": prev["days"], "avg_wake": _opt_round(prev["avg_wake"], 1), "avg_low": _opt_round(prev["avg_low"], 1)}
        if prev is not None
        else None
    )
    return [
        Finding(
            computed_at=computed_at,
            kind="weekly_body_battery",
            metric_a="body_battery",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _weekly_vitals_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
    """Weekly RHR/HRV means against their trailing 28-day baseline — the
    recovery context the alert finding only surfaces when both cross a z
    threshold. One finding per metric so the registry display name applies."""
    findings: list[Finding] = []
    for metric in _WEEKLY_VITALS_METRICS:
        s = series.get(metric)
        if s is None:
            continue
        delta = weekly_baseline_delta(s)
        if delta is None:
            continue
        details = {
            "week_mean": round(delta["week_mean"], 1),
            "baseline_mean": round(delta["baseline_mean"], 1),
            "delta": round(delta["delta"], 1),
            "week_days": delta["week_days"],
            "baseline_days": delta["baseline_days"],
            "unit": METRIC_REGISTRY[metric]["unit_canonical"],
        }
        if "delta_pct" in delta:
            details["delta_pct"] = round(delta["delta_pct"], 1)
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="weekly_vitals",
                metric_a=metric,
                ref_date=delta["window_end"].date(),
                window_start=delta["window_start"].date(),
                window_end=delta["window_end"].date(),
                details=details,
            )
        )
    return findings


def _weekly_activity_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, anchor: pd.Timestamp | None = None
) -> list[Finding]:
    """Weekly totals of the everyday activity metrics (steps, active energy,
    exercise minutes, daylight) with previous-week and 4-week comparisons.
    One finding per metric so the registry display name and unit apply."""
    findings: list[Finding] = []
    for metric in _WEEKLY_ACTIVITY_METRICS:
        s = series.get(metric)
        if s is None:
            continue
        ww = weekly_window(s, agg="sum", anchor=anchor)
        if ww is None or ww.value is None:
            continue
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="weekly_activity",
                metric_a=metric,
                ref_date=ww.window_end.date(),
                window_start=ww.window_start.date(),
                window_end=ww.window_end.date(),
                details={
                    "total": round(ww.value, 1),
                    "daily_avg": round(ww.value / ww.n_days, 1),
                    "days": ww.n_days,
                    "prev_total": _opt_round(ww.prev_value, 1),
                    "baseline_weekly": _opt_round(ww.baseline_value, 1),
                    "unit": METRIC_REGISTRY[metric]["unit_canonical"],
                },
            )
        )
    return findings


def _fitness_marker_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
    """Latest value + ~monthly drift of the slow fitness markers (VO2 Max,
    cardio recovery, body mass). These move too slowly for weekly windows;
    the last reading and its month-over-month change are the story. The
    ``*_90d`` fields add the ~quarter comparison the monthly report narrates
    (shared kind: the same finding serves both report types)."""
    findings: list[Finding] = []
    for metric in _FITNESS_MARKER_METRICS:
        s = series.get(metric)
        if s is None:
            continue
        marker = latest_marker_delta(s, long_gap_days=_MARKER_LONG_GAP_DAYS)
        if marker is None:
            continue
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="fitness_markers",
                metric_a=metric,
                ref_date=marker["latest_date"],
                details={
                    "latest": round(marker["latest"], 2),
                    "latest_date": marker["latest_date"].isoformat(),
                    "prev": _opt_round(marker["prev"]),
                    "prev_date": marker["prev_date"].isoformat() if marker["prev_date"] is not None else None,
                    "delta": _opt_round(marker["delta"]),
                    "prev_90d": _opt_round(marker["prev_long"]),
                    "prev_90d_date": marker["prev_long_date"].isoformat() if marker["prev_long_date"] else None,
                    "delta_90d": _opt_round(marker["delta_long"]),
                    "unit": METRIC_REGISTRY[metric]["unit_canonical"],
                },
            )
        )
    return findings


# --- Monthly summaries (descriptive status findings, docs/ARCHITECTURE.md §4.8) ---
# The monthly analogues of the weekly summaries, for ``narrate --report
# monthly``: rolling 28-day windows (MONTH_PERIOD — four full weeks, so every
# weekday is represented equally) anchored the same way, "previous month" = the
# 28 days before, baseline = mean of the three prior 28-day windows (~ a
# quarter). Each finding also carries a ``weeks`` breakdown (oldest first) so
# the narration can tell the month's trajectory, not just its totals.

_MONTHLY_BASELINE_WINDOWS = 3
_MONTHLY_VITALS_BASELINE_DAYS = 84  # three prior 28-day windows


def _weeks_details(breakdown: list[dict] | None, key: str = "value", ndigits: int = 1) -> list[dict] | None:
    """Serialise a ``week_breakdown`` result for the finding JSON, naming the
    per-week value ``key`` so the details stay self-describing."""
    if breakdown is None:
        return None
    return [
        {
            "start": w["start"].date().isoformat(),
            "end": w["end"].date().isoformat(),
            key: _opt_round(w["value"], ndigits),
        }
        for w in breakdown
    ]


def _merge_weeks(*lists: list[dict] | None) -> list[dict] | None:
    """Zip parallel ``_weeks_details`` lists (same windows) into one entry per week."""
    present = [entries for entries in lists if entries]
    if not present:
        return None
    merged = [dict(entry) for entry in present[0]]
    for entries in present[1:]:
        for target, entry in zip(merged, entries, strict=False):
            target.update({k: v for k, v in entry.items() if k not in ("start", "end")})
    return merged


def _monthly_training_findings(
    db: Session,
    tz: str,
    series: dict[str, pd.Series],
    computed_at: dt.datetime,
    workouts: WorkoutConfig,
    anchor: pd.Timestamp | None = None,
) -> list[Finding]:
    """The training-month snapshot: 28-day session volume and load totals plus
    the per-week trajectory (mirrors ``_weekly_training_findings``)."""
    sessions = load_workout_frame(db, tz)
    if sessions.empty:
        return []
    types = sessions["name"].map(lambda n: canonical_workout_type(n, workouts.type_map))
    summary = weekly_sessions_summary(sessions, types, days=MONTH_PERIOD, anchor=anchor)
    if summary is None:
        return []
    end = summary["window_end"]

    load_name = next((n for n in ("workout_trimp", "workout_load") if n in series), None)
    details: dict = {
        **_volume_details(summary["current"]),
        "prev": _volume_details(summary["previous"]),
        "per_sport": [{"sport": s["sport"], **_volume_details(s)} for s in summary["per_sport"]],
    }
    # Per-week trajectory: session counts off the frame (as a daily count
    # series) and the load sums. A week without workouts is a real zero.
    counts = sessions.groupby("day").size().astype(float)
    sessions_weeks = week_breakdown(counts, agg="sum", anchor=end)
    if sessions_weeks is not None:
        for w in sessions_weeks:
            w["value"] = w["value"] or 0.0
    weeks = _weeks_details(sessions_weeks, key="sessions", ndigits=0)
    if load_name is not None:
        ww = weekly_window(
            series[load_name], agg="sum", days=MONTH_PERIOD, baseline_windows=_MONTHLY_BASELINE_WINDOWS, anchor=anchor
        )
        if ww is not None:
            details["load"] = round(ww.value if ww.value is not None else 0.0, 1)
            details["prev"]["load"] = round(ww.prev_value if ww.prev_value is not None else 0.0, 1)
            details["baseline_load"] = _opt_round(ww.baseline_value, 1)
        load_weeks = week_breakdown(series[load_name], agg="sum", anchor=end)
        if load_weeks is not None:
            for w in load_weeks:
                w["value"] = w["value"] or 0.0
        weeks = _merge_weeks(weeks, _weeks_details(load_weeks, key="load"))
        for sport_entry in details["per_sport"]:
            sport_series = series.get(f"{load_name}_{sport_entry['sport']}")
            if sport_series is not None:
                sw = weekly_window(
                    sport_series,
                    agg="sum",
                    days=MONTH_PERIOD,
                    baseline_windows=_MONTHLY_BASELINE_WINDOWS,
                    anchor=anchor,
                )
                if sw is not None and sw.value is not None:
                    sport_entry["load"] = round(sw.value, 1)
    if weeks is not None:
        details["weeks"] = weeks
    return [
        Finding(
            computed_at=computed_at,
            kind="monthly_training",
            metric_a=load_name or "workout_trimp",
            ref_date=end.date(),
            window_start=summary["window_start"].date(),
            window_end=end.date(),
            details=details,
        )
    ]


def _monthly_sleep_findings(sleep: pd.DataFrame, computed_at: dt.datetime) -> list[Finding]:
    """The sleep-month snapshot: 28-night averages plus the per-week course
    of the nightly mean (mirrors ``_weekly_sleep_findings``)."""
    summary = weekly_sleep_summary(sleep, days=MONTH_PERIOD)
    if summary is None:
        return []

    def _stats_details(stats: dict | None) -> dict | None:
        if stats is None:
            return None
        return {
            "nights": stats["nights"],
            "avg_total_h": _opt_round(stats["avg_total_h"]),
            "avg_deep_h": _opt_round(stats["avg_deep_h"]),
            "avg_rem_h": _opt_round(stats["avg_rem_h"]),
            "deep_pct": _opt_round(stats["deep_pct"], 1),
            "rem_pct": _opt_round(stats["rem_pct"], 1),
            "avg_efficiency": _opt_round(stats["avg_efficiency"], 3),
            "avg_bedtime": _opt_round(stats["avg_bedtime"]),
        }

    details = _stats_details(summary["current"]) or {}
    details["prev"] = _stats_details(summary["previous"])
    weeks = _weeks_details(
        week_breakdown(sleep["total_sleep_h"].dropna(), agg="mean", anchor=summary["window_end"]),
        key="avg_total_h",
        ndigits=2,
    )
    if weeks is not None:
        details["weeks"] = weeks
    return [
        Finding(
            computed_at=computed_at,
            kind="monthly_sleep",
            metric_a="sleep_total_h",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _monthly_stress_findings(db: Session, computed_at: dt.datetime, cfg: StressConfig | None = None) -> list[Finding]:
    """The stress-month snapshot off ``stress_daily`` plus the per-week course
    of the mean score (mirrors ``_weekly_stress_findings``)."""
    cfg = cfg or StressConfig()
    if not cfg.enabled:
        return []
    daily = load_stress_daily(db, since_days=2 * MONTH_PERIOD)  # current + previous month
    summary = weekly_stress_summary(daily, days=MONTH_PERIOD)
    if summary is None:
        return []
    cur = summary["current"]
    details: dict = {
        "days": cur["days"],
        "avg_score": round(cur["avg_score"], 1),
        "high_min": cur["high_min"],
        "medium_min": cur["medium_min"],
        "peak_day": cur["peak_day"].isoformat(),
        "peak_score": round(cur["peak_score"], 1),
        "calm_day": cur["calm_day"].isoformat(),
        "calm_score": round(cur["calm_score"], 1),
    }
    prev = summary["previous"]
    details["prev"] = (
        {"days": prev["days"], "avg_score": round(prev["avg_score"], 1), "high_min": prev["high_min"]}
        if prev is not None
        else None
    )
    weeks = _weeks_details(
        week_breakdown(daily["score"].dropna(), agg="mean", anchor=summary["window_end"]), key="avg_score"
    )
    if weeks is not None:
        details["weeks"] = weeks
    return [
        Finding(
            computed_at=computed_at,
            kind="monthly_stress",
            metric_a="stress",
            ref_date=summary["window_end"].date(),
            window_start=summary["window_start"].date(),
            window_end=summary["window_end"].date(),
            details=details,
        )
    ]


def _monthly_body_battery_findings(
    db: Session, computed_at: dt.datetime, cfg: BodyBatteryConfig | None = None
) -> list[Finding]:
    """The Body-Battery month snapshot off ``body_battery_daily`` plus the
    per-week wake/low course (mirrors ``_weekly_body_battery_findings``)."""
    cfg = cfg or BodyBatteryConfig()
    if not cfg.enabled:
        return []
    daily = load_body_battery_daily(db, since_days=2 * MONTH_PERIOD)  # current + previous month
    summary = weekly_body_battery_summary(daily, days=MONTH_PERIOD)
    if summary is None:
        return []
    cur = summary["current"]
    details: dict = {
        "days": cur["days"],
        "avg_wake": _opt_round(cur["avg_wake"], 1),
        "avg_low": _opt_round(cur["avg_low"], 1),
        "avg_high": _opt_round(cur["avg_high"], 1),
        "avg_charged": _opt_round(cur["avg_charged"], 1),
        "avg_drained": _opt_round(cur["avg_drained"], 1),
    }
    if "min_low" in cur:
        details["min_low"] = round(cur["min_low"], 1)
        details["min_low_day"] = cur["min_low_day"].isoformat()
    prev = summary["previous"]
    details["prev"] = (
        {"days": prev["days"], "avg_wake": _opt_round(prev["avg_wake"], 1), "avg_low": _opt_round(prev["avg_low"], 1)}
        if prev is not None
        else None
    )
    end = summary["window_end"]
    weeks = _merge_weeks(
        _weeks_details(week_breakdown(daily["wake_level"].dropna(), agg="mean", anchor=end), key="avg_wake"),
        _weeks_details(week_breakdown(daily["low_level"].dropna(), agg="mean", anchor=end), key="avg_low"),
    )
    if weeks is not None:
        details["weeks"] = weeks
    return [
        Finding(
            computed_at=computed_at,
            kind="monthly_body_battery",
            metric_a="body_battery",
            ref_date=end.date(),
            window_start=summary["window_start"].date(),
            window_end=end.date(),
            details=details,
        )
    ]


def _monthly_vitals_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
    """Monthly RHR/HRV means against their trailing 84-day baseline, with the
    per-week course (mirrors ``_weekly_vitals_findings``)."""
    findings: list[Finding] = []
    for metric in _WEEKLY_VITALS_METRICS:
        s = series.get(metric)
        if s is None:
            continue
        delta = weekly_baseline_delta(
            s,
            days=MONTH_PERIOD,
            baseline_days=_MONTHLY_VITALS_BASELINE_DAYS,
            min_week_days=10,
            min_baseline_days=21,
        )
        if delta is None:
            continue
        details = {
            "month_mean": round(delta["week_mean"], 1),
            "baseline_mean": round(delta["baseline_mean"], 1),
            "delta": round(delta["delta"], 1),
            "month_days": delta["week_days"],
            "baseline_days": delta["baseline_days"],
            "unit": METRIC_REGISTRY[metric]["unit_canonical"],
        }
        if "delta_pct" in delta:
            details["delta_pct"] = round(delta["delta_pct"], 1)
        weeks = _weeks_details(week_breakdown(s, agg="mean", anchor=delta["window_end"]), key="mean")
        if weeks is not None:
            details["weeks"] = weeks
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="monthly_vitals",
                metric_a=metric,
                ref_date=delta["window_end"].date(),
                window_start=delta["window_start"].date(),
                window_end=delta["window_end"].date(),
                details=details,
            )
        )
    return findings


def _monthly_activity_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, anchor: pd.Timestamp | None = None
) -> list[Finding]:
    """Monthly totals of the everyday activity metrics with previous-month and
    3-month comparisons plus the per-week sums (mirrors ``_weekly_activity_findings``)."""
    findings: list[Finding] = []
    for metric in _WEEKLY_ACTIVITY_METRICS:
        s = series.get(metric)
        if s is None:
            continue
        ww = weekly_window(s, agg="sum", days=MONTH_PERIOD, baseline_windows=_MONTHLY_BASELINE_WINDOWS, anchor=anchor)
        if ww is None or ww.value is None:
            continue
        details = {
            "total": round(ww.value, 1),
            "daily_avg": round(ww.value / ww.n_days, 1),
            "days": ww.n_days,
            "prev_total": _opt_round(ww.prev_value, 1),
            "baseline_monthly": _opt_round(ww.baseline_value, 1),
            "unit": METRIC_REGISTRY[metric]["unit_canonical"],
        }
        weeks = _weeks_details(week_breakdown(s, agg="sum", anchor=ww.window_end), key="total")
        if weeks is not None:
            details["weeks"] = weeks
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="monthly_activity",
                metric_a=metric,
                ref_date=ww.window_end.date(),
                window_start=ww.window_start.date(),
                window_end=ww.window_end.date(),
                details=details,
            )
        )
    return findings
