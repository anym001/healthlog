"""HAE ingestion endpoint.

Accepts the raw Health Auto Export JSON, archives it verbatim, dedups by
content hash, then parses + idempotently upserts. Audit events are logged here
(the endpoint layer), where the request IP and DB facts are available.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import ingest as ingest_svc
from .. import notify
from ..appconfig import get_app_config
from ..config import get_settings
from ..database import get_db
from ..deps import ingest_auth
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


def _client_ip(request: Request) -> str | None:
    """Return the client host only if it's a valid IP (column type is INET);
    proxy/test placeholders like 'testclient' map to NULL rather than erroring."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


@router.post("/api/ingest", response_model=IngestResponse, dependencies=[Depends(ingest_auth)])
async def ingest_payload(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> IngestResponse:
    settings = get_settings()
    body = await request.body()

    if len(body) > settings.max_payload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds {settings.max_payload_bytes} bytes.",
        )

    ip = _client_ip(request)
    type_map = get_app_config().workouts.type_map
    try:
        outcome, result = await run_in_threadpool(_ingest, db, body, ip, type_map)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if outcome == "duplicate":
        audit.info("ingest.duplicate ip=%s", ip)
        return IngestResponse(status="duplicate")

    audit.info(
        "ingest.stored ip=%s metrics=%d(%d new) sleep=%d(%d new) workouts=%d(%d new) "
        "unknown=%d flagged_units=%d implausible=%d",
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
        metric_new=result.metric_new,
        sleep_new=result.sleep_new,
        workout_new=result.workout_new,
    )
