"""Phase 3 analysis pipeline: pure math on synthetic series + DB end-to-end."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app import analysis
from app.analysis import (
    AnalysisResult,
    annual_seasonality,
    circular_bedtime_offset,
    decompose,
    fdr_adjust,
    rolling_mad_anomalies,
    spearman_lag,
    trend_slope,
)
from app.models import Finding, MetricSample, SleepSession

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
