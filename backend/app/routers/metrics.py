"""Opt-in Prometheus scrape endpoint.

Disabled by default (``METRICS_ENABLED``): the endpoint is unauthenticated
(Prometheus convention), so it must only be reachable on a trusted network —
do not forward ``/metrics`` through the public reverse proxy.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..config import get_settings

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    if not get_settings().metrics_enabled:
        # Indistinguishable from an unknown route while disabled.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
