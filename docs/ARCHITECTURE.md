# HealthLog – Architecture & Design

> Self-hosted analysis of Apple Health data with a focus on correlations,
> anomalies and trends — entirely on your own hardware, no external providers.
>
> This document records the **architecture and the design decisions**: the
> *why* behind the code (data model, ingestion contract, analysis methodology,
> privacy bounds). What is implemented and how to use it lives in the README and
> the code; this is where the reasons live.

## 1. Core decisions

- **Data export:** Health Auto Export (iPhone) → REST automation to a self-hosted endpoint
- **Topology:** the always-on server carries **everything statistical** (ingestion + DB + automatic analysis + Grafana, optionally interactive exploration) ⟷ a separate machine is used **only** for the LLM narration; the extra memory (e.g. an Apple Silicon Mac's unified memory, or a box with a capable GPU) only pays off there
- **Analysis core:** classic statistics/ML (correlations, anomalies, trends) — **no** LLM in the critical path
- **LLM:** Ollama on a separate machine with enough memory (≈16–32 GB) as an **optional add-on** for plain-text reports; target class 8–14B (e.g. Qwen 2.5 14B)
- **Data focus:** activity & training, sleep & recovery, vitals
- **Privacy:** 100% own hardware, no external calls — the LLM stays local too
- **Deployment target:** Unraid; the app image should be **public-ready**
  (LinuxServer virtues: PUID/PGID, `/config`, env config) — see §6

## 2. Target architecture

```
┌─ iPhone ──────────────────────────────────────────────┐
│  Health Auto Export  →  Automation "REST API"          │
│  (nightly, JSON POST, with secret header over TLS)     │
└───────────────────────────┬───────────────────────────┘
                            │  HTTPS (reverse proxy)
┌─ Always-on server (Docker / Unraid) ────────────────────┐
│  healthlog  (ONE app container, PID 1 = s6-overlay)     │
│    ├─ service: uvicorn   → FastAPI ingest, 24/7         │
│    │     validates, archives raw JSON, writes (upsert)  │
│    └─ service: scheduler → APScheduler process          │
│          at night → starts analysis as a SUBPROCESS:    │
│          · lag correlations · STL trends · anomalies    │
│          → findings into table `findings`               │
│  TimescaleDB  (Postgres + hypertables)  ← single source │
│  Grafana  → dashboards / trends                          │
└───────────────────────────┬───────────────────────────┘
                            │  read-only (psql)
┌─ LLM host (optional, enough memory) ───────────────────┐
│  LLM narration ONLY: Ollama → report from `findings`    │
│  e.g. an Apple Silicon Mac or a box with a capable GPU  │
└────────────────────────────────────────────────────────┘
(Interactive exploration, if wanted, also runs on the server.)
```

### Process separation instead of container separation (rationale)

Ingest and analysis live **in one container**, but as **separate OS processes**
under `s6-overlay` (clean PID 1: signal handling, zombie reaping, restart policy).
The compute-heavy analysis (pandas/numpy/statsmodels) must **not** run in the uvicorn
process — through the GIL/CPU load it would block the event loop and delay HAE POSTs.
Separate processes = OS-level isolation. The scheduler additionally launches the
analysis as a **short-lived subprocess** (`python -m app.analysis`), so that even a
hard crash in a C extension only kills that subprocess — the scheduler **and** uvicorn
survive, and data intake is structurally shielded. The subprocess runs under a hard
timeout (a wedged run is killed and alerted like a crash), and a missed slot is
caught up at the next start: the scheduler records each successful run in a marker
file under `/config/state/` and, when the most recent cron slot passed without a
run (container down at 03:30), runs the analysis immediately on startup — the
snapshot-replace write makes the extra run idempotent.

| Component | Where | Why |
|---|---|---|
| Ingestion (uvicorn) | app container, long-running process | 24/7 intake of HAE POSTs |
| Scheduler (APScheduler) | app container, own process | triggers at night, logs→stdout, env/TZ clean |
| Analysis | app container, subprocess of the scheduler | fault-isolated, never endangers intake |
| TimescaleDB | own container | Postgres is separate anyway |
| Grafana | own container | straight onto Timescale |
| Interactive exploration (optional, Jupyter) | server | ad-hoc analysis only; pipeline + Grafana cover the normal case, data stays on the server |
| LLM reports | separate host (optional) | the **only** reason for a second machine: enough memory for local 8–14B models (e.g. an Apple Silicon Mac's unified memory, or a GPU box) |

## 3. Tech stack & rationale

| Layer | Choice | Why |
|---|---|---|
| Ingestion | Custom **FastAPI** (instead of the official HAE server) | HAE posts JSON to any endpoint; consistent with SQL storage; familiar stack. The official server relies on MongoDB — a break from the SQL analysis goal. |
| Storage | **TimescaleDB** (Postgres extension) | time-series hypertables, continuous aggregates for daily values, SQL for correlations, native Grafana integration. |
| Scheduler | **APScheduler** (own process under s6) | schedule in code (versioned), logs→stdout (Docker-native), env/TZ clean — vs. cron-in-container friction. |
| Analysis | **Python: pandas + statsmodels + scipy + scikit-learn** | mature, reproducible standard for correlation/trend/anomaly. |
| Dashboards | **Grafana** | minimal effort, straight onto Timescale. |
| Container base | **`python:3-slim` + s6-overlay v3** (exact tag: `backend/Dockerfile`) | slim image (relevant for public use), full control, PUID/PGID + `/config`. |
| LLM (optional) | **Ollama**, 8–14B (e.g. Qwen 2.5 14B) | local on a separate machine with enough memory; receives only finished findings, not the raw data. |

## 4. Data model

> The schema follows the real HAE payload structure (aggregation, fields,
> units), verified against a real export (v2, 7 days, 30 metrics + 1 workout).

### 4.0 Guiding principle: metrics extensible at any time

The data model is deliberately **metric-agnostic** — a new metric (unknown today,
in a future iOS/HAE update or when tracking changes) requires **no schema change and
no migration**. Five building blocks carry this:

1. **Generic values table** (`metric_samples`, §4.2): `metric` is a column,
   **no** metric gets its own table columns. `qty`/`vmin`/`vavg`/`vmax` cover
   both HAE shapes.
2. **Raw archive** (§4.1): takes every payload verbatim — even fields the parser
   doesn't (yet) know are never lost and can be re-parsed later.
3. **Tolerant ingest** (§5): unknown metrics are **accepted, not rejected** — they
   land in `metric_samples` and automatically create a **registry stub**
   (`tier='secondary'`, unit from the payload) that only needs human classification.
   No POST fails on a new metric.
4. **Registry, not code** (§4.5): a metric's behaviour (canonical unit, daily
   aggregate, tier, category) is **data, not code** — "adopting" = one row.
5. **Generic daily aggregates** (§4.7): the view aggregates per `(day, metric)`,
   entirely without metric names in code — new metrics appear automatically.

Consequence: "carry one more metric" normally means **only** extending the HAE
export + possibly maintaining one registry row. Special cases with their own
structure (sleep §4.3, workouts §4.4) remain the only tables with a dedicated schema.

### 4.1 Raw archive (replay-capable)

```sql
-- Every incoming HAE payload verbatim, before parsing.
raw_ingest (id BIGSERIAL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB, source_ip INET, content_hash BYTEA,
            PRIMARY KEY (id, received_at))   -- hypertable on received_at
            -- content_hash indexed → identical re-posts are discarded.
```
Full fidelity: on parser/schema bugs the history can be **re-parsed** without data
loss. The hypertable has a **native compression policy** (chunks older than 7 days;
migration 0016) — repetitive JSON compresses by an order of magnitude, so the
permanent archive stays cheap yet fully queryable. Because a hypertable needs the
partition column in every unique index, the PK is the composite `(id, received_at)`
and dedup uses an indexed SELECT-then-INSERT instead of `ON CONFLICT`; the race this
admits (two *concurrent* identical posts) is harmless — all downstream upserts are
idempotent and HAE posts sequentially.

### 4.2 Parsed measurements (hypertable)

HAE delivers, per metric, a `data` array of buckets in exactly **two shapes**:
- **`{Min, Avg, Max}`** — in practice **only `heart_rate`**.
- **`{qty}`** — all other 29 metrics (incl. HRV, resting HR, respiratory rate, SpO₂).

So **one row per metric bucket** with nullable aggregate columns (fill what HAE
delivers), **not** a single `value`. The model is **generic**: every metric lands
here without a schema change (see inventory §4.6) — the ingest accepts **all** metrics, the
registry classifies them:

```sql
metric_samples (time TIMESTAMPTZ, metric TEXT, source TEXT, unit TEXT,
                qty   DOUBLE PRECISION,   -- point value/sum (29 of 30 metrics)
                vmin  DOUBLE PRECISION,   -- HAE "Min" (really only heart_rate)
                vavg  DOUBLE PRECISION,   -- HAE "Avg"
                vmax  DOUBLE PRECISION,   -- HAE "Max"
                n     INTEGER,            -- sample count in the bucket, if present
                UNIQUE (metric, time, source))   -- idempotency, see §5
                -- hypertable on time
```
**Confirmed quirks:** `date` = `'YYYY-MM-DD HH:MM:SS +0200'` (space, explicit TZ
offset per value → clean as `TIMESTAMPTZ`). `source` can be **empty (`''`)**, a
single device or **pipe-concatenated** (`'Apple Watch …|iPhone …'`) and sometimes
contains a no-break space — the idempotency key must tolerate that (never assume
`source` is NULL-only).

### 4.3 Sleep (own table, interval-based)

Sleep doesn't fit `metric_samples` — it is an interval with phases:

```sql
sleep_sessions (sleep_start TIMESTAMPTZ, sleep_end TIMESTAMPTZ,        -- sleepStart/End
                in_bed_start TIMESTAMPTZ, in_bed_end TIMESTAMPTZ,      -- inBedStart/End
                source TEXT,
                sleep_date DATE,         -- HAE `date` = midnight of the wake-up day
                total_sleep_h, deep_h, core_h, rem_h, awake_h,         -- hours, decimal
                asleep_h, in_bed_h DOUBLE PRECISION,
                UNIQUE NULLS NOT DISTINCT (sleep_end, source))
```
**Natural key = `(sleep_end, source)` (wake-up identity, migration 0011).**
The HAE REST-API push captures the same night multiple times — each with a later
`sleepStart` but identical `sleepEnd`. Keying on `sleep_end` collapses these
re-captures on upsert (the most complete recording, with the largest `total_sleep_h`,
wins), while genuinely separate periods (e.g. a nap with a different end) are kept.
`NULLS NOT DISTINCT` keeps replay idempotent even on a rare NULL `sleep_end`. The view
`sleep_nightly` (migration 0010) additionally reduces to one row per calendar night
(`sleep_date`) for dashboards/analysis.

HAE delivers, per night, `sleepStart`/`sleepEnd`/`inBedStart`/`inBedEnd`, the phase
**hours** (decimal) `deep`/`core`/`rem`/`awake` and `totalSleep` (= `deep+core+rem`);
`asleep`/`inBed` come as `0` (phases broken out separately) → tolerate nullable/0.
**Day assignment is HAE's own:** `date` is midnight of the wake-up day (e.g.
`date=06-09`, `sleepStart=06-08 20:56`, `sleepEnd=06-09 05:56`) → `sleep_date`
adoptable 1:1, matching the correlation convention. Sleep crossing midnight stays one row.

