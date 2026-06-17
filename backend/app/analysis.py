"""Nightly statistical analysis (Phase 3).

Launched as an isolated subprocess by the scheduler so a crash in a C
extension can never take down ingestion. It loads daily series (local-day
grid), computes findings and writes them as a fresh snapshot.

The pure-math helpers (top of the file) take pandas objects and no DB/IO, so
they are unit-tested against synthetic series with a fixed seed. The DB
orchestration (loaders + ``run``) is kept separate.

Findings (PLAN.md §4.7), all derived, never medical advice:
- correlation    Spearman on de-trended series, lags 0..3 days (both
                 directions), FDR-corrected, best lag/direction per pair.
- anomaly        28-day trailing median + MAD robust z; only recent days.
- trend          long-run drift (slope of the trend component + strength).
- seasonality    MSTL(7, 365): annual pattern (amplitude + peak/trough month,
                 with a phase-confidence flag when peak/trough are too close).
- recovery_alert composite early warning: HRV low AND resting HR high together.
- consistency    rolling variability of sleep duration and bedtime.
- training_load  ACWR (acute:chronic workload ratio) on daily workout load;
                 flagged on a load spike or detraining (Banister TRIMP / kcal).

Workouts are folded in as daily-load *series* (build_workout_series): once
``workout_trimp``/``workout_load`` sit on the series grid they flow through the
same correlation/anomaly/trend machinery as every other metric.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
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


def spearman_lag(a: pd.Series, b: pd.Series, lag: int, *, min_overlap: int = MIN_OVERLAP) -> Corr | None:
    """Spearman correlation of ``a[t]`` with ``b[t + lag]`` on common days.

    Both series must be on a complete daily index so the positional shift is
    calendar-correct. Returns None when overlap is too small or a series is
    constant over the overlap.
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


def _prepare_series(s: pd.Series) -> pd.Series:
    """Complete daily index with interior gaps interpolated (edges dropped).

    Decomposition cannot handle NaN; correlation does NOT use this (it keeps
    real paired observations only).
    """
    s = s.dropna()
    if s.empty:
        return s
    full = pd.date_range(s.index.min(), s.index.max(), freq="D")
    return s.reindex(full).interpolate(method="time", limit_area="inside").dropna()


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
    full = pd.date_range(s.index.min(), s.index.max(), freq="D")
    return s.reindex(full).fillna(0.0)


def aggregate_workout_daily(
    sessions: pd.DataFrame, hr_rest: pd.Series, hr_rest_default: float, hr_max: float, sex: str
) -> pd.DataFrame:
    """Per-local-day workout features from a per-session frame.

    ``sessions`` columns: ``day`` (Timestamp), ``duration_s``, ``active_energy_kcal``,
    ``avg_hr``, ``max_hr``, ``intensity``. Returns a frame indexed by day with
    ``trimp`` (Banister sum), ``load`` (active-energy sum), ``duration_h``,
    ``count`` and ``intensity`` (mean). Empty in -> empty out.
    """
    if sessions.empty:
        return pd.DataFrame()
    rows = []
    for r in sessions.itertuples(index=False):
        day = r.day
        rest = float(hr_rest.get(day, hr_rest_default)) if len(hr_rest) else hr_rest_default
        trimp = banister_trimp(r.duration_s, r.avg_hr, rest, hr_max, sex)
        rows.append(
            {
                "day": day,
                "trimp": trimp,
                "load": float(r.active_energy_kcal) if r.active_energy_kcal is not None else 0.0,
                "duration_h": (float(r.duration_s) / 3600.0) if r.duration_s is not None else 0.0,
                "count": 1,
                "intensity": r.intensity,
            }
        )
    df = pd.DataFrame.from_records(rows).set_index("day")
    return df.groupby(level=0).agg(
        trimp=("trimp", "sum"),
        load=("load", "sum"),
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
            FROM sleep_sessions
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
    # Rare multiple sessions per wake-day: sum hours, earliest bedtime.
    agg = df.groupby(level=0).agg(
        total_sleep_h=("total_sleep_h", "sum"),
        deep_h=("deep_h", "sum"),
        rem_h=("rem_h", "sum"),
        in_bed_h=("in_bed_h", "sum"),
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
            SELECT (start_time AT TIME ZONE :tz)::date AS day,
                   duration_s, active_energy_kcal, avg_hr, max_hr, intensity
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
            "day": pd.Timestamp(r.day),
            "duration_s": r.duration_s,
            "active_energy_kcal": r.active_energy_kcal,
            "avg_hr": r.avg_hr,
            "max_hr": r.max_hr,
            "intensity": r.intensity,
        }
        for r in rows
    ]
    return pd.DataFrame.from_records(records)


