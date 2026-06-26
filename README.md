# HealthLog

[![Tests](https://img.shields.io/github/actions/workflow/status/anym001/healthlog/test.yml?label=Tests)](https://github.com/anym001/healthlog/actions/workflows/test.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://github.com/anym001/healthlog/blob/HEAD/LICENSE)
[![Release](https://img.shields.io/github/v/release/anym001/healthlog?label=Release)](https://github.com/anym001/healthlog/releases)
[![GHCR](https://img.shields.io/badge/GHCR-healthlog-2496ED?logo=docker&logoColor=white)](https://github.com/anym001/healthlog/pkgs/container/healthlog)

Self-hosted, privacy-first analysis of your **Apple Health** data — correlations,
anomalies, trends and seasonality, computed on your own hardware. No third
parties, no cloud, no telemetry: your iPhone exports to your server and nothing
leaves it.

Data flows from **Health Auto Export** (HAE) on your iPhone → a FastAPI ingest
endpoint → **TimescaleDB**. A nightly job computes statistical findings and
stores them back in the database, ready to chart in **Grafana** (dashboards
included) or any tool you like. An optional local LLM (Ollama) can turn the
findings into a written weekly report — still entirely on your own machines.

**What you'll need:** a machine that runs Docker (a NAS, an Unraid box, a home
server), an iPhone with the **Health Auto Export** app, and a TLS reverse proxy
in front of the ingest endpoint. Everything else ships in the image.

> The full design and rationale live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
this is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
for a Mac. The full method list and tuning live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Requirements

- Docker (or Podman)
- A reverse proxy with TLS in front of the ingest endpoint (HAE posts over the
  internet) — see [Reverse proxy](#reverse-proxy)
- An iPhone with the **Health Auto Export** app

## Quick start

HealthLog needs a TimescaleDB/Postgres database and the app container, joined on
a private Docker network so the app can reach the database by name. No files to
create — just run the containers.

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

`INGEST_SECRET` is the shared secret HAE sends in the `X-Ingest-Token` header —
any long random ASCII string (`openssl rand -hex 32` is a good default). The
password in `DATABASE_URL` must match `POSTGRES_PASSWORD` above, and `PUID`/`PGID`
decide who owns `/config` on the host (Unraid: `99` / `100`). See
[Configuration](#configuration) for every variable.

**3. Check health:**

```bash
curl -fsS http://localhost:8000/api/health     # → {"status":"ok"}
```

The database has no published port — it's reachable only by the app over the
`health` network. Put a TLS reverse proxy in front of the `healthlog` container
before pointing HAE at it (see [Reverse proxy](#reverse-proxy)).

**4. (Optional) Visualise with Grafana:**

The analysis writes its results to the database, so chart them with whatever you
prefer — Grafana, Metabase, a notebook, plain SQL. Attach that tool to the same
`health` network and point it at `healthlog-db`. The repo ships ready-made
**Grafana dashboards** — see [`grafana/README.md`](grafana/README.md) for details.

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

## Sending data from your iPhone

In **Health Auto Export**, create a **REST API** automation that POSTs to
`https://<your-host>/api/ingest`. See the app's own
[REST API automation guide](https://help.healthyapps.dev/de/health-auto-export/automations/rest-api/)
for where each setting lives; the table below maps them to what this endpoint
expects.

> **Do the one-time [bulk backfill](#bulk-backfill-full-history) first**, *then*
> enable the automation. A multi-year first export is too large for the HTTP
> endpoint, and a "Since Last Sync" automation started on an empty database
> would never carry your history.

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
| Date range | **Since Last Sync** | sends only new data; server-side dedup makes overlap safe |
| Sync cadence | **every 1 hour** | plenty — the analysis runs nightly (`ANALYSIS_CRON`); 5-minute syncs work but are overkill |
| *Use Localized Units* | **off** | HealthLog normalises units itself; localized units only get flagged |

Make sure **Sleep Analysis**, **Heart Rate Variability** and **Resting Heart
Rate** are among the selected metrics — they drive the sleep, consistency and
recovery-alert findings.

**Workouts (optional):** the Health-Metrics automation does not include
workouts. To capture them, add a **second** REST API automation with the same
URL, header and timeout, changing only the workout-specific settings:

| Setting | Value | Why |
|---|---|---|
| Automation type | **REST API** | same endpoint as above |
| URL | `https://<your-host>/api/ingest` | the ingest endpoint |
| Header | key `X-Ingest-Token`, value `<INGEST_SECRET>` | authentication (the exact `INGEST_SECRET` env value) |
| Timeout | `60` | |
| Data type | **Workouts** | this is what makes it a workout export |
| Include Workout Metrics | **on** | delivers the intra-workout heart-rate series — required for zone-based Edwards TRIMP |
| Include Route Data | **off** | GPS routes are never parsed or stored; leaving them out keeps payloads small and location data off the server |
| Time grouping | **minutes** | per-minute HR buckets are the shape the Edwards parser expects |
| Export format | **JSON** | CSV is **not** parsed |
| Export version | **v2** | the parser targets HAE v2 payloads |
| Date range | **Since Last Sync** | sends only new workouts; server-side dedup makes overlap safe |
| Sync cadence | **every 1 hour** | matches the metrics automation; the analysis runs nightly |

The nightly analysis folds workouts in as daily training-load series (TRIMP /
active-energy) for correlation and ACWR findings — set a `profile` in
`config.yaml` to sharpen the HR-based load. When **Include Workout Metrics** is
on, a zone-based **Edwards TRIMP** series (`workout_edwards`) runs in parallel;
it self-gates off when no HR series is present.

To confirm data is arriving, trigger a **Manual Export** and check the logs for
an `ingest.stored …` audit line. For a push confirmation, temporarily set
`notify.events: [ingest, analysis, findings]` and `notify.level: always` in
`config.yaml` (see [Notifications](#notifications)).

## Bulk backfill (full history)

A multi-year first export is too large for the HTTP endpoint (it exceeds
`MAX_PAYLOAD_BYTES` and the proxy timeout). Export the full history from HAE to
JSON file(s), copy them into the `import/` folder under `/config` (created
automatically on first start), and run the backfill CLI. It uses the same
parse/store pipeline as the endpoint and is idempotent, so re-running is always
safe:

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
cron expression interpreted in `TZ`, default `30 3 * * *` (03:30 local time). To
recompute the findings on demand:

```bash
docker exec healthlog healthlog analyze
```

Before trusting the findings, run a read-only data-quality audit. It reports the
latest findings snapshot (per kind), per-metric coverage — flagging core metrics
that have no data or fewer than the ~6-week correlation floor (`analysis.min_overlap`)
— and any stored unit that diverges from its canonical unit:

```bash
docker exec healthlog healthlog audit
```

To check whether the raw archive carries the intra-workout heart-rate series
that zone-based (Edwards) training load needs:

```bash
docker exec healthlog healthlog check-workout-hr
```

Going forward the ingest extracts that series automatically. Workouts ingested
*before* this feature only have it in the raw archive — replay it into the
samples table once (idempotent; safe to re-run):

```bash
docker exec healthlog healthlog rederive-workout-hr
```

## LLM narration

HealthLog can turn the current findings snapshot into a written health report
using a **local** large language model via [Ollama](https://ollama.com/). The
report summarises anomalies, recovery, training load, correlations, trends and
sleep consistency in plain prose. Only statistical findings (z-scores, slopes,
ratios) are sent to the model — **no raw health values ever leave your
network**, and Ollama itself runs entirely on your own hardware.

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

Use `--dry-run` to print the exact findings context that *would* be sent to the
model — the curated, privacy-scrubbed prompt — and then exit without contacting
Ollama or writing a report. It works even when `narrate.ollama_url` is unset, so
you can verify the "data → report" input deterministically (correlation
curation, scrubbed values, lookback window) before trusting the narrative.

To keep the report focused, only the highest-priority correlations are narrated
(cross-domain links — e.g. training load vs next-day respiratory rate — rank
above expected within-subsystem pairs like total vs deep sleep); the rest are
summarised as a count. Tune the cap with `narrate.max_correlations` (default 15,
`0` = narrate them all).

### Running Ollama on a Mac

A Mac with Apple Silicon (unified memory) is well suited to running an 8–14B
model locally. The analysis itself runs on your always-on server; only this
optional narration step talks to the Mac.

1. **Install Ollama** — download the app from
   [ollama.com/download](https://ollama.com/download) (or `brew install ollama`).
   See the [official docs](https://github.com/ollama/ollama/blob/main/README.md)
   for details.

2. **Pull the model** (≈9 GB; needs ~12 GB free RAM to run comfortably):

   ```bash
   ollama pull qwen2.5:14b
   ```

   On a 16 GB Mac, `qwen2.5:7b` is a lighter alternative — set it as
   `narrate.model` in `config.yaml`.

3. **Expose Ollama on your network.** By default Ollama only listens on
   `127.0.0.1`, so the server can't reach it. Bind it to all interfaces by
   setting `OLLAMA_HOST` (see the
   [FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md#how-do-i-configure-ollama-server)):

   ```bash
   launchctl setenv OLLAMA_HOST "0.0.0.0:11434"   # then restart the Ollama app
   ```

   Keep this on a trusted LAN — Ollama has no authentication. Do **not** expose
   port `11434` to the internet.

4. **Point HealthLog at the Mac** in `config.yaml`, using the Mac's LAN IP:

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
| `PUID` / `PGID` | `1000` | host user/group that owns `/config` (Unraid: `99` / `100`) |
| `LOG_LEVEL` | `INFO` | log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one object per line, for Loki/ELK) |
| `CONFIG_FILE` | `/config/config.yaml` | path to the optional structured config (see [config.yaml](#tunables-profile--notifications-configyaml)) |
| `NOTIFY_TOKEN` | *(empty)* | Gotify/PushBits application token — **secret**; the only notify setting kept in the environment (never logged) |

### Tunables, profile & notifications (config.yaml)

Two configuration homes, deliberately split:

- **Environment variables** (the table above) — **secrets and infrastructure**.
- **`config.yaml`** (mounted at `/config/config.yaml`) — structured, non-secret
  *behaviour* and *profile*. **Entirely optional**: a missing or fully-commented
  file means all-default behaviour. The container seeds a fully-commented
  example on first start, so you can discover the knobs and uncomment what you
  want — never put secrets here.

It holds:

- **`analysis`** — the nightly pipeline's tunables (correlation lag range and
  FDR alpha, anomaly window/threshold, trend/seasonality strengths, recovery and
  consistency thresholds, ACWR load-spike/detraining bands). Retune without
  rebuilding the image.
- **`profile`** — your `birth_year`/`sex` (and optional `hr_max`/`hr_rest`).
  Personal but not secret; sharpens the HR-based training load (Banister TRIMP)
  in the workout analysis (see [`docs/workout-analysis.md`](docs/workout-analysis.md)).
  Without it, HR_max is derived from your data and a generic weighting is used.
- **`workouts`** — `load_metric` (which load series to build: `trimp`/`energy`/
  `both`), a `type_map` (localised HAE workout name → canonical sport) and
  `edwards` (zone-based TRIMP, default on). With a map, an extra per-sport load
  series is analysed for each mapped type, so one sport's lagged effect on
  recovery is told apart from another's; unmapped workouts still feed the
  type-agnostic aggregate. `edwards` adds a parallel zone-based load series when
  the intra-workout HR series is present and self-gates off when it isn't.
- **`narrate`** — Ollama endpoint (`ollama_url`), model (`qwen2.5:14b`),
  report language (`en`/`de`, default `en`), lookback window for time-anchored
  findings and HTTP timeout. Off until `ollama_url` is set; used only when you
  run `healthlog narrate` (see [LLM narration](#llm-narration)).
- **`notify`** — push notifications (see below).

Malformed YAML or an out-of-range value fails fast with a clear message. See the
seeded `/config/config.yaml` (or `backend/config.example.yaml`) for every option
with its default.

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
`INFO`.

## Development

Contributing, the test/lint workflow and a map of the codebase live in
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CLAUDE.md`](CLAUDE.md). In short:
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
