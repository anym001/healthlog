# HealthLog

[![Tests](https://img.shields.io/github/actions/workflow/status/anym001/healthlog/test.yml?label=Tests)](https://github.com/anym001/healthlog/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/anym001/healthlog?label=Release)](https://github.com/anym001/healthlog/releases)
[![GHCR](https://img.shields.io/badge/GHCR-healthlog-2496ED?logo=docker&logoColor=white)](https://github.com/anym001/healthlog/pkgs/container/healthlog)

Self-hosted, privacy-first analysis of your **Apple Health** data — correlations,
anomalies, trends and seasonality, computed on your own hardware. No third
parties, no cloud, no telemetry.

Your iPhone exports to your server and nothing leaves it: data flows from
**Health Auto Export** (HAE) → a FastAPI ingest endpoint → **TimescaleDB**.
A nightly job computes statistical findings; **Grafana** visualises them. An
optional local LLM (Ollama) can narrate the findings later.

> **Status:** ingestion + storage (Phase 1) and the nightly analysis pipeline
> (Phase 3) are in place; Grafana dashboards and the LLM narration are next.
> The full design and roadmap live in [`docs/PLAN.md`](docs/PLAN.md).

## Contents

- [How it works](#how-it-works)
- [What it computes](#what-it-computes)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Sending data from your iPhone](#sending-data-from-your-iphone)
- [Bulk backfill (full history)](#bulk-backfill-full-history)
- [Analysis schedule](#analysis-schedule)
- [Configuration](#configuration)
- [Operations](#operations)
- [Reverse proxy](#reverse-proxy)
- [Logging](#logging)
- [Image](#image)
- [Development](#development)

## How it works

```
iPhone — Health Auto Export (nightly JSON POST)
        │  HTTPS + X-Ingest-Token  (reverse proxy / TLS)
        ▼
healthlog container  (s6-overlay: uvicorn + scheduler)
        │
        ├─ POST /api/ingest  → archive raw payload → parse → idempotent upsert
        └─ scheduler         → nightly analysis → findings
        ▼
TimescaleDB  ───────────────►  Grafana
   (raw samples + findings)       (dashboards)
```

The image runs two supervised processes — the ingest API and the analysis
scheduler — plus a one-shot database migration on start, under PUID/PGID with a
`/config` volume (the LinuxServer/Unraid convention). Ingestion is
**metric-agnostic and tolerant**: unknown Apple Health metrics are never
rejected, they are stored and auto-registered, so adopting a new metric is a
data row, not a code change. Every raw payload is archived verbatim, and all
writes are idempotent — re-sending an overlapping export never double-counts.

## What it computes

The nightly pipeline writes **findings** to the database (visualised in Grafana):

- **Lagged correlations** between metrics (Spearman, lags 0–3 days, both
  directions), filtered with a Benjamini–Hochberg false-discovery-rate control
  so you see signal, not noise.
- **Anomalies** — robust rolling median + MAD z-scores that flag days a metric
  drifts well outside its recent normal range.
- **Trends & seasonality** — STL/MSTL decomposition with a weekly and (with ≥2
  years of history) an annual seasonal component, plus a trend slope.
- **Recovery early-warning** — a composite alert when HRV runs low *and* resting
  heart rate runs high at the same time.
- **Sleep insights** — sleep efficiency and consistency (rolling variability of
  duration and bedtime).

All statistics run on the server; only the optional LLM narration is intended
for a Mac. The full method list and tuning live in [`docs/PLAN.md`](docs/PLAN.md).

## Requirements

- Docker (or Podman) with Compose
- A reverse proxy with TLS in front of the ingest endpoint (HAE posts over the
  internet) — see [Reverse proxy](#reverse-proxy)
- An iPhone with the **Health Auto Export** app

## Quick start

HealthLog runs as a small stack: the app container, a TimescaleDB container, and
(optionally) Grafana. There is no separate compose file to download — copy the
two blocks below.

**1. Create `.env`** next to the compose file and set at least a database
password and a strong ingest secret:

```bash
# .env
INGEST_SECRET=replace-with-a-long-random-string   # e.g. `openssl rand -hex 32`
DB_PASSWORD=replace-with-a-strong-password
TZ=Europe/Vienna
ANALYSIS_CRON=30 3 * * *
PUID=1000
PGID=1000
GRAFANA_PASSWORD=replace-me
```

`INGEST_SECRET` is the shared secret HAE sends in the `X-Ingest-Token` header;
any long random ASCII string works (`openssl rand -hex 32` is a good default).
See [Configuration](#configuration) for every variable.

**2. Create `docker-compose.yml`:**

```yaml
services:
  healthlog-db:
    image: timescale/timescaledb:2.17.2-pg16   # pin the exact tag — see Operations
    container_name: healthlog-db
    environment:
      POSTGRES_USER: healthlog
      POSTGRES_PASSWORD: ${DB_PASSWORD:?set a database password}
      POSTGRES_DB: healthlog
    volumes:
      - ./data/db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U healthlog"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  healthlog:
    image: ghcr.io/anym001/healthlog:latest
    container_name: healthlog
    depends_on:
      healthlog-db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+psycopg://healthlog:${DB_PASSWORD}@healthlog-db:5432/healthlog
      INGEST_SECRET: ${INGEST_SECRET:?set a strong ingest secret}
      TZ: ${TZ:-Europe/Vienna}
      ANALYSIS_CRON: ${ANALYSIS_CRON:-30 3 * * *}
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      LOG_FORMAT: ${LOG_FORMAT:-text}
    ports:
      - "8000:8000"
    volumes:
      - ./config:/config
    restart: unless-stopped

  grafana:                       # optional — the findings dashboard
    image: grafana/grafana-oss:11.4.0
    container_name: healthlog-grafana
    depends_on:
      - healthlog-db
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
    volumes:
      - ./data/grafana:/var/lib/grafana
    ports:
      - "3000:3000"
    restart: unless-stopped
```

**3. Start it and check health:**

```bash
docker compose up -d
curl -fsS http://localhost:8000/api/health     # → {"status":"ok"}
```

The database is not published to the host (only reachable inside the compose
network); Grafana listens on `:3000`. Put a TLS reverse proxy in front of the
`healthlog` service before pointing HAE at it.

## Sending data from your iPhone

In **Health Auto Export**, create a **REST API** automation that POSTs to
`https://<your-host>/api/ingest` with the header `X-Ingest-Token: <INGEST_SECRET>`.

Recommended export settings:

- Format **JSON**, export format **v2**, *Aggregate Data* **on**, hourly grouping.
- *Use Localized Units* **off** (fixed metric units keep the data consistent).
- First run: export your **full history** once via the bulk backfill below, then
  let the automation send nightly deltas.

## Bulk backfill (full history)

A multi-year first export is too large for the HTTP endpoint (it exceeds
`MAX_PAYLOAD_BYTES` and the proxy timeout). Export the full history from HAE to
JSON file(s), copy them into the `import/` folder under `/config` (created
automatically on first start), and run the backfill CLI. It uses the same
parse/store pipeline as the endpoint and is idempotent, so re-running is always
safe:

```bash
# Inspect first (parses + reports counts, writes nothing):
docker compose exec healthlog healthlog backfill --dry-run /config/import

# Then import a directory of *.json (or pass individual files):
docker compose exec healthlog healthlog backfill /config/import
```

Each file is committed on its own; identical re-posts are skipped by content
hash. Afterwards the nightly HAE automation takes over with deltas.

## Analysis schedule

The scheduler runs the statistical analysis nightly — `ANALYSIS_CRON`, a 5-field
cron expression interpreted in `TZ`, default `30 3 * * *` (03:30 local time). To
recompute the findings on demand:

```bash
docker compose exec healthlog healthlog analyze
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | TimescaleDB/Postgres connection (psycopg v3 URL) |
| `INGEST_SECRET` | *(required)* | shared secret expected in the `X-Ingest-Token` header |
| `TZ` | `Europe/Vienna` | container clock (log timestamps) **and** the daily-bucket timezone for analysis |
| `ANALYSIS_CRON` | `30 3 * * *` | when the nightly analysis runs (5-field cron, in `TZ`) |
| `MAX_PAYLOAD_BYTES` | `33554432` | max accepted ingest body (32 MiB); larger histories use the backfill CLI |
| `PUID` / `PGID` | `1000` | host user/group that owns `/config` (Unraid: `99` / `100`) |
| `LOG_LEVEL` | `INFO` | log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one object per line, for Loki/ELK) |

## Operations

Your database holds your entire health history — treat it like the irreplaceable
data it is.

### Backups

Take a dump before any database update, and on a schedule you're comfortable
with:

```bash
# Compressed custom-format dump:
docker exec -t healthlog-db pg_dump -U healthlog -Fc healthlog > healthlog-$(date +%F).dump

# Restore into a running, empty database:
cat healthlog-2026-06-16.dump | docker exec -i healthlog-db pg_restore -U healthlog -d healthlog --clean --if-exists
```

The `./data/db` volume (the database files) and the `/config` mount (logs, the
import drop folder) are the only state worth keeping; everything else is rebuilt
from the image.

### Updating the database image

Pin the **exact** TimescaleDB tag (`timescale/timescaledb:2.17.2-pg16`) — never
`latest` or a floating tag — so recreating the container can't silently change
the engine under your data. Then update deliberately:

- **TimescaleDB patch/minor** within the same Postgres major (e.g.
  `2.17.2 → 2.18.x`, still `-pg16`): pull the new image, recreate the container,
  then upgrade the extension once (the new binary does not do this for you):

  ```bash
  docker exec -t healthlog-db psql -U healthlog -d healthlog -c "ALTER EXTENSION timescaledb UPDATE;"
  ```

- **Postgres major bump** (`pg16 → pg17`): this changes the on-disk format and is
  **not** a plain image swap — it needs a dump/restore (or `pg_upgrade`). Back up
  first, then restore the dump into a fresh `pg17` container.

Rule of thumb: keep the database version fixed until you choose to move it with a
backup in hand. The app image (`healthlog`) is decoupled and can be updated freely.

## Reverse proxy

HealthLog's ingest endpoint is reached by HAE over the internet, so it belongs
behind a TLS-terminating reverse proxy (nginx, Caddy, Traefik …). Only the
`healthlog` service needs to be exposed; keep the database internal and put
Grafana behind authentication. Nginx example:

```nginx
server {
    listen 443 ssl;
    server_name health.example.com;

    location / {
        proxy_pass         http://localhost:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

## Logging

By default the app logs to `stdout`/`stderr` (`docker logs`). Set `LOG_FORMAT=json`
for structured output suitable for aggregators (Loki, ELK). Persistence is an
operations concern: use a Docker log driver, or mount `/config` and rely on your
platform's log retention. Each ingest and the nightly analysis run are logged at
`INFO`.

## Image

Published to the **GitHub Container Registry (GHCR)**:

```bash
docker pull ghcr.io/anym001/healthlog:<tag>
```

| Tag | Source | Purpose |
|---|---|---|
| `:X.Y.Z` | Release tag on `main` | **Production** — fixed version; update when ready |
| `:latest` | Latest release | Tracks the newest production release |
| `:dev` | Latest `dev` state | **Maintainers only** — pre-production/staging |

**Recommendation:** pin production to a fixed `:X.Y.Z` tag so a new release does
not silently update your instance, and rollback is just pointing back to the old
tag.

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md). TL;DR: feature branch → PR against
`dev`; a release is a `vX.Y.Z` tag on `main`, which builds and publishes the
versioned image to GHCR. The test suite (`ruff` + `pytest` against a real
TimescaleDB + a Docker smoke boot) gates every PR.

---

Built with [Claude Code](https://claude.ai/code)
