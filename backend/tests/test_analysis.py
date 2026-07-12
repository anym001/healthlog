"""Phase 3 analysis pipeline: pure math on synthetic series + DB end-to-end."""

from __future__ import annotations

import datetime as dt
import uuid

import numpy as np
import pandas as pd
from sqlalchemy import func, select, text

from app import analysis
from app.analysis import (
    AnalysisResult,
    _hr_zone_weight,
    acute_chronic_ratio,
    aggregate_workout_daily,
    annual_seasonality,
    auto_neutral,
    banister_trimp,
    body_battery_timeline,
    circular_bedtime_offset,
    decompose,
    edwards_trimp,
    ewma,
    fdr_adjust,
    fill_zero_within_span,
    resolve_hr_max,
    resolve_hr_rest,
    rolling_mad_anomalies,
    spearman_lag,
    stress_intraday_from_hr,
    stress_state,
    summarize_body_battery_day,
    summarize_stress_day,
    training_status,
    trend_slope,
)
from app.analysis.body_battery import _resolve_neutral, compute_body_battery
from app.analysis.constants import BODY_BATTERY_NEUTRAL
from app.analysis.refresh import run_refresh
from app.analysis.stress import compute_stress, hr_window_bounds
from app.appconfig import AnalysisConfig, AppConfig, BodyBatteryConfig, ProfileConfig, StressConfig, WorkoutConfig
from app.models import (
    BodyBatteryDaily,
    BodyBatteryIntraday,
    Finding,
    FindingHistory,
    MetricSample,
    SleepSession,
    StressDaily,
    StressIntraday,
    Workout,
    WorkoutHrSample,
    WorkoutLoadDaily,
)
from app.workout_types import canonical_workout_type

UTC = dt.UTC


