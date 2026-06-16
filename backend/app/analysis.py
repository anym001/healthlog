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

from .config import get_settings
from .logging_config import configure_logging
from .models import Finding
from .registry import METRIC_REGISTRY

log = logging.getLogger("healthlog.analysis")

# --- Tunables (deliberately module-level so they are easy to revisit) ------
MAX_LAG = 3  # Spearman lag range in days
MIN_OVERLAP = 42  # >= ~6 weeks of paired days before a correlation is trusted
CORR_KEEP_ALPHA = 0.10  # keep a correlation when its FDR-adjusted p <= this
FDR_ALPHA = 0.05

ANOMALY_WINDOW = 28  # trailing days for the median + MAD baseline
ANOMALY_THRESHOLD = 3.5  # robust z (|0.6745 * (x - median) / MAD|)
ANOMALY_RECENT_DAYS = 14  # only report anomalies within this recent window

WEEK_PERIOD = 7
SEASONAL_PERIOD = 365
TREND_STRENGTH_MIN = 0.30  # report a trend only above this strength
SEASONALITY_STRENGTH_MIN = 0.20  # report annual seasonality only above this
SEASONAL_MIN_PEAK_TROUGH_GAP = 2  # months; a near-adjacent peak/trough means the
#                                   annual phase estimate is unreliable (flagged)

RECOVERY_RECENT_DAYS = 14
RECOVERY_Z = 1.5  # both HRV (low) and resting HR (high) must exceed this
RECOVERY_SLEEP_Z = -1.0  # short sleep that reinforces the alert (not required)

CONSISTENCY_WINDOW = 28  # days over which sleep variability is measured
CONSISTENCY_DURATION_STD = 1.0  # hours; above => "irregular" duration
CONSISTENCY_BEDTIME_STD = 1.0  # hours; above => "irregular" bedtime


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


def spearman_lag(a: pd.Series, b: pd.Series, lag: int) -> Corr | None:
    """Spearman correlation of ``a[t]`` with ``b[t + lag]`` on common days.

    Both series must be on a complete daily index so the positional shift is
    calendar-correct. Returns None when overlap is too small or a series is
    constant over the overlap.
    """
    if lag < 0:
        return None
    paired = pd.concat([a, b.shift(-lag)], axis=1, join="inner").dropna()
    n = len(paired)
    if n < MIN_OVERLAP:
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


