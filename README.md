<h1>
  <img src="https://raw.githubusercontent.com/anym001/healthlog/HEAD/assets/icons/icon-192.png" alt="" width="42" height="42" align="top">
  HealthLog
</h1>

[![Tests](https://img.shields.io/github/actions/workflow/status/anym001/healthlog/test.yml?label=Tests)](https://github.com/anym001/healthlog/actions/workflows/test.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://github.com/anym001/healthlog/blob/HEAD/LICENSE)
[![Release](https://img.shields.io/github/v/release/anym001/healthlog?label=Release)](https://github.com/anym001/healthlog/releases)
[![GHCR](https://img.shields.io/badge/GHCR-healthlog-2496ED?logo=docker&logoColor=white)](https://github.com/anym001/healthlog/pkgs/container/healthlog)

Self-hosted, privacy-first analysis of your **Apple Health** data — correlations,
anomalies, trends and seasonality, all on your own hardware. No cloud, no
telemetry, nothing leaves your network.

Data flows from **Health Auto Export** (HAE) on your iPhone → a FastAPI ingest
endpoint → **TimescaleDB**. A nightly job writes statistical findings back into
the database, ready to chart in **Grafana** (dashboards included) or any tool you
like. An optional local LLM (Ollama) can turn the findings into a written weekly
report.

**What you'll need:** a machine that runs Docker (a NAS, an Unraid box, a home
server), an iPhone with the **Health Auto Export** app, and a TLS reverse proxy
in front of the ingest endpoint. Everything else ships in the image.

> The full design and rationale live in [`docs/ARCHITECTURE.md`](https://github.com/anym001/healthlog/blob/HEAD/docs/ARCHITECTURE.md).

## Contents

- [How it works](#how-it-works)
- [What it computes](#what-it-computes)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Image](#image)
- [Sending data from your iPhone](#sending-data-from-your-iphone)
- [Bulk backfill (full history)](#bulk-backfill-full-history)
- [Analysis schedule](#analysis-schedule)
- [LLM narration](#llm-narration)
- [Configuration](#configuration)
- [Operations](#operations)
- [Reverse proxy](#reverse-proxy)
- [Logging](#logging)
- [Metrics](#metrics-optional)
- [Development](#development)
- [License](#license)

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
TimescaleDB  ───────────────►  dashboard of your choice (Grafana, …)
   (raw samples + findings)
```

The container runs the ingest API and the nightly analysis under PUID/PGID with a
`/config` volume (the Unraid convention), migrating the database on start. Two
guarantees matter day to day: unknown Apple Health metrics are accepted
automatically — you never update anything to start tracking something new — and
every export is de-duplicated server-side, so re-sending an overlapping sync
never double-counts. Raw payloads are archived verbatim. The *why* behind all of
this is in [`docs/ARCHITECTURE.md`](https://github.com/anym001/healthlog/blob/HEAD/docs/ARCHITECTURE.md).

## What it computes

The nightly pipeline writes **findings** to the database, ready to query or chart
in any dashboard tool:

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
- **Training load** — workouts become daily-load series (HR-based Banister TRIMP
  and active-energy; optionally split per sport), so their lagged effect on
  recovery and sleep falls out of the correlation engine, plus an ACWR
  (acute:chronic) load-spike / detraining alert — overall and, with a type map,
  per sport. When the export carries an intra-workout HR series, a zone-based
  **Edwards TRIMP** (`workout_edwards`) runs in parallel and resolves intervals
  that the single-average Banister load smooths over. HR_max/zone weighting
  sharpen with an optional `profile` (see below).

All statistics run on the server; only the optional LLM narration is intended
for a separate machine. The full method list and tuning live in [`docs/ARCHITECTURE.md`](https://github.com/anym001/healthlog/blob/HEAD/docs/ARCHITECTURE.md).

## Requirements

- Docker (or Podman)
- A reverse proxy with TLS in front of the ingest endpoint (HAE posts over the
  internet) — see [Reverse proxy](#reverse-proxy)
- An iPhone with the **Health Auto Export** app

## Quick start

HealthLog needs a TimescaleDB/Postgres database and the app container. The
fastest way is the Compose file shipped in this repo; plain `docker run` works
too (e.g. on Unraid, where each container is a template).

### Option A — Docker Compose (recommended)

Grab [`docker-compose.yml`](https://github.com/anym001/healthlog/blob/HEAD/docker-compose.yml) and
[`.env.example`](https://github.com/anym001/healthlog/blob/HEAD/.env.example) from this repo (or clone it), then:

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD and INGEST_SECRET (openssl rand -hex 32)
docker compose up -d
```

That starts TimescaleDB (with a healthcheck the app waits for) and HealthLog,
with `./config` mounted at `/config`. Every optional knob (`TZ`, `PUID`/`PGID`,
a pinned image version, …) is a commented line in `.env.example`.

### Option B — plain `docker run`

**1. Create the network and start TimescaleDB:**

```bash
docker network create health

docker run -d \
  --name healthlog-db \
  --network health \
  -e POSTGRES_USER=healthlog \
  -e POSTGRES_PASSWORD=change-me-strong \
  -e POSTGRES_DB=healthlog \
  -v /mnt/user/appdata/healthlog-db:/var/lib/postgresql/data \
  --restart unless-stopped \
  timescale/timescaledb:2.17.2-pg16     # pin the exact tag — see Operations
```

**2. Start HealthLog** once the database is up:

```bash
docker run -d \
  --name healthlog \
  --network health \
  -p 8000:8000 \
  -e DATABASE_URL='postgresql+psycopg://healthlog:change-me-strong@healthlog-db:5432/healthlog' \
  -e INGEST_SECRET='replace-with-a-long-random-string' \
  -e TZ=Europe/Vienna \
  -e ANALYSIS_CRON='30 3 * * *' \
  -e PUID=1000 -e PGID=1000 \
  -v /mnt/user/appdata/healthlog:/config \
  --restart unless-stopped \
  ghcr.io/anym001/healthlog:latest
```

The password in `DATABASE_URL` must match `POSTGRES_PASSWORD` above; `INGEST_SECRET`
is any long random string (`openssl rand -hex 32`); `PUID`/`PGID` own `/config` on
the host (Unraid: `99` / `100`). See [Configuration](#configuration) for every variable.

### Check health

```bash
curl -fsS http://localhost:8000/api/health     # → {"status":"ok","version":"X.Y.Z"}
```

The endpoint verifies database connectivity, so `ok` means the whole stack is up;
the image's Docker `HEALTHCHECK` probes it too, so `docker ps` shows `(healthy)`
once ingest is ready. The database has no published port — put a TLS reverse proxy
in front of the `healthlog` container before pointing HAE at it (see
[Reverse proxy](#reverse-proxy)).

### Visualise with Grafana (optional)

The analysis writes its results to the database, so chart them with whatever you
prefer — Grafana, Metabase, a notebook, plain SQL. Attach that tool to the same
Docker network as the database and point it at the DB container (`healthlog-db`
in both the Compose and `docker run` setups). The repo ships
ready-made **Grafana dashboards** — see [`grafana/README.md`](https://github.com/anym001/healthlog/blob/HEAD/grafana/README.md)
for details.

## Image

Published to the **GitHub Container Registry (GHCR)** and mirrored to
**Docker Hub** — pull from whichever you prefer, the images are identical:

```bash
docker pull ghcr.io/anym001/healthlog:<tag>
docker pull anym001/healthlog:<tag>          # Docker Hub mirror
```

| Tag | Source | Purpose |
|---|---|---|
| `:X.Y.Z` | Release tag on `main` | **Production** — fixed version; update when ready |
| `:latest` | Latest release | Tracks the newest production release |
| `:dev` | Latest `dev` state | **Maintainers only** — pre-production/staging |

**Recommendation:** pin production to a fixed `:X.Y.Z` tag so a new release does
not silently update your instance, and rollback is just pointing back to the old
tag.

## Sending data from your iPhone

In **Health Auto Export**, create a **REST API** automation that POSTs to
`https://<your-host>/api/ingest`. See the app's own
[REST API automation guide](https://help.healthyapps.dev/de/health-auto-export/automations/rest-api/)
for where each setting lives; the table below maps them to what this endpoint
expects.

> **Do the one-time [bulk backfill](#bulk-backfill-full-history) first**, *then*
> enable the automation. A multi-year first export is too large for the HTTP
> endpoint, and an hourly automation started on an empty database only carries
> the last day or two, never your full history.

| Setting | Value | Why |
|---|---|---|
| Automation type | **REST API** | |
| URL | `https://<your-host>/api/ingest` | the ingest endpoint |
| Header | key `X-Ingest-Token`, value `<INGEST_SECRET>` | authentication (the exact `INGEST_SECRET` env value) |
| Timeout | `60` | |
| Data type | **Health Metrics** | workouts need a second automation (below) |
| Health metrics | *All* is fine | unknown metrics are accepted and auto-registered as `secondary` |
| Export format | **JSON** | CSV is **not** parsed |
| Export version | **v2** | the parser targets HAE v2 payloads |
| Aggregate Data | **on** | drastically reduces payload size |
| Time grouping | **hourly** | the analysis runs on a daily grid, so sub-hourly detail isn't needed |
| Batch Requests | **off** | deltas are small; large one-offs go through the backfill CLI instead |
| Date range | **Standard** | full previous day + today; catches data Apple finalises late (e.g. sleep stages written after waking). Server-side dedup makes the overlap safe |
| Sync cadence | **every 1 hour** | plenty — the analysis runs nightly (`ANALYSIS_CRON`); 5-minute syncs work but are overkill |
| *Use Localized Units* | **off** | HealthLog normalises units itself; localized units only get flagged |

Make sure **Sleep Analysis**, **Heart Rate Variability** and **Resting Heart
Rate** are among the selected metrics — they drive the sleep, consistency and
recovery-alert findings.

**Workouts (optional):** the Health-Metrics automation does not include workouts.
To capture them, add a **second** REST API automation with the **same settings as
above**, changing only these:

| Setting | Value | Why |
|---|---|---|
| Data type | **Workouts** | makes it a workout export |
| Include Workout Metrics | **on** | delivers the intra-workout HR series — required for zone-based Edwards TRIMP |
| Include Route Data | **on** for the route map | stores the GPS track of outdoor workouts for the Workout Detail dashboard's route map. Leave **off** to keep payloads smaller and location out of the database — everything except that map works without it |
| Time grouping | **minutes** | per-minute HR buckets are the shape the Edwards parser expects |

The nightly analysis folds workouts in as daily training-load series (TRIMP /
active-energy) for correlation and ACWR findings; set a `profile` in `config.yaml`
to sharpen the HR-based load. With **Include Workout Metrics** on, a zone-based
**Edwards TRIMP** series (`workout_edwards`) runs in parallel (self-gating off
when no HR series is present). **Include Route Data** stores the GPS track for the
dashboard's route map only — the analysis never uses location.

To confirm data is arriving, trigger a **Manual Export** and check the logs for
an `ingest.stored …` audit line. For a push confirmation, temporarily set
`notify.events: [ingest, analysis, findings]` and `notify.level: always` in
`config.yaml` (see [Notifications](#notifications)).

## Bulk backfill (full history)

A multi-year first export exceeds the HTTP endpoint's limits (`MAX_PAYLOAD_BYTES`,
proxy timeout). Export the full history from HAE to JSON file(s), copy them into the
`import/` folder under `/config` (created on first start), and run the backfill CLI —
same parse/store pipeline as the endpoint, idempotent, so re-running is safe:

```bash
# Inspect first (parses + reports counts, writes nothing):
docker exec healthlog healthlog backfill --dry-run /config/import

# Then import a directory of *.json (or pass individual files):
docker exec healthlog healthlog backfill /config/import
```

Each file is committed on its own; identical re-posts are skipped by content
hash. Afterwards the nightly HAE automation takes over with deltas.

## Analysis schedule

The scheduler runs the statistical analysis nightly — `ANALYSIS_CRON`, a 5-field
cron expression interpreted in `TZ`, default `30 3 * * *` (03:30 local time). A
missed slot is caught up automatically: if the container was down when the slot
passed (host reboot, update), the analysis runs once at the next start instead
of silently skipping a day. To recompute the findings on demand:

```bash
docker exec healthlog healthlog analyze
```

Before trusting the findings, run a read-only data-quality audit — it reports the
latest findings snapshot, per-metric coverage (flagging core metrics with no data or
below the ~6-week `analysis.min_overlap` floor), units that diverge from canonical,
and workout names that map to no canonical sport (map them via `workouts.type_map`):

```bash
docker exec healthlog healthlog audit
```

Zone-based (Edwards) training load needs the intra-workout HR series; check whether
the raw archive carries it:

```bash
docker exec healthlog healthlog check-workout-hr
```

New ingests extract it automatically. For workouts ingested *before* this feature,
replay it from the raw archive once (idempotent):

```bash
docker exec healthlog healthlog rederive-workout-hr
```

## LLM narration

HealthLog can turn the current findings snapshot into a written health report via a
**local** LLM ([Ollama](https://ollama.com/)) — anomalies, recovery, training load,
correlations, trends and sleep consistency in plain prose. The model receives the
statistical findings context (z-scores, slopes, ratios — plus the measured value
where a finding carries one, e.g. an anomaly's reading). It is sent **only to the
Ollama endpoint you configure** (`narrate.ollama_url`) — point it at a machine in
your own network to keep the data local.

```bash
docker exec healthlog healthlog narrate
# With an optional focus note:
docker exec healthlog healthlog narrate --note "Focus on the HRV/training link."
# German report, last 14 days:
docker exec healthlog healthlog narrate --language de --lookback-days 14
# Inspect what the model would receive — no Ollama call, no report written:
docker exec healthlog healthlog narrate --dry-run
```

The report is printed to stdout and written to `/config/narration/YYYY-MM-DD.md`
(the directory is created on first use). It is **off until you set
`narrate.ollama_url`** in `config.yaml`.

`--dry-run` prints the exact findings context that *would* be sent —
correlation curation, included values, lookback window — and exits without
contacting Ollama. It works even when `narrate.ollama_url` is unset, so you can
verify the input deterministically before trusting the narrative.

To keep the report focused, only the highest-priority correlations are narrated
(cross-domain links — e.g. training load vs next-day respiratory rate — rank
above expected within-subsystem pairs like total vs deep sleep); the rest are
summarised as a count. Tune the cap with `narrate.max_correlations` (default 15,
`0` = narrate them all).

### Running Ollama for narration

Any machine with enough memory can run an 8–14B model locally — an Apple Silicon
Mac (unified memory) or a box with a capable GPU both work well. The analysis
itself runs on your always-on server; only this optional narration step talks to
the Ollama host. The steps below use macOS as the example.

1. **Install Ollama** — download the app from
   [ollama.com/download](https://ollama.com/download) (or `brew install ollama`).
   See the [official docs](https://github.com/ollama/ollama/blob/main/README.md)
   for details.

2. **Pull the model** (≈9 GB; needs ~12 GB free RAM to run comfortably):

   ```bash
   ollama pull qwen2.5:14b
   ```

   On a host with ~16 GB, `qwen2.5:7b` is a lighter alternative — set it as
   `narrate.model` in `config.yaml`.

3. **Expose Ollama on your network.** By default Ollama only listens on
   `127.0.0.1`, so the server can't reach it. Bind it to all interfaces by
   setting `OLLAMA_HOST` (on macOS via `launchctl`, as below; on Linux set it in
   the service's environment — see the
   [FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md#how-do-i-configure-ollama-server)):

   ```bash
   launchctl setenv OLLAMA_HOST "0.0.0.0:11434"   # then restart the Ollama app
   ```

   Keep this on a trusted LAN — Ollama has no authentication. Do **not** expose
   port `11434` to the internet.

4. **Point HealthLog at the Ollama host** in `config.yaml`, using its LAN IP:

   ```yaml
   narrate:
     ollama_url: http://192.168.1.100:11434
     model: qwen2.5:14b
     language: en          # en | de
   ```

5. **Verify** the server can reach Ollama, then generate a report:

   ```bash
   docker exec healthlog curl -fsS http://192.168.1.100:11434/api/tags   # lists models
   docker exec healthlog healthlog narrate
   ```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | TimescaleDB/Postgres connection (psycopg v3 URL) |
| `INGEST_SECRET` | *(required)* | shared secret expected in the `X-Ingest-Token` header |
| `TZ` | `Europe/Vienna` | container clock (log timestamps) **and** the daily-bucket timezone for analysis |
| `ANALYSIS_CRON` | `30 3 * * *` | when the nightly analysis runs (5-field cron, in `TZ`) |
| `MAX_PAYLOAD_BYTES` | `33554432` | max accepted ingest body (32 MiB); larger histories use the backfill CLI |
| `TRUSTED_PROXIES` | *(private ranges)* | reverse proxies (comma-separated IPs/CIDRs, or `*`) trusted to set `X-Real-IP`/`X-Forwarded-For` for the audit client IP; empty trusts the standard private + loopback ranges |
| `PUID` / `PGID` | `1000` | host user/group that owns `/config` (Unraid: `99` / `100`) |
| `LOG_LEVEL` | `INFO` | log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one object per line, for Loki/ELK) |
| `METRICS_ENABLED` | `false` | expose the Prometheus `/metrics` endpoint (see [Metrics](#metrics-optional)) |
| `API_DOCS_ENABLED` | `false` | serve the interactive API docs (`/docs`, `/redoc`, `/openapi.json`); off they answer 404. Keep off on an internet-facing instance — the docs map the whole API surface |
| `CONFIG_FILE` | `/config/config.yaml` | path to the optional structured config (see [config.yaml](#tunables-profile--notifications-configyaml)) |
| `NOTIFY_TOKEN` | *(empty)* | Gotify/PushBits application token — **secret**; the only notify setting kept in the environment (never logged) |

### Tunables, profile & notifications (config.yaml)

Two configuration homes, deliberately split:

- **Environment variables** (the table above) — **secrets and infrastructure**.
- **`config.yaml`** (at `/config/config.yaml`) — non-secret *behaviour* and
  *profile*. **Entirely optional**: a missing or fully-commented file means
  all-default behaviour. The container seeds a commented example on first start,
  so you can uncomment what you want — never put secrets here.

It holds:

- **`analysis`** — the nightly pipeline's tunables (correlation lag range and
  FDR alpha, anomaly window/threshold, trend/seasonality strengths, recovery and
  consistency thresholds, ACWR load-spike/detraining bands). Retune without
  rebuilding the image.
- **`profile`** — your `birth_year`/`sex` (and optional `hr_max`/`hr_rest`).
  Personal but not secret; sharpens the HR-based training load (see
  [`docs/workout-analysis.md`](https://github.com/anym001/healthlog/blob/HEAD/docs/workout-analysis.md)). Without it, HR_max is
  derived from your data.
- **`workouts`** — `load_metric` (`trimp`/`energy`/`both`), a `type_map`
  (localised HAE workout name → canonical sport) and `edwards` (zone-based TRIMP,
  default on). A `type_map` adds a per-sport load series per mapped type, so one
  sport's lagged effect is told apart from another's; unmapped workouts still feed
  the type-agnostic aggregate.
- **`narrate`** — Ollama endpoint, model, report language, lookback, timeout and
  `thinking` mode. Off until `ollama_url` is set (see [LLM narration](#llm-narration)).
- **`notify`** — push notifications (see below).

Changes are **picked up automatically** — no restart needed (the file's mtime is
checked on each access). Malformed YAML fails fast at startup; a bad *edit* while
running never takes the service down — the previous config stays active and a
warning is logged. See the seeded `/config/config.yaml` (or
`backend/config.example.yaml`) for every option with its default.

#### Notifications

HealthLog can push run outcomes and health alerts to a
[Gotify](https://gotify.net/)-compatible endpoint
([PushBits](https://github.com/pushbits/server) works too — it relays into a
Matrix room). Configure the behaviour under `notify:` in `config.yaml`; the
**token is the one secret** and comes from the `NOTIFY_TOKEN` environment
variable (putting it in YAML is rejected). Leave `notify.url` empty to disable.
Three independent sources can notify, chosen via `notify.events`:

- **`analysis`** — the nightly analysis run: a crash always pages; the clean OK
  summary is sent only at `level: always`.
- **`findings`** — health alerts from a run: recent anomalies and recovery
  alerts (low HRV with high resting heart rate). Sent whenever a run surfaces
  any, regardless of level.
- **`ingest`** — an *empty* HAE sync (a payload that produced no rows) always
  pages; each successful sync is reported only at `level: always`.

Messages carry only counters and metric kinds — never raw health values.
Notifications are strictly best-effort: a failed or misconfigured push is
logged and ignored, and never affects ingestion or analysis.

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

The database volume (`/var/lib/postgresql/data`) and the `/config` mount (logs,
the import drop folder) are the only state worth keeping; everything else is
rebuilt from the image.

### Disk usage

The raw archive and sample table are TimescaleDB hypertables with automatic
**compression policies** (raw after 7 days, samples after 30): repetitive JSON
compresses by an order of magnitude, so the full verbatim archive stays cheap. No
action needed. Re-importing overlapping history stays safe — writes into compressed
chunks just take a little longer.

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

The app image (`healthlog`) is decoupled and updates freely; keep the database
version fixed until you deliberately move it, backup in hand.

## Reverse proxy

HealthLog's ingest endpoint is reached by HAE over the internet, so it belongs
behind a TLS-terminating reverse proxy (nginx, Caddy, Traefik …). Only the
`healthlog` container needs to be exposed; the database has no published port and
stays internal. Nginx example:

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
`INFO`; failed ingest authentication is audit-logged at `WARNING` with the client
IP (never the token), ready for fail2ban & co.

## Metrics (optional)

Set `METRICS_ENABLED=true` to expose a Prometheus scrape endpoint at `/metrics`
with ingest counters: requests by outcome (`stored`, `duplicate`, `invalid`,
`too_large`, `unauthorized`, `unconfigured`), rows by kind, and the unix time of the
last stored sync — alert on that going stale to catch a silently broken HAE
automation. Counters and timestamps only; raw health data is never exported. The
endpoint is unauthenticated (Prometheus convention): enable it only on a trusted
network and do **not** forward `/metrics` through the public reverse proxy.

## Development

Contributing, the test/lint workflow and a map of the codebase live in
[`CONTRIBUTING.md`](https://github.com/anym001/healthlog/blob/HEAD/CONTRIBUTING.md) and [`CLAUDE.md`](https://github.com/anym001/healthlog/blob/HEAD/CLAUDE.md). In short:
feature branch → PR against `dev`; release by tagging `vX.Y.Z` on `main`.

## License

HealthLog is released under the **GNU Affero General Public License v3.0**
(AGPL-3.0). You may use, redistribute, and modify the software — but if you
offer a (modified) version as a networked service, you must make the complete
source code available (AGPL §13). The full text is in
[`LICENSE`](https://github.com/anym001/healthlog/blob/HEAD/LICENSE).

Copyright (C) 2026 anym001

---

Built with [Claude Code](https://claude.ai/code)