def _daily(values, start="2026-01-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(np.asarray(values, dtype="float64"), index=idx)


# --- Spearman lag correlation ---------------------------------------------


def test_spearman_lag_detects_known_lag():
    rng = np.random.default_rng(0)
    a = _daily(rng.normal(size=150))
    # b[t] = a[t-2] + small noise  =>  a leads b by 2 days.
    b = a.shift(2) + _daily(rng.normal(scale=0.1, size=150))
    at_lag2 = spearman_lag(a, b, 2)
    at_lag0 = spearman_lag(a, b, 0)
    assert at_lag2 is not None and at_lag2.coef > 0.8
    assert at_lag2.p < 1e-6
    assert abs(at_lag0.coef) < at_lag2.coef


def test_spearman_lag_independent_series_not_strong():
    rng = np.random.default_rng(1)
    a = _daily(rng.normal(size=150))
    b = _daily(rng.normal(size=150))
    res = spearman_lag(a, b, 0)
    assert res is not None
    assert abs(res.coef) < 0.3


def test_spearman_lag_too_short_returns_none():
    rng = np.random.default_rng(2)
    a = _daily(rng.normal(size=30))
    b = _daily(rng.normal(size=30))
    assert spearman_lag(a, b, 0) is None  # below MIN_OVERLAP


# --- FDR --------------------------------------------------------------------


def test_fdr_adjust_ge_raw_and_preserves_length():
    p = [0.001, 0.01, 0.04, 0.2, 0.5]
    adj = fdr_adjust(p)
    assert len(adj) == len(p)
    assert all(a >= raw - 1e-12 for a, raw in zip(adj, p, strict=True))


def test_fdr_adjust_empty():
    assert fdr_adjust([]) == []


# --- Anomalies --------------------------------------------------------------


def test_rolling_mad_anomalies_flags_spike():
    rng = np.random.default_rng(3)
    vals = 50 + rng.normal(scale=2.0, size=60)
    vals[55] = 90.0  # clear spike
    s = _daily(vals)
    anomalies = rolling_mad_anomalies(s)
    assert s.index[55] in anomalies.index
    assert anomalies.loc[s.index[55], "z"] > analysis.ANOMALY_THRESHOLD


def test_rolling_mad_anomalies_clean_series_is_empty():
    rng = np.random.default_rng(4)
    s = _daily(50 + rng.normal(scale=1.0, size=60))
    assert rolling_mad_anomalies(s).empty


def test_global_robust_z_scales_to_full_history():
    s = _daily(np.arange(101, dtype=float))  # 0..100: median 50, MAD 25
    assert abs(analysis._global_robust_z(s, 100.0) - 0.6745 * 50 / 25) < 1e-9
    assert analysis._global_robust_z(_daily(np.full(40, 7.0)), 7.0) is None  # no scale


def test_anomaly_global_guard_drops_window_only_spike():
    # Wide global spread (alternating 0/30) makes 30 a normal recurring high; a
    # calm recent window makes its trailing z explode. The window flags the last
    # day, but it is unremarkable vs the full history -> the guard drops it.
    vals = np.concatenate([np.tile([0.0, 30.0], 60), np.tile([15.0, 15.2], 14), [30.0]])
    s = _daily(vals)
    assert abs(analysis.robust_z(s).iloc[-1]) > analysis.ANOMALY_THRESHOLD  # window flags it
    assert abs(analysis._global_robust_z(s, 30.0)) < 2.5  # but normal vs history
    assert analysis._anomaly_findings({"hr": s}, dt.datetime.now(UTC)) == []
    off = analysis._anomaly_findings({"hr": s}, dt.datetime.now(UTC), AnalysisConfig(anomaly_min_global_z=0.0))
    assert len(off) == 1  # guard off lets the window-only spike through


def test_anomaly_keeps_genuine_extreme():
    # A stable series with an unprecedented final value: extreme in both views.
    s = _daily(np.concatenate([np.tile([10.0, 10.5], 70), [40.0]]))
    out = analysis._anomaly_findings({"hr": s}, dt.datetime.now(UTC))
    assert len(out) == 1 and out[0].metric_a == "hr"
    assert "global_z" in out[0].details


def test_anomaly_dedupes_workout_family_same_day():
    # One hard session surfaces in several co-derived workout-load series; they
    # collapse to a single anomaly for that day. A non-workout anomaly on the
    # same day is independent and kept.
    def spike(peak):
        return _daily(np.concatenate([np.tile([10.0, 10.5], 70), [peak]]))

    series = {"workout_trimp": spike(40.0), "workout_edwards": spike(45.0), "hr": spike(40.0)}
    out = analysis._anomaly_findings(series, dt.datetime.now(UTC))
    assert sorted(f.metric_a for f in out) == ["hr", "workout_edwards"]  # family -> strongest only


# --- Trend / seasonality ----------------------------------------------------


def test_decompose_detects_upward_trend():
    rng = np.random.default_rng(5)
    t = np.arange(150)
    vals = 100 + 0.5 * t + 2 * np.sin(2 * np.pi * t / 7) + rng.normal(scale=1.0, size=150)
    decomp = decompose(_daily(vals))
    assert decomp is not None
    assert trend_slope(decomp.trend) > 0.3
    assert analysis._component_strength(decomp.trend, decomp.resid) > analysis.TREND_STRENGTH_MIN


def test_decompose_flat_series_has_weak_trend():
    rng = np.random.default_rng(6)
    decomp = decompose(_daily(100 + rng.normal(scale=1.0, size=150)))
    assert decomp is not None
    assert abs(trend_slope(decomp.trend)) < 0.05


def _trend_decomp(trend: pd.Series) -> analysis.Decomp:
    """A Decomp carrying only a (zero-residual) trend, so strength is 1.0."""
    zeros = pd.Series(np.zeros(len(trend)), index=trend.index)
    return analysis.Decomp(trend=trend, resid=zeros, seasonal={}, has_annual=False)


def test_trend_monotonicity_high_for_ramp_low_for_meander():
    n = 200
    assert analysis.trend_monotonicity(_daily(np.linspace(0.0, 10.0, n))) > 0.99
    assert analysis.trend_monotonicity(_daily(5 * np.cos(np.linspace(0, 2 * np.pi, n)))) < 0.3
    assert analysis.trend_monotonicity(_daily(np.full(n, 3.0))) is None  # constant: no direction


def test_trend_drops_smooth_meander():
    # Both have a strong (smooth, zero-residual) trend, but only the ramp goes
    # somewhere; the meander wanders up then back -> not a trend.
    n = 200
    ramp = _daily(np.linspace(0.0, 10.0, n))
    meander = _daily(5 * np.cos(np.linspace(0, 2 * np.pi, n)))
    series = {"ramp": ramp, "meander": meander}
    decomps = {"ramp": _trend_decomp(ramp), "meander": _trend_decomp(meander)}

    trends, _ = analysis._trend_and_seasonality_findings(series, dt.datetime.now(UTC), decomps=decomps)
    assert {f.metric_a for f in trends} == {"ramp"}  # meander dropped

    # Disabling the guard lets both through -> proof the guard is the discriminator.
    cfg = AnalysisConfig(trend_min_monotonicity=0.0)
    both, _ = analysis._trend_and_seasonality_findings(series, dt.datetime.now(UTC), cfg, decomps=decomps)
    assert {f.metric_a for f in both} == {"ramp", "meander"}


def test_annual_seasonality_detected_over_two_years():
    rng = np.random.default_rng(7)
    t = np.arange(800)
    vals = 100 + 10 * np.sin(2 * np.pi * t / 365) + 2 * np.sin(2 * np.pi * t / 7) + rng.normal(scale=1.0, size=800)
    decomp = decompose(_daily(vals))
    assert decomp is not None and decomp.has_annual
    annual = annual_seasonality(decomp)
    assert annual is not None
    assert annual["strength"] >= analysis.SEASONALITY_STRENGTH_MIN
    assert annual["peak_month"] != annual["trough_month"]
    # A clean sine peaks and troughs ~6 months apart -> phase is trustworthy.
    assert annual["phase_confident"] is True


def _annual_decomp(month_level: dict[int, float]) -> analysis.Decomp:
    """A synthetic Decomp whose annual component takes a fixed value per month."""
    idx = pd.date_range("2024-01-01", periods=730, freq="D")
    seasonal = pd.Series([month_level.get(d.month, 0.0) for d in idx], index=idx, dtype="float64")
    resid = pd.Series(np.zeros(len(idx)), index=idx)
    return analysis.Decomp(trend=resid, resid=resid, seasonal={analysis.SEASONAL_PERIOD: seasonal}, has_annual=True)


def test_seasonality_phase_flagged_uncertain_when_peak_trough_adjacent():
    annual = annual_seasonality(_annual_decomp({5: 1.0, 6: -1.0}))  # May peak, Jun trough
    assert annual["peak_month"] == 5 and annual["trough_month"] == 6
    assert annual["phase_confident"] is False


def test_seasonality_phase_confident_when_peak_trough_far_apart():
    annual = annual_seasonality(_annual_decomp({1: 1.0, 7: -1.0}))  # Jan peak, Jul trough
    assert annual["phase_confident"] is True


def _multiyear_seasonal(fn) -> pd.Series:
    """A three-year daily seasonal component, value per day from ``fn(timestamp)``."""
    idx = pd.date_range("2022-01-01", periods=365 * 3, freq="D")
    return pd.Series([fn(ts) for ts in idx], index=idx, dtype="float64")


def _seasonal_decomp(season: pd.Series) -> analysis.Decomp:
    """A Decomp carrying only an annual component (zero trend/resid => strength 1)."""
    zeros = pd.Series(np.zeros(len(season)), index=season.index)
    return analysis.Decomp(trend=zeros, resid=zeros, seasonal={analysis.SEASONAL_PERIOD: season}, has_annual=True)


def test_seasonal_reproducibility_high_for_recurring_cycle():
    # The same month-by-month shape every year -> the annual cycle recurs.
    season = _multiyear_seasonal(lambda ts: np.sin(2 * np.pi * ts.dayofyear / 365))
    assert analysis._seasonal_reproducibility(season) > 0.8


def test_seasonal_reproducibility_low_for_wandering_shape():
    # A cycle whose phase shifts every year: MSTL fits "seasonal" strength, but
    # the shape does not recur -> low/negative reproducibility (the artefact).
    shift = {2022: 0, 2023: 120, 2024: 240}
    season = _multiyear_seasonal(lambda ts: np.sin(2 * np.pi * (ts.dayofyear + shift[ts.year]) / 365))
    assert analysis._seasonal_reproducibility(season) < 0.3


def test_seasonal_reproducibility_none_with_single_year():
    season = _multiyear_seasonal(lambda ts: np.sin(2 * np.pi * ts.dayofyear / 365)).iloc[:300]
    assert analysis._seasonal_reproducibility(season) is None


def test_seasonality_drops_non_reproducible_artefact():
    # Two series with equally strong annual components: one recurs year over year,
    # the other wanders. Both clear the strength floor; only the reproducible one
    # is a trustworthy seasonality finding.
    shift = {2022: 0, 2023: 120, 2024: 240}
    recurs = _multiyear_seasonal(lambda ts: np.sin(2 * np.pi * ts.dayofyear / 365))
    wanders = _multiyear_seasonal(lambda ts: np.sin(2 * np.pi * (ts.dayofyear + shift[ts.year]) / 365))
    series = {"recurs": recurs, "wanders": wanders}  # values unused; decomps drive seasonality
    decomps = {"recurs": _seasonal_decomp(recurs), "wanders": _seasonal_decomp(wanders)}

    _, seasons = analysis._trend_and_seasonality_findings(series, dt.datetime.now(UTC), decomps=decomps)
    assert {f.metric_a for f in seasons} == {"recurs"}  # wandering shape dropped

    # Disabling the guard lets both through -> proof the guard is the discriminator.
    cfg = AnalysisConfig(seasonality_reproducibility_min=0.0)
    _, both = analysis._trend_and_seasonality_findings(series, dt.datetime.now(UTC), cfg, decomps=decomps)
    assert {f.metric_a for f in both} == {"recurs", "wanders"}


# --- Correlation de-trending + de-duplication -------------------------------


def test_correlation_collapses_spurious_trend_only_pairs():
    # Two independent noise series with opposite linear trends correlate near
    # -1 on raw levels (pure trend artefact). De-trending must collapse that to
    # a weak residual: the shared drift is gone, the noise is independent.
    rng = np.random.default_rng(20)
    n = 400
    t = np.arange(n)
    up = _daily(0.5 * t + rng.normal(scale=1.0, size=n))
    down = _daily(-0.5 * t + rng.normal(scale=1.0, size=n))

    assert abs(spearman_lag(up, down, 0).coef) > 0.9  # raw: pure trend artefact
    findings = analysis._correlation_findings({"up": up, "down": down}, dt.datetime.now(UTC))
    assert all(abs(float(f.coefficient)) < 0.3 for f in findings)  # collapsed


def test_correlation_drops_shared_seasonality_pairs():
    # Two metrics that share a strong weekly rhythm but have independent
    # day-to-day residuals correlate strongly when only the trend is removed
    # (seasonality leaks through) — the old basis. That link is an artefact: on
    # the residual basis (trend AND seasonal removed) it collapses, so the engine
    # must report nothing.
    rng = np.random.default_rng(77)
    n = 400
    t = np.arange(n)
    weekly = 5.0 * np.sin(2 * np.pi * t / 7)  # shared weekly pattern, no trend
    a = _daily(weekly + rng.normal(scale=1.0, size=n))
    b = _daily(weekly + rng.normal(scale=1.0, size=n))

    # De-trended (seasonality retained) the two co-move strongly...
    da = a - analysis.decompose(a).trend.reindex(a.index)
    db = b - analysis.decompose(b).trend.reindex(b.index)
    assert abs(spearman_lag(da, db, 0).coef) > 0.5  # spurious shared-seasonality link

    # ...but on the residual basis the shared rhythm is gone and nothing survives.
    findings = analysis._correlation_findings({"a": a, "b": b}, dt.datetime.now(UTC))
    assert findings == []


def test_correlation_raw_corroboration_drops_residual_only_artefacts():
    # The mirror artefact: a weak shared day-to-day base is swamped at the raw
    # level by large, *orthogonal* weekly seasonals, so the raw series barely
    # correlate — but de-seasonalising recovers the base and the residual looks
    # strong. That is a residual-only artefact (what a sparse/derived metric's
    # decomposition noise produces); the raw-corroboration guard must drop it.
    rng = np.random.default_rng(101)
    n = 400
    t = np.arange(n)
    base = np.zeros(n)
    for k in range(1, n):
        base[k] = 0.8 * base[k - 1] + rng.normal()  # shared AR(1) day-to-day signal
    a = _daily(base + 8 * np.sin(2 * np.pi * t / 7) + rng.normal(scale=0.3, size=n))
    b = _daily(base + 8 * np.cos(2 * np.pi * t / 7) + rng.normal(scale=0.3, size=n))

    # The residual correlation is strong, but the raw one is ~0 (uncorroborated).
    assert spearman_lag(a, b, 0).coef < 0.2  # raw: not visible
    ra = analysis._residual_series(a, analysis.decompose(a))
    rb = analysis._residual_series(b, analysis.decompose(b))
    assert spearman_lag(ra, rb, 0).coef > 0.5  # residual: looks strong

    # With the guard (default) the pair is rejected; disabling it lets the
    # residual-only artefact through — proof that the guard is what dropped it.
    assert analysis._correlation_findings({"a": a, "b": b}, dt.datetime.now(UTC)) == []
    kept = analysis._correlation_findings({"a": a, "b": b}, dt.datetime.now(UTC), AnalysisConfig(corr_raw_min_abs=0.0))
    assert len(kept) == 1 and abs(float(kept[0].coefficient)) > 0.5


def test_correlation_keeps_one_finding_per_pair():
    # An autocorrelated common signal makes several lags significant, but the
    # output must collapse to a single best row for the {a, b} pair.
    rng = np.random.default_rng(21)
    n = 300
    base = np.zeros(n)
    for k in range(1, n):
        base[k] = 0.8 * base[k - 1] + rng.normal()  # stationary AR(1): no trend
    a = _daily(base + rng.normal(scale=0.3, size=n))
    b = _daily(base + rng.normal(scale=0.3, size=n))
    findings = analysis._correlation_findings({"a": a, "b": b}, dt.datetime.now(UTC))
    assert len(findings) == 1
    assert {findings[0].metric_a, findings[0].metric_b} == {"a", "b"}


def test_correlation_finding_stamps_comparison_coefs():
    # The reported coefficient is the residual (trend + seasonal removed) Spearman.
    # Each finding also records two comparison coefficients at the same lag for
    # transparency: raw_coef (nothing removed) and detr_coef (only trend removed).
    # With a trend- and seasonality-free common signal all three agree closely.
    rng = np.random.default_rng(60)
    n = 300
    base = np.zeros(n)
    for k in range(1, n):
        base[k] = 0.8 * base[k - 1] + rng.normal()  # stationary AR(1): no trend
    a = _daily(base + rng.normal(scale=0.3, size=n))
    b = _daily(base + rng.normal(scale=0.3, size=n))
    findings = analysis._correlation_findings({"a": a, "b": b}, dt.datetime.now(UTC))
    assert len(findings) == 1
    coef = float(findings[0].coefficient)  # residual basis
    assert coef > 0.4
    raw = findings[0].details["raw_coef"]
    assert raw is not None and raw > 0.5
    assert abs(raw - coef) < 0.2  # trend/seasonality-free: raw ~ residual
    detr = findings[0].details["detr_coef"]
    assert detr is not None and abs(detr - coef) < 0.2  # likewise detr ~ residual


# --- Activity-volume suppression --------------------------------------------


def test_is_workout_load_family():
    for name in (
        "workout_trimp",
        "workout_load",
        "workout_edwards",
        "workout_duration",
        "workout_count",
        "workout_intensity",
        "workout_load_stair_stepper",
        "workout_edwards_yoga",
    ):
        assert analysis._is_workout_load_family(name), name
    for name in ("workout_pace", "resting_heart_rate", "step_count", "sleep_total_h"):
        assert not analysis._is_workout_load_family(name), name


def test_is_activity_volume():
    # Workout load-family and Apple activity-ring metrics are both activity volume.
    for name in (
        "workout_trimp",
        "workout_load_yoga",
        "workout_intensity",
        "apple_exercise_time",
        "apple_stand_time",
        "active_energy",
        "step_count",
        "walking_running_distance",
        "flights_climbed",
    ):
        assert analysis._is_activity_volume(name), name
    # Body-state metrics are not activity volume.
    for name in ("resting_heart_rate", "sleep_total_h", "respiratory_rate", "heart_rate"):
        assert not analysis._is_activity_volume(name), name


def test_is_redundant_activity_pair():
    # Both activity-volume -> movement/training composition, not a health insight.
    assert analysis._is_redundant_activity_pair("workout_trimp", "workout_load")  # load cross-measure
    assert analysis._is_redundant_activity_pair("workout_load", "workout_duration")  # load vs duration
    assert analysis._is_redundant_activity_pair("workout_trimp", "workout_trimp_running")  # aggregate vs child
    assert analysis._is_redundant_activity_pair("workout_load_yoga", "workout_load_stair_stepper")  # sport vs sport
    assert analysis._is_redundant_activity_pair("step_count", "walking_running_distance")  # ring vs ring
    assert analysis._is_redundant_activity_pair("active_energy", "apple_stand_time")  # ring vs ring
    assert analysis._is_redundant_activity_pair("apple_exercise_time", "workout_duration")  # ring vs load
    assert analysis._is_redundant_activity_pair("active_energy", "workout_load")  # ring vs load
    assert analysis._is_redundant_activity_pair("workout_load", "workout_intensity")  # intensity vs load
    assert analysis._is_redundant_activity_pair("workout_intensity", "active_energy")  # intensity vs ring
    # Kept: an activity-volume series vs any body-state metric (the useful pairs).
    assert not analysis._is_redundant_activity_pair("workout_load_running", "resting_heart_rate")
    assert not analysis._is_redundant_activity_pair("step_count", "resting_heart_rate")
    assert not analysis._is_redundant_activity_pair("active_energy", "sleep_total_h")
    assert not analysis._is_redundant_activity_pair("workout_intensity", "sleep_total_h")  # the kept gem


def test_correlation_finding_stamps_priority_tier():
    # The finding carries the layer-2 priority tier so narration/Grafana can rank
    # by the same rule: cross-subsystem -> 2, sleep-architecture self -> 0.
    rng = np.random.default_rng(7)
    base = rng.normal(size=80)
    idx = pd.date_range("2026-01-01", periods=80, freq="D")
    cross = analysis._correlation_findings(
        {
            "sleep_total_h": pd.Series(base, index=idx),
            "respiratory_rate": pd.Series(-0.8 * base + rng.normal(scale=0.3, size=80), index=idx),
        },
        dt.datetime.now(UTC),
    )
    assert cross and cross[0].details["priority_tier"] == 2
    within = analysis._correlation_findings(
        {
            "sleep_total_h": pd.Series(base, index=idx),
            "sleep_rem_h": pd.Series(0.7 * base + rng.normal(scale=0.3, size=80), index=idx),
        },
        dt.datetime.now(UTC),
    )
    assert within and within[0].details["priority_tier"] == 0


def test_correlation_suppresses_same_target_cross_measure():
    # trimp and edwards of the same target move together by construction; even a
    # perfect relationship must not be emitted as a "finding".
    rng = np.random.default_rng(33)
    base = _daily(rng.normal(size=120))
    series = {"workout_trimp_yoga": base, "workout_edwards_yoga": base * 1.4 + 0.01}
    assert analysis._correlation_findings(series, dt.datetime.now(UTC)) == []


def test_correlation_suppresses_activity_ring_vs_load():
    # Apple's exercise minutes track workout duration almost perfectly; that is
    # the same activity logged two ways, so no finding must be emitted.
    rng = np.random.default_rng(34)
    base = _daily(rng.normal(size=120))
    series = {"apple_exercise_time": base, "workout_duration": base * 1.1 + 0.01}
    assert analysis._correlation_findings(series, dt.datetime.now(UTC)) == []


# --- Sparse-series guard (min_active) ---------------------------------------


def test_spearman_lag_min_active_filters_mostly_zero_series():
    # Two 0-filled series with only a few coincidental active days: enough grid
    # overlap to clear min_overlap, but far too few non-zero days to trust.
    rng = np.random.default_rng(44)
    n = 120
    a = np.zeros(n)
    b = np.zeros(n)
    for k in (10, 40, 70, 100):  # 4 coincidental active days
        a[k] = rng.uniform(1, 5)
        b[k + 1] = rng.uniform(1, 5)
    sa, sb = _daily(a), _daily(b)
    # Without the guard a result is produced; with it (min_active=10) it is None.
    assert spearman_lag(sa, sb, 1, min_overlap=42) is not None
    assert spearman_lag(sa, sb, 1, min_overlap=42, min_active=10) is None


def test_spearman_lag_min_active_keeps_dense_series():
    # A continuous (never-zero) pair is unaffected by the active-day guard.
    rng = np.random.default_rng(45)
    base = rng.normal(size=120)
    a = _daily(base + 100 + rng.normal(scale=0.1, size=120))
    b = _daily(base + 100 + rng.normal(scale=0.1, size=120))
    res = spearman_lag(a, b, 0, min_overlap=42, min_active=10)
    assert res is not None and res.coef > 0.8


# --- Effect-size floor (corr_min_abs) ---------------------------------------


def test_correlation_effect_size_floor_drops_weak_pairs():
    # A weak but, over a long series, "significant" correlation: ~0.15. The
    # default floor (0.25) must drop it; disabling the floor must keep it.
    rng = np.random.default_rng(55)
    n = 600
    base = rng.normal(size=n)
    a = _daily(base)
    b = _daily(0.16 * base + rng.normal(size=n))  # weak shared component
    raw = spearman_lag(a, b, 0)
    assert raw is not None and 0.05 < abs(raw.coef) < 0.30  # weak-but-present

    series = {"a": a, "b": b}
    assert analysis._correlation_findings(series, dt.datetime.now(UTC)) == []  # default floor 0.25
    kept = analysis._correlation_findings(series, dt.datetime.now(UTC), AnalysisConfig(corr_min_abs=0.0))
    assert len(kept) == 1 and abs(float(kept[0].coefficient)) < 0.30


# --- Bedtime offset (circular) ---------------------------------------------


def test_circular_bedtime_offset_no_midnight_wrap():
    s = circular_bedtime_offset(_daily([22.0, 0.5, 2.0, 23.0]))
    assert list(s.to_numpy()) == [4.0, 6.5, 8.0, 5.0]
    # 23:00 and 01:00 stay close (5 vs 7), no 22-hour jump.
    pair = circular_bedtime_offset(_daily([23.0, 1.0]))
    assert abs(pair.iloc[0] - pair.iloc[1]) == 2.0


# --- Recovery alert (composite) --------------------------------------------


def test_recovery_alert_fires_on_low_hrv_high_rhr():
    rng = np.random.default_rng(8)
    n = 40
    rhr = 50 + rng.normal(scale=2.0, size=n)
    hrv = 60 + rng.normal(scale=3.0, size=n)
    rhr[-1] = 72  # spike up (bad)
    hrv[-1] = 35  # drop down (bad)
    series = {"resting_heart_rate": _daily(rhr), "heart_rate_variability": _daily(hrv)}

    findings = analysis._recovery_findings(series, dt.datetime.now(UTC))
    assert len(findings) == 1
    assert findings[0].kind == "recovery_alert"
    assert findings[0].ref_date == _daily(rhr).index[-1].date()


def test_recovery_alert_silent_when_only_one_signal():
    rng = np.random.default_rng(9)
    n = 40
    rhr = 50 + rng.normal(scale=2.0, size=n)
    hrv = 60 + rng.normal(scale=3.0, size=n)
    rhr[-1] = 72  # only resting HR up; HRV normal
    series = {"resting_heart_rate": _daily(rhr), "heart_rate_variability": _daily(hrv)}
    assert analysis._recovery_findings(series, dt.datetime.now(UTC)) == []


# --- DB end-to-end ----------------------------------------------------------


def _add_metric(db, metric, values, start_date=dt.date(2026, 1, 1)):
    for i, v in enumerate(values):
        day = start_date + dt.timedelta(days=i)
        db.add(
            MetricSample(
                time=dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
                metric=metric,
                source="",
                qty=float(v),
            )
        )


def test_run_writes_correlation_findings_as_snapshot(db):
    rng = np.random.default_rng(10)
    steps = rng.integers(3000, 12000, size=60).astype(float)
    # Resting HR falls as daily activity rises: a strong *cross-domain* pair
    # (activity volume vs body state) that the activity-volume suppression keeps.
    rhr = 80.0 - 0.002 * steps + rng.normal(scale=1.0, size=60)
    _add_metric(db, "step_count", steps)
    _add_metric(db, "resting_heart_rate", rhr)
    db.flush()

    result = analysis.run(db)
    assert isinstance(result, AnalysisResult)
    assert result.correlations >= 1

    pair = (
        db.execute(
            select(Finding).where(
                Finding.kind == "correlation",
                Finding.metric_a.in_(["step_count", "resting_heart_rate"]),
                Finding.metric_b.in_(["step_count", "resting_heart_rate"]),
            )
        )
        .scalars()
        .all()
    )
    assert pair, "expected a step_count<->resting_heart_rate correlation"
    assert pair[0].p_value_adj is not None

    # Snapshot: a second run replaces, not accumulates.
    count_after_first = db.execute(select(func.count()).select_from(Finding)).scalar_one()
    analysis.run(db)
    count_after_second = db.execute(select(func.count()).select_from(Finding)).scalar_one()
    assert count_after_second == count_after_first

    # History: every run appends its full snapshot; computed_at is the run key.
    history_count = db.execute(select(func.count()).select_from(FindingHistory)).scalar_one()
    assert history_count == count_after_first + count_after_second
    distinct_runs = db.execute(select(func.count(func.distinct(FindingHistory.computed_at)))).scalar_one()
    assert distinct_runs == 2


def test_run_survives_a_crashing_finding_builder(db, monkeypatch):
    # One pathological series must cost only its own finding kind: the run
    # still writes tonight's snapshot for everything else instead of leaving
    # the stale previous snapshot in place.
    import importlib

    run_mod = importlib.import_module("app.analysis.run")

    rng = np.random.default_rng(10)
    steps = rng.integers(3000, 12000, size=60).astype(float)
    rhr = 80.0 - 0.002 * steps + rng.normal(scale=1.0, size=60)
    _add_metric(db, "step_count", steps)
    _add_metric(db, "resting_heart_rate", rhr)
    db.flush()

    def boom(*_a, **_k):
        raise RuntimeError("pathological series")

    monkeypatch.setattr(run_mod, "_anomaly_findings", boom)
    result = analysis.run(db)  # must not raise
    assert result.anomalies == 0
    assert result.correlations >= 1  # the other builders still ran and wrote


def test_aggregate_workout_daily_warns_on_degenerate_hr():
    # A configured hr_rest above the resolved hr_max slips past the profile
    # validator (which only checks explicit pairs); the zeroed TRIMP must be
    # logged, not silently masked as "no training load".
    import logging

    from app.analysis.pure import aggregate_workout_daily

    sessions = pd.DataFrame(
        [
            {
                "day": pd.Timestamp("2026-01-01"),
                "duration_s": 3600.0,
                "active_energy_kcal": 500.0,
                "avg_hr": 120.0,
                "max_hr": 150.0,
                "intensity": None,
            }
        ]
    )
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    analysis_logger = logging.getLogger("healthlog.analysis")
    analysis_logger.addHandler(handler)
    try:
        out = aggregate_workout_daily(
            sessions, pd.Series(dtype="float64"), hr_rest_default=170.0, hr_max=160.0, sex="unspecified"
        )
    finally:
        analysis_logger.removeHandler(handler)
    assert out["trimp"].iloc[0] == 0.0  # degenerate params yield zero load
    assert any("TRIMP is 0" in rec.getMessage() for rec in records)


def test_decompose_all_isolates_a_crashing_series(monkeypatch):
    from app.analysis import findings as findings_mod

    calls: list[str] = []

    def boom(_s):
        calls.append("called")
        raise RuntimeError("no convergence")

    monkeypatch.setattr(findings_mod, "decompose", boom)
    s = pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2))
    out = findings_mod._decompose_all({"a": s, "b": s})
    assert out == {"a": None, "b": None}  # degraded to "undecomposable", not raised
    assert len(calls) == 2  # the second series was still attempted


