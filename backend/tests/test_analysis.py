"""Phase 3 analysis pipeline: pure math on synthetic series + DB end-to-end."""

from __future__ import annotations

import datetime as dt
import uuid

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app import analysis
from app.analysis import (
    AnalysisResult,
    _hr_zone_weight,
    acute_chronic_ratio,
    aggregate_workout_daily,
    annual_seasonality,
    banister_trimp,
    circular_bedtime_offset,
    decompose,
    edwards_trimp,
    fdr_adjust,
    fill_zero_within_span,
    resolve_hr_max,
    resolve_hr_rest,
    rolling_mad_anomalies,
    spearman_lag,
    trend_slope,
)
from app.appconfig import AnalysisConfig, AppConfig, ProfileConfig, WorkoutConfig
from app.models import Finding, MetricSample, SleepSession, Workout, WorkoutHrSample
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