def fdr_adjust(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (empty in -> empty out)."""
    if not pvalues:
        return []
    _, p_adj, _, _ = multipletests(pvalues, alpha=FDR_ALPHA, method="fdr_bh")
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

    def total(self) -> int:
        return (
            self.correlations
            + self.anomalies
            + self.trends
            + self.seasonality
            + self.recovery_alerts
            + self.consistency
        )


def core_metrics() -> list[str]:
    return [m for m, spec in METRIC_REGISTRY.items() if spec["tier"] == "core"]


def build_series(db: Session, tz: str) -> dict[str, pd.Series]:
    """All analysis series: core metrics + derived sleep series."""
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


def _correlation_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
    detrended = _detrend_for_correlation(series)
    names = list(detrended)
    candidates: list[tuple[str, str, int, Corr]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            c0 = spearman_lag(detrended[a], detrended[b], 0)
            if c0:
                candidates.append((a, b, 0, c0))
            for lag in range(1, MAX_LAG + 1):
                cab = spearman_lag(detrended[a], detrended[b], lag)  # a leads b
                if cab:
                    candidates.append((a, b, lag, cab))
                cba = spearman_lag(detrended[b], detrended[a], lag)  # b leads a
                if cba:
                    candidates.append((b, a, lag, cba))

    if not candidates:
        return []
    adj = fdr_adjust([c.p for *_, c in candidates])

    # FDR sees every lag/direction we tested (multiple-testing honesty); for
    # presentation keep only the single strongest lag/direction per unordered
    # metric pair, so a slow pair isn't listed 5x across lags and directions.
    best: dict[frozenset[str], tuple[str, str, int, Corr, float]] = {}
    for (a, b, lag, c), p_adj in zip(candidates, adj, strict=True):
        if p_adj > CORR_KEEP_ALPHA:
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


def _anomaly_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
    findings = []
    for name, s in series.items():
        s = s.dropna()
        if s.empty:
            continue
        cutoff = s.index.max() - pd.Timedelta(days=ANOMALY_RECENT_DAYS)
        anomalies = rolling_mad_anomalies(s)
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


def _trend_and_seasonality_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> tuple[list, list]:
    trends, seasons = [], []
    for name, s in series.items():
        decomp = decompose(s)
        if decomp is None:
            continue
        start, end = decomp.trend.index.min().date(), decomp.trend.index.max().date()

        strength = _component_strength(decomp.trend, decomp.resid)
        if strength >= TREND_STRENGTH_MIN:
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
        if annual and annual["strength"] >= SEASONALITY_STRENGTH_MIN:
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


def _recovery_findings(series: dict[str, pd.Series], computed_at: dt.datetime) -> list[Finding]:
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
    cutoff = paired.index.max() - pd.Timedelta(days=RECOVERY_RECENT_DAYS)
    findings = []
    for ts, row in paired.iterrows():
        if ts < cutoff:
            continue
        if row["rhr"] >= RECOVERY_Z and row["hrv"] <= -RECOVERY_Z:
            short_sleep = bool(sleep_z is not None and ts in sleep_z.index and sleep_z.loc[ts] <= RECOVERY_SLEEP_Z)
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


def _consistency_findings(db: Session, tz: str, computed_at: dt.datetime) -> list[Finding]:
    sleep = load_sleep_frame(db, tz)
    if sleep.empty:
        return []
    findings = []

    duration = _reindex_full(sleep["total_sleep_h"]).dropna()
    if len(duration) >= CONSISTENCY_WINDOW:
        std = float(duration.tail(CONSISTENCY_WINDOW).std())
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="consistency",
                metric_a="sleep_total_h",
                window_start=duration.index[-CONSISTENCY_WINDOW].date(),
                window_end=duration.index[-1].date(),
                severity=round(std, 4),
                note="irregular sleep duration" if std > CONSISTENCY_DURATION_STD else "stable sleep duration",
                details={"std_hours": round(std, 4), "threshold": CONSISTENCY_DURATION_STD},
            )
        )

    bedtime = circular_bedtime_offset(_reindex_full(sleep["bedtime"]).dropna())
    if len(bedtime) >= CONSISTENCY_WINDOW:
        std = float(bedtime.tail(CONSISTENCY_WINDOW).std())
        findings.append(
            Finding(
                computed_at=computed_at,
                kind="consistency",
                metric_a="bedtime",
                window_start=bedtime.index[-CONSISTENCY_WINDOW].date(),
                window_end=bedtime.index[-1].date(),
                severity=round(std, 4),
                note="irregular bedtime" if std > CONSISTENCY_BEDTIME_STD else "stable bedtime",
                details={"std_hours": round(std, 4), "threshold": CONSISTENCY_BEDTIME_STD},
            )
        )
    return findings


def run(db: Session, tz: str | None = None) -> AnalysisResult:
    """Compute all findings and write them as a fresh snapshot (flush only)."""
    tz = tz or get_settings().local_tz
    computed_at = dt.datetime.now(dt.UTC)
    series = build_series(db, tz)

    correlations = _correlation_findings(series, computed_at)
    anomalies = _anomaly_findings(series, computed_at)
    trends, seasons = _trend_and_seasonality_findings(series, computed_at)
    recovery = _recovery_findings(series, computed_at)
    consistency = _consistency_findings(db, tz, computed_at)

    db.execute(delete(Finding))  # snapshot: replace the previous run
    db.add_all([*correlations, *anomalies, *trends, *seasons, *recovery, *consistency])
    db.flush()

    return AnalysisResult(
        correlations=len(correlations),
        anomalies=len(anomalies),
        trends=len(trends),
        seasonality=len(seasons),
        recovery_alerts=len(recovery),
        consistency=len(consistency),
    )


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    from .database import SessionLocal

    db = SessionLocal()
    try:
        result = run(db, settings.local_tz)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("analysis run failed")
        raise
    finally:
        db.close()

    log.info(
        "analysis done: correlations=%d anomalies=%d trends=%d seasonality=%d recovery_alerts=%d consistency=%d",
        result.correlations,
        result.anomalies,
        result.trends,
        result.seasonality,
        result.recovery_alerts,
        result.consistency,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