def test_build_series_includes_sleep_efficiency_and_consistency(db):
    rng = np.random.default_rng(11)
    start = dt.date(2026, 1, 1)
    for i in range(35):
        wake = start + dt.timedelta(days=i)
        sleep_start = dt.datetime(wake.year, wake.month, wake.day, 22, tzinfo=UTC) - dt.timedelta(days=1)
        total = 7.5 + float(rng.normal(scale=0.5))
        db.add(
            SleepSession(
                sleep_start=sleep_start,
                sleep_end=sleep_start + dt.timedelta(hours=8),
                in_bed_start=sleep_start - dt.timedelta(minutes=10),
                in_bed_end=sleep_start + dt.timedelta(hours=8, minutes=10),
                source="",
                sleep_date=wake,
                total_sleep_h=total,
                deep_h=1.5,
                rem_h=1.8,
            )
        )
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna")
    assert "sleep_efficiency" in series
    assert "sleep_total_h" in series
    eff = series["sleep_efficiency"].dropna()
    assert ((eff > 0) & (eff <= 1.05)).all()  # slept / in-bed ratio

    consistency = analysis._consistency_findings(db, "Europe/Vienna", dt.datetime.now(UTC))
    kinds = {f.metric_a for f in consistency}
    assert {"sleep_total_h", "bedtime"} <= kinds


