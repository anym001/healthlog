"""Pure analysis math (no DB, no I/O) — unit-tested with synthetic series.

The helpers here take pandas/numpy objects and return numbers or frames; they
hold no database or config state, so they are tested directly against synthetic
series with a fixed seed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.seasonal import MSTL, STL

from ..appconfig import ProfileConfig
from .constants import (
    ACWR_ACUTE_DAYS,
    ACWR_CHRONIC_DAYS,
    ANOMALY_THRESHOLD,
    ANOMALY_WINDOW,
    ATL_DAYS,
    BODY_BATTERY_ACTIVE_DRAIN_RATE,
    BODY_BATTERY_CHARGE_RATE,
    BODY_BATTERY_DRAIN_RATE,
    BODY_BATTERY_NEUTRAL,
    BODY_BATTERY_NEUTRAL_CEIL,
    BODY_BATTERY_NEUTRAL_FLOOR,
    BODY_BATTERY_NEUTRAL_MIN_MINUTES,
    BODY_BATTERY_NEUTRAL_PERCENTILE,
    BODY_BATTERY_SEED_LEVEL,
    BODY_BATTERY_SLEEP_CHARGE_RATE,
    CTL_DAYS,
    CTL_TREND_LOOKBACK_DAYS,
    FDR_ALPHA,
    HR_MAX_DATA_CEIL,
    HR_MAX_DATA_FLOOR,
    HR_MAX_FALLBACK,
    HR_REST_FALLBACK,
    HR_REST_MIN_PERIODS,
    HR_REST_WINDOW,
    MIN_OVERLAP,
    SEASONAL_MIN_PEAK_TROUGH_GAP,
    SEASONAL_MIN_SHARED_MONTHS,
    SEASONAL_PERIOD,
    STRESS_ACTIVE_STEPS_PER_MIN,
    STRESS_BUCKET_MINUTES,
    STRESS_GAP_CAP_MINUTES,
    STRESS_HRV_WEIGHT,
    STRESS_RESERVE_FULL,
    STRESS_ZONE_HIGH,
    STRESS_ZONE_LOW,
    STRESS_ZONE_MEDIUM,
    WEEK_PERIOD,
    log,
)


@dataclass
class Corr:
    coef: float
    p: float
    n: int
    start: dt.date
    end: dt.date


def _mad(x: np.ndarray) -> float:
    """Median absolute deviation."""
    return float(np.median(np.abs(x - np.median(x))))


def spearman_lag(
    a: pd.Series,
    b: pd.Series,
    lag: int,
    *,
    min_overlap: int = MIN_OVERLAP,
    min_active: int = 0,
) -> Corr | None:
    """Spearman correlation of ``a[t]`` with ``b[t + lag]`` on common days.

    Both series must be on a complete daily index so the positional shift is
    calendar-correct. Returns None when overlap is too small or a series is
    constant over the overlap. With ``min_active`` set, also returns None unless
    *each* series has at least that many non-zero days in the overlap — the
    effective sample size for 0-filled sparse series (per-sport workout load),
    where the grid length overstates how much real co-variation there is.
    """
    if lag < 0:
        return None
    paired = pd.concat([a, b.shift(-lag)], axis=1, join="inner").dropna()
    n = len(paired)
    if n < min_overlap:
        return None
    x = paired.iloc[:, 0].to_numpy()
    y = paired.iloc[:, 1].to_numpy()
    if np.std(x) == 0 or np.std(y) == 0:
        return None
    if min_active and (int(np.count_nonzero(x)) < min_active or int(np.count_nonzero(y)) < min_active):
        return None
    res = stats.spearmanr(x, y)
    coef, p = float(res.statistic), float(res.pvalue)
    if np.isnan(coef) or np.isnan(p):
        return None
    return Corr(coef=coef, p=p, n=n, start=paired.index.min().date(), end=paired.index.max().date())


def fdr_adjust(pvalues: list[float], *, alpha: float = FDR_ALPHA) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (empty in -> empty out)."""
    if not pvalues:
        return []
    _, p_adj, _, _ = multipletests(pvalues, alpha=alpha, method="fdr_bh")
    return [float(p) for p in p_adj]


def robust_z(s: pd.Series, window: int = ANOMALY_WINDOW) -> pd.Series:
    """Robust z-score of each day vs the *preceding* ``window`` days.

    Uses the median + MAD of the prior window (the current day excluded) so a
    spike cannot mask itself. NaN where the baseline is undefined.
    """
    prior = s.shift(1)
    med = prior.rolling(window, min_periods=window).median()
    mad = prior.rolling(window, min_periods=window).apply(_mad, raw=True)
    mad = mad.replace(0.0, np.nan)
    return 0.6745 * (s - med) / mad


def rolling_mad_anomalies(s: pd.Series, window: int = ANOMALY_WINDOW, threshold: float = ANOMALY_THRESHOLD):
    """Return a DataFrame(date, value, z) for days whose robust z exceeds threshold."""
    s = s.dropna()
    z = robust_z(s, window)
    flagged = z[z.abs() > threshold]
    return pd.DataFrame(
        {"value": s.reindex(flagged.index).to_numpy(), "z": flagged.to_numpy()},
        index=flagged.index,
    )


def _global_robust_z(s: pd.Series, value: float) -> float | None:
    """Robust z of ``value`` against the series' *entire* history (median + MAD).

    The trailing-window z (``robust_z``) asks "is today unusual versus the last
    few weeks"; this asks "is it unusual versus everything we've seen". It is the
    corroboration view for anomalies: a day inflated only because the recent
    window was unusually calm (e.g. a normal hard workout after a taper) is
    modest here, while a genuine extreme is large in both. ``None`` when the
    global MAD is zero (a constant series offers no scale to judge against).
    """
    arr = s.to_numpy()
    scale = _mad(arr)
    if scale == 0:
        return None
    return 0.6745 * (value - float(np.median(arr))) / scale


@dataclass
class Decomp:
    trend: pd.Series
    resid: pd.Series
    seasonal: dict[int, pd.Series] = field(default_factory=dict)
    has_annual: bool = False


def _daily_grid(s: pd.Series) -> pd.DatetimeIndex:
    """Complete daily DatetimeIndex spanning the series' first..last date."""
    return pd.date_range(s.index.min(), s.index.max(), freq="D")


