"""Environment-driven configuration.

Mirrors PocketLog's operator conventions: everything is set via environment
variables, with sensible defaults so a fresh container boots without ceremony.
Persistent state lives under ``/config`` (LinuxServer/Unraid standard).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # --- Storage -----------------------------------------------------------
    # SQLAlchemy URL for the TimescaleDB/Postgres backend. The psycopg (v3)
    # driver is used. Example:
    #   postgresql+psycopg://healthlog:healthlog@db:5432/healthlog
    database_url: str = Field(
        default="postgresql+psycopg://healthlog:healthlog@127.0.0.1:5432/healthlog",
        alias="DATABASE_URL",
    )

    # --- Ingestion ---------------------------------------------------------
    # Shared secret expected in the ingest request header. Empty => the ingest
    # endpoint fails closed (503) until configured.
    ingest_secret: str = Field(default="", alias="INGEST_SECRET")
    ingest_header: str = Field(default="X-Ingest-Token", alias="INGEST_HEADER")
    # Reject payloads larger than this (HAE backfills can be large, but a hard
    # cap protects the service). Default 32 MiB.
    max_payload_bytes: int = Field(default=32 * 1024 * 1024, alias="MAX_PAYLOAD_BYTES")

    # --- Time --------------------------------------------------------------
    # The local timezone all daily buckets are computed in. The whole analysis
    # rests on the calendar-day grid, so this must match the user's locale.
    local_tz: str = Field(default="Europe/Vienna", alias="LOCAL_TZ")

    # --- Scheduler --------------------------------------------------------
    # When the nightly analysis runs (in local_tz), as a 5-field cron
    # expression. Default: 03:30 every day.
    analysis_cron: str = Field(default="30 3 * * *", alias="ANALYSIS_CRON")

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="text", alias="LOG_FORMAT")  # text | json


@lru_cache
def get_settings() -> Settings:
    return Settings()