def test_load_sleep_frame_one_row_per_day_picks_main_sleep(db):
    # Two distinct sleep periods on one wake-day (a nap + the main night, with
    # different sleep_end so both are kept by ingest). sleep_nightly must reduce
    # them to one row per day, choosing the most complete (the main night), and
    # the frame must not sum them. (Overlapping same-end re-captures can no longer
    # reach the table at all — see the ingest constraint, migration 0011.)
    wake = dt.date(2026, 2, 1)
    main = SleepSession(
        sleep_start=dt.datetime(2026, 1, 31, 22, tzinfo=UTC),
        sleep_end=dt.datetime(wake.year, wake.month, wake.day, 6, tzinfo=UTC),
        source="Apple Watch",
        sleep_date=wake,
        total_sleep_h=8.0,
        deep_h=1.2,
        rem_h=1.8,
        in_bed_h=8.0,
    )
    nap = SleepSession(
        sleep_start=dt.datetime(wake.year, wake.month, wake.day, 14, tzinfo=UTC),
        sleep_end=dt.datetime(wake.year, wake.month, wake.day, 14, 40, tzinfo=UTC),
        source="Apple Watch",
        sleep_date=wake,
        total_sleep_h=0.6,
        rem_h=0.0,
        in_bed_h=0.6,
    )
    db.add_all([main, nap])
    db.flush()

    frame = analysis.load_sleep_frame(db, "Europe/Vienna")
    day = pd.Timestamp(wake)
    assert len(frame) == 1
    # The main night (8.0 h), not the 8.6 h sum of the two periods.
    assert frame.loc[day, "total_sleep_h"] == 8.0
    assert frame.loc[day, "deep_h"] == 1.2


def test_run_with_no_data_is_clean(db):
    # No samples at all: run must not error and must write nothing.
    result = analysis.run(db)
    assert result.total() == 0
    assert db.execute(select(func.count()).select_from(Finding)).scalar_one() == 0


# --- Workout training load: Banister TRIMP ---------------------------------


def test_banister_trimp_female_weight_exceeds_male_at_same_load():
    male = banister_trimp(3600, 150, 60, 180, "male")
    female = banister_trimp(3600, 150, 60, 180, "female")
    assert male > 0 and female > male  # female weighting is steeper


def test_banister_trimp_zero_without_usable_inputs():
    assert banister_trimp(3600, None, 60, 180) == 0.0  # no avg_hr (e.g. strength)
    assert banister_trimp(0, 150, 60, 180) == 0.0  # zero duration
    assert banister_trimp(3600, 150, 180, 150) == 0.0  # hr_max <= hr_rest


def test_banister_trimp_clamps_hr_reserve_to_one():
    # avg_hr above hr_max -> HRr clamps at 1.0, not >1.
    capped = banister_trimp(3600, 250, 60, 180, "male")
    at_max = banister_trimp(3600, 180, 60, 180, "male")
    assert capped == at_max


# --- Zone-based (Edwards) TRIMP --------------------------------------------


def _hr_samples(bpms, *, step_s=60, start="2026-01-01 12:00:00") -> pd.DataFrame:
    """A per-sample HR frame (ts, bpm), evenly spaced by ``step_s`` seconds."""
    ts = pd.to_datetime(start) + pd.to_timedelta(np.arange(len(bpms)) * step_s, unit="s")
    return pd.DataFrame({"ts": ts, "bpm": np.asarray(bpms, dtype="float64")})


def test_hr_zone_weight_boundaries():
    assert _hr_zone_weight(80, 200) == 0  # 0.40 of HR_max -> below zone 1
    assert _hr_zone_weight(100, 200) == 1  # 0.50 -> zone 1
    assert _hr_zone_weight(140, 200) == 3  # 0.70 -> zone 3
    assert _hr_zone_weight(180, 200) == 5  # 0.90 -> zone 5
    assert _hr_zone_weight(200, 200) == 5  # 1.00 stays at the top zone
    assert _hr_zone_weight(150, 0) == 0  # no HR_max -> no zone


def test_edwards_trimp_sums_minutes_times_zone_weight():
    # Each interval = 1 min; the last sample has no following interval.
    s = _hr_samples([100, 140, 150])  # zone 1 (w1) then zone 3 (w3)
    assert edwards_trimp(s, 200) == 1.0 * 1 + 1.0 * 3


def test_edwards_trimp_rescales_interval_time_to_duration():
    s = _hr_samples([100, 140, 150])  # raw covered time = 120 s
    assert edwards_trimp(s, 200, duration_s=240) == 2 * (1.0 * 1 + 1.0 * 3)  # x2


def test_edwards_trimp_zero_without_usable_series():
    assert edwards_trimp(None, 200) == 0.0
    assert edwards_trimp(_hr_samples([150]), 200) == 0.0  # a single sample = no interval
    assert edwards_trimp(_hr_samples([80, 80, 80]), 200) == 0.0  # all below zone 1
    assert edwards_trimp(_hr_samples([150, 150]), 0) == 0.0  # no HR_max


def test_edwards_trimp_resolves_intervals_banister_smooths():
    # Same number of samples, different intensity distribution -> different load
    # (the point of Edwards over a single-average Banister TRIMP).
    steady = edwards_trimp(_hr_samples([150, 150, 150]), 200)  # 0.75 -> w3 twice
    spiky = edwards_trimp(_hr_samples([120, 180, 150]), 200)  # 0.60 -> w2, 0.90 -> w5
    assert steady == 6.0 and spiky == 7.0


# --- HR_max / HR_rest fallback chains --------------------------------------


def test_resolve_hr_max_prefers_profile_override():
    assert resolve_hr_max(ProfileConfig(hr_max=200), pd.Series([195.0, 205.0])) == 200.0


def test_resolve_hr_max_uses_tanaka_from_birth_year():
    age = dt.date.today().year - 1990
    assert resolve_hr_max(ProfileConfig(birth_year=1990), None) == 208.0 - 0.7 * age


def test_resolve_hr_max_data_driven_is_clamped():
    assert resolve_hr_max(ProfileConfig(), pd.Series([250.0])) == 210.0  # ceil
    assert resolve_hr_max(ProfileConfig(), pd.Series([130.0])) == 160.0  # floor
    assert resolve_hr_max(ProfileConfig(), None) == 190.0  # nothing -> constant


def test_resolve_hr_rest_default_chain():
    # profile override wins
    series, default = resolve_hr_rest(_daily([50.0] * 40), ProfileConfig(hr_rest=55))
    assert default == 55.0
    # no profile, data present -> measured median
    series, default = resolve_hr_rest(_daily([48.0] * 40), ProfileConfig())
    assert default == 48.0
    assert not series.dropna().empty
    # nothing at all -> constant fallback
    series, default = resolve_hr_rest(None, ProfileConfig())
    assert default == 60.0 and series.empty


# --- Zero-fill + daily aggregation -----------------------------------------


def test_fill_zero_within_span_fills_rest_days_with_zero():
    s = pd.Series([5.0, 3.0], index=pd.to_datetime(["2026-01-01", "2026-01-04"]))
    filled = fill_zero_within_span(s)
    assert list(filled.index) == list(pd.date_range("2026-01-01", "2026-01-04", freq="D"))
    assert filled.loc["2026-01-02"] == 0.0 and filled.loc["2026-01-03"] == 0.0


def test_aggregate_workout_daily_sums_sessions_per_day():
    d1, d2 = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")
    sessions = pd.DataFrame.from_records(
        [
            {
                "day": d1,
                "duration_s": 3600,
                "active_energy_kcal": 500.0,
                "avg_hr": 150.0,
                "max_hr": 170.0,
                "intensity": 6.0,
            },
            {
                "day": d1,
                "duration_s": 1800,
                "active_energy_kcal": 300.0,
                "avg_hr": 140.0,
                "max_hr": 160.0,
                "intensity": 5.0,
            },
            {
                "day": d2,
                "duration_s": 3600,
                "active_energy_kcal": 400.0,
                "avg_hr": 120.0,
                "max_hr": 150.0,
                "intensity": 4.0,
            },
        ]
    )
    daily = aggregate_workout_daily(sessions, pd.Series(dtype="float64"), 60.0, 180.0, "male")
    assert daily.loc[d1, "count"] == 2
    assert daily.loc[d1, "load"] == 800.0
    assert daily.loc[d1, "duration_h"] == 1.5
    assert daily.loc[d1, "trimp"] > daily.loc[d2, "trimp"]  # harder day -> more load
    assert daily.loc[d1, "intensity"] == 5.5  # mean of the two sessions


# --- ACWR / training-load finding ------------------------------------------


def test_acute_chronic_ratio_detects_spike():
    s = _daily([10.0] * 21 + [30.0] * 7)  # 28 days, recent week 3x baseline
    acwr = acute_chronic_ratio(s)
    assert acwr is not None
    _, _, ratio = acwr
    assert ratio == 2.0  # acute 30 / chronic 15


def test_acute_chronic_ratio_needs_history():
    assert acute_chronic_ratio(_daily([10.0] * 20)) is None  # < 28 days
    assert acute_chronic_ratio(_daily([0.0] * 30)) is None  # chronic load 0


