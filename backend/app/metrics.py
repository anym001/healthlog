"""Prometheus instrumentation for the ingest path.

Counters live in the uvicorn process and are served by the opt-in
``/metrics`` endpoint (``METRICS_ENABLED``, see ``routers/metrics.py``).
They cover only what this process observes — HTTP ingest outcomes and row
counts; the nightly analysis runs in a separate process and reports through
notifications/logs instead. Values are counters and unix timestamps only:
no raw health data is ever exported this way.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# One increment per /api/ingest request, labelled by outcome:
#   stored | duplicate | invalid | too_large | unauthorized | unconfigured
INGEST_REQUESTS = Counter(
    "healthlog_ingest_requests_total",
    "Ingest requests by outcome.",
    ["outcome"],
)

# Rows parsed from stored (non-duplicate) payloads, labelled by kind.
INGEST_ROWS = Counter(
    "healthlog_ingest_rows_total",
    "Rows upserted from stored ingests, by kind.",
    ["kind"],
)

# Unix time of the last stored (non-duplicate) ingest — alert when it goes
# stale to catch a silently broken HAE automation.
LAST_INGEST = Gauge(
    "healthlog_last_ingest_timestamp_seconds",
    "Unix time of the last stored (non-duplicate) ingest.",
)
