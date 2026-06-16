# HealthLog

Self-hosted, privacy-first analysis of Apple Health data: correlations,
anomalies and trends on your own hardware — no third parties, no cloud.

Data flows from **Health Auto Export** (iPhone) → a FastAPI ingest endpoint →
**TimescaleDB**. A nightly job computes statistical findings; **Grafana**
visualises them. An optional local LLM (Ollama) narrates the findings later.

> **Status:** Phase 1 (ingestion + storage). The full design and roadmap live
> in [`docs/PLAN.md`](docs/PLAN.md).

## Architecture

```
iPhone (Health Auto Export, nightly JSON POST)
        │  HTTPS + secret header (reverse proxy / TLS)
        ▼
healthlog container (s6-overlay: uvicorn + scheduler)
   ├─ POST /api/ingest  → archive raw → parse → idempotent upsert
   └─ scheduler         → nightly analysis (Phase 3)
        ▼
TimescaleDB  ──►  Grafana
```

The image runs two supervised processes (ingest API and scheduler) plus a
oneshot migration, with PUID/PGID and a `/config` volume (LinuxServer/Unraid
convention).

## Quick start

```bash
cp .env.example .env        # set INGEST_SECRET (and DB_PASSWORD)
docker compose up -d
curl -fsS http://localhost:8000/api/health
```

Point a reverse proxy with TLS at the `healthlog` service, then configure the
Health Auto Export **REST API** automation to POST JSON to
`https://<host>/api/ingest` with header `X-Ingest-Token: <INGEST_SECRET>`.

### HAE export settings

- Format **JSON**, export version **v2**, *Aggregate data* on, hourly grouping.
- *Use Localized Units* **off**; fixed metric unit preferences.
- First run: export the full history (backfill, see below); then nightly deltas.

### Bulk backfill (full history)

A multi-year first export is too large for the HTTP endpoint (it exceeds
`MAX_PAYLOAD_BYTES` and the proxy timeout). Export the full history from HAE to
JSON file(s), copy them onto the server (e.g. into `/config/import`), and run
the backfill CLI — it uses the same parse/store pipeline as the endpoint and is
idempotent, so re-running is always safe:

```bash
# Inspect first (parses + reports counts, writes nothing):
docker compose exec healthlog python -m app.backfill --dry-run /config/import

# Then import a directory of *.json (or pass individual files):
docker compose exec healthlog python -m app.backfill /config/import
```

Each file is committed on its own; identical re-posts are skipped by content
hash. Afterwards the nightly HAE automation takes over with deltas.

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…` | TimescaleDB/Postgres connection |
| `INGEST_SECRET` | *(required)* | shared secret for `X-Ingest-Token` |
| `LOCAL_TZ` | `Europe/Vienna` | timezone for daily buckets |
| `PUID` / `PGID` | `1000` | ownership of `/config` |
| `LOG_LEVEL` | `INFO` | log verbosity |
| `LOG_FORMAT` | `text` | `text` or `json` |

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md). TL;DR: feature branch → PR against
`dev`; release via a `vX.Y.Z` tag on `main`. Images publish to GHCR (Docker Hub
is a planned addition).