def test_training_load_finding_flags_spike_only_outside_band():
    spike = analysis._training_load_findings({"workout_trimp": _daily([10.0] * 21 + [30.0] * 7)}, dt.datetime.now(UTC))
    assert len(spike) == 1
    assert spike[0].kind == "training_load"
    assert spike[0].metric_a == "workout_trimp"
    assert spike[0].severity == 2.0

    detrain = analysis._training_load_findings({"workout_trimp": _daily([30.0] * 21 + [5.0] * 7)}, dt.datetime.now(UTC))
    assert len(detrain) == 1 and "detraining" in detrain[0].note

    balanced = analysis._training_load_findings({"workout_trimp": _daily([20.0] * 28)}, dt.datetime.now(UTC))
    assert balanced == []  # inside the safe band -> no finding


def test_training_load_prefers_trimp_over_energy():
    series = {"workout_trimp": _daily([20.0] * 28), "workout_load": _daily([10.0] * 21 + [40.0] * 7)}
    # trimp is balanced -> no finding even though energy would spike.
    assert analysis._training_load_findings(series, dt.datetime.now(UTC)) == []


def test_training_load_targets_aggregate_and_per_sport():
    series = {
        "workout_trimp": _daily([1.0]),
        "workout_load": _daily([1.0]),
        "workout_trimp_running": _daily([1.0]),
        "workout_load_running": _daily([1.0]),
        "workout_load_cycling": _daily([1.0]),  # cycling has only the kcal series
    }
    targets = analysis._training_load_targets(series)
    assert targets[0] == "workout_trimp"  # aggregate first, prefers TRIMP
    # one target per family, TRIMP preferred where available
    assert set(targets) == {"workout_trimp", "workout_trimp_running", "workout_load_cycling"}


def test_training_load_per_sport_with_activity_guard():
    series = {
        "workout_trimp": _daily([20.0] * 28),  # aggregate: balanced -> no finding
        "workout_trimp_running": _daily([10.0] * 21 + [30.0] * 7),  # spike, trains daily
        "workout_trimp_cycling": _daily([0.0] * 26 + [50.0, 50.0]),  # only 2 active days
    }
    findings = analysis._training_load_findings(series, dt.datetime.now(UTC), AnalysisConfig())
    # Running spikes and is trained enough; cycling is too sparse to trust.
    assert {f.metric_a for f in findings} == {"workout_trimp_running"}


def test_training_load_activity_guard_is_configurable():
    series = {"workout_trimp_cycling": _daily([0.0] * 26 + [50.0, 50.0])}  # 2 active days
    # Default guard (8 active days) suppresses the sparse sport.
    assert analysis._training_load_findings(series, dt.datetime.now(UTC), AnalysisConfig()) == []
    # Relaxing the guard lets the (genuine) spike through.
    relaxed = analysis._training_load_findings(series, dt.datetime.now(UTC), AnalysisConfig(acwr_min_active_days=1))
    assert [f.metric_a for f in relaxed] == ["workout_trimp_cycling"]


# --- Training status (CTL/ATL/TSB) ------------------------------------------


def test_ewma_matches_explicit_recursion():
    rng = np.random.default_rng(7)
    s = _daily(rng.uniform(0, 50, size=100))
    out = ewma(s, 42.0)
    y = 0.0
    for x in s:
        y += (x - y) / 42.0
    assert abs(float(out.iloc[-1]) - y) < 1e-9
    # zero-seeded: the first smoothed value is x_0 / tau, not x_0
    assert abs(float(out.iloc[0]) - float(s.iloc[0]) / 42.0) < 1e-9


def test_training_status_needs_history_and_load():
    assert training_status(_daily([10.0] * 41)) is None  # < one CTL time constant
    assert training_status(_daily([0.0] * 60)) is None  # no load -> no status


def test_training_status_taper_turns_form_positive():
    building = training_status(_daily([20.0] * 56 + [30.0] * 14))
    tapering = training_status(_daily([20.0] * 56 + [0.0] * 14))
    assert building is not None and tapering is not None
    assert building.tsb < 0 < tapering.tsb  # hard block -> negative form; taper -> positive
    assert tapering.atl < building.atl
    assert abs(building.tsb_pct - building.tsb / building.ctl) < 1e-12


def test_tsb_zone_bands():
    from app.analysis.findings import _tsb_zone

    cfg = AnalysisConfig()
    assert _tsb_zone(0.20, cfg) == "detraining"
    assert _tsb_zone(0.08, cfg) == "fresh"
    assert _tsb_zone(0.0, cfg) == "neutral"
    assert _tsb_zone(-0.15, cfg) == "productive"
    assert _tsb_zone(-0.40, cfg) == "overreaching_risk"


def test_training_status_finding_written_every_run():
    # A steady load is not alert-worthy, but the status snapshot still exists.
    findings = analysis._training_status_findings({"workout_trimp": _daily([20.0] * 90)}, dt.datetime.now(UTC))
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "training_status"
    assert f.metric_a == "workout_trimp"
    assert f.details["zone"] in ("neutral", "productive")  # warm-up keeps TSB mildly negative
    assert f.details["ctl_trend"] == "rising"  # the base is still building from the zero seed
    assert f.severity == abs(f.details["tsb_pct"])


def test_training_status_finding_flags_overreach_zone():
    findings = analysis._training_status_findings(
        {"workout_trimp": _daily([10.0] * 56 + [30.0] * 14)}, dt.datetime.now(UTC)
    )
    assert len(findings) == 1
    assert findings[0].details["zone"] == "overreaching_risk"
    assert "overreaching" in findings[0].note


def test_training_status_aggregate_only_prefers_trimp():
    series = {
        "workout_trimp": _daily([20.0] * 90),
        "workout_load": _daily([300.0] * 90),
        "workout_trimp_running": _daily([20.0] * 90),
    }
    findings = analysis._training_status_findings(series, dt.datetime.now(UTC))
    assert [f.metric_a for f in findings] == ["workout_trimp"]  # one snapshot, aggregate, TRIMP preferred


# --- Per-sport type mapping (Iteration 2) ----------------------------------


def test_canonical_workout_type_maps_case_insensitively():
    tmap = {"Outdoor Run": "running", "Traditional Strength Training": "strength"}
    assert canonical_workout_type("Outdoor Run", tmap) == "running"
    assert canonical_workout_type("outdoor run", tmap) == "running"  # case-insensitive
    # Multi-word canonical types are slugged into safe series-name suffixes.
    assert canonical_workout_type("Traditional Strength Training", tmap) == "strength"


def test_canonical_workout_type_unmapped_is_none():
    tmap = {"Outdoor Run": "running"}
    assert canonical_workout_type("Quidditch Match", tmap) is None  # unknown to map + built-in
    assert canonical_workout_type(None, tmap) is None
    assert canonical_workout_type("", tmap) is None


def test_canonical_workout_type_uses_builtin_without_config():
    # The built-in map normalises common Apple types out of the box (no config).
    assert canonical_workout_type("Outdoor Run", {}) == "running"
    assert canonical_workout_type("Pool Swim", {}) == "swimming"
    # Cross-language stability: German and English names fold to one type.
    assert canonical_workout_type("Laufen", {}) == canonical_workout_type("Outdoor Run", {}) == "running"
    assert canonical_workout_type("Radfahren", {}) == "cycling"


def test_canonical_workout_type_config_overrides_builtin():
    # An operator entry wins over the built-in mapping for the same name.
    assert canonical_workout_type("Outdoor Run", {"Outdoor Run": "trail running"}) == "trail_running"


def test_canonical_workout_type_slugs_spaces():
    assert canonical_workout_type("X", {"X": "Trail Running"}) == "trail_running"


# --- Workout series: DB end-to-end -----------------------------------------


def _add_workout(db, start: dt.datetime, *, duration_s, avg_hr, max_hr, energy, name="Outdoor Run"):
    hae_id = uuid.uuid4()
    db.add(
        Workout(
            hae_id=hae_id,
            start_time=start,
            end_time=start + dt.timedelta(seconds=duration_s),
            name=name,
            duration_s=float(duration_s),
            active_energy_kcal=float(energy),
            avg_hr=float(avg_hr),
            max_hr=float(max_hr),
            source="",
        )
    )
    return hae_id


def _add_hr_series(db, hae_id, start: dt.datetime, *, count, base=110, span=70, step_s=60):
    """Attach ~per-minute HR samples spanning several zones to a workout."""
    for k in range(count):
        db.add(
            WorkoutHrSample(
                workout_hae_id=hae_id,
                ts=start + dt.timedelta(seconds=k * step_s),
                bpm=float(base + (k % 5) * (span / 4.0)),  # sweeps 110..180
            )
        )


def test_build_series_includes_workout_load_series(db):
    rng = np.random.default_rng(30)
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(
            db,
            when,
            duration_s=1800 + 600 * (i % 3),
            avg_hr=130 + rng.integers(-5, 15),
            max_hr=170,
            energy=300 + 50 * (i % 4),
        )
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna")
    assert "workout_trimp" in series
    assert "workout_load" in series
    # Workout series are densified: a continuous daily index, no gaps.
    trimp = series["workout_trimp"]
    assert (trimp.index == pd.date_range(trimp.index.min(), trimp.index.max(), freq="D")).all()


def test_build_series_load_metric_energy_only(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=140, max_hr=175, energy=350)
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna", workouts=WorkoutConfig(load_metric="energy"))
    assert "workout_load" in series
    assert "workout_trimp" not in series  # gated off by load_metric


def test_run_with_workouts_is_clean_and_snapshots(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=140, max_hr=175, energy=350)
    db.flush()

    result = analysis.run(db, "Europe/Vienna", AppConfig())
    assert isinstance(result, AnalysisResult)
    # Re-running replaces, never accumulates (snapshot semantics).
    first = db.execute(select(func.count()).select_from(Finding)).scalar_one()
    analysis.run(db, "Europe/Vienna", AppConfig())
    assert db.execute(select(func.count()).select_from(Finding)).scalar_one() == first