### 4.4 Workouts

HAE delivers, per workout, a **stable `id` (UUID)** — a better idempotency key than
`(start, type, source)`. Scalars come as `{qty, units}` objects, plus a `heartRate`
summary `{min, avg, max}` and intra-workout time series (`heartRateData`,
`stepCount`, …). The HR series is parsed into `workout_hr_samples` (for zone-based
Edwards TRIMP, see [`workout-analysis.md`](workout-analysis.md)). With route export
enabled, an outdoor workout also carries a `route` array → `workout_route_points`
(behind the Workout Detail geomap — display only); the parser accepts both HAE route
schemas (v2 `latitude`/`longitude`, v1 `lat`/`lon`). Other time series stay in the
raw archive.

```sql
workouts (hae_id UUID PRIMARY KEY,        -- HAE `id`, stable → idempotency
          start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
          name TEXT,                       -- HAE `name` (LOCALISED, see below)
          location TEXT, is_indoor BOOL,
          duration_s, total_energy_kcal, active_energy_kcal,
          distance_km, avg_hr, max_hr, hr_recovery,   -- recovery indicator
          intensity, elevation_up_m,
          temperature_c, humidity_pct DOUBLE PRECISION,  -- environment context
          source TEXT)

workout_hr_samples (workout_hae_id UUID REFERENCES workouts ON DELETE CASCADE,
                    ts TIMESTAMPTZ, bpm DOUBLE PRECISION,
                    PRIMARY KEY (workout_hae_id, ts))   -- intra-workout HR series

workout_route_points (workout_hae_id UUID REFERENCES workouts ON DELETE CASCADE,
                      ts TIMESTAMPTZ, lat, lon DOUBLE PRECISION NOT NULL,
                      altitude_m, speed_mps DOUBLE PRECISION,  -- optional
                      PRIMARY KEY (workout_hae_id, ts))   -- intra-workout GPS route
```
**Watch out — localisation:** `name` is language-dependent (`'Outdoor Walk'`) — like
units, workout types need normalisation (mapping localised→canonical), otherwise
types fragment across language switches. `app/workout_types.py` handles this (built-in
DE+EN map → canonical slug, extensible via `workouts.type_map` in `config.yaml`).
`duration` in seconds, energy in `kJ` (→ normalise to kcal, §4.5).