def _prepare_series(s: pd.Series) -> pd.Series:
    """Complete daily index with interior gaps interpolated (edges dropped).

    Decomposition cannot handle NaN; correlation does NOT use this (it keeps
    real paired observations only).
    """
    s = s.dropna()
    if s.empty:
        return s
    return s.reindex(_daily_grid(s)).interpolate(method="time", limit_area="inside").dropna()


def decompose(s: pd.Series) -> Decomp | None:
    """STL/MSTL decomposition. MSTL(7, 365) once there are >= 2 years; else STL(7)."""
    prepared = _prepare_series(s)
    n = len(prepared)
    if n < 2 * WEEK_PERIOD + 1:
        return None
    if n >= 2 * SEASONAL_PERIOD:
        res = MSTL(prepared, periods=(WEEK_PERIOD, SEASONAL_PERIOD)).fit()
        seasonal = {
            WEEK_PERIOD: res.seasonal[f"seasonal_{WEEK_PERIOD}"],
            SEASONAL_PERIOD: res.seasonal[f"seasonal_{SEASONAL_PERIOD}"],
        }
        return Decomp(trend=res.trend, resid=res.resid, seasonal=seasonal, has_annual=True)
    res = STL(prepared, period=WEEK_PERIOD, robust=True).fit()
    return Decomp(trend=res.trend, resid=res.resid, seasonal={WEEK_PERIOD: res.seasonal}, has_annual=False)


def _component_strength(component: pd.Series, resid: pd.Series) -> float:
    """Trend/seasonal strength after Wang/Hyndman: 1 - Var(R) / Var(C + R)."""
    cr = (component + resid).to_numpy()
    denom = float(np.var(cr))
    if denom == 0:
        return 0.0
    return float(max(0.0, 1.0 - np.var(resid.to_numpy()) / denom))


def trend_slope(trend: pd.Series) -> float:
    """Least-squares slope of the trend component, per day."""
    y = trend.to_numpy()
    x = np.arange(len(y))
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def trend_monotonicity(trend: pd.Series) -> float | None:
    """How consistently the (smooth) trend component moves in one direction.

    ``_component_strength`` only measures that the trend is smooth relative to
    the residual; it cannot tell a genuine directional drift from a smooth
    meander that wanders up then back. This is ``|Spearman(trend, time)|``: ~1 for
    a steady climb/decline, ~0 for a meander. It is the directional corroboration
    view for trends, mirroring the other kinds' second-view guards. ``None`` when
    the trend is constant (no direction to judge).
    """
    y = trend.dropna().to_numpy()
    if len(y) < 2 or np.std(y) == 0:
        return None
    rho = stats.spearmanr(y, np.arange(len(y))).statistic
    return None if np.isnan(rho) else abs(float(rho))