def _series_from_rows(rows) -> pd.Series:
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r.day for r in rows])
    vals = [float(r.value) if r.value is not None else np.nan for r in rows]
    s = pd.Series(vals, index=idx, dtype="float64")
    full = pd.date_range(s.index.min(), s.index.max(), freq="D")
    return s.reindex(full)


def _reindex_full(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty:
        return s
    return s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="D"))


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

    def total(self) -> int:
        return (
            self.correlations
            + self.anomalies
            + self.trends
            + self.seasonality
            + self.recovery_alerts
            + self.consistency
            + self.training_load
        )


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
    Empty when there are no workouts.
    """
    sessions = load_workout_frame(db, tz)
    if sessions.empty:
        return {}

    hr_max = resolve_hr_max(profile, sessions["max_hr"])
    hr_rest, hr_rest_default = resolve_hr_rest(rhr, profile)
    daily = aggregate_workout_daily(sessions, hr_rest, hr_rest_default, hr_max, profile.sex)
    if daily.empty:
        return {}

    out: dict[str, pd.Series] = {}
    columns = {"workout_duration": "duration_h", "workout_count": "count"}
    if workouts.load_metric in ("trimp", "both"):
        columns["workout_trimp"] = "trimp"
    if workouts.load_metric in ("energy", "both"):
        columns["workout_load"] = "load"
    for name, col in columns.items():
        s = fill_zero_within_span(daily[col])
        if not s.dropna().empty and s.std() > 0:
            out[name] = s
    # Intensity is an average (NaN where absent), not a load total -> no 0-fill.
    intensity = _reindex_full(daily["intensity"])
    if not intensity.dropna().empty and intensity.std() > 0:
        out["workout_intensity"] = intensity
    return out


def build_series(
    db: Session,
    tz: str,
    profile: ProfileConfig | None = None,
    workouts: WorkoutConfig | None = None,
) -> dict[str, pd.Series]:
    """All analysis series: core metrics + derived sleep + workout-load series."""
    profile = profile or _DEFAULT_APP_CONFIG.profile
    workouts = workouts or _DEFAULT_APP_CONFIG.workouts
    series: dict[str, pd.Series] = {}
    for metric in core_metrics():
        s = load_daily_series(db, metric, METRIC_REGISTRY[metric]["agg_default"], tz)
        if not s.dropna().empty:
            series[metric] = s

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


def _detrend_for_correlation(series: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Subtract each metric's long-run trend so correlations measure day-to-day
    co-movement, not shared drift.

    Two metrics that merely trend in opposite directions over the years
    otherwise correlate spuriously (e.g. weight up vs. VO2 max down). Real
    observations only: the series keeps its complete daily index (NaN at
    gaps/edges) so lag shifts stay calendar-correct, but no interpolated point
    enters a correlation. A series too short to decompose is dropped.
    """
    out: dict[str, pd.Series] = {}
    for name, s in series.items():
        decomp = decompose(s)
        if decomp is None:
            continue
        detrended = s - decomp.trend.reindex(s.index)
        if not detrended.dropna().empty:
            out[name] = detrended
    return out


