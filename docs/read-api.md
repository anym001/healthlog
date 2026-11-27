# Read API v1 — design proposal

**Status:** proposal, not built. Nothing here is a commitment until a first
consumer confirms it would switch to it (see §1). Discussion:
[#107](https://github.com/anym001/healthlog/issues/107).

## 1. Purpose and precondition

Apps built on HealthLog today read its tables directly, which is unsupported
(`ARCHITECTURE.md` §10): the schema is internal and changes with correctness
fixes, new HAE data types and performance work. A small, versioned, read-only
HTTP API would give such apps a stable contract while the schema stays free
to change.

The first and only known consumer is the Companion
([JHenzi/healthlog-ml](https://github.com/JHenzi/healthlog-ml)). v1 is shaped
by what it reads today (`companion/app/upstream.py`):

| Companion reads today | v1 replacement |
|---|---|
| `metric_samples` | `GET /metrics/{metric}/samples` |
| `metric_registry` | `GET /metrics` |
| `sleep_sessions` | `GET /sleep/sessions` |
| `stress_intraday`, `body_battery_intraday` | `GET /stress/intraday`, `GET /body-battery/intraday` |
| `workouts` | `GET /workouts` |
| `raw_ingest` → State of Mind | `GET /state-of-mind` (parsed since #111) |
| `raw_ingest` → "is new data arriving?" | `GET /status` (`last_ingest_at`, `ingest_seq`) |

**Precondition for building it:** the Companion's maintainer confirms that
the Companion would read only through this API and drop its database role
on HealthLog's database. An API without a consumer is a contract that has to
be kept stable for nobody.

## 2. Goals and non-goals

**Goals**
- One stable, documented, read-only interface to HealthLog's base data.
- No new exposure: the API never leaves the internal network (§3).
- Small enough to keep stable: fixed queries, no query language.

**Non-goals (v1)**
- Write endpoints of any kind. HealthLog stays a single-user pipeline with no
  editable user data (`ARCHITECTURE.md` §10).
- `raw_ingest` (the archive of raw HAE payloads) and the analysis results
  (`findings*`, `workout_load_daily`). The analysis stays internal and free to
  change.
- Browser clients: no CORS, no cookies, no sessions.
- GPS routes (`workout_route_points`) and per-workout HR samples. Addable
  later as additive endpoints if a consumer needs them; GPS is the most
  identifying data HealthLog holds, so it stays out until there is a reason.
- Symptoms, medications, heart rate notifications, ECG summaries. Addable
  additively once a consumer asks.

## 3. Deployment: a separate, internal-only listener

Port 8000 is the ingest endpoint and is usually forwarded to the internet by
the operator's reverse proxy for Health Auto Export. Putting health-data reads
on the same port would make a proxy misconfiguration a full data leak.

- **Separate ASGI app** (`app.read_api`) served by its own s6 longrun
  (`read-api`, a second uvicorn) on `READ_API_PORT` (default `8001`). Same
  image, same code base, no extra container.
- **Off by default:** without `READ_TOKENS` the service stays down and port
  8001 is not bound at all.
- **Not published:** `docker-compose.yml` does not map 8001. Consumers join the
  Docker network (`healthlog-db`'s network, or a dedicated `healthlog-api`
  network) and reach `http://healthlog:8001`. The docs say plainly: never route
  8001 through the reverse proxy.
- The ingest app on 8000 does not mount any `/api/v1` route, so even a proxy
  forwarding everything on 8000 exposes nothing new.

## 4. Authentication: named tokens

- **Config (ENV, it is infrastructure + secrets):**
  `READ_TOKENS=companion:<sha256-hex>,scripts:<sha256-hex>`. Only **hashes** of
  the tokens are stored, so the environment (and `docker inspect`) never shows
  a usable token.
- **Issuing:** `healthlog read-token new <name>` prints a fresh token (32
  random bytes, URL-safe) once, plus the `name:hash` entry to add to
  `READ_TOKENS`. HealthLog never stores the token itself.
- **Revoking:** remove the entry and restart the container. Each consumer has
  its own token, so revoking one doesn't affect the others.
- **On the wire:** `Authorization: Bearer <token>` only, never a query
  parameter (URLs end up in logs). The server hashes the presented token and
  compares it against every configured hash with `hmac.compare_digest`.
- **Every token = all health data in scope.** v1 has no per-token scopes.
  The docs say so next to the variable.
- **Failures:** 401 with no detail, audit-logged with the client IP (never the
  presented token), like ingest's `ingest.auth_failed`, so fail2ban can act on
  it.

## 5. Database access

- All SQL lives in one module (`app/read_api/queries.py`). It is the only code
  that maps the internal schema to the API, and the only place a schema change
  has to be absorbed.
- Every request runs in a **read-only transaction** (`SET TRANSACTION READ
  ONLY`) with `SET LOCAL statement_timeout = '10s'`.
- Fixed, parameterised queries only. No filters beyond the documented
  parameters, no free-form SQL, no sorting options.
- Reads go to base tables, not the views migrations recreate. Where a view's
  logic is the contract (daily values), the query reproduces it in the
  queries module so a view rebuild cannot change the API.

## 6. Endpoints (v1)

All under `/api/v1`, `GET` only, JSON only.

| Endpoint | Returns | Range limit |
|---|---|---|
| `/status` | API version, app version, `tz`, `last_ingest_at`, `ingest_seq` (opaque, increases with every stored payload), last nightly analysis and intraday refresh times | none |
| `/metrics` | registry: `metric`, `display_name`, `unit`, `agg_default`, `category`, `tier`, `auto_registered` | none |
| `/metrics/{metric}/daily` | per local day (HealthLog's `TZ`): `avg`, `min`, `max`, `sum`, `n` | 10 years |
| `/metrics/{metric}/samples` | raw samples: `time`, `source`, `unit`, `qty`, `min`, `avg`, `max`, `n` | 31 days, paged |
| `/sleep/sessions` | per session as stored (naps included, no de-duplication): start/end, in-bed start/end, `source`, `sleep_date`, stage hours | 1 year |
| `/workouts` | per workout: `id` (HAE's UUID), times, `name`, `type` (canonical slug), indoor/location, duration, energy, distance, HR avg/max/recovery, intensity, elevation, temperature, humidity, `source` | 1 year |
| `/state-of-mind` | per entry: `id` (HAE's UUID), times, `kind`, `valence`, `classification`, `labels`, `associations` | 1 year |
| `/stress/daily` | per day: `score`, minutes per state, `hrv_z`, `computed_at` | 10 years |
| `/stress/intraday` | per minute: `time`, `stress`, `hr`, `state` | 31 days, paged |
| `/body-battery/daily` | per day: wake/high/low level, `charged`, `drained`, `computed_at` | 10 years |
| `/body-battery/intraday` | per minute: `time`, `level` | 31 days, paged |

**Derived data (stress, Body Battery).** These are HealthLog's own models,
not Apple data. The *format* is part of the contract; the *values* are not:
they change when the model changes and the operator runs `rederive-stress` /
`rederive-body-battery`. Responses carry `meta.derived = true` and
`meta.model_version`, a version constant per model bumped whenever its output
changes (to be introduced in `analysis/constants.py`). A consumer that caches
these values compares `model_version` and recomputes.

**Identifiers.** Workouts and State of Mind carry HAE's stable UUIDs. Sleep
sessions and samples have no stable ID (`rederive-sleep` rebuilds sessions),
so the API doesn't expose one; consumers key on `(start, source)`.

## 7. Request and response conventions

- **Range:** `from` and `to` are required on every list endpoint, half-open
  `[from, to)`. Timestamps are ISO 8601 with an offset; daily endpoints take
  dates. A range over the endpoint's limit is rejected with 400, not
  truncated.
- **Paging (keyset):** `limit` (default 5,000, max 10,000) and an opaque
  `cursor`. The response has `next_cursor` (null on the last page). Keyset
  rather than offset, so deep pages stay cheap and stable while ingest writes.
- **Envelope:** `{"data": [...], "next_cursor": "...", "meta": {...}}`.
- **Times** in UTC (`Z`); **units** as in the registry (`unit_canonical`).
- **Errors:** `application/problem+json` (RFC 9457) with a stable `type` per
  error.
- **No streaming in v1.** Paging covers the volumes (a month of minute-level
  heart rate is ~45k rows, five pages) without long-lived responses.

## 8. Limits

- **Rate limit per token:** in-memory token bucket, e.g. 120 requests/minute,
  429 with `Retry-After`. In memory is enough: one process, one user.
- **Statement timeout** (§5), **row cap** per page (§7), **range caps** (§6).
- The read API runs in its own process, so a heavy read cannot block ingest's
  event loop. It shares the database, so timeouts and caps are what keep it
  from starving the nightly analysis.

## 9. Audit log

One line per request on the existing `healthlog.audit` logger:

```
api.read token=companion fp=3f9a1c2e endpoint=/api/v1/metrics/heart_rate/samples from=2026-11-01T00:00Z to=2026-11-08T00:00Z rows=5000 status=200 ms=84
```

`fp` is the first 8 hex digits of the token's hash. Never values, never
tokens. Auth failures and rate-limit hits are logged with the client IP.

## 10. Versioning and compatibility

- **`/api/v1` changes are additive only:** new endpoints, new optional
  parameters, new response fields. Consumers must ignore unknown fields.
- **Breaking changes** (removing or renaming a field or endpoint, changing a
  type or a unit, tightening a limit) go to `/api/v2`, served alongside v1 for
  at least six months and two releases.
- **Committed spec:** `docs/api/openapi-v1.json`. A test regenerates the
  OpenAPI document and fails on any difference that isn't additive (a removed
  path, parameter or field, a changed type, a parameter that became required).
  An additive change updates the committed file in the same PR.
- **Schema changes** in HealthLog are absorbed in `queries.py`; the
  per-endpoint tests run against the migrated schema, so a migration that
  breaks the mapping fails CI.

## 11. Privacy and security summary

- HealthLog still makes no outbound calls. The API only answers.
- Internal network only (§3); no CORS; FastAPI's `/docs` stay off on 8001.
- A token is equivalent to read access to all health data in scope (§4).
- Out of scope by design: raw payloads, analysis results, GPS routes.
- What a consumer does with the data (and any external services it uses) is
  the consumer's responsibility, as `ARCHITECTURE.md` §10 states for apps
  built on HealthLog.

## 12. Implementation plan

One PR against `dev` with the whole of v1, tests and docs (README:
`READ_TOKENS`, `READ_API_PORT`, `healthlog read-token`; `ARCHITECTURE.md`: a
read-API section). v1 is only useful to the Companion once every read it
makes is covered, so shipping it in parts would only add releases without a
usable state in between. Within the PR, the work is ordered as:

1. **Skeleton:** `read-api` s6 service, `READ_TOKENS` / `READ_API_PORT`,
   `healthlog read-token new`, auth, rate limit, audit, read-only
   transactions, `/status`, `/metrics`, the committed OpenAPI file and its
   compatibility test.
2. **Metric data:** `/metrics/{metric}/daily`, `/metrics/{metric}/samples`
   with keyset paging.
3. **Events:** `/sleep/sessions`, `/workouts`, `/state-of-mind`.
4. **Derived:** `model_version` constants, `/stress/*`, `/body-battery/*`.

Once it is released, the Companion can drop every direct read.

## 13. Costs and risks

- **Maintenance:** every schema change that touches a covered table must keep
  `queries.py` and the API output unchanged. The tests make this visible, but
  it is real work for each such change.
- **No committed consumer yet** (§1). Mitigated by not building before
  confirmation.
- **Derived values change** under the same format. Mitigated by
  `meta.model_version`.
- **Contract surface:** 11 endpoints. Smaller than direct table access (no
  `raw_ingest`, no findings), larger than the analysis needs. Anything beyond
  this list waits for a concrete request.
