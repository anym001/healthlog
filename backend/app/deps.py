"""Shared dependencies: ingest authentication via a shared-secret header."""

from __future__ import annotations

import hmac
import ipaddress
import logging

from fastapi import Header, HTTPException, Request, status

from .config import get_settings
from .metrics import INGEST_REQUESTS

audit = logging.getLogger("healthlog.audit")


def client_ip(request: Request) -> str | None:
    """Return the client host only if it's a valid IP (the audit column type
    is INET); proxy/test placeholders like 'testclient' map to None rather
    than erroring."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


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


async def ingest_auth(request: Request, x_ingest_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency. Header name is fixed as ``X-Ingest-Token`` (HAE
    sends custom headers); the secret value comes from the environment.

    Failures are audit-logged with the client IP (never the provided token)
    so an operator can spot brute-force attempts and feed fail2ban & co."""
    try:
        verify_ingest_token(x_ingest_token)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            INGEST_REQUESTS.labels(outcome="unauthorized").inc()
            audit.warning("ingest.auth_failed ip=%s", client_ip(request))
        else:  # 503: fails closed until INGEST_SECRET is configured
            INGEST_REQUESTS.labels(outcome="unconfigured").inc()
            audit.warning("ingest.rejected reason=unconfigured ip=%s", client_ip(request))
        raise
