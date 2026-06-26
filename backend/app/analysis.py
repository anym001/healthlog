"""Nightly statistical analysis (Phase 3).

Launched as an isolated subprocess by the scheduler so a crash in a C
extension can never take down ingestion. It loads daily series (local-day
grid), computes findings and writes them as a fresh snapshot.

The pure-math helpers (top of the file) take pandas objects and no DB/IO, so
they are unit-tested against synthetic series with a fixed seed. The DB
orchestration (loaders + ``run``) is kept separate.

Findings (ARCHITECTURE.md §4.8), all derived, never medical advice:
- correlation    Spearman on de-trended series, lags 0..3 days (both
                 directions), FDR-corrected, best lag/direction per pair.
- anomaly        28-day trailing median + MAD robust z; only recent days.
- trend          long-run drift (slope of the trend component + strength).
- seasonality    MSTL(7, 365): annual pattern (amplitude + peak/trough month,
                 with a phase-confidence flag when peak/trough are too close).
- recovery_alert composite early warning: HRV low AND resting HR high together.
- consistency    rolling variability of sleep duration and bedtime.
- training_load  ACWR (acute:chronic workload ratio) on daily workout load,
                 overall and per sport; flagged on a load spike or detraining
                 (Banister TRIMP / kcal).

Workouts are folded in as daily-load *series* (build_workout_series): once
``workout_trimp``/``workout_load``/``workout_edwards`` sit on the series grid
they flow through the same correlation/anomaly/trend machinery as every other
metric. ``workout_edwards`` (zone-based TRIMP) is built only when an
intra-workout HR series is stored; it resolves intervals that the single-average
Banister TRIMP smooths over.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import delete, text
from sqlalchemy.orm import Session
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.seasonal import MSTL, STL

from .appconfig import AnalysisConfig, AppConfig, ProfileConfig, WorkoutConfig, load_config
from .config import get_settings
from .logging_config import configure_logging
from .models import Finding
from .registry import METRIC_REGISTRY
from .workout_types import canonical_workout_type

log = logging.getLogger("healthlog.analysis")

# --- Tunables ---------------------------------------------------------------
# The single source of truth for these defaults is app/appconfig.py
# (AnalysisConfig); config.yaml (analysis.*) overrides them per deployment. The
# module constants below mirror those defaults and serve as the back-compatible
# function defaults (used whenever no config is threaded in).
_DEFAULTS = AnalysisConfig()
# The full default config drives the back-compatible ``run``/``build_series``
# behaviour (profile + workout knobs) when no config is threaded in.
_DEFAULT_APP_CONFIG = AppConfig()

MAX_LAG = _DEFAULTS.max_lag  # Spearman lag range in days
MIN_OVERLAP = _DEFAULTS.min_overlap  # >= ~6 weeks of paired days before trusted
CORR_KEEP_ALPHA = _DEFAULTS.corr_keep_alpha  # keep when FDR-adjusted p <= this
FDR_ALPHA = _DEFAULTS.fdr_alpha
CORR_MIN_ACTIVE = _DEFAULTS.corr_min_active  # min non-zero days per series in a pair's overlap
CORR_MIN_ABS = _DEFAULTS.corr_min_abs  # effect-size floor: min |coefficient| to report

ANOMALY_WINDOW = _DEFAULTS.anomaly_window  # trailing days for median + MAD baseline
ANOMALY_THRESHOLD = _DEFAULTS.anomaly_threshold  # robust z (|0.6745*(x-med)/MAD|)
ANOMALY_RECENT_DAYS = _DEFAULTS.anomaly_recent_days  # only report recent anomalies

# Structural periods are domain constants, not operator tunables.
WEEK_PERIOD = 7
SEASONAL_PERIOD = 365
SEASONAL_MIN_PEAK_TROUGH_GAP = 2  # months; a near-adjacent peak/trough means the
#                                   annual phase estimate is unreliable (flagged)

TREND_STRENGTH_MIN = _DEFAULTS.trend_strength_min  # report a trend above this
SEASONALITY_STRENGTH_MIN = _DEFAULTS.seasonality_strength_min  # annual seasonality

RECOVERY_RECENT_DAYS = _DEFAULTS.recovery_recent_days
RECOVERY_Z = _DEFAULTS.recovery_z  # both HRV (low) and resting HR (high) exceed this
RECOVERY_SLEEP_Z = _DEFAULTS.recovery_sleep_z  # short sleep reinforces (optional)

CONSISTENCY_WINDOW = _DEFAULTS.consistency_window  # days of sleep variability
CONSISTENCY_DURATION_STD = _DEFAULTS.consistency_duration_std  # hours; above => irregular
CONSISTENCY_BEDTIME_STD = _DEFAULTS.consistency_bedtime_std  # hours; above => irregular

# Training load (workout pipeline). Structural windows, not operator tunables:
# ACWR is conventionally a 7-day acute over a 28-day chronic mean.
ACWR_ACUTE_DAYS = 7
ACWR_CHRONIC_DAYS = 28
# HR_rest is the trailing-median resting heart rate; HR_max / HR_rest fall back
# along the chains documented in docs/workout-analysis.md §3.1.
HR_REST_WINDOW = 28
HR_REST_MIN_PERIODS = 7
HR_REST_FALLBACK = 60.0  # last-resort resting HR when no data and no profile
HR_MAX_FALLBACK = 190.0  # last-resort max HR when neither profile nor data give one
HR_MAX_DATA_FLOOR = 160.0  # clamp for the data-driven HR_max estimate
HR_MAX_DATA_CEIL = 210.0


# ===========================================================================
# Pure math (no DB, no I/O) — unit-tested with synthetic series.
# ===========================================================================


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


def annual_seasonality(decomp: Decomp) -> dict | None:
    """Amplitude, peak/trough month and strength of the annual component."""
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
    for r in sessions.itertuples(index=False):
        day = r.day
        rest = float(hr_rest.get(day, hr_rest_default)) if len(hr_rest) else hr_rest_default
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
    df = pd.DataFrame.from_records(rows).set_index("day")
    return df.groupby(level=0).agg(
        trimp=("trimp", "sum"),
        load=("load", "sum"),
        edwards=("edwards", "sum"),
        duration_h=("duration_h", "sum"),
        count=("count", "sum"),
        intensity=("intensity", "mean"),
    )


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


# ===========================================================================
# DB loaders.
# ===========================================================================


def load_daily_series(db: Session, metric: str, agg: str, tz: str) -> pd.Series:
    """Daily value per local day for ``metric`` using its registry aggregate.

    ``COALESCE(vavg, qty)`` etc. handles both HAE shapes (only heart_rate fills
    Min/Avg/Max; everything else fills qty). Returned on a complete daily index
    with NaN for missing days (so lag shifts stay calendar-correct).
    """
    sql = text(
        """
        SELECT (time AT TIME ZONE :tz)::date AS day,
               CASE :agg
                 WHEN 'sum' THEN sum(qty)
                 WHEN 'avg' THEN avg(coalesce(vavg, qty))
                 WHEN 'min' THEN min(coalesce(vmin, qty))
                 WHEN 'max' THEN max(coalesce(vmax, qty))
               END AS value
        FROM metric_samples
        WHERE metric = :metric
        GROUP BY 1
        ORDER BY 1
        """
    )
    rows = db.execute(sql, {"tz": tz, "agg": agg, "metric": metric}).all()
    return _series_from_rows(rows)


def load_sleep_frame(db: Session, tz: str) -> pd.DataFrame:
    """Per wake-day sleep aggregates: durations, efficiency and bedtime offset."""
    rows = db.execute(
        text(
            """
            SELECT sleep_date, sleep_start, in_bed_start, in_bed_end,
                   total_sleep_h, deep_h, rem_h, in_bed_h
            FROM sleep_nightly
            WHERE sleep_date IS NOT NULL
            ORDER BY sleep_date
            """
        )
    ).all()
    if not rows:
        return pd.DataFrame()

    zone = ZoneInfo(tz)
    records = []
    for r in rows:
        in_bed_h = r.in_bed_h
        if r.in_bed_start is not None and r.in_bed_end is not None:
            in_bed_h = (r.in_bed_end - r.in_bed_start).total_seconds() / 3600.0
        bedtime = np.nan
        if r.sleep_start is not None:
            local = r.sleep_start.astimezone(zone)
            bedtime = local.hour + local.minute / 60.0
        records.append(
            {
                "day": pd.Timestamp(r.sleep_date),
                "total_sleep_h": r.total_sleep_h,
                "deep_h": r.deep_h,
                "rem_h": r.rem_h,
                "in_bed_h": in_bed_h,
                "bedtime": bedtime,
            }
        )
    df = pd.DataFrame.from_records(records).set_index("day")
    # sleep_nightly already yields one consolidated session per wake-day; the
    # groupby is a defensive no-op. Use max (not sum) so any stray duplicate
    # picks the most complete night instead of double-counting overlapping
    # API re-captures (see migration 0010).
    agg = df.groupby(level=0).agg(
        total_sleep_h=("total_sleep_h", "max"),
        deep_h=("deep_h", "max"),
        rem_h=("rem_h", "max"),
        in_bed_h=("in_bed_h", "max"),
        bedtime=("bedtime", "min"),
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        agg["efficiency"] = np.where(agg["in_bed_h"] > 0, agg["total_sleep_h"] / agg["in_bed_h"], np.nan)
    return agg


def load_workout_frame(db: Session, tz: str) -> pd.DataFrame:
    """One row per workout session, tagged with its local calendar day.

    Returns the raw per-session fields the workout aggregation needs; TRIMP and
    HR_max/HR_rest are computed downstream (pure helpers) because they depend on
    the profile and the measured resting-HR series, not just the row. Empty
    frame when there are no workouts.
    """
    rows = db.execute(
        text(
            """
            SELECT hae_id,
                   (start_time AT TIME ZONE :tz)::date AS day,
                   name, duration_s, active_energy_kcal, avg_hr, max_hr, intensity
            FROM workouts
            WHERE start_time IS NOT NULL
            ORDER BY start_time
            """
        ),
        {"tz": tz},
    ).all()
    if not rows:
        return pd.DataFrame()
    records = [
        {
            "hae_id": str(r.hae_id),
            "day": pd.Timestamp(r.day),
            "name": r.name,
            "duration_s": r.duration_s,
            "active_energy_kcal": r.active_energy_kcal,
            "avg_hr": r.avg_hr,
            "max_hr": r.max_hr,
            "intensity": r.intensity,
        }
        for r in rows
    ]
    return pd.DataFrame.from_records(records)


def load_workout_hr_samples(db: Session) -> dict[str, pd.DataFrame]:
    """Intra-workout HR samples grouped per workout (keyed by ``hae_id`` string).

    Each value is a frame with ``ts`` (sample time) and ``bpm`` columns, sorted
    by time. Empty dict when no workout carries an HR series. The samples feed
    zone-based (Edwards) TRIMP; zone boundaries are derived per run from HR_max,
    never stored.
    """
    rows = db.execute(text("SELECT workout_hae_id, ts, bpm FROM workout_hr_samples ORDER BY workout_hae_id, ts")).all()
    if not rows:
        return {}
    by_id: dict[str, list[tuple]] = {}
    for r in rows:
        by_id.setdefault(str(r.workout_hae_id), []).append((r.ts, float(r.bpm)))
    out: dict[str, pd.DataFrame] = {}
    for hid, pairs in by_id.items():
        frame = pd.DataFrame(pairs, columns=["ts", "bpm"])
        frame["ts"] = pd.to_datetime(frame["ts"])
        out[hid] = frame
    return out


def _series_from_rows(rows) -> pd.Series:
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r.day for r in rows])
    vals = [float(r.value) if r.value is not None else np.nan for r in rows]
    s = pd.Series(vals, index=idx, dtype="float64")
    return s.reindex(_daily_grid(s))


def _reindex_full(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty:
        return s
    return s.reindex(_daily_grid(s))


# ===========================================================================
# Orchestration.
# ===========================================================================


@dataclass
class AnalysisResult:
    correlations: int = 0
    anomalies: int = 0
    trends: int = 0
    seasonality: int = 0
    recovery_alerts: int = 0
    consistency: int = 0
    training_load: int = 0

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
    """
    return {name: decompose(s) for name, s in series.items()}


