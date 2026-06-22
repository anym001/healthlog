"""HAE ingestion endpoint.

Accepts the raw Health Auto Export JSON, archives it verbatim, dedups by
content hash, then parses + idempotently upserts. Audit events are logged here
(the endpoint layer), where the request IP and DB facts are available.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
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


def _parse_and_store(db: Session, body: bytes, ip: str | None):
    """JSON parse + content hash + idempotent DB store.

    Runs off the event loop (``run_in_threadpool``): ``json.loads`` on a large
    body and the synchronous SQLAlchemy work are both CPU/IO-bound and would
    otherwise block every concurrent request. Returns ``(status, result)`` with
    a ``None`` result for a duplicate; raises ``ValueError`` for a malformed
    body (the caller maps it to HTTP 400)."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid JSON body.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")

    content_hash = hashlib.sha256(body).digest()
    if not ingest_svc.archive_raw(db, payload, content_hash, ip):
        db.commit()
        return "duplicate", None

    result = ingest_svc.store(db, ingest_svc.parse_payload(payload))
    db.commit()
    return "stored", result


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
    try:
        outcome, result = await run_in_threadpool(_parse_and_store, db, body, ip)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if outcome == "duplicate":
        audit.info("ingest.duplicate ip=%s", ip)
        return IngestResponse(status="duplicate")

    audit.info(
        "ingest.stored ip=%s metrics=%d(%d new) sleep=%d(%d new) workouts=%d(%d new) unknown=%d",
        ip,
        result.metric_rows,
        result.metric_new,
        result.sleep_rows,
        result.sleep_new,
        result.workout_rows,
        result.workout_new,
        result.unknown_metrics,
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
        metric_new=result.metric_new,
        sleep_new=result.sleep_new,
        workout_new=result.workout_new,
    )
