# HealthLog

Self-hosted, privacy-first analysis of Apple Health data: correlations,
anomalies and trends on your own hardware — no third parties, no cloud.

Data flows from **Health Auto Export** (iPhone) → a FastAPI ingest endpoint →
**TimescaleDB**. A nightly job computes statistical findings; **Grafana**
visualises them. An optional local LLM (Ollama) narrates the findings later.

> **Status:** ingestion + storage (Phase 1) and the nightly analysis pipeline
> (Phase 3) are in place. The full design and roadmap live in
> [`docs/PLAN.md`](docs/PLAN.md).

## Architecture

```
iPhone (Health Auto Export, nightly JSON POST)
        │  HTTPS + secret header (reverse proxy / TLS)
        ▼
healthlog container (s6-overlay: uvicorn + scheduler)
   ├─ POST /api/ingest  → archive raw → parse → idempotent upsert
   └─ scheduler         → nightly analysis → findings (correlations,
                          anomalies, trends, seasonality, recovery alerts)
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
JSON file(s), copy them into the `import/` folder under `/config` (created
automatically on first start), and run the backfill CLI — it uses the same
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

### Analysis schedule

The scheduler runs the statistical analysis nightly (`ANALYSIS_CRON`, a 5-field
cron expression, default `30 3 * * *`). To recompute the findings on demand:

```bash
docker compose exec healthlog healthlog analyze
```

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…` | TimescaleDB/Postgres connection |
| `INGEST_SECRET` | *(required)* | shared secret for `X-Ingest-Token` |
| `TZ` | `Europe/Vienna` | container clock (log timestamps) and the daily-bucket timezone |
| `ANALYSIS_CRON` | `30 3 * * *` | when the nightly analysis runs (5-field cron, in `TZ`) |
| `PUID` / `PGID` | `1000` | ownership of `/config` |
| `LOG_LEVEL` | `INFO` | log verbosity |
| `LOG_FORMAT` | `text` | `text` or `json` |

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md). TL;DR: feature branch → PR against
`dev`; release via a `vX.Y.Z` tag on `main`. Images publish to GHCR (Docker Hub
is a planned addition).
