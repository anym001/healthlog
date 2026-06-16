"""Shared dependencies: ingest authentication via a shared-secret header."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


def verify_ingest_token(provided: str | None) -> None:
    """Constant-time check of the ingest secret. Fails closed when unset."""
    settings = get_settings()
    if not settings.ingest_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is not configured (INGEST_SECRET unset).",
        )
    if not provided or not hmac.compare_digest(provided, settings.ingest_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest token.")


async def ingest_auth(x_ingest_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency. Header name is fixed as ``X-Ingest-Token`` (HAE
    sends custom headers); the secret value comes from the environment."""
    verify_ingest_token(x_ingest_token)
