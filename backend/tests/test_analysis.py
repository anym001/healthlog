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
    acute_chronic_ratio,
    aggregate_workout_daily,
    annual_seasonality,
    banister_trimp,
    circular_bedtime_offset,
    decompose,
    fdr_adjust,
    fill_zero_within_span,
    resolve_hr_max,
    resolve_hr_rest,
    rolling_mad_anomalies,
    spearman_lag,
    trend_slope,
)
from app.appconfig import AppConfig, ProfileConfig, WorkoutConfig
from app.models import Finding, MetricSample, SleepSession, Workout

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
    energy = 0.04 * steps + rng.normal(scale=5.0, size=60)  # strongly correlated, same day
    _add_metric(db, "step_count", steps)
    _add_metric(db, "active_energy", energy)
    db.flush()

    result = analysis.run(db)
    assert isinstance(result, AnalysisResult)
    assert result.correlations >= 1

    pair = (
        db.execute(
            select(Finding).where(
                Finding.kind == "correlation",
                Finding.metric_a.in_(["step_count", "active_energy"]),
                Finding.metric_b.in_(["step_count", "active_energy"]),
            )
        )
        .scalars()
        .all()
    )
    assert pair, "expected a step_count<->active_energy correlation"
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


# --- Workout series: DB end-to-end -----------------------------------------


def _add_workout(db, start: dt.datetime, *, duration_s, avg_hr, max_hr, energy):
    db.add(
        Workout(
            hae_id=uuid.uuid4(),
            start_time=start,
            end_time=start + dt.timedelta(seconds=duration_s),
            name="Outdoor Run",
            duration_s=float(duration_s),
            active_energy_kcal=float(energy),
            avg_hr=float(avg_hr),
            max_hr=float(max_hr),
            source="",
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