Two **SQL functions** (`workout_trimp(p_tz, p_hr_max, p_since)` per session,
`daily_trimp(...)` per day; migration `0019_workout_trimp_functions`) provide the
single live Banister-TRIMP definition every Grafana panel reads — sample-resolved
over `workout_hr_samples` with an average-HR fallback, same consolidation role
the `sleep_metrics` view plays for sleep efficiency. They are deliberately
distinct from the analysis' profile-driven TRIMP series (`workout-analysis.md`
§3.1): zero-setup, parameterised by the dashboard's HR-Max variable.

Those profile-driven daily series — `workout_trimp` (Banister), `workout_edwards`
(zone-based), `workout_load` (kcal), `workout_duration`, `workout_count`,
`workout_intensity` and their per-sport children (`workout_trimp_running` …) —
are in turn persisted per nightly run (migration `0020_workout_load_daily`):

```sql
workout_load_daily (series TEXT, day DATE,
                    value DOUBLE PRECISION NOT NULL,
                    computed_at TIMESTAMPTZ,
                    PRIMARY KEY (series, day))   -- nightly snapshot, delete + rewrite
```

Snapshot semantics like `findings` — each run rewrites the whole table, because
past days legitimately change when the rolling resting-HR baseline or the
resolved HR_max shifts. This feeds the Grafana panels only the nightly analysis
can serve (Banister vs. Edwards comparison, per-sport zone load); the live
`workout_trimp` functions above stay the source for everything interactive.

### 4.5 Metric registry (normalisation)

```sql
metric_registry (metric TEXT PRIMARY KEY, display_name TEXT,
                 unit_canonical TEXT,
                 agg_default TEXT,   -- 'avg'|'sum'|'min'|'max': which daily value counts
                 category TEXT,      -- 'activity'|'sleep'|'vital'|'mobility'|'environment'
                 tier TEXT)          -- 'core' (correlation/trend focus) | 'secondary'
```
Prevents the same physiological quantity from fragmenting under multiple names/units
(kcal vs. kJ, `count/min`), and tells the analysis **which** daily aggregate makes
sense per metric (steps→sum, resting HR→min, HRV→avg). The **`tier`** separates
analysis focus (`core`) from "carried along, but secondary" (`secondary`): the ingest stores
**everything**, the correlation/anomaly pipeline runs by default over `core` only
(bounding the multiple-testing load, §11), `secondary` stays queryable at any time.

**Unit guard:** HAE ships the unit per value and can localise it. **Confirmed in
reality:** energy arrives as **`kJ`** (not kcal), plus `kcal/hr·kg`, `km/hr`, `m/s`,
`degC`, `ml/(kg·min)`. `metric_registry.unit_canonical` is the target unit; on ingest
the incoming `unit` is checked against it → on mismatch **convert** (known factor,
kJ→kcal ×0.239006) **or flag**, never silently accept (this case occurred in a real
export; a test pins it).

