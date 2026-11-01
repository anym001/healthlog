# Contributing to HealthLog

## Branching & releases

- Develop on short-lived `feature/*` branches, branched from `dev`.
- **Open PRs against `dev`**, never directly against `main`.
- `main` is updated exclusively via a `dev → main` release PR.
- **A release is a tag push**: `git tag v0.1.0 && git push origin v0.1.0`
  triggers `build.yml` (versioned image + GitHub release). Merging to `main`
  alone does *not* release.
- Image channels: `:dev` (+ `:dev-<sha>`) is the maintainer staging channel;
  `:vX.Y.Z` / `:latest` is production.
- Before a release:
  - If the TimescaleDB tag in `docker-compose.yml` changed since the last release,
    say so in the release notes: back up, update the database image, restart
    `healthlog` (it updates the extension on start; without superuser rights it
    logs the manual command, README "Updating the database image").
  - Check [s6-overlay](https://github.com/just-containers/s6-overlay/releases) for
    a new release. Dependabot can't see it: bump `S6_OVERLAY_VERSION` and both
    `*_SHA256` sums in `backend/Dockerfile` together (from the release's
    `.sha256` files).
- `main` and `dev` are protected (PR required, green checks, no force pushes).

## Checks (must be green before merge)

`test.yml` runs on every PR and is reused by the build workflows:

- `ruff check` + `ruff format --check` (backend)
- `pytest` against a real TimescaleDB service
- a Docker smoke boot (PUID/PGID, `/config`, migrate, health, end-to-end POST)

Run locally:

```bash
cd backend
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+psycopg://healthlog:healthlog@127.0.0.1:5432/healthlog
ruff check . && ruff format --check . && python -m pytest -q
```

## Conventions

- **Language:** everything is English — code, comments, YAML, docs, commit
  messages, PR titles/descriptions.
- **Schema changes:** generate an Alembic revision, never manual `ALTER TABLE`.
  Keep TimescaleDB-specific DDL guarded so the suite runs on plain Postgres.
- **Metrics are extensible by design** (see `docs/ARCHITECTURE.md` §4.0): adopting a
  metric is a `metric_registry` row, not a migration.
- **Money/precision** and other domain rules: see `docs/ARCHITECTURE.md`.

## Dependencies

Dependabot opens weekly grouped PRs (github-actions, pip, docker) against
`dev`. They run through the same `test.yml` gate as any other PR.
