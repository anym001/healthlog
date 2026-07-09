"""Finding builders and series assembly.

Builds the analysis series (core metrics + derived sleep + workout-load) and the
finding kinds (correlation, anomaly, trend, seasonality, recovery_alert,
consistency, training_load, training_status) on top of the pure helpers and DB
loaders.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, fields

import pandas as pd
from sqlalchemy.orm import Session

from ..appconfig import AnalysisConfig, ProfileConfig, WorkoutConfig
from ..models import Finding
from ..registry import METRIC_REGISTRY
from ..workout_types import canonical_workout_type
from .constants import (
    _DEFAULT_APP_CONFIG,
    _DEFAULTS,
    ACWR_ACUTE_DAYS,
    ACWR_CHRONIC_DAYS,
    ATL_DAYS,
    CTL_DAYS,
    CTL_TREND_LOOKBACK_DAYS,
    CTL_TREND_REL,
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
    resolve_hr_max,
    resolve_hr_rest,
    robust_z,
    rolling_mad_anomalies,
    spearman_lag,
    training_status,
    trend_monotonicity,
    trend_slope,
)


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