def test_run_persists_workout_load_series_snapshot(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        hae_id = _add_workout(db, when, duration_s=2400, avg_hr=140, max_hr=175, energy=350)
        if i % 2 == 0:  # an HR series on every other session -> Edwards self-gates on
            _add_hr_series(db, hae_id, when, count=40)
    db.flush()

    cfg = AppConfig(workouts=WorkoutConfig(type_map={"Outdoor Run": "running"}))
    analysis.run(db, "Europe/Vienna", cfg)

    rows = db.execute(select(WorkoutLoadDaily)).scalars().all()
    by_series: dict[str, list[WorkoutLoadDaily]] = {}
    for r in rows:
        by_series.setdefault(r.series, []).append(r)

    # The aggregate load family, the zone-based parallel and the per-sport child.
    for name in ("workout_trimp", "workout_load", "workout_edwards", "workout_duration", "workout_count"):
        assert name in by_series, name
    assert "workout_trimp_running" in by_series and "workout_edwards_running" in by_series
    # Densified span: one row per calendar day, real zeros included.
    assert len(by_series["workout_trimp"]) == 35
    assert sum(r.value for r in by_series["workout_count"]) == 35
    # Days with an HR series carry a positive Edwards load; days without stay 0.
    edwards = {r.day: r.value for r in by_series["workout_edwards"]}
    assert edwards[start] > 0 and edwards[start + dt.timedelta(days=1)] == 0

    # Re-running replaces, never accumulates (snapshot semantics like findings).
    analysis.run(db, "Europe/Vienna", cfg)
    assert db.execute(select(func.count()).select_from(WorkoutLoadDaily)).scalar_one() == len(rows)


def test_findings_feed_view_renders_per_kind_detail(db):
    computed_at = dt.datetime(2026, 7, 1, 2, 0, tzinfo=UTC)
    db.add_all(
        [
            Finding(
                computed_at=computed_at,
                kind="training_load",
                metric_a="workout_trimp",
                ref_date=dt.date(2026, 6, 28),
                severity=0.7,
                details={"ratio": 1.85},
            ),
            Finding(
                computed_at=computed_at,
                kind="anomaly",
                metric_a="resting_heart_rate",
                ref_date=dt.date(2026, 6, 29),
                severity=0.5,
                details={"z": 4.2, "value": 61.0},
            ),
            Finding(
                computed_at=computed_at,
                kind="correlation",
                metric_a="workout_trimp",
                metric_b="sleep_total_h",
                lag_days=2,
                coefficient=0.8,
                window_end=dt.date(2026, 6, 30),
                severity=0.8,
            ),
            Finding(
                computed_at=computed_at,
                kind="trend",
                metric_a="vo2_max",
                window_end=dt.date(2026, 6, 30),
                severity=0.42,
            ),
        ]
    )
    db.flush()

    feed = {r.kind: r for r in db.execute(text("SELECT kind, day, detail FROM findings_feed ORDER BY kind")).all()}
    assert feed["training_load"].detail == "ACWR 1.85"
    assert feed["training_load"].day == dt.date(2026, 6, 28)  # ref_date wins
    assert feed["anomaly"].detail == "4.2σ (val: 61.00)"
    assert feed["correlation"].detail == "r=0.80 lag=2d"
    assert feed["correlation"].day == dt.date(2026, 6, 30)  # window_end fallback
    assert feed["trend"].detail == "0.42"  # generic severity fallback


def test_build_series_splits_load_by_sport(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400, name="Outdoor Run")
        if i % 3 == 0:  # cycling every third day
            _add_workout(
                db,
                when + dt.timedelta(hours=2),
                duration_s=3600,
                avg_hr=135,
                max_hr=170,
                energy=500,
                name="Outdoor Cycle",
            )
        if i % 5 == 0:  # a sport unknown to both the config map and the built-in
            _add_workout(
                db,
                when + dt.timedelta(hours=4),
                duration_s=1800,
                avg_hr=120,
                max_hr=150,
                energy=200,
                name="Quidditch Match",
            )
    db.flush()

    workouts = WorkoutConfig(type_map={"Outdoor Run": "running", "Outdoor Cycle": "cycling"})
    series = analysis.build_series(db, "Europe/Vienna", workouts=workouts)

    # Type-agnostic aggregate stays; per-sport series are added for mapped types.
    assert "workout_trimp" in series and "workout_load" in series
    assert "workout_trimp_running" in series and "workout_load_running" in series
    assert "workout_trimp_cycling" in series and "workout_load_cycling" in series
    # The unrecognised sport feeds only the aggregate, never its own series.
    assert not any("quidditch" in name for name in series)


def test_build_series_no_type_split_for_unrecognised_sport(db):
    start = dt.date(2026, 1, 1)
    for i in range(30):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400, name="Quidditch Match")
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna")  # default config, no type_map
    assert "workout_trimp" in series
    # Unknown to the built-in map and no config entry -> aggregate only, no split.
    assert not any(name.startswith("workout_trimp_") for name in series)


def test_build_series_splits_by_builtin_map_without_config(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400, name="Outdoor Run")
        if i % 3 == 0:
            _add_workout(
                db, when + dt.timedelta(hours=2), duration_s=3600, avg_hr=135, max_hr=170, energy=500, name="Radfahren"
            )
    db.flush()

    # No type_map configured: the built-in map alone normalises the localised
    # names, so per-sport series appear out of the box (German + English fold).
    series = analysis.build_series(db, "Europe/Vienna")
    assert "workout_trimp_running" in series
    assert "workout_trimp_cycling" in series


def test_build_series_per_sport_respects_load_metric(db):
    start = dt.date(2026, 1, 1)
    for i in range(30):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400, name="Outdoor Run")
    db.flush()

    workouts = WorkoutConfig(load_metric="energy", type_map={"Outdoor Run": "running"})
    series = analysis.build_series(db, "Europe/Vienna", workouts=workouts)
    assert "workout_load_running" in series
    assert "workout_trimp_running" not in series  # trimp gated off by load_metric


# --- Zone-based (Edwards) TRIMP: DB end-to-end -----------------------------


def test_build_series_includes_edwards_when_hr_samples_present(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        hid = _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400)
        _add_hr_series(db, hid, when, count=40)
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna")  # edwards default on
    assert "workout_edwards" in series
    edwards = series["workout_edwards"]
    # Parallel to Banister, densified to a complete daily grid, non-trivial.
    assert "workout_trimp" in series
    assert (edwards.index == pd.date_range(edwards.index.min(), edwards.index.max(), freq="D")).all()
    assert (edwards > 0).any()


def test_build_series_no_edwards_without_hr_samples(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400)
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna")  # on, but no samples stored
    assert "workout_trimp" in series
    assert "workout_edwards" not in series  # self-gates off with no data


def test_build_series_edwards_can_be_disabled(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        hid = _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400)
        _add_hr_series(db, hid, when, count=40)
    db.flush()

    series = analysis.build_series(db, "Europe/Vienna", workouts=WorkoutConfig(edwards=False))
    assert "workout_trimp" in series
    assert "workout_edwards" not in series  # gated off by config


def test_build_series_edwards_per_sport(db):
    start = dt.date(2026, 1, 1)
    for i in range(35):
        day = start + dt.timedelta(days=i)
        when = dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        run = _add_workout(db, when, duration_s=2400, avg_hr=150, max_hr=180, energy=400, name="Outdoor Run")
        _add_hr_series(db, run, when, count=40)
        cyc = _add_workout(
            db, when + dt.timedelta(hours=2), duration_s=3600, avg_hr=140, max_hr=175, energy=500, name="Outdoor Cycle"
        )
        _add_hr_series(db, cyc, when + dt.timedelta(hours=2), count=60)
    db.flush()

    workouts = WorkoutConfig(type_map={"Outdoor Run": "running", "Outdoor Cycle": "cycling"})
    series = analysis.build_series(db, "Europe/Vienna", workouts=workouts)
    assert "workout_edwards" in series
    assert "workout_edwards_running" in series and "workout_edwards_cycling" in series


# --- Stress proxy (pure) ---------------------------------------------------


def _minute_hr(values, start="2026-03-16 08:00", tz="UTC") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="min", tz=tz)
    return pd.Series(np.asarray(values, dtype="float64"), index=idx)


def test_stress_state_bands():
    assert stress_state(10, 25, 50, 75) == "rest"
    assert stress_state(25, 25, 50, 75) == "low"  # boundary is inclusive of the upper band
    assert stress_state(60, 25, 50, 75) == "medium"
    assert stress_state(90, 25, 50, 75) == "high"


def test_stress_scales_with_hr_reserve():
    # rest=55, max=155 -> reserve 100; reserve_full 0.5 saturates at 50 bpm over rest.
    hr = _minute_hr([55, 80, 105, 130])
    df = stress_intraday_from_hr(hr, hr_rest_day=55.0, hr_max=155.0, reserve_full=0.5)
    stresses = list(df["stress"])
    assert stresses[0] == 0  # at rest
    assert stresses[1] == 50  # (80-55)/(0.5*100) = 0.5
    assert stresses[2] == 100  # (105-55)/50 = 1.0 (saturated)
    assert stresses[3] == 100  # clamped
    assert list(df["state"]) == ["rest", "medium", "high", "high"]


def test_stress_workout_minutes_are_active_not_scored():
    hr = _minute_hr([60, 150, 150, 60])
    wstart = pd.Timestamp("2026-03-16 08:01", tz="UTC")
    intervals = [(wstart, wstart + pd.Timedelta(minutes=2))]
    df = stress_intraday_from_hr(hr, 55.0, 155.0, workout_intervals=intervals)
    assert list(df["state"]) == ["rest", "active", "active", "rest"]
    assert pd.isna(df["stress"].iloc[1]) and pd.isna(df["stress"].iloc[2])


def test_stress_step_active_buckets_are_gated():
    # A brisk walk (>= active_steps_per_min steps in a bucket) elevates HR but is
    # movement, not stress: those buckets go "active" like workout minutes.
    hr = _minute_hr([60, 120, 120, 60])
    steps = pd.Series([0.0, 80.0, 90.0, 5.0], index=hr.index)
    df = stress_intraday_from_hr(hr, 55.0, 155.0, steps=steps, active_steps_per_min=60.0)
    assert list(df["state"]) == ["rest", "active", "active", "rest"]
    assert pd.isna(df["stress"].iloc[1]) and pd.isna(df["stress"].iloc[2])


def test_stress_step_gating_disabled_with_zero_threshold():
    hr = _minute_hr([120])
    steps = pd.Series([200.0], index=hr.index)
    df = stress_intraday_from_hr(hr, 55.0, 155.0, steps=steps, active_steps_per_min=0.0)
    assert df["state"].iloc[0] != "active"


def test_stress_overlapping_workout_intervals_merge():
    # Overlapping intervals (e.g. a multisport session logged twice) must not
    # confuse the vectorised membership test.
    hr = _minute_hr([150, 150, 150, 150])
    t0 = hr.index[0]
    intervals = [
        (t0, t0 + pd.Timedelta(minutes=3)),
        (t0 + pd.Timedelta(minutes=1), t0 + pd.Timedelta(minutes=2)),
    ]
    df = stress_intraday_from_hr(hr, 55.0, 155.0, workout_intervals=intervals)
    assert list(df["state"]) == ["active", "active", "active", "high"]


def test_stress_hrv_modulation_raises_on_low_hrv():
    hr = _minute_hr([90, 90])
    base = stress_intraday_from_hr(hr, 55.0, 155.0, hrv_z=None, hrv_weight=0.3)["stress"].iloc[0]
    low_hrv = stress_intraday_from_hr(hr, 55.0, 155.0, hrv_z=-2.0, hrv_weight=0.3)["stress"].iloc[0]
    high_hrv = stress_intraday_from_hr(hr, 55.0, 155.0, hrv_z=2.0, hrv_weight=0.3)["stress"].iloc[0]
    assert low_hrv > base > high_hrv
    # modulation is clamped to [1 - hrv_weight, 1 + hrv_weight] = [0.7, 1.3]
    assert low_hrv == round(base * 1.3)
    assert high_hrv == round(base * 0.7)


