"""Liveness/readiness endpoint.

Verifies database connectivity (``SELECT 1``) so a container-level
``HEALTHCHECK`` — and anything watching it (Unraid, Compose ``depends_on``,
an uptime monitor) — sees "unhealthy" when TimescaleDB is unreachable,
instead of ingest failures only surfacing at the next HAE sync.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..database import get_db
from ..schemas import HealthResponse

router = APIRouter()


def _check_db(db: Session) -> None:
    db.execute(text("SELECT 1"))


@router.get("/api/health", response_model=HealthResponse)
async def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        # Synchronous SQLAlchemy work off the event loop, like the ingest path.
        await run_in_threadpool(_check_db, db)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unreachable.",
        ) from exc
    return HealthResponse(status="ok", version=get_settings().app_version)