def _correlation_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
    detrended = _detrend_for_correlation(series)
    names = list(detrended)
    candidates: list[tuple[str, str, int, Corr]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            c0 = spearman_lag(detrended[a], detrended[b], 0, min_overlap=cfg.min_overlap)
            if c0:
                candidates.append((a, b, 0, c0))
            for lag in range(1, cfg.max_lag + 1):
                cab = spearman_lag(detrended[a], detrended[b], lag, min_overlap=cfg.min_overlap)  # a leads b
                if cab:
                    candidates.append((a, b, lag, cab))
                cba = spearman_lag(detrended[b], detrended[a], lag, min_overlap=cfg.min_overlap)  # b leads a
                if cba:
                    candidates.append((b, a, lag, cba))

    if not candidates:
        return []
    adj = fdr_adjust([c.p for *_, c in candidates], alpha=cfg.fdr_alpha)

    # FDR sees every lag/direction we tested (multiple-testing honesty); for
    # presentation keep only the single strongest lag/direction per unordered
    # metric pair, so a slow pair isn't listed 5x across lags and directions.
    best: dict[frozenset[str], tuple[str, str, int, Corr, float]] = {}
    for (a, b, lag, c), p_adj in zip(candidates, adj, strict=True):
        if p_adj > cfg.corr_keep_alpha:
            continue
        key = frozenset((a, b))
        prev = best.get(key)
        if prev is None or abs(c.coef) > abs(prev[3].coef):
            best[key] = (a, b, lag, c, p_adj)

    findings = []
    for a, b, lag, c, p_adj in best.values():
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
                details={"n": c.n},
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
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> tuple[list, list]:
    cfg = cfg or _DEFAULTS
    trends, seasons = [], []
    for name, s in series.items():
        decomp = decompose(s)
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
    db: Session, tz: str, computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    cfg = cfg or _DEFAULTS
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


def _training_load_findings(
    series: dict[str, pd.Series], computed_at: dt.datetime, cfg: AnalysisConfig | None = None
) -> list[Finding]:
    """ACWR on the daily workout load; flagged only when it leaves the safe band.

    Computed on ``workout_trimp`` when available (HR-based, the better signal),
    else ``workout_load`` (kcal). A ratio above ``acwr_high`` is a load spike
    (overload risk), below ``acwr_low`` is detraining; inside the band is normal
    and yields no finding (mirrors anomalies/recovery — only alerts are stored).
    """
    cfg = cfg or _DEFAULTS
    name = "workout_trimp" if "workout_trimp" in series else "workout_load" if "workout_load" in series else None
    if name is None:
        return []
    acwr = acute_chronic_ratio(series[name])
    if acwr is None:
        return []
    acute, chronic, ratio = acwr
    if cfg.acwr_low <= ratio <= cfg.acwr_high:
        return []
    note = (
        "training load spike (acute load high vs. chronic)"
        if ratio > cfg.acwr_high
        else "detraining (acute load low vs. chronic)"
    )
    return [
        Finding(
            computed_at=computed_at,
            kind="training_load",
            metric_a=name,
            ref_date=series[name].dropna().index.max().date(),
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
    ]


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
    series = build_series(db, tz, app_cfg.profile, app_cfg.workouts)

    correlations = _correlation_findings(series, computed_at, cfg)
    anomalies = _anomaly_findings(series, computed_at, cfg)
    trends, seasons = _trend_and_seasonality_findings(series, computed_at, cfg)
    recovery = _recovery_findings(series, computed_at, cfg)
    consistency = _consistency_findings(db, tz, computed_at, cfg)
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

    log.info(
        "analysis done: correlations=%d anomalies=%d trends=%d seasonality=%d "
        "recovery_alerts=%d consistency=%d training_load=%d",
        result.correlations,
        result.anomalies,
        result.trends,
        result.seasonality,
        result.recovery_alerts,
        result.consistency,
        result.training_load,
    )

    from .notify import notify_analysis

    notify_analysis(app_config.notify, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
