"""Pipeline orchestration: ``run`` (compute + write the snapshot) and ``main``.

Launched as an isolated subprocess by the scheduler (see ``scheduler.py``) so a
crash in a C extension can never take down ingestion.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from ..appconfig import AppConfig, load_config
from ..cli_support import bootstrap, db_session
from ..config import get_settings
from ..models import FINDING_FIELDS, Finding, FindingHistory, WorkoutLoadDaily
from .body_battery import run_body_battery
from .constants import _DEFAULT_APP_CONFIG, log
from .findings import (
    AnalysisResult,
    _anomaly_findings,
    _body_battery_findings,
    _consistency_findings,
    _correlation_findings,
    _decompose_all,
    _fitness_marker_findings,
    _is_workout_load_family,
    _monthly_activity_findings,
    _monthly_body_battery_findings,
    _monthly_sleep_findings,
    _monthly_stress_findings,
    _monthly_training_findings,
    _monthly_vitals_findings,
    _recovery_findings,
    _stress_findings,
    _training_load_findings,
    _training_status_findings,
    _trend_and_seasonality_findings,
    _weekly_activity_findings,
    _weekly_body_battery_findings,
    _weekly_sleep_findings,
    _weekly_stress_findings,
    _weekly_training_findings,
    _weekly_vitals_findings,
    build_series,
    series_anchor,
)
from .load import load_sleep_frame
from .stress import run_stress


def _guarded(kind: str, build, empty):
    """Run one finding builder, containing its failures.

    A single pathological series (e.g. a decomposition that fails to converge)
    must cost only its own finding kind, not the whole night's snapshot — the
    stale previous snapshot would otherwise keep being served until someone
    intervenes. Logs the crash and falls back to ``empty`` for that kind.
    """
    try:
        return build()
    except Exception:
        log.exception("%s findings failed; skipping this kind for this run", kind)
        return empty


def _persist_workout_series(db: Session, series: dict[str, pd.Series], computed_at: dt.datetime) -> None:
    """Snapshot the daily workout-load series into ``workout_load_daily``.

    The findings pipeline is the only place the profile-driven load series
    exist (Banister ``workout_trimp``, zone-based ``workout_edwards``, the
    kcal/duration/count/intensity aggregates, their per-sport children); this
    write keeps them queryable for Grafana instead of the run discarding them.
    Delete + rewrite like the ``findings`` snapshot — past days legitimately
    change when the rolling resting-HR baseline or the resolved HR_max shifts,
    so an upsert would strand rows of days that no longer exist.
    """
    rows = [
        {"series": name, "day": day.date(), "value": float(value), "computed_at": computed_at}
        for name, s in series.items()
        if _is_workout_load_family(name)
        for day, value in s.dropna().items()
    ]
    db.execute(delete(WorkoutLoadDaily))
    if rows:
        db.execute(insert(WorkoutLoadDaily), rows)


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
    # Persist the workout-load series (guarded: losing the chartable snapshot
    # must not sink the findings run, and vice versa).
    _guarded("workout-series-persist", lambda: _persist_workout_series(db, series, computed_at), None)
    # Decompose once; correlation de-trending and trend/seasonality both reuse it.
    decomps = _decompose_all(series)

    correlations = _guarded("correlation", lambda: _correlation_findings(series, computed_at, cfg, decomps), [])
    anomalies = _guarded("anomaly", lambda: _anomaly_findings(series, computed_at, cfg), [])
    trends, seasons = _guarded(
        "trend/seasonality", lambda: _trend_and_seasonality_findings(series, computed_at, cfg, decomps), ([], [])
    )
    recovery = _guarded("recovery", lambda: _recovery_findings(series, computed_at, cfg), [])
    consistency = _guarded("consistency", lambda: _consistency_findings(db, tz, computed_at, cfg, sleep=sleep), [])
    training_load = _guarded("training_load", lambda: _training_load_findings(series, computed_at, cfg), [])
    training_status = _guarded("training_status", lambda: _training_status_findings(series, computed_at, cfg), [])

    # Stress: compute the intraday timeline + daily summary into its own tables
    # (guarded so a failure can't sink the findings snapshot), then read the
    # fresh stress_daily back for the alert-only finding.
    _guarded(
        "stress-compute",
        lambda: run_stress(db, tz, app_cfg.stress, app_cfg.profile, app_cfg.stress.window_days),
        None,
    )
    db.flush()
    stress = _guarded("stress", lambda: _stress_findings(db, computed_at, app_cfg.stress), [])

    # Body Battery: integrate the fresh stress_intraday timeline into the 0-100
    # energy-reserve tables (guarded), then read body_battery_daily back for the
    # alert-only finding. Runs after the stress flush — it reads those rows.
    _guarded(
        "body-battery-compute",
        lambda: run_body_battery(db, tz, app_cfg.body_battery, app_cfg.body_battery.window_days),
        None,
    )
    db.flush()
    body_battery = _guarded("body_battery", lambda: _body_battery_findings(db, computed_at, app_cfg.body_battery), [])

    # Weekly/monthly summaries: descriptive status findings for the weekly and
    # monthly reports (narrate --report weekly|monthly). Anchored on the last
    # day holding any data, so a lagging export can't produce an empty window.
    anchor = series_anchor(series)
    weekly_training = _guarded(
        "weekly_training", lambda: _weekly_training_findings(db, tz, series, computed_at, app_cfg.workouts, anchor), []
    )
    weekly_sleep = _guarded("weekly_sleep", lambda: _weekly_sleep_findings(sleep, computed_at), [])
    weekly_stress = _guarded("weekly_stress", lambda: _weekly_stress_findings(db, computed_at, app_cfg.stress), [])
    weekly_battery = _guarded(
        "weekly_body_battery", lambda: _weekly_body_battery_findings(db, computed_at, app_cfg.body_battery), []
    )
    weekly_vitals = _guarded("weekly_vitals", lambda: _weekly_vitals_findings(series, computed_at), [])
    weekly_activity = _guarded("weekly_activity", lambda: _weekly_activity_findings(series, computed_at, anchor), [])
    monthly_training = _guarded(
        "monthly_training",
        lambda: _monthly_training_findings(db, tz, series, computed_at, app_cfg.workouts, anchor),
        [],
    )
    monthly_sleep = _guarded("monthly_sleep", lambda: _monthly_sleep_findings(sleep, computed_at), [])
    monthly_stress = _guarded("monthly_stress", lambda: _monthly_stress_findings(db, computed_at, app_cfg.stress), [])
    monthly_battery = _guarded(
        "monthly_body_battery", lambda: _monthly_body_battery_findings(db, computed_at, app_cfg.body_battery), []
    )
    monthly_vitals = _guarded("monthly_vitals", lambda: _monthly_vitals_findings(series, computed_at), [])
    monthly_activity = _guarded("monthly_activity", lambda: _monthly_activity_findings(series, computed_at, anchor), [])
    fitness_markers = _guarded("fitness_markers", lambda: _fitness_marker_findings(series, computed_at), [])

    db.execute(delete(Finding))  # snapshot: replace the previous run
    db.add_all(
        [
            *correlations,
            *anomalies,
            *trends,
            *seasons,
            *recovery,
            *consistency,
            *training_load,
            *training_status,
            *stress,
            *body_battery,
            *weekly_training,
            *weekly_sleep,
            *weekly_stress,
            *weekly_battery,
            *weekly_vitals,
            *weekly_activity,
            *monthly_training,
            *monthly_sleep,
            *monthly_stress,
            *monthly_battery,
            *monthly_vitals,
            *monthly_activity,
            *fitness_markers,
        ]
    )
    db.flush()
    # Archive this snapshot (append-only) before the next run replaces it, so
    # findings stay queryable over time; computed_at is the per-run key.
    db.execute(
        insert(FindingHistory).from_select(
            list(FINDING_FIELDS),
            select(*(Finding.__table__.c[field] for field in FINDING_FIELDS)),
        )
    )

    return AnalysisResult(
        correlations=len(correlations),
        anomalies=len(anomalies),
        trends=len(trends),
        seasonality=len(seasons),
        recovery_alerts=len(recovery),
        consistency=len(consistency),
        training_load=len(training_load),
        training_status=len(training_status),
        stress=len(stress),
        body_battery=len(body_battery),
        weekly_training=len(weekly_training),
        weekly_sleep=len(weekly_sleep),
        weekly_stress=len(weekly_stress),
        weekly_body_battery=len(weekly_battery),
        weekly_vitals=len(weekly_vitals),
        weekly_activity=len(weekly_activity),
        monthly_training=len(monthly_training),
        monthly_sleep=len(monthly_sleep),
        monthly_stress=len(monthly_stress),
        monthly_body_battery=len(monthly_battery),
        monthly_vitals=len(monthly_vitals),
        monthly_activity=len(monthly_activity),
        fitness_markers=len(fitness_markers),
    )


def main() -> int:
    settings = bootstrap()
    app_config = load_config(settings.config_file)

    with db_session() as db:
        try:
            result = run(db, settings.local_tz, app_config)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("analysis run failed")
            raise

    log.info("analysis done: %s", " ".join(f"{name}={count}" for name, count in result.counts()))

    from ..notify import notify_analysis

    notify_analysis(app_config.notify, result)
    return 0
