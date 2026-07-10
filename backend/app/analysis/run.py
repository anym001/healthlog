"""Pipeline orchestration: ``run`` (compute + write the snapshot) and ``main``.

Launched as an isolated subprocess by the scheduler (see ``scheduler.py``) so a
crash in a C extension can never take down ingestion.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from ..appconfig import AppConfig, load_config
from ..cli_support import bootstrap, db_session
from ..config import get_settings
from ..models import FINDING_FIELDS, Finding, FindingHistory
from .constants import _DEFAULT_APP_CONFIG, log
from .findings import (
    AnalysisResult,
    _anomaly_findings,
    _consistency_findings,
    _correlation_findings,
    _decompose_all,
    _recovery_findings,
    _stress_findings,
    _training_load_findings,
    _trend_and_seasonality_findings,
    build_series,
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

    correlations = _guarded("correlation", lambda: _correlation_findings(series, computed_at, cfg, decomps), [])
    anomalies = _guarded("anomaly", lambda: _anomaly_findings(series, computed_at, cfg), [])
    trends, seasons = _guarded(
        "trend/seasonality", lambda: _trend_and_seasonality_findings(series, computed_at, cfg, decomps), ([], [])
    )
    recovery = _guarded("recovery", lambda: _recovery_findings(series, computed_at, cfg), [])
    consistency = _guarded("consistency", lambda: _consistency_findings(db, tz, computed_at, cfg, sleep=sleep), [])
    training_load = _guarded("training_load", lambda: _training_load_findings(series, computed_at, cfg), [])

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

    db.execute(delete(Finding))  # snapshot: replace the previous run
    db.add_all([*correlations, *anomalies, *trends, *seasons, *recovery, *consistency, *training_load, *stress])
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
        stress=len(stress),
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