def _detrend_for_correlation(
    series: dict[str, pd.Series], decomps: dict[str, Decomp | None] | None = None
) -> dict[str, pd.Series]:
    """Subtract each metric's long-run trend so correlations measure day-to-day
    co-movement, not shared drift.

    Two metrics that merely trend in opposite directions over the years
    otherwise correlate spuriously (e.g. weight up vs. VO2 max down). Real
    observations only: the series keeps its complete daily index (NaN at
    gaps/edges) so lag shifts stay calendar-correct, but no interpolated point
    enters a correlation. A series too short to decompose is dropped.

    ``decomps`` reuses a precomputed decomposition cache when provided; absent
    one (e.g. a direct test call) each series is decomposed on the fly.
    """
    out: dict[str, pd.Series] = {}
    for name, s in series.items():
        decomp = decomps.get(name) if decomps is not None else decompose(s)
        if decomp is None:
            continue
        detrended = s - decomp.trend.reindex(s.index)
        if not detrended.dropna().empty:
            out[name] = detrended
    return out


def _residual_series(s: pd.Series, decomp: Decomp) -> pd.Series:
    """``s`` with both trend AND seasonal components removed (the STL residual),
    keeping real observations only.

    The de-trended series (trend only removed) still carries seasonality, so two
    metrics that merely share a weekly/annual rhythm can co-move through it.
    Correlating on this residual instead measures pure day-to-day deviation. Used
    only to stamp ``details.resid_coef`` for now (transparency / validation)."""
    out = s - decomp.trend.reindex(s.index)
    for seasonal in decomp.seasonal.values():
        out = out - seasonal.reindex(s.index)
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
    detrended = _detrend_for_correlation(series, decomps)
    names = list(detrended)
    candidates: list[tuple[str, str, int, Corr]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if _is_redundant_activity_pair(a, b):
                continue  # both measure activity volume (load or Apple ring) — structural, not health
            ma = cfg.corr_min_active
            c0 = spearman_lag(detrended[a], detrended[b], 0, min_overlap=cfg.min_overlap, min_active=ma)
            if c0:
                candidates.append((a, b, 0, c0))
            for lag in range(1, cfg.max_lag + 1):
                cab = spearman_lag(detrended[a], detrended[b], lag, min_overlap=cfg.min_overlap, min_active=ma)
                if cab:
                    candidates.append((a, b, lag, cab))  # a leads b
                cba = spearman_lag(detrended[b], detrended[a], lag, min_overlap=cfg.min_overlap, min_active=ma)
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
        # Raw (non-de-trended) Spearman at the same lag/direction, for transparency
        # about how much the de-trending shaped the reported coefficient. When the
        # raw co-movement is ~0 but the de-trended one is strong, the de-trending
        # manufactured the correlation rather than revealing it.
        raw = spearman_lag(series[a], series[b], lag, min_overlap=2, min_active=0)
        # Residual (trend AND seasonal removed) Spearman at the same lag. Compared
        # with raw and the stored (trend-only) coefficient it tells whether a
        # de-trended correlation lives in the shared seasonality (artefact) or the
        # day-to-day residual (genuine).
        dec_a = decomps.get(a) if decomps is not None else decompose(series[a])
        dec_b = decomps.get(b) if decomps is not None else decompose(series[b])
        resid = None
        if dec_a is not None and dec_b is not None:
            resid = spearman_lag(
                _residual_series(series[a], dec_a),
                _residual_series(series[b], dec_b),
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
        if resid is not None:
            details["resid_coef"] = round(resid.coef, 4)
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


def _anomaly_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
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
            findings.append(
                Finding(
                    computed_at=computed_at,
                    kind="anomaly",
                    metric_a=name,
                    ref_date=ts.date(),
                    severity=round(abs(float(row["z"])), 4),
                    details={"value": round(float(row["value"]), 4), "z": round(float(row["z"]), 4)},
                )
            )
    return findings


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
        if strength >= cfg.trend_strength_min:
            slope = trend_slope(decomp.trend)
            trends.append(
                Finding(
                    computed_at=computed_at,
                    kind="trend",
                    metric_a=name,
                    window_start=start,
                    window_end=end,
                    severity=round(strength, 4),
                    details={"slope_per_day": round(slope, 6), "strength": round(strength, 4)},
                )
            )

        annual = annual_seasonality(decomp)
        if annual and annual["strength"] >= cfg.seasonality_strength_min:
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


def run(db: Session, tz: str | None = None, config: AppConfig | None = None) -> AnalysisResult:
    """Compute all findings and write them as a fresh snapshot (flush only).

    ``config`` supplies the analysis tunables plus the physiological profile and
    workout knobs; when omitted the built-in defaults (``AppConfig()``) are used,
    so callers that don't care about config (e.g. tests) behave exactly as
    before.
    """
    tz = tz or get_settings().local_tz
    app_cfg = config or _DEFAULT_APP_CONFIG
    cfg = app_cfg.analysis
    computed_at = dt.datetime.now(dt.UTC)
    # Load sleep once and share it across build_series + the consistency pass.
    sleep = load_sleep_frame(db, tz)
    series = build_series(db, tz, app_cfg.profile, app_cfg.workouts, sleep=sleep)
    # Decompose once; correlation de-trending and trend/seasonality both reuse it.
    decomps = _decompose_all(series)

    correlations = _correlation_findings(series, computed_at, cfg, decomps)
    anomalies = _anomaly_findings(series, computed_at, cfg)
    trends, seasons = _trend_and_seasonality_findings(series, computed_at, cfg, decomps)
    recovery = _recovery_findings(series, computed_at, cfg)
    consistency = _consistency_findings(db, tz, computed_at, cfg, sleep=sleep)
    training_load = _training_load_findings(series, computed_at, cfg)

    db.execute(delete(Finding))  # snapshot: replace the previous run
    db.add_all([*correlations, *anomalies, *trends, *seasons, *recovery, *consistency, *training_load])
    db.flush()

    return AnalysisResult(
        correlations=len(correlations),
        anomalies=len(anomalies),
        trends=len(trends),
        seasonality=len(seasons),
        recovery_alerts=len(recovery),
        consistency=len(consistency),
        training_load=len(training_load),
    )


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app_config = load_config(settings.config_file)

    from .database import SessionLocal

    db = SessionLocal()
    try:
        result = run(db, settings.local_tz, app_config)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("analysis run failed")
        raise
    finally:
        db.close()

    log.info("analysis done: %s", " ".join(f"{name}={count}" for name, count in result.counts()))

    from .notify import notify_analysis

    notify_analysis(app_config.notify, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
