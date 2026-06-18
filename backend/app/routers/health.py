"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import HealthResponse

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
