# CLAUDE.md

Guidance for working in this repository — for Claude Code and human contributors
alike. End-user documentation is in [`README.md`](README.md); the design and
rationale live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/workout-analysis.md`](docs/workout-analysis.md).

## What this is

HealthLog: self-hosted, privacy-first Apple Health analysis. Data flows from
Health Auto Export → a FastAPI ingest endpoint → TimescaleDB; a nightly job
computes statistical `findings`, charted in Grafana or narrated by an optional
local LLM. Everything runs on the user's own hardware — no external calls.

## Layout

- `backend/app/` — the application (one Python package):
  - `routers/` — FastAPI endpoints (`/api/ingest`, `/api/health`)
  - `ingest.py` — HAE payload parser (pure function) + idempotent upsert
  - `analysis/` — nightly statistics, split by role: `pure.py` (DB-free math,
    unit-tested), `load.py` (DB loaders), `findings.py` (series assembly + finding
    builders), `run.py` (orchestration), `constants.py` (tunables). `__init__.py`
    re-exports the flat public API; `python -m app.analysis` runs it as an isolated
    subprocess via the scheduler.
  - `narrate/` — LLM narration, split by role: `prompts.py` (per-language system
    prompts), `context.py` (privacy scrub + findings → model context, one
    `_section_*` builder per finding kind), `loader.py` (findings query),
    `client.py` (Ollama HTTP client), `cli.py` (the `narrate` command).
    `__init__.py` re-exports the flat public API.
  - `cli.py` — the `healthlog` operator CLI (`backfill`, `analyze`, `audit`,
    `narrate`, `check-workout-hr`, `rederive-workout-hr`); `cli_support.py` —
    shared command scaffolding (`bootstrap`, `db_session`, `module_main`)
  - `registry.py`, `units.py`, `workout_types.py` — normalisation (metric registry,
    unit guard, localised-workout-name → canonical-slug map)
  - `appconfig.py` — `config.yaml` model; `config.py` — env-var `Settings`
  - `notify.py`, `audit.py`, `diagnostics.py`, `rederive.py`,
    `backfill.py`, `scheduler.py`
- `backend/migrations/versions/` — Alembic migrations (the schema is migrations-only)
- `backend/tests/` — pytest (parser, idempotency, analysis math, registry, …)
- `grafana/` — provisioned dashboards
- `root/etc/s6-overlay/` — s6 service definitions (uvicorn, scheduler, one-shot migrate)
- `docs/` — architecture & design

## Commands (run in `backend/`)

- **Lint:** `ruff check .` and `ruff format --check .` (version pinned in
  `requirements-dev.txt`, line-length 120).
- **Tests:** `python -m pytest -q`. Needs a reachable Postgres/TimescaleDB via
  `DATABASE_URL` (default
  `postgresql+psycopg://healthlog:healthlog@127.0.0.1:5432/healthlog`). Spin one up:
  ```bash
  docker run -d -p 5432:5432 \
    -e POSTGRES_USER=healthlog -e POSTGRES_PASSWORD=healthlog -e POSTGRES_DB=healthlog \
    timescale/timescaledb:2.17.2-pg16
  ```
- **Dev deps:** `pip install -r requirements-dev.txt`.
- **New migration:** `alembic revision -m "…"`, then hand-edit. Guard
  TimescaleDB-only DDL (e.g. `create_hypertable`) so the suite still runs on plain
  Postgres.

## Conventions

- **English everywhere** — code, comments, YAML, docs, commits, PRs. The one
  exception is intentional user-facing content: the German narration prompt and
  localised report strings in the `narrate/` package (`prompts.py`, `context.py`).
- **Branching:** short-lived `feature/*` branch → PR against `dev`, never `main`.
  Release = a `vX.Y.Z` tag on `main` (builds + publishes the image to GHCR + Docker Hub).
- **Schema = migrations**, never a manual `ALTER TABLE`. Keep Timescale-specific DDL
  guarded.
- **Ingest is idempotent and metric-agnostic:** unknown metrics are accepted and
  auto-registered (`tier='secondary'`), never rejected. A metric's behaviour is a
  `metric_registry` row (data), not code.
- **Analysis math is DB-free and seed-deterministic** (pure helpers tested against
  synthetic series); keep new math in that form.
- **Two config homes:** ENV = secrets + infrastructure; `config.yaml` = behaviour +
  profile. Never put secrets in YAML.

## Keeping the docs current

When a change touches the **public surface**, update docs in the *same* PR:

- a new/renamed CLI command, env var, or `config.yaml` key → `README.md`
  (and `backend/config.example.yaml`);
- a data-model, ingestion, or analysis-method change → `docs/ARCHITECTURE.md`
  (training-load specifics → `docs/workout-analysis.md`).

Code cross-references the design docs by section anchor (e.g. `ARCHITECTURE.md
§4.8`); if you renumber a section, fix the referrers (`grep -rn "ARCHITECTURE"
backend`). The docs hold the *why* — keep volatile lists (the metric inventory,
tunable names) pointing at the code that owns them rather than duplicating it, so
there is less to drift.