def _seasonal_reproducibility(season: pd.Series) -> float | None:
    """Year-over-year stability of the annual seasonal *shape*.

    A genuine annual cycle repeats its month-by-month profile from one calendar
    year to the next; an annual component MSTL has overfit to a one-off cluster
    (typical of sparse or derived metrics) does not. Returns the mean Spearman
    correlation between every pair of years' monthly seasonal profiles, or
    ``None`` if fewer than two years share enough months to compare.

    This is the corroboration guard for seasonality, mirroring the raw-
    corroboration guard for correlations (ARCHITECTURE.md §4.8): a single STL run
    always fits *some* seasonal, so a high in-sample strength is not enough — the
    pattern must recur across years to be trusted.
    """
    by_year_month = season.groupby([season.index.year, season.index.month]).mean()
    table = by_year_month.unstack(0)  # rows = month (1..12), columns = calendar year
    years = list(table.columns)
    if len(years) < 2:
        return None
    cors: list[float] = []
    for i in range(len(years)):
        for j in range(i + 1, len(years)):
            pair = pd.concat([table[years[i]], table[years[j]]], axis=1).dropna()
            if len(pair) >= SEASONAL_MIN_SHARED_MONTHS:
                cors.append(float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")))
    if not cors:
        return None
    return float(np.mean(cors))


def annual_seasonality(decomp: Decomp) -> dict | None:
    """Amplitude, peak/trough month, strength and reproducibility of the annual
    component."""
    if not decomp.has_annual or SEASONAL_PERIOD not in decomp.seasonal:
        return None
    season = decomp.seasonal[SEASONAL_PERIOD]
    strength = _component_strength(season, decomp.resid)
    by_month = season.groupby(season.index.month).mean()
    peak_month = int(by_month.idxmax())
    trough_month = int(by_month.idxmin())
    # A real annual cycle peaks and troughs ~6 months apart; when they land in
    # near-adjacent months the amplitude may be real but the phase is not
    # trustworthy (often a sparse/noisy series), so flag it.
    gap = abs(peak_month - trough_month) % 12
    gap = min(gap, 12 - gap)
    return {
        "strength": strength,
        "reproducibility": _seasonal_reproducibility(season),
        "amplitude": float(season.max() - season.min()),
        "peak_month": peak_month,
        "trough_month": trough_month,
        "phase_confident": gap >= SEASONAL_MIN_PEAK_TROUGH_GAP,
    }


def circular_bedtime_offset(local_hours: pd.Series) -> pd.Series:
    """Map clock hours to "hours since 18:00" so typical bedtimes don't wrap
    across midnight (22:00->4, 00:30->6.5, 02:00->8). Variability is then a
    plain std."""
    return (local_hours - 18.0) % 24.0


# --- Workout training load (Banister TRIMP) --------------------------------


def banister_trimp(
    duration_s: float | None,
    avg_hr: float | None,
    hr_rest: float,
    hr_max: float,
    sex: str = "unspecified",
) -> float:
    """Banister TRIMP for one session from a single average heart rate.

    ``TRIMP = minutes * HRr * y`` with ``HRr`` the heart-rate reserve fraction
    and ``y`` a sex-specific exponential weight (Banister 1991). Returns 0.0 for
    a session without a usable ``avg_hr`` or duration — those carry no HR-based
    load (the kcal fallback series covers them). ``sex`` other than ``female``
    uses the male weighting (the documented default).
    """
    if duration_s is None or avg_hr is None or hr_max <= hr_rest:
        return 0.0
    if not np.isfinite(duration_s) or not np.isfinite(avg_hr) or duration_s <= 0:
        return 0.0
    minutes = duration_s / 60.0
    hrr = (avg_hr - hr_rest) / (hr_max - hr_rest)
    hrr = float(min(1.0, max(0.0, hrr)))
    if sex == "female":
        weight = 0.86 * np.exp(1.67 * hrr)
    else:
        weight = 0.64 * np.exp(1.92 * hrr)
    return float(minutes * hrr * weight)


# Zone-based (Edwards) TRIMP. Five intensity zones by % of HR_max with linear
# weights 1..5; below the zone-1 floor the time carries no load (weight 0).
# Boundaries are fractions of HR_max — derived per run, never frozen at ingest.
EDWARDS_ZONE_LOWER = (0.5, 0.6, 0.7, 0.8, 0.9)  # zone floors as a fraction of HR_max


def _hr_zone_weight(bpm: float, hr_max: float) -> int:
    """Edwards zone weight (0..5) for a heart rate, by % of HR_max."""
    if hr_max <= 0:
        return 0
    frac = bpm / hr_max
    return sum(1 for lower in EDWARDS_ZONE_LOWER if frac >= lower)


def edwards_trimp(samples: pd.DataFrame | None, hr_max: float, duration_s: float | None = None) -> float:
    """Edwards (zone-based) TRIMP for one session from its intra-workout HR series.

    ``Edwards TRIMP = Σ minutes_in_zone · zone_weight`` over the five zones. Each
    consecutive pair of samples defines an interval whose time is attributed to
    the zone of its starting heart rate. When ``duration_s`` is given the
    interval times are rescaled to sum to it, so recording gaps don't distort
    the total (and Edwards minutes line up with the Banister session minutes).

    Returns 0.0 for a session without a usable series (fewer than two timed
    samples, or no HR_max) — those carry no zone-based load (Banister still
    covers them via ``avg_hr``). Unlike Banister it resolves intervals: a 4×4
    session and a steady run with the same average HR get different loads.
    """
    if samples is None or hr_max <= 0 or len(samples) < 2:
        return 0.0
    s = samples.dropna(subset=["ts", "bpm"]).sort_values("ts")
    if len(s) < 2:
        return 0.0
    ts = pd.to_datetime(s["ts"])
    bpm = s["bpm"].to_numpy(dtype="float64")
    # Seconds per interval (sample k -> k+1); via pandas so tz-aware timestamps
    # work (their numpy form is object dtype, which np.diff cannot subtract).
    deltas = np.clip(ts.diff().dt.total_seconds().to_numpy()[1:], 0.0, None)
    total = float(deltas.sum())
    if total <= 0:
        return 0.0
    scale = (duration_s / total) if (duration_s and duration_s > 0) else 1.0
    load = 0.0
    for delta, hr in zip(deltas, bpm[:-1], strict=True):
        weight = _hr_zone_weight(float(hr), hr_max)
        if weight:
            load += (delta * scale / 60.0) * weight
    return float(load)


def resolve_hr_max(profile: ProfileConfig, observed_max_hr: pd.Series | None) -> float:
    """HR_max along the fallback chain (profile override -> Tanaka age formula ->
    data-driven -> constant). Always returns a usable number."""
    if profile.hr_max is not None:
        return float(profile.hr_max)
    if profile.birth_year is not None:
        age = dt.date.today().year - profile.birth_year
        return float(208.0 - 0.7 * age)  # Tanaka (more accurate than 220 - age)
    if observed_max_hr is not None:
        peak = observed_max_hr.dropna()
        if not peak.empty:
            return float(min(HR_MAX_DATA_CEIL, max(HR_MAX_DATA_FLOOR, float(peak.max()))))
    return HR_MAX_FALLBACK


def resolve_hr_rest(rhr: pd.Series | None, profile: ProfileConfig) -> tuple[pd.Series, float]:
    """A per-day resting-HR series (trailing median, personalised, time-varying)
    plus a scalar fallback for days outside the measured span.

    The fallback is the profile override, else the overall measured median, else
    a constant — so a workout day always resolves to *some* HR_rest.
    """
    if profile.hr_rest is not None:
        default = float(profile.hr_rest)
    elif rhr is not None and not rhr.dropna().empty:
        default = float(rhr.dropna().median())
    else:
        default = HR_REST_FALLBACK
    if rhr is None or rhr.dropna().empty:
        return pd.Series(dtype="float64"), default
    rolling = rhr.rolling(HR_REST_WINDOW, min_periods=HR_REST_MIN_PERIODS).median()
    return rolling.fillna(default), default


def fill_zero_within_span(s: pd.Series) -> pd.Series:
    """Reindex to a complete daily grid over the observed span, filling gaps with
    0.0 (not interpolated): a day without a workout is a real zero of load, not a
    missing measurement. Edges outside the first/last observed day stay absent."""
    s = s.dropna()
    if s.empty:
        return s
    return s.reindex(_daily_grid(s)).fillna(0.0)


def aggregate_workout_daily(
    sessions: pd.DataFrame, hr_rest: pd.Series, hr_rest_default: float, hr_max: float, sex: str
) -> pd.DataFrame:
    """Per-local-day workout features from a per-session frame.

    ``sessions`` columns: ``day`` (Timestamp), ``duration_s``, ``active_energy_kcal``,
    ``avg_hr``, ``max_hr``, ``intensity`` and an optional precomputed ``edwards``
    (zone-based TRIMP per session). Returns a frame indexed by day with ``trimp``
    (Banister sum), ``load`` (active-energy sum), ``edwards`` (zone-based sum, 0.0
    when no series), ``duration_h``, ``count`` and ``intensity`` (mean). Empty in
    -> empty out.
    """
    if sessions.empty:
        return pd.DataFrame()
    rows = []
    degenerate_hr = 0
    for r in sessions.itertuples(index=False):
        day = r.day
        rest = float(hr_rest.get(day, hr_rest_default)) if len(hr_rest) else hr_rest_default
        if r.avg_hr is not None and hr_max <= rest:
            degenerate_hr += 1  # banister_trimp silently yields 0.0 for these
        trimp = banister_trimp(r.duration_s, r.avg_hr, rest, hr_max, sex)
        edwards = float(getattr(r, "edwards", 0.0) or 0.0)
        rows.append(
            {
                "day": day,
                "trimp": trimp,
                "load": float(r.active_energy_kcal) if r.active_energy_kcal is not None else 0.0,
                "edwards": edwards,
                "duration_h": (float(r.duration_s) / 3600.0) if r.duration_s is not None else 0.0,
                "count": 1,
                "intensity": r.intensity,
            }
        )
    if degenerate_hr:
        # A configured hr_rest above the (data-driven) hr_max slips past the
        # profile validator; surface it instead of masking it as zero load.
        log.warning(
            "TRIMP is 0 for %d session(s): resolved hr_max (%.0f) <= hr_rest — "
            "check profile.hr_max/hr_rest against the measured data",
            degenerate_hr,
            hr_max,
        )
    df = pd.DataFrame.from_records(rows).set_index("day")
    return df.groupby(level=0).agg(
        trimp=("trimp", "sum"),
        load=("load", "sum"),
        edwards=("edwards", "sum"),
        duration_h=("duration_h", "sum"),
        count=("count", "sum"),
        intensity=("intensity", "mean"),
    )


# --- Stress proxy -----------------------------------------------------------
# All 0-100. Intraday score from the heart-rate elevation above the personal
# resting baseline (workouts excluded), optionally modulated by HRV. See
# docs/ARCHITECTURE.md §4.9 for the model and its RR-interval caveat.

STRESS_STATES: tuple[str, ...] = ("rest", "low", "medium", "high", "active", "unmeasurable")
MEASURED_STRESS_STATES: frozenset[str] = frozenset({"rest", "low", "medium", "high"})


def stress_state(stress: float, zone_low: float, zone_medium: float, zone_high: float) -> str:
    """Garmin-style zone label for a 0-100 stress value."""
    if stress < zone_low:
        return "rest"
    if stress < zone_medium:
        return "low"
    if stress < zone_high:
        return "medium"
    return "high"


def _in_intervals(index: pd.DatetimeIndex, intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> np.ndarray:
    """Boolean mask of index positions inside any half-open ``[start, end)`` interval.

    Overlapping intervals are merged first so a single ``searchsorted`` pass is
    valid (vectorised replacement for a per-timestamp linear interval scan).
    """
    mask = np.zeros(len(index), dtype=bool)
    if not intervals or len(index) == 0:
        return mask
    merged: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    starts = pd.DatetimeIndex([s for s, _ in merged])
    ends = pd.DatetimeIndex([e for _, e in merged])
    pos = starts.searchsorted(index, side="right") - 1
    valid = pos >= 0
    mask[valid] = index[valid] < ends[pos[valid]]
    return mask


def stress_intraday_from_hr(
    hr: pd.Series,
    hr_rest_day: float,
    hr_max: float,
    workout_intervals: list[tuple[pd.Timestamp, pd.Timestamp]] | None = None,
    hrv_z: float | None = None,
    *,
    reserve_full: float = STRESS_RESERVE_FULL,
    hrv_weight: float = STRESS_HRV_WEIGHT,
    zone_low: float = STRESS_ZONE_LOW,
    zone_medium: float = STRESS_ZONE_MEDIUM,
    zone_high: float = STRESS_ZONE_HIGH,
    steps: pd.Series | None = None,
    active_steps_per_min: float = STRESS_ACTIVE_STEPS_PER_MIN,
) -> pd.DataFrame:
    """Per-bucket stress (0-100) + state from a day's heart-rate buckets.

    ``hr`` is the day's representative HR per bucket (index = bucket time, values
    bpm). Stress scales the heart-rate reserve above the personal resting
    baseline: ``HRr = clamp((hr - hr_rest) / (reserve_full·(hr_max - hr_rest)),
    0, 1)`` → ``100·HRr``. A low-HRV day (negative ``hrv_z``) multiplies the
    score up (Stufe 3); the multiplier is clamped to ``[1 - hrv_weight,
    1 + hrv_weight]``. Buckets inside a workout interval are ``state="active"``
    with NULL stress (Garmin's grey band); with ``steps`` given (per-bucket step
    counts on the same cadence) a bucket at/above ``active_steps_per_min`` is
    likewise ``"active"`` — everyday movement (a brisk walk, stairs) elevates HR
    without being psychological stress, mirroring Garmin's accelerometer gating.
    Buckets with no usable HR or a degenerate reserve are ``"unmeasurable"``.
    Returns a frame indexed by ``ts`` with ``stress`` (int or None), ``hr``,
    ``state``; empty in → empty out. Vectorised — no per-bucket Python loop.
    """
    index = hr.index
    bpm = hr.to_numpy(dtype="float64", na_value=np.nan)
    modifier = 1.0
    if hrv_z is not None and hrv_weight > 0:
        modifier = float(np.clip(1.0 - hrv_weight * hrv_z, 1.0 - hrv_weight, 1.0 + hrv_weight))

    active = _in_intervals(index, workout_intervals or [])
    if steps is not None and active_steps_per_min > 0 and not steps.empty:
        bucket_steps = steps.reindex(index).to_numpy(dtype="float64", na_value=np.nan)
        with np.errstate(invalid="ignore"):
            active |= bucket_steps >= active_steps_per_min

    reserve = hr_max - hr_rest_day
    measured = (~np.isnan(bpm) & ~active) if reserve > 0 else np.zeros(len(index), dtype=bool)
    with np.errstate(invalid="ignore", divide="ignore"):
        hrr = np.clip((bpm - hr_rest_day) / (reserve_full * reserve) if reserve > 0 else bpm * np.nan, 0.0, 1.0)
        stress = np.clip(100.0 * hrr * modifier, 0.0, 100.0)
        zones = np.select(
            [stress < zone_low, stress < zone_medium, stress < zone_high],
            ["rest", "low", "medium"],
            default="high",
        )

    states = np.full(len(index), "unmeasurable", dtype=object)
    states[active] = "active"
    states[measured] = zones[measured]

    stress_col = np.full(len(index), None, dtype=object)
    stress_col[measured] = [int(v) for v in np.rint(stress[measured])]

    return pd.DataFrame({"stress": stress_col, "hr": bpm, "state": states}, index=index.rename("ts"))


def _bucket_dwell(index: pd.DatetimeIndex, gap_cap_minutes: float) -> tuple[np.ndarray, np.ndarray]:
    """Dwell minutes per bucket (time to the next reading, capped) + the excess.

    The last bucket gets the nominal cadence; a silence longer than the cap is a
    measurement gap — the excess is returned separately (unmeasurable time).
    """
    gaps = index.to_series().diff().shift(-1).dt.total_seconds().to_numpy() / 60.0
    gaps = np.maximum(gaps, 0.0)  # NaN (last bucket) propagates
    dwell = np.where(np.isnan(gaps), STRESS_BUCKET_MINUTES, np.minimum(gaps, gap_cap_minutes))
    extra = np.where(np.isnan(gaps), 0.0, np.maximum(gaps - gap_cap_minutes, 0.0))
    return dwell, extra


def summarize_stress_day(intraday: pd.DataFrame, gap_cap_minutes: float = STRESS_GAP_CAP_MINUTES) -> dict:
    """Aggregate a day's per-bucket stress frame into a summary dict.

    Each reading covers the time until the next one (capped at
    ``gap_cap_minutes``; the excess is unmeasurable), so minutes-in-zone weight
    each bucket by its dwell — mirroring the interval attribution in
    :func:`edwards_trimp`. ``score`` is the dwell-weighted mean stress over the
    measured (non-active, non-gap) minutes, or ``None`` when nothing was
    measured. Returns ``score`` and ``{rest,low,medium,high,active,unmeasurable,
    measured}_min`` (integer minutes). Empty in → all-zero, score ``None``.
    """
    empty = {f"{s}_min": 0 for s in STRESS_STATES} | {"measured_min": 0, "score": None}
    if intraday.empty:
        return empty

    df = intraday.sort_index()
    dwell, extra = _bucket_dwell(df.index, gap_cap_minutes)
    states = df["state"].to_numpy()
    stresses = pd.to_numeric(df["stress"], errors="coerce").to_numpy(dtype="float64")

    minutes = {s: float(dwell[states == s].sum()) for s in STRESS_STATES}
    minutes["unmeasurable"] += float(extra.sum())
    scored = np.isin(states, list(MEASURED_STRESS_STATES)) & ~np.isnan(stresses)
    weight = float(dwell[scored].sum())
    weighted_sum = float((stresses[scored] * dwell[scored]).sum())

    measured = sum(minutes[s] for s in MEASURED_STRESS_STATES)
    return {
        "score": round(weighted_sum / weight, 1) if weight > 0 else None,
        "rest_min": int(round(minutes["rest"])),
        "low_min": int(round(minutes["low"])),
        "medium_min": int(round(minutes["medium"])),
        "high_min": int(round(minutes["high"])),
        "active_min": int(round(minutes["active"])),
        "unmeasurable_min": int(round(minutes["unmeasurable"])),
        "measured_min": int(round(measured)),
    }


# --- Body Battery -----------------------------------------------------------
# A Garmin-style 0-100 energy reserve: the stress timeline integrated against
# recovery. See docs/ARCHITECTURE.md §4.10 for the model, its self-correcting
# sleep re-anchor, and the (shared with stress) RR-interval proxy caveat.


def auto_neutral(
    intraday: pd.DataFrame,
    sleep_intervals: list[tuple[pd.Timestamp, pd.Timestamp, float]] | None = None,
    *,
    percentile: float = BODY_BATTERY_NEUTRAL_PERCENTILE,
    min_minutes: int = BODY_BATTERY_NEUTRAL_MIN_MINUTES,
) -> float | None:
    """Derive the energy-neutral stress level from the personal distribution.

    The stress score is relative to the personal resting baseline, so a fixed
    neutral threshold sits wrong for most people — a calm baseline keeps every
    day below it and the battery pins at 100 (and vice versa). Instead, take
    ``percentile`` of the *measured awake* stress values in ``intraday`` (the
    trailing stress timeline): "your typical calm waking level". Sleep buckets
    are excluded (their near-zero stress would drag the percentile down);
    active/unmeasurable buckets carry no stress and drop out on their own. The
    result is clamped to ``[BODY_BATTERY_NEUTRAL_FLOOR,
    BODY_BATTERY_NEUTRAL_CEIL]`` so a degenerate distribution can never disable
    charging or drain half the scale. Returns ``None`` with fewer than
    ``min_minutes`` usable values (too little history to calibrate — the caller
    falls back to the fixed default).
    """
    if intraday.empty:
        return None
    stresses = pd.to_numeric(intraday["stress"], errors="coerce").to_numpy(dtype="float64")
    asleep = _in_intervals(intraday.index, [(s, e) for s, e, _eff in sleep_intervals or []])
    values = stresses[~np.isnan(stresses) & ~asleep]
    if len(values) < min_minutes:
        return None
    value = round(float(np.percentile(values, percentile)), 1)
    return float(np.clip(value, BODY_BATTERY_NEUTRAL_FLOOR, BODY_BATTERY_NEUTRAL_CEIL))


def body_battery_timeline(
    intraday: pd.DataFrame,
    sleep_intervals: list[tuple[pd.Timestamp, pd.Timestamp, float]] | None = None,
    *,
    neutral: float = BODY_BATTERY_NEUTRAL,
    charge_rate: float = BODY_BATTERY_CHARGE_RATE,
    drain_rate: float = BODY_BATTERY_DRAIN_RATE,
    sleep_charge_rate: float = BODY_BATTERY_SLEEP_CHARGE_RATE,
    active_drain_rate: float = BODY_BATTERY_ACTIVE_DRAIN_RATE,
    seed_level: float = BODY_BATTERY_SEED_LEVEL,
    gap_cap_minutes: float = STRESS_GAP_CAP_MINUTES,
) -> pd.DataFrame:
    """Integrate a stress timeline into a 0-100 Body-Battery ``level`` per bucket.

    ``intraday`` is the per-bucket stress frame (index = bucket time, columns
    ``stress`` 0-100/None and ``state`` rest/low/medium/high/active/unmeasurable),
    as stored in ``stress_intraday``. ``sleep_intervals`` are ``(start, end,
    efficiency)`` tuples. Each bucket contributes a balance *rate* (points/min),
    weighted by its dwell (time to the next bucket, capped at ``gap_cap_minutes``
    — the excess is a measurement gap that holds the level):

    - inside a sleep interval → ``+sleep_charge_rate·efficiency`` (the nightly
      re-anchor: a full night pushes the battery toward 100, clamped);
    - ``state="active"`` (workout) → ``−active_drain_rate``;
    - ``state="unmeasurable"`` / no stress → ``0`` (hold, invent nothing);
    - otherwise a measured awake bucket → drain above ``neutral`` stress
      (``−drain_rate·(stress−neutral)/(100−neutral)``), charge at/below it
      (``+charge_rate·(neutral−stress)/neutral``).

    Integrated as ``level(t) = clamp(level(t−dwell) + rate·dwell, 0, 100)`` from a
    neutral ``seed_level`` at the first bucket; the seed washes out within days as
    sleep re-anchors the battery, so the result is deterministic without any
    cross-run carry-over. Returns a frame indexed by ``ts`` with a float
    ``level``; empty in → empty out. Run once over the whole window (the
    integrator is continuous across day boundaries), then slice per day.
    """
    if intraday.empty:
        return pd.DataFrame({"level": pd.Series(dtype="float64")}, index=intraday.index[:0])

    df = intraday.sort_index()
    dwell, _extra = _bucket_dwell(df.index, gap_cap_minutes)
    states = df["state"].to_numpy()
    stresses = pd.to_numeric(df["stress"], errors="coerce").to_numpy(dtype="float64")

    # Sleep-efficiency per bucket; NaN = awake. First matching interval wins
    # (list order = sorted by start), mirroring the old per-bucket linear scan.
    eff = np.full(len(df), np.nan)
    for s, e, f in sleep_intervals or []:
        sel = (df.index >= s) & (df.index < e) & np.isnan(eff)
        eff[sel] = f if f and f > 0 else 1.0
    asleep = ~np.isnan(eff)

    # Balance rate per bucket (points/min), priority: sleep > active > hold.
    denom_hi = 100.0 - neutral
    with np.errstate(invalid="ignore"):
        over = (stresses - neutral) / denom_hi if denom_hi > 0 else np.ones_like(stresses)
        under = (neutral - stresses) / neutral if neutral > 0 else np.zeros_like(stresses)
        awake_rate = np.where(stresses > neutral, -drain_rate * over, charge_rate * under)
        rate = np.select(
            [asleep, states == "active", (states == "unmeasurable") | np.isnan(stresses)],
            [sleep_charge_rate * np.where(asleep, eff, 0.0), -active_drain_rate, 0.0],
            default=awake_rate,
        )

    # The clamp makes the recurrence non-linear, so the accumulation itself
    # stays a (tight, numpy-fed) loop.
    step = rate * dwell
    level = float(np.clip(seed_level, 0.0, 100.0))
    levels = np.empty(len(df))
    for i, delta in enumerate(step):
        level += delta
        if level < 0.0:
            level = 0.0
        elif level > 100.0:
            level = 100.0
        levels[i] = level

    return pd.DataFrame({"level": levels}, index=df.index)


def summarize_body_battery_day(timeline: pd.DataFrame, wake_ts: pd.Timestamp | None = None) -> dict:
    """Aggregate a day's Body-Battery level series into a summary dict.

    ``timeline`` is the day's slice of :func:`body_battery_timeline` (index =
    bucket time, column ``level``). ``wake_ts`` is the end of the day's main sleep
    (the longest sleep interval ending that day); the level at/just before it is
    ``wake_level`` — what you started the day with. Returns ``wake_level``,
    ``high_level``, ``low_level`` (0-100 ints or ``None``) plus ``charged`` /
    ``drained`` (total points gained / lost across the day). Empty in → all
    ``None`` / 0.
    """
    empty = {"wake_level": None, "high_level": None, "low_level": None, "charged": 0.0, "drained": 0.0}
    if timeline.empty:
        return empty

    levels = timeline["level"].to_numpy(dtype=float)
    diffs = np.diff(levels)
    charged = float(diffs[diffs > 0].sum()) if diffs.size else 0.0
    drained = float(-diffs[diffs < 0].sum()) if diffs.size else 0.0

    wake_level = None
    if wake_ts is not None:
        mask = timeline.index <= wake_ts
        if mask.any():
            wake_level = int(round(float(levels[mask][-1])))

    return {
        "wake_level": wake_level,
        "high_level": int(round(float(levels.max()))),
        "low_level": int(round(float(levels.min()))),
        "charged": round(charged, 1),
        "drained": round(drained, 1),
    }


def acute_chronic_ratio(s: pd.Series) -> tuple[float, float, float] | None:
    """ACWR = mean(last 7d) / mean(last 28d) on a dense daily load series.

    Returns ``(acute, chronic, ratio)`` or None when there is too little history
    or the chronic load is zero (ratio undefined / meaningless)."""
    s = s.dropna()
    if len(s) < ACWR_CHRONIC_DAYS:
        return None
    acute = float(s.tail(ACWR_ACUTE_DAYS).mean())
    chronic = float(s.tail(ACWR_CHRONIC_DAYS).mean())
    if chronic <= 0:
        return None
    return acute, chronic, acute / chronic


def ewma(s: pd.Series, tau: float) -> pd.Series:
    """Exponentially weighted moving average with time constant ``tau`` days:
    ``y_t = y_{t-1} + (x_t − y_{t-1}) / tau``, seeded from zero load before the
    series starts (``y_{-1} = 0``). The Banister impulse-response smoothing
    behind CTL (tau=42) and ATL (tau=7). Expects a dense daily series (the
    0-filled workout-load series); NaN rows are dropped."""
    values = s.dropna()
    if values.empty:
        return values
    # ewm(adjust=False) seeds y_0 = x_0; prepend an explicit 0 so the recursion
    # starts from "no prior load" instead, then drop the seed row again.
    seed = pd.Series([0.0], index=[values.index[0] - pd.Timedelta(days=1)])
    return pd.concat([seed, values]).ewm(alpha=1.0 / tau, adjust=False).mean().iloc[1:]


@dataclass(frozen=True)
class TrainingStatus:
    """Banister fitness/form snapshot on a daily load series (docs/workout-analysis.md §5.2)."""

    ctl: float  # fitness: EWMA(load, 42d)
    atl: float  # fatigue: EWMA(load, 7d)
    tsb: float  # form: ctl − atl
    tsb_pct: float  # tsb / ctl — the scale-free basis for the zone bands
    ctl_ago: float | None  # ctl CTL_TREND_LOOKBACK_DAYS earlier (None: too little history)


def training_status(s: pd.Series) -> TrainingStatus | None:
    """CTL/ATL/TSB at the end of a dense daily load series.

    Returns None when there is less than one CTL time constant (42 days) of
    history — the EWMA would still be dominated by its warm-up — or when the
    chronic load is zero (a normalised TSB is undefined, and with no training
    there is no status to describe)."""
    s = s.dropna()
    if len(s) < CTL_DAYS:
        return None
    ctl_series = ewma(s, CTL_DAYS)
    ctl = float(ctl_series.iloc[-1])
    atl = float(ewma(s, ATL_DAYS).iloc[-1])
    if ctl <= 0:
        return None
    ctl_ago = (
        float(ctl_series.iloc[-1 - CTL_TREND_LOOKBACK_DAYS]) if len(ctl_series) > CTL_TREND_LOOKBACK_DAYS else None
    )
    tsb = ctl - atl
    return TrainingStatus(ctl=ctl, atl=atl, tsb=tsb, tsb_pct=tsb / ctl, ctl_ago=ctl_ago)


# --- Weekly summaries (descriptive, for the weekly report) ------------------
# All helpers aggregate a trailing 7-day window plus comparison windows and
# return plain dicts/dataclasses for the finding builders. Anchoring is
# data-driven: the caller passes the last day that has any data (so a lagging
# export doesn't produce an empty "current week"), falling back to the series'
# own last observation.


@dataclass(frozen=True)
class WeeklyWindow:
    """Aggregate of a trailing window plus its comparison windows."""

    value: float | None  # aggregate of the current window (None: no data in it)
    prev_value: float | None  # aggregate of the window immediately before
    baseline_value: float | None  # mean per-window aggregate of the preceding windows
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    n_days: int  # days with data inside the current window


def weekly_window(
    s: pd.Series,
    agg: str = "sum",
    days: int = WEEK_PERIOD,
    baseline_windows: int = 4,
    anchor: pd.Timestamp | None = None,
) -> WeeklyWindow | None:
    """Aggregate the trailing ``days`` window of a daily series, with comparisons.

    ``value`` aggregates the current window ending at ``anchor`` (default: the
    series' last observation), ``prev_value`` the window immediately before it,
    and ``baseline_value`` is the mean of the per-window aggregates over the
    ``baseline_windows`` windows preceding the current one (None when none of
    them holds data). ``agg`` is ``sum`` or ``mean``; both skip missing days
    (the workout-load series are already 0-filled, so a rest day counts as 0
    there while a gap in e.g. steps is simply absent). None for an empty series.
    """
    s = s.dropna()
    if s.empty:
        return None
    end = anchor if anchor is not None else s.index.max()
    start = end - pd.Timedelta(days=days - 1)

    def _agg(win: pd.Series) -> float | None:
        if win.empty:
            return None
        return float(win.sum()) if agg == "sum" else float(win.mean())

    current = s[(s.index >= start) & (s.index <= end)]
    prev = s[(s.index >= start - pd.Timedelta(days=days)) & (s.index < start)]
    baseline_vals = []
    for i in range(1, baseline_windows + 1):
        w_start = start - pd.Timedelta(days=days * i)
        v = _agg(s[(s.index >= w_start) & (s.index < w_start + pd.Timedelta(days=days))])
        if v is not None:
            baseline_vals.append(v)
    return WeeklyWindow(
        value=_agg(current),
        prev_value=_agg(prev),
        baseline_value=float(np.mean(baseline_vals)) if baseline_vals else None,
        window_start=start,
        window_end=end,
        n_days=int(len(current)),
    )


def weekly_sessions_summary(
    sessions: pd.DataFrame,
    types: pd.Series | None = None,
    days: int = WEEK_PERIOD,
    anchor: pd.Timestamp | None = None,
) -> dict | None:
    """Weekly workout-volume stats off the per-session frame.

    ``sessions`` is the ``load_workout_frame`` result (``day``, ``duration_s``,
    ``active_energy_kcal``, ``distance_km``); ``types`` the per-session
    canonical sport slug aligned to it (None entries = unrecognised). Returns
    ``{window_start, window_end, current, previous, per_sport}`` where
    ``current``/``previous`` hold ``sessions``/``duration_h``/``distance_km``/
    ``energy_kcal`` totals and ``per_sport`` breaks the current window down per
    recognised sport. None when there are no sessions at all; a window without
    sessions yields zero totals (a training-free week is a real 0).
    """
    if sessions.empty:
        return None
    end = anchor if anchor is not None else sessions["day"].max()
    start = end - pd.Timedelta(days=days - 1)

    def _totals(frame: pd.DataFrame) -> dict:
        return {
            "sessions": int(len(frame)),
            "duration_h": float(frame["duration_s"].fillna(0.0).sum() / 3600.0),
            "distance_km": float(frame["distance_km"].fillna(0.0).sum()) if "distance_km" in frame else 0.0,
            "energy_kcal": float(frame["active_energy_kcal"].fillna(0.0).sum()),
        }

    current = sessions[(sessions["day"] >= start) & (sessions["day"] <= end)]
    prev_start = start - pd.Timedelta(days=days)
    previous = sessions[(sessions["day"] >= prev_start) & (sessions["day"] < start)]

    per_sport = []
    if types is not None and not current.empty:
        cur_types = types.loc[current.index]
        for sport in sorted({t for t in cur_types if isinstance(t, str)}):
            per_sport.append({"sport": sport, **_totals(current[cur_types == sport])})

    return {
        "window_start": start,
        "window_end": end,
        "current": _totals(current),
        "previous": _totals(previous),
        "per_sport": per_sport,
    }


def weekly_sleep_summary(sleep: pd.DataFrame, days: int = WEEK_PERIOD) -> dict | None:
    """Weekly sleep averages off the per-night frame (``load_sleep_frame``).

    Returns ``{window_start, window_end, current, previous}``; each window holds
    ``nights``, ``avg_total_h``, ``avg_deep_h``/``avg_rem_h`` (+ their share of
    total sleep), ``avg_efficiency`` and ``avg_bedtime`` (clock hour, circular
    mean so bedtimes across midnight average correctly). Anchored on the last
    night with a total — sleep is its own nightly cadence, an external anchor
    would only cut off the most recent night. None without any night in the
    current window.
    """
    if sleep.empty:
        return None
    total = sleep["total_sleep_h"].dropna()
    if total.empty:
        return None
    end = total.index.max()
    start = end - pd.Timedelta(days=days - 1)

    def _stats(win: pd.DataFrame) -> dict | None:
        tot = win["total_sleep_h"].dropna()
        if tot.empty:
            return None
        out: dict = {"nights": int(len(tot)), "avg_total_h": float(tot.mean())}
        for col, key in (("deep_h", "avg_deep_h"), ("rem_h", "avg_rem_h"), ("efficiency", "avg_efficiency")):
            vals = win[col].dropna() if col in win else pd.Series(dtype="float64")
            out[key] = float(vals.mean()) if not vals.empty else None
        for key, pct_key in (("avg_deep_h", "deep_pct"), ("avg_rem_h", "rem_pct")):
            out[pct_key] = (
                out[key] / out["avg_total_h"] * 100.0 if out[key] is not None and out["avg_total_h"] > 0 else None
            )
        bed = win["bedtime"].dropna() if "bedtime" in win else pd.Series(dtype="float64")
        out["avg_bedtime"] = float((circular_bedtime_offset(bed).mean() + 18.0) % 24.0) if not bed.empty else None
        return out

    current = _stats(sleep[(sleep.index >= start) & (sleep.index <= end)])
    if current is None:
        return None
    previous = _stats(sleep[(sleep.index >= start - pd.Timedelta(days=days)) & (sleep.index < start)])
    return {"window_start": start, "window_end": end, "current": current, "previous": previous}


def weekly_stress_summary(daily: pd.DataFrame, days: int = WEEK_PERIOD) -> dict | None:
    """Weekly stress profile off the ``stress_daily`` frame.

    Returns ``{window_start, window_end, current, previous}``; each window holds
    ``days`` (with a score), ``avg_score`` and the ``high_min``/``medium_min``
    zone-minute totals; ``current`` adds the peak and calmest day. Anchored on
    the frame's last day. None without a scored day in the current window.
    """
    if daily.empty:
        return None
    scored = daily["score"].dropna()
    if scored.empty:
        return None
    end = scored.index.max()
    start = end - pd.Timedelta(days=days - 1)

    def _stats(win: pd.DataFrame) -> dict | None:
        scores = win["score"].dropna()
        if scores.empty:
            return None
        return {
            "days": int(len(scores)),
            "avg_score": float(scores.mean()),
            "high_min": int(win["high_min"].sum()),
            "medium_min": int(win["medium_min"].sum()),
        }

    cur_frame = daily[(daily.index >= start) & (daily.index <= end)]
    current = _stats(cur_frame)
    if current is None:
        return None
    cur_scores = cur_frame["score"].dropna()
    current["peak_day"] = cur_scores.idxmax().date()
    current["peak_score"] = float(cur_scores.max())
    current["calm_day"] = cur_scores.idxmin().date()
    current["calm_score"] = float(cur_scores.min())
    previous = _stats(daily[(daily.index >= start - pd.Timedelta(days=days)) & (daily.index < start)])
    return {"window_start": start, "window_end": end, "current": current, "previous": previous}


def weekly_body_battery_summary(daily: pd.DataFrame, days: int = WEEK_PERIOD) -> dict | None:
    """Weekly Body-Battery profile off the ``body_battery_daily`` frame.

    Returns ``{window_start, window_end, current, previous}``; each window holds
    ``days``, the mean wake/low/high levels and the mean daily charged/drained
    totals; ``current`` adds the deepest trough and its day. Anchored on the
    frame's last day. None without a day in the current window.
    """
    if daily.empty:
        return None
    end = daily.index.max()
    start = end - pd.Timedelta(days=days - 1)

    def _stats(win: pd.DataFrame) -> dict | None:
        if win.empty:
            return None
        out: dict = {"days": int(len(win))}
        for col, key in (
            ("wake_level", "avg_wake"),
            ("low_level", "avg_low"),
            ("high_level", "avg_high"),
            ("charged", "avg_charged"),
            ("drained", "avg_drained"),
        ):
            vals = win[col].dropna()
            out[key] = float(vals.mean()) if not vals.empty else None
        return out

    cur_frame = daily[(daily.index >= start) & (daily.index <= end)]
    current = _stats(cur_frame)
    if current is None:
        return None
    lows = cur_frame["low_level"].dropna()
    if not lows.empty:
        current["min_low"] = float(lows.min())
        current["min_low_day"] = lows.idxmin().date()
    previous = _stats(daily[(daily.index >= start - pd.Timedelta(days=days)) & (daily.index < start)])
    return {"window_start": start, "window_end": end, "current": current, "previous": previous}


def weekly_baseline_delta(
    s: pd.Series,
    days: int = WEEK_PERIOD,
    baseline_days: int = 28,
    min_week_days: int = 3,
    min_baseline_days: int = 7,
) -> dict | None:
    """Weekly mean of a daily metric against its trailing baseline.

    ``week_mean`` averages the trailing ``days`` window (anchored on the last
    observation); ``baseline_mean`` averages the ``baseline_days`` immediately
    before that window. None when either side has too few observed days to be
    representative (``min_week_days``/``min_baseline_days``) — a sparse vital
    should stay out of the report rather than anchor a misleading delta.
    """
    s = s.dropna()
    if s.empty:
        return None
    end = s.index.max()
    start = end - pd.Timedelta(days=days - 1)
    week = s[(s.index >= start) & (s.index <= end)]
    baseline = s[(s.index >= start - pd.Timedelta(days=baseline_days)) & (s.index < start)]
    if len(week) < min_week_days or len(baseline) < min_baseline_days:
        return None
    week_mean = float(week.mean())
    baseline_mean = float(baseline.mean())
    out = {
        "window_start": start,
        "window_end": end,
        "week_mean": week_mean,
        "baseline_mean": baseline_mean,
        "delta": week_mean - baseline_mean,
        "week_days": int(len(week)),
        "baseline_days": baseline_days,
    }
    if baseline_mean != 0:
        out["delta_pct"] = (week_mean - baseline_mean) / abs(baseline_mean) * 100.0
    return out


def latest_marker_delta(s: pd.Series, min_gap_days: int = 28) -> dict | None:
    """Latest observation of a slow-moving marker plus its change over ~a month.

    Returns ``{latest, latest_date, prev, prev_date, delta}`` where ``prev`` is
    the most recent observation at least ``min_gap_days`` older than the latest
    one (``prev``/``delta`` are None when history is too short). None for an
    empty series. Meant for markers like VO2 Max or body mass, where the last
    reading and its monthly drift say more than any weekly aggregate.
    """
    s = s.dropna()
    if s.empty:
        return None
    latest_date = s.index.max()
    out: dict = {"latest": float(s.loc[latest_date]), "latest_date": latest_date.date()}
    older = s[s.index <= latest_date - pd.Timedelta(days=min_gap_days)]
    if older.empty:
        out.update({"prev": None, "prev_date": None, "delta": None})
        return out
    prev_date = older.index.max()
    prev = float(older.loc[prev_date])
    out.update({"prev": prev, "prev_date": prev_date.date(), "delta": out["latest"] - prev})
    return out
