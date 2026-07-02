"""FastAPI application wiring.

Holds only app-level concerns: logging bootstrap, security-headers middleware
and router includes. Endpoints live in ``app/routers/*``.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import routers
from .appconfig import get_app_config
from .config import get_settings
from .logging_config import configure_logging

_settings = get_settings()
configure_logging(_settings.log_level, _settings.log_format)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    # Load config.yaml once at boot so a broken file fails startup with a
    # clean message; afterwards edits hot-reload (bad edits keep the last
    # good config — see appconfig.get_app_config).
    get_app_config()
    # The interactive docs are runtime-generated routes, not files in the
    # image; passing None unregisters them (404) — the only lever there is.
    docs_enabled = settings.api_docs_enabled
    # The version is stamped into the image from the release tag (APP_VERSION
    # build arg); a source checkout runs as "dev".
    app = FastAPI(
        title="HealthLog",
        version=settings.app_version,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(routers.health.router)
    app.include_router(routers.ingest.router)
    app.include_router(routers.metrics.router)
    return app


app = create_app()
