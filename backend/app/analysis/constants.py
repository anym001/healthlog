"""Analysis tunables and structural constants.

The single source of truth for the configurable defaults is ``app/appconfig.py``
(``AnalysisConfig``); ``config.yaml`` (``analysis.*``) overrides them per
deployment. The module constants here mirror those defaults and serve as the
back-compatible function defaults used whenever no config is threaded in.
Structural periods (week/season) and the HR fallback chain are domain constants,
not operator tunables.
"""

from __future__ import annotations

import logging

from ..appconfig import AnalysisConfig, AppConfig

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
CORR_RAW_MIN_ABS = _DEFAULTS.corr_raw_min_abs  # raw-corroboration floor: min |raw Spearman|, matching sign

ANOMALY_WINDOW = _DEFAULTS.anomaly_window  # trailing days for median + MAD baseline
ANOMALY_THRESHOLD = _DEFAULTS.anomaly_threshold  # robust z (|0.6745*(x-med)/MAD|)
ANOMALY_RECENT_DAYS = _DEFAULTS.anomaly_recent_days  # only report recent anomalies
ANOMALY_MIN_GLOBAL_Z = _DEFAULTS.anomaly_min_global_z  # global-corroboration floor: min |robust z vs full history|

# Structural periods are domain constants, not operator tunables.
WEEK_PERIOD = 7
SEASONAL_PERIOD = 365
SEASONAL_MIN_PEAK_TROUGH_GAP = 2  # months; a near-adjacent peak/trough means the
#                                   annual phase estimate is unreliable (flagged)
SEASONAL_MIN_SHARED_MONTHS = 6  # >= half a year of overlapping calendar months
#                                 before two years' seasonal shapes are compared

TREND_STRENGTH_MIN = _DEFAULTS.trend_strength_min  # report a trend above this
TREND_MIN_MONOTONICITY = _DEFAULTS.trend_min_monotonicity  # directional consistency floor
SEASONALITY_STRENGTH_MIN = _DEFAULTS.seasonality_strength_min  # annual seasonality
SEASONALITY_REPRODUCIBILITY_MIN = _DEFAULTS.seasonality_reproducibility_min  # year-over-year shape recurrence

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
# Training status (Banister impulse-response): CTL/ATL are the conventional
# 42/7-day EWMA time constants; the CTL trend compares against 28 days earlier
# and calls it rising/falling beyond a 5% relative change. Structural, like the
# ACWR windows; the zone bands on TSB/CTL are the operator tunables (tsb_*).
CTL_DAYS = 42
ATL_DAYS = 7
CTL_TREND_LOOKBACK_DAYS = 28
CTL_TREND_REL = 0.05
# HR_rest is the trailing-median resting heart rate; HR_max / HR_rest fall back
# along the chains documented in docs/workout-analysis.md §3.1.
HR_REST_WINDOW = 28
HR_REST_MIN_PERIODS = 7
HR_REST_FALLBACK = 60.0  # last-resort resting HR when no data and no profile
HR_MAX_FALLBACK = 190.0  # last-resort max HR when neither profile nor data give one
HR_MAX_DATA_FLOOR = 160.0  # clamp for the data-driven HR_max estimate
HR_MAX_DATA_CEIL = 210.0
