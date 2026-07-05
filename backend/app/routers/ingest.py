"""HAE ingestion endpoint.

Accepts the raw Health Auto Export JSON, archives it verbatim, dedups by
content hash, then parses + idempotently upserts. Audit events are logged here
(the endpoint layer), where the request IP and DB facts are available.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import ingest as ingest_svc
from .. import notify
from ..appconfig import get_app_config
from ..config import get_settings
from ..database import get_db
from ..deps import client_ip, ingest_auth
from ..metrics import INGEST_REQUESTS, INGEST_ROWS, LAST_INGEST
from ..schemas import IngestResponse

router = APIRouter()
log = logging.getLogger("healthlog.api")
audit = logging.getLogger("healthlog.audit")


def _ingest(db: Session, body: bytes, ip: str | None, type_map: dict[str, str] | None):
    """Run the shared ingest pipeline + commit, off the event loop.

    ``ingest_svc.ingest_bytes`` (json.loads, SHA-256, the synchronous
    SQLAlchemy work) and ``commit`` are all CPU/IO-bound and would otherwise
    block every concurrent request, so the endpoint dispatches this via
    ``run_in_threadpool``. ``type_map`` is the operator's ``workouts.type_map``
    for workout-type normalisation. Returns ``(status, result)`` with a ``None``
    result for a duplicate; raises ``ValueError`` for a malformed body (the
    caller maps it to HTTP 400)."""
    outcome, result = ingest_svc.ingest_bytes(db, body, ip, type_map=type_map)
    db.commit()
    return outcome, result


def _payload_too_large(limit: int) -> HTTPException:
    INGEST_REQUESTS.labels(outcome="too_large").inc()
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"Payload exceeds {limit} bytes.",
    )


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Read the request body while enforcing ``limit`` *before* buffering.

    A truthful Content-Length is rejected up front; without one (or with a
    lying one) the body is streamed and cut off as soon as it crosses the
    limit, so an oversized POST can never balloon memory to its full size."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _payload_too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _payload_too_large(limit)
    return bytes(body)


@router.post("/api/ingest", response_model=IngestResponse, dependencies=[Depends(ingest_auth)])
async def ingest_payload(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> IngestResponse:
    settings = get_settings()
    body = await _read_body_capped(request, settings.max_payload_bytes)

    ip = client_ip(request)
    type_map = get_app_config().workouts.type_map
    try:
        outcome, result = await run_in_threadpool(_ingest, db, body, ip, type_map)
    except ValueError as exc:
        INGEST_REQUESTS.labels(outcome="invalid").inc()
        audit.info("ingest.invalid ip=%s error=%s", ip, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        # Anything past the parse guard (a DB deadlock, a connection blip, an
        # unexpected payload shape) must still leave an audit trail and count —
        # a bare framework 500 would be invisible to the operator.
        INGEST_REQUESTS.labels(outcome="error").inc()
        audit.info("ingest.error ip=%s error=%s", ip, type(exc).__name__)
        log.exception("ingest failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ingest failed; see server logs.",
        ) from exc

    if outcome == "duplicate":
        INGEST_REQUESTS.labels(outcome="duplicate").inc()
        audit.info("ingest.duplicate ip=%s", ip)
        return IngestResponse(status="duplicate")

    INGEST_REQUESTS.labels(outcome="stored").inc()
    INGEST_ROWS.labels(kind="metric").inc(result.metric_rows)
    INGEST_ROWS.labels(kind="sleep").inc(result.sleep_rows)
    INGEST_ROWS.labels(kind="workout").inc(result.workout_rows)
    LAST_INGEST.set_to_current_time()

    audit.info(
        "ingest.stored ip=%s metrics=%d(%d new) sleep=%d(%d new) workouts=%d(%d new) "
        "unknown=%d flagged_units=%d implausible=%d dropped_workouts=%d",
        ip,
        result.metric_rows,
        result.metric_new,
        result.sleep_rows,
        result.sleep_new,
        result.workout_rows,
        result.workout_new,
        result.unknown_metrics,
        result.flagged_units,
        result.implausible_values,
        result.dropped_workouts,
    )
    # Best-effort push after the response is sent (never blocks the HAE sync).
    background_tasks.add_task(
        notify.notify_ingest,
        get_app_config().notify,
        metric_rows=result.metric_rows,
        sleep_rows=result.sleep_rows,
        workout_rows=result.workout_rows,
        metric_new=result.metric_new,
        sleep_new=result.sleep_new,
        workout_new=result.workout_new,
    )
    return IngestResponse(
        status="stored",
        metric_rows=result.metric_rows,
        sleep_rows=result.sleep_rows,
        workout_rows=result.workout_rows,
        unknown_metrics=result.unknown_metrics,
        flagged_units=result.flagged_units,
        implausible_values=result.implausible_values,
        dropped_workouts=result.dropped_workouts,
        metric_new=result.metric_new,
        sleep_new=result.sleep_new,
        workout_new=result.workout_new,
    )