**Plausibility envelope:** beyond units, a metric may carry optional `value_min`/
`value_max` bounds (canonical unit) in the registry seed (`app/registry.py`). After
unit normalisation the ingest parser drops any value outside the envelope — a spurious
`heart_rate = 0` or negative `step_count` never reaches `metric_samples`, so it can't
corrupt the median/MAD baselines or correlations. The bounds are generous sanity rails
(non-negativity for counts, wide physiological ranges for vitals), not clinical limits.
Dropped values are **counted** (`implausible_values` in the ingest response, alongside
`flagged_units` and `dropped_workouts` — workouts discarded for a missing/unparseable
id — and logged) but never lost — the verbatim payload still lands in the raw archive
(§4.1), so a later re-derive can recover them once the envelope widens. A
flagged-but-unconverted value is exempt: its unit isn't canonical, so the bounds would
compare against the wrong scale.

### 4.6 Metric inventory & tiering

The curated registry — every metric's tier, canonical unit, daily aggregate
(`agg_default`) and category — is **seeded and owned by code**: `app/registry.py`
(the seed) plus the curation migrations (`0003_curate_metrics`,
`0006_curate_cycling_distance`, `0008_workout_categories`, …), with
`test_registry.py` pinning its consistency. That is the single source of truth;
this section explains how it is organised rather than re-listing it here (where it
would only drift). For the live state run `healthlog audit`.

Two axes:

- **`tier`** — `core` metrics drive the correlation/anomaly/trend pipeline by
  default (bounding the multiple-testing load, §11); `secondary` metrics are
  ingested and stay queryable but are kept out of the default analysis. Roughly:
  activity totals, the heart/HRV/resting-HR vitals, sleep, `vo2_max` and
  `cardio_recovery` are `core`; the walking-gait/mobility metrics, audio exposure
  and `basal_energy_burned` are `secondary`.
- **`category`** — `activity` | `sleep` | `vital` | `mobility` | `environment`,
  plus `mindfulness`/`nutrition` (added when the first backfill surfaced
  `mindful_minutes`/`dietary_water`).

Carrying more metrics costs practically nothing (§4.0): new ones land automatically
in the raw archive and `metric_samples`, "adopted" with a single registry row.

### 4.7 Daily aggregates (view)