def test_stress_degenerate_reserve_is_unmeasurable():
    hr = _minute_hr([80, 90])
    df = stress_intraday_from_hr(hr, hr_rest_day=160.0, hr_max=150.0)  # rest >= max
    assert list(df["state"]) == ["unmeasurable", "unmeasurable"]
    assert df["stress"].isna().all()


def test_summarize_stress_day_weights_by_dwell_and_counts_zones():
    # 30 min rest at ~0, then 10 min high at 100; score is dwell-weighted.
    hr = _minute_hr([55.0] * 30 + [155.0] * 10)
    df = stress_intraday_from_hr(hr, 55.0, 155.0, reserve_full=0.5)
    summ = summarize_stress_day(df)
    assert summ["rest_min"] == 30
    assert summ["high_min"] == 10
    assert summ["measured_min"] == 40
    # last bucket contributes 1 nominal minute; ~ (30*0 + 10*100)/40
    assert 24.0 <= summ["score"] <= 26.0


def test_summarize_stress_day_caps_long_gaps_as_unmeasurable():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-03-16 08:00", tz="UTC"), pd.Timestamp("2026-03-16 09:00", tz="UTC")])
    hr = pd.Series([60.0, 60.0], index=idx)
    df = stress_intraday_from_hr(hr, 55.0, 155.0)
    summ = summarize_stress_day(df, gap_cap_minutes=10.0)
    # 60-minute gap: 10 min dwell for the first bucket, 50 min unmeasurable.
    assert summ["unmeasurable_min"] == 50
    assert summ["rest_min"] == 11  # 10 (capped) + 1 (trailing nominal)


def test_summarize_stress_day_empty():
    summ = summarize_stress_day(pd.DataFrame(columns=["stress", "hr", "state"]))
    assert summ["score"] is None and summ["measured_min"] == 0


# --- Stress proxy (DB end-to-end) ------------------------------------------


def _seed_stress_history(db, spike_days=(37,), workout_days=(37, 38)):
    """40 days of daily RHR/HRV baseline + per-minute HR for the last 3 days."""
    base = dt.date(2026, 3, 1)
    rng = np.random.default_rng(0)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0 + rng.normal(0, 1)))
        db.add(MetricSample(time=at4, metric="heart_rate_variability", source="", qty=45.0 + rng.normal(0, 3)))
    for d in (37, 38, 39):
        day = base + dt.timedelta(days=d)
        for m in range(600):
            ts = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
            bpm = 58.0
            if d in spike_days and 60 <= m < 480:
                bpm = 115.0
            if 300 <= m < 360 and d in workout_days:
                bpm = 150.0
            db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=bpm, vmin=bpm - 5, vmax=bpm + 5))
        if d in workout_days:
            wstart = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=300)
            db.add(
                Workout(
                    hae_id=uuid.uuid4(),
                    start_time=wstart,
                    end_time=wstart + dt.timedelta(minutes=60),
                    name="Run",
                    duration_s=3600,
                    avg_hr=150,
                    max_hr=160,
                )
            )
    db.flush()


def test_compute_stress_writes_tables_and_is_idempotent(db):
    _seed_stress_history(db)
    cfg = StressConfig()
    res1 = compute_stress(db, "Europe/Vienna", cfg, ProfileConfig(), since_days=cfg.window_days)
    assert res1.days == 3 and res1.buckets == 1800
    daily = {r.day: r for r in db.execute(select(StressDaily)).scalars()}
    assert len(daily) == 3
    # The spike day (2026-04-07) scores clearly higher than a calm day.
    spike = daily[dt.date(2026, 4, 7)]
    calm = daily[dt.date(2026, 4, 9)]
    assert spike.score > calm.score
    assert spike.high_min > 0
    # Workout minutes are excluded from the score (active band).
    assert spike.active_min == 60
    intraday_count = db.execute(select(func.count()).select_from(StressIntraday)).scalar_one()
    assert intraday_count == 1800

    # Idempotent: a re-run replaces the window, no duplication.
    res2 = compute_stress(db, "Europe/Vienna", cfg, ProfileConfig(), since_days=cfg.window_days)
    assert res2.days == 3 and res2.buckets == 1800
    assert db.execute(select(func.count()).select_from(StressIntraday)).scalar_one() == 1800
    assert db.execute(select(func.count()).select_from(StressDaily)).scalar_one() == 3


def test_run_emits_stress_alert_for_high_stress_day(db):
    _seed_stress_history(db)
    result = analysis.run(db)
    assert result.stress >= 1
    alert = db.execute(select(Finding).where(Finding.kind == "stress")).scalars().first()
    assert alert is not None
    assert alert.metric_a == "stress"
    assert alert.ref_date == dt.date(2026, 4, 7)
    assert alert.severity >= StressConfig().alert_score
    assert alert.details["high_min"] > 0


def test_compute_stress_disabled_writes_nothing(db):
    _seed_stress_history(db)
    res = compute_stress(db, "Europe/Vienna", StressConfig(enabled=False), ProfileConfig(), since_days=90)
    assert res.days == 0 and res.buckets == 0
    assert db.execute(select(func.count()).select_from(StressDaily)).scalar_one() == 0


def test_compute_stress_no_hr_data_is_clean(db):
    res = compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=90)
    assert res.days == 0 and res.buckets == 0


def test_compute_stress_dedupes_multi_source_buckets(db):
    # Two sources report heart_rate at the same minute: must not collide on the
    # stress_intraday ts primary key (they are averaged into one bucket).
    base = dt.date(2026, 3, 1)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0))
    day = base + dt.timedelta(days=39)
    for m in range(120):
        ts = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        db.add(MetricSample(time=ts, metric="heart_rate", source="Apple Watch", vavg=80.0))
        db.add(MetricSample(time=ts, metric="heart_rate", source="iPhone", vavg=90.0))
    db.flush()

    res = compute_stress(db, "Europe/Vienna", StressConfig(min_measured_min=1), ProfileConfig(), since_days=90)
    assert res.buckets == 120  # one row per minute, not two
    hr = {r.ts: r.hr for r in db.execute(select(StressIntraday)).scalars()}
    assert len(hr) == 120
    assert next(iter(hr.values())) == 85.0  # (80 + 90) / 2


def test_compute_stress_gates_step_active_minutes(db):
    # Per-minute steps during elevated HR (a brisk walk) turn those buckets
    # "active" instead of high-stress.
    base = dt.date(2026, 3, 1)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0))
    day = base + dt.timedelta(days=39)
    for m in range(120):
        ts = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        walking = 30 <= m < 60
        db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=110.0 if walking else 58.0))
        db.add(MetricSample(time=ts, metric="step_count", source="", qty=110.0 if walking else 0.0))
    db.flush()

    compute_stress(db, "Europe/Vienna", StressConfig(min_measured_min=1), ProfileConfig(), since_days=90)
    states = {r.ts: r.state for r in db.execute(select(StressIntraday)).scalars()}
    assert states[dt.datetime(day.year, day.month, day.day, 8, 40, tzinfo=UTC)] == "active"
    daily = db.execute(select(StressDaily)).scalars().one()
    assert daily.active_min == 30


def test_compute_stress_ignores_coarse_step_buckets(db):
    # Hourly step totals must not gate single co-timed buckets: the cadence
    # guard self-disables the gate on coarse step data.
    base = dt.date(2026, 3, 1)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0))
    day = base + dt.timedelta(days=39)
    for m in range(120):
        ts = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=58.0))
    for h in (8, 9):
        ts = dt.datetime(day.year, day.month, day.day, h, tzinfo=UTC)
        db.add(MetricSample(time=ts, metric="step_count", source="", qty=3000.0))
    db.flush()

    compute_stress(db, "Europe/Vienna", StressConfig(min_measured_min=1), ProfileConfig(), since_days=90)
    states = [r.state for r in db.execute(select(StressIntraday)).scalars()]
    assert "active" not in states


# --- Body Battery (pure) ---------------------------------------------------


def _bb_frame(rows, start="2026-03-16 08:00") -> pd.DataFrame:
    """Build a per-minute stress-intraday frame from (stress, state) tuples."""
    idx = pd.date_range(start, periods=len(rows), freq="min", tz="UTC")
    return pd.DataFrame({"stress": [r[0] for r in rows], "state": [r[1] for r in rows]}, index=idx)


def test_body_battery_drains_on_stress_and_charges_on_rest():
    high = body_battery_timeline(_bb_frame([(100, "high")] * 60), seed_level=50.0, drain_rate=0.2)
    assert high["level"].iloc[-1] < 50.0
    assert high["level"].is_monotonic_decreasing

    rest = body_battery_timeline(_bb_frame([(0, "rest")] * 60), seed_level=50.0, charge_rate=0.1)
    assert rest["level"].iloc[-1] > 50.0
    assert rest["level"].is_monotonic_increasing


def test_body_battery_clamps_to_0_and_100():
    drain = body_battery_timeline(_bb_frame([(100, "high")] * 600), seed_level=50.0, drain_rate=0.2)
    assert drain["level"].min() == 0.0
    charge = body_battery_timeline(_bb_frame([(0, "rest")] * 2000), seed_level=50.0, charge_rate=0.1)
    assert charge["level"].max() == 100.0


def test_body_battery_sleep_charges_more_than_awake_rest():
    start = pd.Timestamp("2026-03-16 23:00", tz="UTC")
    frame = _bb_frame([(0, "rest")] * 480, start="2026-03-16 23:00")
    sleep = [(start, start + pd.Timedelta(minutes=480), 1.0)]
    asleep = body_battery_timeline(frame, sleep, seed_level=20.0)["level"].iloc[-1]
    awake = body_battery_timeline(frame, [], seed_level=20.0)["level"].iloc[-1]
    assert asleep > awake > 20.0
    assert asleep >= 90.0  # 20 + ~480*0.15 sleep charge


def test_body_battery_active_drains_and_unmeasurable_holds():
    active = body_battery_timeline(_bb_frame([(None, "active")] * 60), seed_level=50.0, active_drain_rate=0.3)
    assert active["level"].iloc[-1] < 50.0
    hold = body_battery_timeline(_bb_frame([(None, "unmeasurable")] * 60), seed_level=50.0)
    assert (hold["level"] == 50.0).all()


def test_body_battery_seed_washes_out_after_full_sleep():
    start = pd.Timestamp("2026-03-16 22:00", tz="UTC")
    frame = _bb_frame([(0, "rest")] * 800, start="2026-03-16 22:00")
    sleep = [(start, start + pd.Timedelta(minutes=800), 1.0)]
    from_empty = body_battery_timeline(frame, sleep, seed_level=0.0)["level"].iloc[-1]
    from_full = body_battery_timeline(frame, sleep, seed_level=100.0)["level"].iloc[-1]
    assert from_empty == from_full == 100.0


def test_body_battery_timeline_empty():
    empty = pd.DataFrame({"stress": [], "state": []}, index=pd.DatetimeIndex([], name="ts"))
    assert body_battery_timeline(empty).empty


def test_summarize_body_battery_day():
    idx = pd.date_range("2026-03-16 06:00", periods=5, freq="h", tz="UTC")
    tl = pd.DataFrame({"level": [50.0, 60.0, 55.0, 80.0, 70.0]}, index=idx)
    summ = summarize_body_battery_day(tl, wake_ts=idx[1])
    assert summ["high_level"] == 80
    assert summ["low_level"] == 50
    assert summ["wake_level"] == 60  # level at/just before the wake timestamp
    assert summ["charged"] == 35.0  # (+10) + (+25)
    assert summ["drained"] == 15.0  # (-5) + (-10)


def test_summarize_body_battery_day_empty():
    summ = summarize_body_battery_day(pd.DataFrame({"level": []}))
    assert summ["wake_level"] is None and summ["charged"] == 0.0 and summ["low_level"] is None


def test_auto_neutral_percentile_and_clamps():
    # 2000 awake minutes uniformly 0..99: the 40th percentile of the personal
    # distribution (≈39.6) becomes the neutral level.
    value = auto_neutral(_bb_frame([(v % 100, "rest") for v in range(2000)]))
    assert value is not None and 39.0 <= value <= 40.0

    # Floor: an all-zero distribution must not disable awake charging entirely.
    assert auto_neutral(_bb_frame([(0, "rest")] * 1500)) == 5.0
    # Ceiling: a high distribution must not mark half the scale energy-neutral.
    assert auto_neutral(_bb_frame([(90, "high")] * 1500)) == 50.0


def test_auto_neutral_excludes_sleep_minutes():
    # Half the minutes are near-zero sleep stress; unmasked they would drag the
    # percentile to the floor. Only the awake half may calibrate the neutral.
    start = pd.Timestamp("2026-03-16 00:00", tz="UTC")
    frame = _bb_frame([(0, "rest")] * 1500 + [(30, "low")] * 1500, start="2026-03-16 00:00")
    sleep = [(start, start + pd.Timedelta(minutes=1500), 1.0)]
    assert auto_neutral(frame, sleep) == 30.0
    assert auto_neutral(frame) == 5.0  # without the mask: 40th pct 0 → clamped to the floor


def test_auto_neutral_needs_enough_data():
    assert auto_neutral(_bb_frame([(20, "rest")] * 100)) is None
    empty = pd.DataFrame({"stress": [], "state": []}, index=pd.DatetimeIndex([], name="ts"))
    assert auto_neutral(empty) is None


# --- Body Battery (DB end-to-end) ------------------------------------------


def _seed_body_battery_history(db):
    """The stress seed plus a sleep session ending in-window on the last two days."""
    _seed_stress_history(db)
    base = dt.date(2026, 3, 1)
    for d in (38, 39):
        wake_day = base + dt.timedelta(days=d)
        prev = wake_day - dt.timedelta(days=1)
        s_start = dt.datetime(prev.year, prev.month, prev.day, 23, tzinfo=UTC)
        s_end = dt.datetime(wake_day.year, wake_day.month, wake_day.day, 8, 30, tzinfo=UTC)
        db.add(
            SleepSession(
                sleep_start=s_start,
                sleep_end=s_end,
                in_bed_start=s_start,
                in_bed_end=s_end,
                source="",
                sleep_date=wake_day,
                total_sleep_h=9.0,
                in_bed_h=9.5,
            )
        )
    db.flush()


def test_compute_body_battery_writes_tables_and_is_idempotent(db):
    _seed_body_battery_history(db)
    compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=90)
    db.flush()
    cfg = BodyBatteryConfig()
    res1 = compute_body_battery(db, "Europe/Vienna", cfg, since_days=cfg.window_days)
    assert res1.days == 3 and res1.buckets == 1800
    daily = {r.day: r for r in db.execute(select(BodyBatteryDaily)).scalars()}
    assert len(daily) == 3
    for r in daily.values():
        assert 0 <= r.low_level <= r.high_level <= 100
        assert r.charged >= 0 and r.drained >= 0
    # A day whose main sleep ends inside the measured window carries a wake level.
    assert daily[dt.date(2026, 4, 9)].wake_level is not None
    assert db.execute(select(func.count()).select_from(BodyBatteryIntraday)).scalar_one() == 1800

    # Idempotent: a re-run over the same stress timeline replaces the window.
    res2 = compute_body_battery(db, "Europe/Vienna", cfg, since_days=cfg.window_days)
    assert res2.days == 3 and res2.buckets == 1800
    assert db.execute(select(func.count()).select_from(BodyBatteryIntraday)).scalar_one() == 1800
    assert db.execute(select(func.count()).select_from(BodyBatteryDaily)).scalar_one() == 3


def test_run_emits_body_battery_alert_for_drained_day(db):
    # A baseline plus one day of sustained high HR drains the battery to empty.
    base = dt.date(2026, 3, 1)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0))
    day = base + dt.timedelta(days=39)
    for m in range(600):
        ts = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=150.0))
    db.flush()

    result = analysis.run(db)
    assert result.body_battery >= 1
    alert = db.execute(select(Finding).where(Finding.kind == "body_battery")).scalars().first()
    assert alert is not None
    assert alert.metric_a == "body_battery"
    assert alert.ref_date == dt.date(2026, 4, 9)
    # severity is the depletion (100 − low level): higher = worse, like every other kind
    assert alert.severity >= 100 - BodyBatteryConfig().alert_level
    assert alert.details["low_level"] <= BodyBatteryConfig().alert_level


def test_compute_body_battery_windowed_matches_full_history(db):
    # A day's *last* write happens on the run where it is the window's first
    # day; the warm-up margin must make that write identical to the
    # full-history computation instead of restarting from the neutral seed.
    _seed_body_battery_history(db)
    compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=None)
    db.flush()
    cfg = BodyBatteryConfig()

    compute_body_battery(db, "Europe/Vienna", cfg, since_days=None)
    db.flush()
    full_intraday = {r.ts: r.level for r in db.execute(select(BodyBatteryIntraday)).scalars()}
    full_daily = {
        r.day: (r.wake_level, r.high_level, r.low_level) for r in db.execute(select(BodyBatteryDaily)).scalars()
    }

    # Windowed recompute with the last measured day's predecessor as window
    # start — exactly the position where the seed used to land.
    res = compute_body_battery(db, "Europe/Vienna", cfg, since_days=2)
    db.flush()
    assert res.days == 2
    windowed_intraday = {r.ts: r.level for r in db.execute(select(BodyBatteryIntraday)).scalars()}
    windowed_daily = {
        r.day: (r.wake_level, r.high_level, r.low_level) for r in db.execute(select(BodyBatteryDaily)).scalars()
    }
    assert windowed_intraday == full_intraday
    assert windowed_daily == full_daily


def test_compute_body_battery_disabled_writes_nothing(db):
    _seed_body_battery_history(db)
    compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=90)
    db.flush()
    res = compute_body_battery(db, "Europe/Vienna", BodyBatteryConfig(enabled=False), since_days=90)
    assert res.days == 0 and res.buckets == 0
    assert db.execute(select(func.count()).select_from(BodyBatteryDaily)).scalar_one() == 0


def test_body_battery_daily_gated_by_measured_minutes(db):
    # A barely-worn day (30 informative buckets < min_measured_min) keeps its
    # intraday timeline but gets no daily summary row — a gap, not a guess.
    base = dt.date(2026, 3, 1)
    for d in range(40):
        day = base + dt.timedelta(days=d)
        at4 = dt.datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        db.add(MetricSample(time=at4, metric="resting_heart_rate", source="", qty=55.0))
    full_day = base + dt.timedelta(days=38)
    sparse_day = base + dt.timedelta(days=39)
    for m in range(600):
        ts = dt.datetime(full_day.year, full_day.month, full_day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=58.0))
    for m in range(30):
        ts = dt.datetime(sparse_day.year, sparse_day.month, sparse_day.day, 8, tzinfo=UTC) + dt.timedelta(minutes=m)
        db.add(MetricSample(time=ts, metric="heart_rate", source="", vavg=58.0))
    db.flush()

    compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=90)
    db.flush()
    compute_body_battery(db, "Europe/Vienna", BodyBatteryConfig(), since_days=90)
    days = {r.day for r in db.execute(select(BodyBatteryDaily)).scalars()}
    assert full_day in days
    assert sparse_day not in days
    # The sparse day's intraday levels are still stored (630 buckets total).
    assert db.execute(select(func.count()).select_from(BodyBatteryIntraday)).scalar_one() == 630


def test_compute_body_battery_auto_neutral_uses_personal_distribution(db):
    # With `neutral` unset (the default) the run derives the energy-neutral
    # level from the trailing personal stress distribution: it must equal a run
    # explicitly pinned to that derived value, and differ from the historical
    # fixed default — proving the auto path is wired in, not decorative.
    _seed_body_battery_history(db)
    compute_stress(db, "Europe/Vienna", StressConfig(), ProfileConfig(), since_days=90)
    db.flush()

    _start, end, _first = hr_window_bounds(db, "Europe/Vienna", 90)
    derived = _resolve_neutral(db, BodyBatteryConfig(), end)
    assert derived != BODY_BATTERY_NEUTRAL  # this seed's calm distribution sits below the fixed 25

    compute_body_battery(db, "Europe/Vienna", BodyBatteryConfig(), since_days=90)
    db.flush()
    auto_rows = {r.ts: r.level for r in db.execute(select(BodyBatteryIntraday)).scalars()}

    compute_body_battery(db, "Europe/Vienna", BodyBatteryConfig(neutral=derived), since_days=90)
    db.flush()
    pinned_rows = {r.ts: r.level for r in db.execute(select(BodyBatteryIntraday)).scalars()}
    assert pinned_rows == auto_rows

    compute_body_battery(db, "Europe/Vienna", BodyBatteryConfig(neutral=BODY_BATTERY_NEUTRAL), since_days=90)
    db.flush()
    fixed_rows = {r.ts: r.level for r in db.execute(select(BodyBatteryIntraday)).scalars()}
    assert fixed_rows != auto_rows


def test_run_refresh_recomputes_last_two_days(db):
    # The hourly intraday refresh recomputes only the trailing two local days of
    # stress + Body Battery (no findings) so today's timeline stays current.
    _seed_body_battery_history(db)
    run_refresh(db, "Europe/Vienna", AppConfig())
    stress_days = {r.day for r in db.execute(select(StressDaily)).scalars()}
    bb_days = {r.day for r in db.execute(select(BodyBatteryDaily)).scalars()}
    assert stress_days == {dt.date(2026, 4, 8), dt.date(2026, 4, 9)}
    assert bb_days == {dt.date(2026, 4, 8), dt.date(2026, 4, 9)}