Currently a **plain SQL view** (no Timescale continuous aggregate — the data volume
doesn't require it; a CA can replace it later without a schema break). It computes
**all** aggregates per `(day, metric)`; the analysis picks, per metric, the column
indicated by `metric_registry.agg_default`:

```sql
daily_metrics (day, metric, avg, vmin, vmax, sum, n)
  -- (time AT TIME ZONE 'Europe/Vienna')::date  ← local day, NOT UTC!
```
The view uses `COALESCE(vavg, qty)` / `COALESCE(vmin, qty)` / `COALESCE(vmax, qty)`
(migration `0005_daily_metrics_coalesce`), identical to the analysis loader
`load_daily_series` — so Grafana and the pipeline see the same daily values. `avg` is
an unweighted mean of the bucket means — sufficient for daily granularity, exactly
recomputable from the raw archive.

### 4.8 Pipeline findings (pure statistics, no LLM)

```sql
findings (id, computed_at, kind TEXT,            -- correlation|anomaly|trend|seasonality|recovery_alert|consistency|training_load|training_status|stress|body_battery|weekly_*|monthly_*|fitness_markers
          metric_a TEXT, metric_b TEXT,          -- metric_b only for correlation
          lag_days INT, coefficient DOUBLE PRECISION,
          p_value DOUBLE PRECISION, p_value_adj DOUBLE PRECISION,  -- FDR
          ref_date DATE,                          -- reference day (anomaly/trend point)
          window_start DATE, window_end DATE,     -- analysis window
          severity DOUBLE PRECISION,
          details JSONB,                          -- kind-specific extras
          note TEXT)
```
`ref_date`/`window_*` make a finding **markable** in Grafana (annotation on the day of
the anomaly). Non-applicable fields stay NULL.

`findings` is the **current snapshot** — each run deletes and rewrites it, so every
consumer (Grafana, narration, audit) always sees exactly one coherent result set.
Each run additionally **appends** its snapshot to `findings_history` (same columns,
one shared `computed_at` per run as the run key), so findings stay queryable over
time — "since when has the ACWR been warning?", "how many recovery alerts this
month?". The archive is query-only: the pipeline never reads it, and at a few
hundred rows per day it needs no retention policy.

A thin **`findings_feed`** view (migration `0020`) exposes the snapshot with a
per-kind one-line `detail` string and a display `day` (`ref_date` falling back
to `window_end`), so the Grafana findings tables share one rendering definition
instead of each duplicating the CASE expression.

**Finding types** (snapshot per run, `app/analysis/findings.py`):
- **correlation** — Spearman on the **residual** series (STL trend *and* seasonal
  components subtracted), so the coefficient measures pure day-to-day co-movement;
  lags 0–3 days (both directions), FDR `p_value_adj`, per pair only the **strongest**
  lag/direction. Removing only the trend left shared weekly/annual rhythms in, which
  correlated spuriously (validated on live data). Each finding stamps two comparison
  Spearmans at the same lag — the **raw** coefficient (`details.raw_coef`) and the old
  **de-trended** one (`details.detr_coef`) — and the raw value also **guards**: a pair
  is reported only if raw and residual agree in sign with `|raw_coef| ≥
  analysis.corr_raw_min_abs` (default 0.15), which rejects both single-basis artefact
  classes (seasonality-only and decomposition-noise) metric-agnostically — the
  discriminator is a property of the *pair*, not per-metric coverage. Two relevance
  filters then cut structural noise: an **effect-size floor** (`analysis.corr_min_abs`,
  default 0.25) drops negligible pairs, and **activity-volume suppression** drops a pair
  when *both* series measure how much you moved/trained (`_is_activity_volume`) —
  activity vs a body-state metric is kept. Survivors get a **priority tier**
  (`details.priority_tier`, via `_pair_tier`) so the narration and the Grafana "Top
  Correlations" panel lead with cross-subsystem links.
- **anomaly** — 28-day trailing median + MAD (robust z), last 14 days only. The
  trailing z inflates when the recent window is unusually calm (a hard workout after a
  taper scores z>20 yet is normal vs the whole history), so a flag is **corroborated
  against the full history**: kept only if `|z vs the global median+MAD|` clears
  `anomaly_min_global_z` (`details.global_z`). Co-derived workout-load metrics
  (`workout_{trimp,load,edwards,…}`) flagging the **same day** collapse to the single
  strongest anomaly rather than reporting one session several times.
- **trend** — STL trend component (slope + strength). Strength (Wang/Hyndman) only
  certifies smoothness, not direction — a smooth up-then-back meander scores high with
  no net drift — so a finding also requires monotonic movement:
  `|Spearman(trend, time)|` ≥ `trend_min_monotonicity` (`details.monotonicity`).
- **seasonality** — MSTL(7, 365): yearly pattern (amplitude + peak/trough month), from
  ≥2 years; phase flagged uncertain (`phase_confident`) if peak and trough are <2 months
  apart. MSTL fits *some* annual component for every series, so a finding is kept only
  if the seasonal *shape* **recurs** year over year (`details.reproducibility` = mean
  Spearman between calendar years' monthly profiles, floor
  `seasonality_reproducibility_min`) — rejecting a one-off overfit while keeping a
  genuine cycle.
- **recovery_alert** — combined: HRV notably low **and** resting HR high (+ optionally
  short sleep).
- **consistency** — rolling spread of sleep duration and bedtime (midnight wrap handled).
- **training_load** — ACWR (acute 7-day / chronic 28-day) on the daily training load
  (`workout_trimp`, HR-based via Banister; otherwise `workout_load` in kcal); only
  flagged on a load spike (overload) or detraining. Details see
  [`workout-analysis.md`](workout-analysis.md).
- **training_status** — descriptive CTL/ATL/TSB (fitness/fatigue/form) snapshot on the
  aggregate daily load, written **every** run (a status like consistency, not an
  alert); zone classified on TSB/CTL. Feeds the narration's baseline. Details see
  [`workout-analysis.md`](workout-analysis.md) §5.2.
- **stress** — alert-only surfacing of a high-stress **day** (daily score ≥
  `stress.alert_score`, recent days only); the continuous score + timeline live in
  their own tables (§4.9), this only lets the narration mention notable days.
- **body_battery** — alert-only surfacing of a low-reserve **day** (daily low level ≤
  `body_battery.alert_level`, recent days only); the continuous 0-100 reserve +
  timeline live in their own tables (§4.10), this only lets the narration flag a day
  the tank ran near empty.
- **weekly_\*** — descriptive week summaries for the weekly report (`narrate
  --report weekly`); status findings like `training_status`, written **every** run.
  All windows are trailing 7 days anchored on the **last day with any data** (shared
  anchor via `series_anchor`, so a lagging export can't produce an empty week);
  "previous week" is the window immediately before, the 4-week baseline the mean of
  the four windows before that. Five kinds: **weekly_training** (session count,
  duration, distance, kcal from `workouts` + the weekly load total off the daily
  load series, overall and per sport), **weekly_sleep** (nightly averages: duration,
  deep/REM shares, efficiency, circular-mean bedtime), **weekly_stress** /
  **weekly_body_battery** (week profile off `stress_daily` / `body_battery_daily` —
  the whole week, not just alert days), **weekly_vitals** (RHR/HRV week mean vs the
  trailing 28-day baseline) and **weekly_activity** (weekly totals of steps, active
  energy, exercise minutes, daylight). Pure math in `weekly_window` /
  `weekly_*_summary` (`pure.py`).
- **monthly_\*** — the monthly analogues (`narrate --report monthly`), same five
  kinds and anchoring over a rolling **28-day** window (`MONTH_PERIOD` = four full
  weeks, so every weekday is represented equally): "previous month" is the 28 days
  before, the baseline the mean of the three prior windows (~a quarter; vitals
  compare against the trailing 84 days). Each finding's details additionally carry
  a `weeks` list — the window split into four 7-day aggregates, oldest first
  (`week_breakdown` in `pure.py`) — so the monthly narration can describe the
  month's course, not just its totals. The alert kinds' `*_recent_days` tunables
  default to 31 so the stored snapshot covers the monthly report's 28-day lookback.
- **fitness_markers** — latest reading of the slow markers (VO2 Max, cardio
  recovery, body mass) plus the change against the most recent reading ≥28 days
  older, and against ≥90 days older for the monthly report's ~quarter view
  (`latest_marker_delta`); they move too slowly for weekly windows. Shared by the
  weekly and monthly reports.

The pure analysis math is DB-free and tested against synthetic series (known
lag/anomaly/trend/yearly-season) with a fixed seed (§7); plus a DB end-to-end test.

### 4.9 Stress proxy (own tables)

A Garmin-style **stress score** derived from the data Apple Health actually
exports. It is explicitly **not** the Garmin/Firstbeat value: that needs
beat-to-beat **RR intervals**, which HAE does not export (heart rate arrives only
as per-minute `{Min,Avg,Max}` buckets or a `{qty}`). So the score is a *proxy*
that tracks the user's **own baseline** over time and is **not comparable** to a
Garmin number.

```sql
stress_intraday (ts TIMESTAMPTZ PK, stress SMALLINT, hr REAL, state TEXT)  -- one row per HR bucket
stress_daily    (day DATE PK, score REAL, {rest,low,medium,high,active,unmeasurable}_min INT,
                 hrv_z REAL, computed_at)                                   -- one row per local day
```

**Model** (`app/analysis/stress.py` + pure helpers in `pure.py`,
`stress_intraday_from_hr` / `summarize_stress_day`): for each non-workout
heart-rate bucket, stress scales the reserve above the personal resting baseline —
`HRr = clamp((hr − HR_rest) / (reserve_full·(HR_max − HR_rest)), 0, 1)` → `100·HRr`
— reusing the same `HR_rest`/`HR_max` resolution as the training load
([`workout-analysis.md`](workout-analysis.md) §3.1). A low-HRV day (negative
`hrv_z`) multiplies the score up (the HRV calibration); workout minutes are
excluded (`state="active"`, Garmin's grey band) and gaps are `"unmeasurable"`.
The daily `score` is the dwell-weighted mean over measured minutes; a day below
`stress.min_measured_min` measured minutes yields no row (a gap, not a zero).

**Movement gating** — Garmin gates activity out via the accelerometer; the proxy
approximates that with per-minute **step counts**: a bucket at/above
`stress.active_steps_per_min` (default 60 — a normal walking cadence is ~100
steps/min) is classified `"active"` like a workout minute, so a brisk walk or a
stair climb elevates the heart rate without registering as stress. The gate needs
per-minute `step_count` buckets and **self-disables** on coarser data (median
step-bucket gap > 2 min — an hourly step total would spuriously gate the single
co-timed bucket); `0` turns it off.

**Storage & recompute** — dedicated tables, **never** written back into
`metric_samples` (which stays a replayable mirror of the raw archive, §4.1). The
nightly run recomputes a trailing window (`stress.window_days`) and replaces those
rows idempotently; `healthlog rederive-stress [--all|--days N]` rebuilds the full
history (e.g. after a backfill or a config change). In between, the hourly
**intraday refresh** (`app/analysis/refresh.py`, scheduled via `INTRADAY_CRON`)
recomputes just the trailing two days of stress + Body Battery — no findings, no
notifications — so today's timeline stays current between nightly runs. Grafana
reads both tables (the **Stress** dashboard); a high-stress day also becomes a
`stress` finding (§4.8) for the narration. All tunables live under `stress.*` in
`config.yaml`.

### 4.10 Body Battery (own tables)

A Garmin-style **energy reserve** (0-100): the intraday stress timeline (§4.9)
integrated against recovery. It is a **proxy on a proxy** — it builds on the stress
score, which HAE cannot derive from beat-to-beat RR intervals — so it too tracks the
user's **own baseline** and is **not comparable** to a Garmin number.

```sql
body_battery_intraday (ts TIMESTAMPTZ PK, level SMALLINT)                 -- one row per bucket
body_battery_daily    (day DATE PK, wake_level, high_level, low_level SMALLINT,
                       charged, drained REAL, computed_at)                -- one row per local day
```

**Model** (`app/analysis/body_battery.py` + pure helpers in `pure.py`,
`body_battery_timeline` / `summarize_body_battery_day`): a **self-correcting rate
integrator** over the window's `stress_intraday` buckets plus the sleep intervals.
Each bucket contributes a balance rate (points/min), dwell-weighted like the stress
summary: awake buckets **drain** above a `neutral` stress level
(`−drain_rate·(stress−neutral)/(100−neutral)`) and **charge** at/below it
(`+charge_rate·(neutral−stress)/neutral`); a bucket inside a sleep interval charges
`+sleep_charge_rate·efficiency`; a workout bucket (`state="active"`) drains
`−active_drain_rate`; an `unmeasurable` bucket holds (rate 0, invents nothing).
Integrated as `level(t) = clamp(level(t−dwell) + rate·dwell, 0, 100)` from a neutral
`seed_level` at the window's first bucket. Because the integrator is **continuous
across day boundaries**, it is run once over the whole window and then sliced per
local day for the summary (`wake_level` = the level at the end of the day's main
sleep, i.e. what you started the day with). A day with fewer than
`body_battery.min_measured_min` informative stress minutes stores no daily row —
a barely-worn day's level merely *holds*, so a summary would feign knowledge
(mirroring `stress.min_measured_min`); the intraday timeline is stored regardless.

**Auto-calibrated neutral** — the stress score is relative to the personal resting
baseline (§4.9), so a *fixed* energy-neutral level sits wrong for most people: with a
calm baseline every day scores below it and the battery pins at 100 (a high baseline
pins it at 0). By default (`body_battery.neutral` unset) each run therefore derives
the neutral level from the personal distribution: the
`BODY_BATTERY_NEUTRAL_PERCENTILE`th percentile of the measured *awake* stress minutes
over the trailing `BODY_BATTERY_NEUTRAL_LOOKBACK_DAYS` (sleep buckets excluded — their
near-zero stress would drag the percentile down), clamped to
`[BODY_BATTERY_NEUTRAL_FLOOR, BODY_BATTERY_NEUTRAL_CEIL]` (pure helper
`auto_neutral`). The lookback is anchored at the recompute window's *end*, not at the
window itself, so the nightly, hourly-refresh and full-rederive runs of the same data
agree on one neutral and windowed recomputes still reproduce full-history rows. Under
`BODY_BATTERY_NEUTRAL_MIN_MINUTES` of usable data the run falls back to the fixed
default (`BODY_BATTERY_NEUTRAL`); either way the value in effect is logged. Setting
`body_battery.neutral` to a number pins it and skips the derivation. The derived
neutral tracks the baseline as it drifts (like the stress baseline itself), which is
the point — absolute levels months apart are comparable only in the proxy-relative
sense both scores already carry.

**Drift & the sleep re-anchor** — an accumulator's risk is unbounded drift. It is
avoided *without* a hard-coded reset: sleep charges strongly and the level is clamped
at 100, so a normal night pushes the battery back toward full and the `seed_level`
washes out within a few days. The wake level is therefore an **emergent** function of
sleep quality, not a fixed number — mirroring how Garmin's battery visibly re-charges
overnight.

**Warm-up margin** — a day's *last* write happens on the nightly run where it is the
trailing window's first day; storing that run's values verbatim would permanently
keep the seed-influenced computation for every archived day. A bounded recompute
therefore integrates `BODY_BATTERY_WARMUP_DAYS` (7) extra days *before* the stored
range — enough sleep re-anchors to settle the integrator — and stores only the
requested window, so the archived levels match what a full-history run would produce.

**Storage & recompute** — dedicated tables, **never** written back into
`metric_samples` (§4.1), mirroring the stress precedent. The nightly run recomputes a
trailing window (`body_battery.window_days`) and replaces those rows idempotently,
**after** the stress pass has flushed (it reads the fresh `stress_intraday`);
`healthlog rederive-body-battery [--all|--days N]` rebuilds the full history — run it
*after* `rederive-stress`. Grafana reads both tables (the **Stress** dashboard's Body
Battery panels); a low-reserve day also becomes a `body_battery` finding (§4.8) for
the narration. All tunables live under `body_battery.*` in `config.yaml`.

## 5. Ingestion contract

- **Idempotency/upsert:** on nightly exports HAE sends **overlapping windows**.
  Without dedup, duplicates grow and distort daily aggregates. Hence
  `INSERT … ON CONFLICT (UNIQUE key) DO UPDATE` on all target tables.
  `raw_ingest.content_hash` discards identical re-posts early (indexed
  SELECT-then-INSERT, see §4.1). Upserts keep working against **compressed**
  `metric_samples` chunks (TimescaleDB decompresses the affected segments) —
  a full-history re-backfill stays a no-op, just slower on old chunks.
- **Backfill vs. delta:** on the first manual HAE export, load the **entire Apple
  Health history** (years) once as a bulk backfill → then **nightly deltas**. That way
  correlations are sound from day 1 (≥6–8 weeks). The full export blows past the HTTP
  limit (`MAX_PAYLOAD_BYTES`) and the proxy timeout; so it runs **file-based** via the
  CLI `healthlog backfill <path>` (file or directory, `--dry-run` to check) — the same
  `archive_raw → parse → store` pipeline as the endpoint, committed per file,
  idempotent (re-run = no-op thanks to `content_hash` dedup + upsert).
- **Time zone:** stored in `TIMESTAMPTZ`; all daily buckets in **local TZ
  (Europe/Vienna)**, since the daily grid is the basis of all analyses.
- **Robustness & extensibility:** payload size limit, secret header checked in constant
  time. **Unknown metrics are accepted, never rejected** (§4.0): they land in the raw
  archive **and** in `metric_samples` and automatically create a registry stub
  (`tier='secondary'`, unit from the payload, `agg_default` heuristic from the shape:
  `qty`→`sum`, `Min/Avg/Max`→`avg`). A POST never fails on a new metric; classification
  can be backfilled at any time.
- **Keep units stable:** on the HAE side *"Use Localized Units" = OFF* and fixed unit
  preferences per metric (metric system: kcal/km/kg/°C, HR `count/min`, HRV `ms`, SpO₂
  `%`). Server-side the registry unit guard additionally applies (§4.5) — the app
  setting is precaution, the registry is the safeguard.

## 6. Container topology & deployment

- **One app image** `healthlog` (`python:3-slim` + s6-overlay v3), two s6 services
  (uvicorn, APScheduler), analysis as a subprocess (§2).
- **PUID/PGID + `/config`:** the entrypoint chowns `/config`, drops privileges.
  `/config` holds persistence that isn't in the DB: ingest secret, `config.yaml`,
  narration output, logs, possibly DB backups/export.
- **Env-driven config:** `INGEST_SECRET`, `DATABASE_URL`, `TZ`, `ANALYSIS_CRON`
  (5-field cron), `LOG_LEVEL`, `LOG_FORMAT` (text/json) — secrets + infrastructure via
  ENV, behaviour + profile via `config.yaml` (see
  [`workout-analysis.md`](workout-analysis.md) §4).
- **Compose:** `timescaledb` + `healthlog` + `grafana`; DB not publicly exposed,
  Grafana behind auth, reverse proxy/TLS in front of ingest.
- **Public readiness:** shipped `docker-compose.yml` + `.env.example`, sensible
  defaults, no telemetry, documented HAE automation setup.

## 7. Tests & quality

**A green suite is the mandatory gate before every image push** (§8). The core risk
isn't in CRUD but in **parser correctness** and **analysis math** — that's exactly
where the test focus sits.

- **Backend lint:** `ruff check` + `ruff format --check`.
- **Parser/ingest tests (pytest):** real HAE payload as a fixture → expected rows in
  `metric_samples`/`sleep_sessions`/`workouts`. Pins the translation of the HAE quirks
  (Min/Avg/Max buckets, units).
- **Idempotency:** posting the same payload twice → no duplicates, upsert applies,
  `content_hash` dedup discards the re-post. (The core risk from §5.)
- **TZ bucketing:** a sample around midnight (Europe/Vienna) lands in the correct local
  day; a sleep session is assigned to the wake-up day (§4.3).
- **Analysis math:** synthetic series with a **known** lag correlation → the pipeline
  finds it at the right lag; an injected anomaly is detected; FDR correction lowers
  chance hits. Reproducible with a fixed seed.
- **Migrations against real Postgres/TimescaleDB:** a `service` container in CI,
  `alembic upgrade head` from an empty schema — otherwise the DDL only ever runs at the
  end user's the first time.
- **Smoke:** build the image, boot it with `PUID/PGID` + `/config` mount + Timescale
  service, query `/api/health`, check that the entrypoint chowns and both s6 services
  come up.

## 8. CI/CD – GitHub workflows

Four workflows with pinned action SHAs:

| Workflow | Trigger | Does |
|---|---|---|
| `test.yml` | `pull_request` **+** `workflow_call` | lint, pytest (incl. migrations against the Timescale service), smoke. Reusable so build workflows can gate on it. |
| `dev.yml` | push to `dev` | `uses: test.yml` → only when green: build + push `:dev` and `:dev-<sha>` to **GHCR + Docker Hub**. |
| `build.yml` | push tag `v*` (+ `workflow_dispatch`) | `uses: test.yml` → when green: build + push `:vX.Y.Z` and `:latest` to **GHCR + Docker Hub** + GitHub release (`generate_release_notes`). |
| `dockerhub-readme.yml` | push to `main` touching `README.md` (+ `workflow_dispatch`) | syncs `README.md` to the Docker Hub repository description (`peter-evans/dockerhub-description`). Independent of the image build so a docs-only edit refreshes the Docker Hub page without a release. |

- **Registries: GHCR + Docker Hub** (`ghcr.io/<owner>/healthlog`,
  `docker.io/<DOCKERHUB_USERNAME>/healthlog`). The build pushes both in one step via
  `docker/metadata-action`; the mirror needs the `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`
  secrets (the token also needs write access for the README sync).
- Least privilege: `test.yml` has `contents: read`; only `dev.yml`/`build.yml` request
  `packages: write` (resp. `contents: write` for the release) on their own jobs.
- Platform `linux/amd64` (Unraid target); arm64 can be added on demand (§10).
- **Tests block the push:** a red run prevents `:dev`/`:vX.Y.Z` — a broken commit never
  reaches an image.

**Dependabot** (`.github/dependabot.yml`) keeps dependencies current — three
ecosystems, **weekly**, PRs against **`dev`** (never `main`, §9), each grouped (one PR
instead of many, avoiding mutual merge conflicts on the pinned SHA lines):
- `github-actions` (`/`) — keeps the SHA pins of the three workflows fresh.
- `pip` (`/backend`) — `requirements.txt` + `requirements-dev.txt`.
- `docker` (`/backend`) — base-image bumps (`python:*-slim` tag, s6-overlay).

Dependabot PRs pass the same `test.yml` gate as any other PR.

## 9. Branching & release

- Development on short-lived `feature/*` branches, forked off `dev`.
- **PRs always against `dev`**, never directly against `main`.
- `main` is updated exclusively via a PR `dev → main`; **release = tag push**
  `vX.Y.Z` on `main` → triggers `build.yml` (versioned image + release).
- `:dev` = maintainer staging channel, `:vX.Y.Z`/`:latest` = production.
- `main` and `dev` protected by ruleset (PR required, green checks, no force pushes).
- English throughout (code, comments, commits, PRs).
- The binding rules live in `CONTRIBUTING.md`.

## 10. Scope & deliberate boundaries

- **Single-tenant** (no `user_id`/`subject_id`). The analysis is per person; a later
  multi-user/subject extension is a clean migration (column nullable + default 1 +
  backfill), since the idempotency keys are already stable.
- **Raw payload archived verbatim** (`raw_ingest`, JSONB), retention permanent —
  kept cheap by the native compression policy (§4.1); a CA can replace the
  daily-aggregate view later without a schema break.
- **ECG deliberately off** (raw waveforms, no analytical value, payload burden).
  GPS routes are stored **only** when the operator enables route export in HAE
  (`workout_route_points`, display-only — the analysis never uses location).
  The generic model (§4.0) tolerates further health categories (medications,
  symptoms as event markers) at any time without a schema change, should they
  become relevant.
- **Optional / on the server, not built in:** interactive Jupyter exploration only
  covers the ad-hoc case that pipeline + Grafana already cover.
- **Out of scope (addable on demand):** an arm64 image (currently `linux/amd64` only),
  a dedicated web app.

## 11. Methodological pitfalls

- **Daily grid:** resample everything onto calendar days (local TZ), otherwise metrics aren't comparable.
- **Time lag:** effects act with delay (training today → HRV tomorrow) — use lag correlations, not just lag 0.
- **Spearman over Pearson:** physiological data is often non-linear/non-normal.
- **Multiple testing:** many metric pairs → chance hits. FDR correction (`p_value_adj`), treat findings as hints. Correlation ≠ causation.
- **Enough history:** before ~6–8 weeks correlations are not very sound (→ bulk backfill, §5).
- **Seasonality:** account for weekday effects (weekend ≠ workday) in anomalies.
- **Aggregate semantics:** take the right daily value per metric (registry) — sum steps, minimise resting HR, no "avg over everything".

## 12. Privacy checklist

- Ingest endpoint reachable only over TLS + secret header/token (HAE supports custom headers).
- DB not publicly exposed; Grafana behind auth.
- LLM purely local (Ollama, no API key, no network egress); the narration receives only
  statistical finding quantities, never raw values (`scrub_details()`).
- Don't enable telemetry in any component.
