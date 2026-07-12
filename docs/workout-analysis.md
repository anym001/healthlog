# Workout analysis & structured configuration

> Design and methodology of the training-load analysis — extends
> [`ARCHITECTURE.md`](ARCHITECTURE.md) with the workout part of the pipeline.
> Workouts are condensed into **daily series** and run through the same machinery
> (lag correlations, anomalies, trends): a type-agnostic daily load
> (`workout_trimp` + `workout_load`, ACWR), an optional per-sport load via
> `workouts.type_map`, and zone-based Edwards TRIMP (`workout_edwards`) from the
> intra-workout HR series (§9).

## 1. Motivation

Workouts are fully ingested and stored in the `workouts` table (`app/ingest.py`).
Left out of the analysis, the interesting relationships — training today →
recovery/sleep tomorrow — would go unused: `build_series()` would only know
`metric_samples` (registry `core`) and `sleep_sessions`.

Goal: condense workouts into **daily series** and run them through the **existing**
machinery (lag correlations, anomalies, trends, recovery).

## 2. Guiding idea: turn events into daily series

Once a workout quantity sits as a daily series in the `series` dict,
`_correlation_findings()` automatically tests both directions and lags 0–3. The
bulk of the work is therefore a loader + a few lines in `build_series()`, not a new
analysis engine — analogous to today's sleep series.

### 2.1 Daily features

`load_workout_frame()` returns one row per session tagged with its local day
(`start_time AT TIME ZONE :tz)::date`); `build_series()` aggregates them into the
daily series below (TRIMP via pure helpers):

| Series | Derivation | agg |
|---|---|---|
| `workout_trimp` | daily sum of Banister TRIMP (sessions **with** `avg_hr`) | sum |
| `workout_load` | daily sum of `active_energy_kcal` (fallback, covers **all** sessions) | sum |
| `workout_duration` | daily sum of `duration_s` → h | sum |
| `workout_count` | sessions/day | sum |
| `workout_intensity` | last-weighted mean of `intensity` (if present) | avg |

`workout_trimp` and `workout_load` run **in parallel** (don't mix different units).
Agreement → robust signal; divergence → informative ("much energy, little HR load" =
a long easy session).

Since migration `0020_workout_load_daily` each run also **persists** this
daily-series snapshot — including the zone-based `workout_edwards` parallel and
the per-sport children (§9) — into the `workout_load_daily` table
(`ARCHITECTURE.md` §4.4), so Grafana can chart what only the nightly analysis
computes (Banister vs. Edwards, per-sport zone load). Snapshot semantics like
`findings`: delete + rewrite per run, because past days legitimately change when
the rolling HR_rest baseline or the resolved HR_max shifts.

### 2.2 Central nuance: **0 instead of NaN**

For metrics a missing day means NaN (no measurement). For workouts a training-free
day is a **genuine 0**. The workout series are therefore reindexed over
`[first … last workout day]` and gaps **filled with 0** (not interpolated), only
**within** the observed span. Result: a dense series, ideal for
correlation/anomaly.

## 3. HR-based TRIMP (Banister)

With only **one** average HR per session (Apple Watch provides `avg_hr`/`max_hr`)
plus duration and resting/max HR, Banister TRIMP is computable **without** an
intra-workout time series. The zone-based **Edwards** variant needs the
per-second HR and runs in parallel when that series is present (§9).

```
d     = duration_s / 60                              # minutes
HRr   = clamp((avg_hr − HR_rest) / (HR_max − HR_rest), 0, 1)   # HR-reserve fraction
y     = sex == "female" ? 0.86·e^(1.67·HRr) : 0.64·e^(1.92·HRr)
TRIMP = d · HRr · y
```

`workout_trimp` (day) = Σ TRIMP over the day's sessions. Sessions without `avg_hr`
(often strength) don't contribute → that's what the kcal fallback series is for.

### 3.1 HR_max / HR_rest — fallback chains

```
HR_max  =  profile.hr_max                          # explicit override (max test) wins
        ?: 208 − 0.7 · (year − profile.birth_year) # Tanaka, more accurate than 220−age
        ?: clamp(max(observed max_hr), 160, 210)   # data-driven, always available

HR_rest =  28-day rolling median(resting_heart_rate)  # measured, personalised, time-varying
        ?: profile.hr_rest                            # configured value wins for unmeasured days
        ?: overall median(resting_heart_rate)         # measured fallback when no profile value
        ?: 60                                          # last resort
```

Where measurements exist, the rolling 28-day median always wins. For days it
cannot cover (outside the measured span, or too few points), a configured
`profile.hr_rest` takes priority **over** the overall measured median — an
explicit profile value is treated as an operator override, not a last resort.

From **`birth_year`** (not `age`) the age is recomputed each run, so HR_max drifts
correctly (~0.7/year). The pipeline runs **even with no profile at all** (data-driven
HR_max, male weighting as a documented default) — the profile values are **optional
refinement**, not a precondition.

## 4. Configuration: `config.yaml`

Infrastructure config is ENV-based (`app/config.py:Settings`). For structured values
(profile, type mapping, tunables) HealthLog uses a `config.yaml`. Clear split:

```
ENV   = secrets + infrastructure  (INGEST_SECRET, DATABASE_URL, TZ, PUID/PGID,
                                    LOG_*, ANALYSIS_CRON, NOTIFY_TOKEN)
YAML  = behaviour + profile       (profile, workouts, analysis tunables, and the
                                    non-secret notify fields)
```

### 4.1 File layout (`/config/config.yaml`, mounted)

```yaml
profile:
  birth_year: 1990        # optional → age-based HR_max (Tanaka)
  sex: male               # male | female | unspecified → TRIMP weighting
  hr_max:                 # optional, explicit override (otherwise derived)
  hr_rest:                # optional, otherwise measured from resting_heart_rate

workouts:
  load_metric: both       # trimp | energy | both
  type_map:               # localised HAE name → canonical type
    Laufen: running        # (for type-separated TRIMP)
    Radfahren: cycling
    Krafttraining: strength

analysis:                 # nightly-pipeline tunables (lags, anomaly window, ACWR
  max_lag: 3              #   bands, …). The full set with defaults lives in
  # …                     #   backend/config.example.yaml — the single source;
                          #   analysis/constants.py is the fallback.

notify:                   # token stays in ENV (NOTIFY_TOKEN)
  url:
  events: [analysis, findings]   # ingest | analysis | findings
  level: problems                # problems | always
  verify_tls: true
```

### 4.2 Loading model

- `Settings` (pydantic-settings, ENV) stays for **secrets + infrastructure** and
  drives the s6 services (uvicorn, scheduler, migrate).
- `AppConfig` (pydantic `BaseModel`) loads from `config.yaml` via `load_config()` +
  `validate_config()` in `app/appconfig.py`; ENV only overrides secrets
  (`NOTIFY_TOKEN`). A missing file means valid defaults. `config.example.yaml`
  ships with the repo; the real `config.yaml` is mounted and gitignored.
- The analysis (`app/analysis/`, subprocess) pulls tunables + profile from
  `AppConfig` (the `constants.py` module values are the fallback when the file is
  absent); `profile`/`workouts` drive the TRIMP/HR_max logic as pure, unit-testable
  helpers. Ingest/uvicorn primarily need ENV.
- The non-secret `notify` fields live in the `notify:` block; the token stays in ENV.

## 5. New findings from training load

### 5.1 ACWR (acute:chronic workload ratio)

A sports-science standard, complementing the generic anomalies:

```
ACWR = mean_7d(workout_trimp) / mean_28d(workout_trimp)
```

- `> ~1.5` = load spike (overload/injury risk), `< 0.8` = detraining.
- Stored as `kind = "training_load"` in `findings` — fits in `String(16)`, **no
  migration** needed; `severity = ACWR`, `details = {acute, chronic, ratio}`.
- Computed on `workout_trimp` (more meaningful than on energy). A good candidate to
  also include in the notify `findings` (alongside `anomalies` + `recovery_alerts`).

### 5.2 Training status (CTL/ATL/TSB) — a status, not an alert

The Banister impulse-response smoothing of the same daily load (§10 has the
formulas): CTL = EWMA 42 d ("fitness"), ATL = EWMA 7 d ("fatigue"),
TSB = CTL − ATL ("form"). As an **alert** this would double-score the ACWR —
normalised form is `TSB/CTL = 1 − ATL/CTL`, i.e. monotonically an EWMA variant
of the same acute-vs-chronic idea — so the alerting role stays with §5.1.
What was missing is **context**: the narration could not say *where* the
training state stands when nothing is alert-worthy.

- `kind = "training_status"` (fits `String(16)`, no migration), written **every**
  run like the consistency findings; `metric_a` = the aggregate load series
  (TRIMP preferred), **aggregate only** — form is systemic, not per-sport.
- `details = {ctl, atl, tsb, tsb_pct, zone, ctl_ago, ctl_trend, …}`;
  `severity = |tsb_pct|`. Zones are classified on **TSB/CTL** (scale-free on the
  relative TRIMP estimate — absolute TSB bands only make sense on a calibrated
  TSS scale): `tsb_overreach_pct`/`tsb_fresh_pct`/`tsb_detraining_pct` in
  `config.yaml` (defaults −0.30 / 0.05 / 0.15). `ctl_trend`
  (rising/flat/falling vs. 28 days earlier) states whether the base is growing.
- Skipped below one CTL time constant (42 days) of history — the zero-seeded
  EWMA is still warm-up dominated — and when the chronic load is zero.
- Consumers: the narration renders it as its own section (the report's baseline:
  "productive, base rising"); **notify ignores it** (not an alert; the run
  summary lists its count only at `level: always`). The Fitness & Form section of
  Grafana's Training dashboard computes the same numbers live in SQL — the finding is the nightly snapshot on
  the analysis' richer profile-driven TRIMP (§3.1), queryable over time via
  `findings_history`.

## 6. What it unlocks (lag correlations)

Once the series are in the dict, among others these fall out:

- `workout_trimp[t]` → **HRV[t+1]** (a hard day depresses next-day HRV)
- `workout_trimp[t]` → **resting_heart_rate[t+1]**
- `workout_trimp[t]` → **sleep_deep_h / sleep_total_h / sleep_efficiency**
- `workout_trimp[t]` → **respiratory_rate**, **cardio_recovery**

## 7. Where it lives in the code

1. `app/analysis/`: `load_workout_frame()` (`load.py`) + ~6 lines in `build_series()`
   (`findings.py`; append series, 0-fill), TRIMP/HR_max as pure helpers (`pure.py`).
2. `app/analysis/findings.py`: `_training_load_findings()` (ACWR).
3. `AnalysisResult`: a `training_load` counter (otherwise workout correlations land in
   the `correlations` counter).
4. Config: `AppConfig` + `config.yaml` (section 4).
5. **No** registry row (workout series are hand-wired like sleep).
6. Schema: no per-metric columns; the run's daily-series snapshot is persisted
   into `workout_load_daily` (migration `0020`, `_persist_workout_series()` in
   `run.py`) for the Grafana load-model panels.
7. Tests: `load_workout_frame` (aggregation + 0-fill), Banister TRIMP, the HR_max
   fallback chain, ACWR — all as pure functions against synthetic data with a fixed
   seed.

## 8. Caveats & limits

- **Energy is an imperfect load proxy** (a long walk vs. short intervals) → hence TRIMP
  as an HR-based second series.
- **Banister smooths intervals** (one average HR per session): 4×4 intervals and a
  steady run with the same average HR/same duration yield the same TRIMP. Zone-based
  would resolve that — but needs the per-second HR.
- **HR_max estimation** is the biggest uncertainty; data-driven it tends to
  underestimate. For **relative** comparisons (trends/anomalies/ACWR) uncritical, for
  absolute numbers less sound → `profile.birth_year` resp. `hr_max` improve it.
- **Overlap with `active_energy`**: its daily sum already contains the workout energy →
  a strong `workout_load`↔`active_energy` correlation is expected/trivial. Both count as
  *activity volume*, so the correlation finder suppresses the pair (ARCHITECTURE §4.8,
  `_is_activity_volume`); only activity-vs-body-state pairs are reported.
- **Null inflation** for infrequent trainers → weaker statistics; the `min_overlap`
  guard applies sensibly.
- **Correlation ≠ causation** (ARCHITECTURE §11) — the lag direction "training →
  recovery" is physiologically plausible, but the test only measures co-movement.

## 9. Load metrics in detail

- **Type-agnostic:** all workouts into one daily load (`workout_trimp` +
  `workout_load`), ACWR, profile via `config.yaml`. Bypasses the `name` type mapping
  entirely.
- **Type-separated:** `workouts.type_map` (localised `name` → canonical type,
  case-insensitive) **additionally** produces a load series per sport
  (`workout_trimp_<type>` / `workout_load_<type>`, gated via `load_metric`). Unmapped
  workouts keep feeding only the aggregate. Correlations between two
  activity-volume series (aggregate↔its own sport component, sport↔sport, and
  activity-ring↔load pairs) are training/movement composition rather than a
  health signal and are excluded (`_is_redundant_activity_pair`); a load series
  against any body-state metric (sleep, recovery, vitals) remains — that is
  where the value is.
- **ACWR per sport:** ACWR runs on the aggregate **and** per sport
  (`_training_load_targets`, TRIMP preferred). Guard against false alarms for
  rarely-practised sports: a series with fewer than `analysis.acwr_min_active_days`
  (default 8) training days in the 28 chronic days is skipped.
- **Zone-based Edwards TRIMP:** from the intra-workout HR series (`heartRateData`, HAE
  delivers ~minute buckets `{Min, Avg, Max}` with a timestamp) a parallel daily load
  `workout_edwards` arises **in addition** to Banister (+ per sport
  `workout_edwards_<type>`), gated via `workouts.edwards` (default `true`,
  **self-gated**: with no stored samples nothing is emitted). Edwards sums
  `Σ minutes-in-zone · zone-weight` over five zones (50–60–70–80–90–100% HR_max,
  weights 1–5; below that weight 0). Each two consecutive samples form an interval
  whose time is credited to the zone of the start HR; the interval times are rescaled
  to `duration_s` so recording gaps don't distort the value. Thus Edwards resolves
  intervals that Banister's single average HR smooths over (§8). Zone boundaries depend
  on HR_max → computed **at analysis time**, never frozen at ingest.
  - **Architecture (option B):** the series is parsed **at ingest** into its own table
    `workout_hr_samples` (`(workout_hae_id, ts)` as the idempotency key, CASCADE on the
    workout) — not read from the raw archive at analysis time, so that "raw = cold
    storage" is preserved. Migration `0004_workout_hr_samples`.
  - **History:** the content-hash dedup prevents already-archived payloads from being
    re-parsed on re-post → a one-off `healthlog rederive-workout-hr` replays the HR
    samples from the `raw_ingest` archive (idempotent; the workouts already exist).
    Checkable beforehand with `healthlog check-workout-hr`.
  - **ACWR stays on Banister/kcal** (`_training_load_targets`): Edwards is a **parallel**
    load series for correlations/anomalies/trends, not an additional ACWR target
    (scoring the same training load three times would be redundant). `workout_edwards*`
    falls under the same activity-volume suppression as every other load series
    (`_is_redundant_activity_pair`) — including cross-comparisons between
    trimp/load/edwards, which are composition, not health signal. Edwards' value
    surfaces in correlations against body-state metrics and in its own
    anomaly/trend findings.

## 10. Fitness & form (CTL / ATL / TSB) — dashboard-side

The classic performance-management chart (Banister impulse-response, as popularised
by TrainingPeaks/Garmin) on top of the same daily TRIMP:

```
CTL_t = CTL_{t-1} + (TRIMP_t − CTL_{t-1}) / 42     # "fitness", slow EWMA
ATL_t = ATL_{t-1} + (TRIMP_t − ATL_{t-1}) / 7      # "fatigue", fast EWMA
TSB_t = CTL_t − ATL_t                              # "form"
```

This lives **entirely in the Training dashboard's Fitness & Form section**
(the `workout_trimp` / `daily_trimp` SQL functions — migration 0019, shared by
every TRIMP panel — supply the per-session and daily TRIMP; the panel adds the
recursive EWMA CTE over the dense 0-filled series), **not** in the nightly
analysis, deliberately:

- CTL/ATL/TSB are **descriptive smoothings**, not alert-worthy statistics — the
  alerting role stays with the ACWR finding (§5.1), which is the same
  acute-vs-chronic idea expressed as a ratio; a TSB *alert* would score the same
  load twice. The nightly run stores the numbers as the **`training_status`
  status finding** (§5.2, narration context, never notified), and the dashboard
  pulls the analysis *in*: the PMC chart overlays the stored `training_load` and
  `recovery_alert` findings as annotations (from `findings_history`,
  deduplicated to one marker per alert day), and an ACWR history panel charts
  the 7d/28d ratio per day against the §5 bands.
- Chart-side derivation needs **no schema, no stored derived series** — the same
  reasoning that keeps the other dashboard TRIMP panels in SQL. The dashboard's
  TRIMP mirrors the analysis' fallback chains (§3.1) as far as SQL can reach:
  HR_max defaults to `auto` = `clamp(max(observed max_hr), 160, 210)` with the
  dashboard variable as the explicit override, and HR_rest is a per-day 28-day
  rolling median of the measured resting HR (overall median, then 60, as
  fallbacks). What the dashboard *cannot* see is `config.yaml` (birth_year/sex),
  so it stays a *relative* view; the analysis keeps the profile-driven chain.
- **Sample-resolved Banister:** where an intra-workout HR series is stored, the
  session TRIMP is the Banister formula applied per sample interval (interval
  time credited to the start sample and rescaled to `duration_s`, the same
  crediting scheme as Edwards, §9) instead of one average HR — so intervals
  cost more than a steady run with the same average, which single-average
  Banister smooths over (§8). Sessions without samples keep the average-HR
  formula, which yields the *identical* value for a constant-HR session — both
  variants are the same unit, so mixing them in one daily sum does not violate
  §2.1's don't-mix-units rule (unlike Edwards' zone weights, which is why the
  daily load is not Edwards here).
- The EWMA warms up from the first recorded workout day (seeded with
  `TRIMP_0/42` resp. `/7`). Every panel follows the dashboard time picker: the
  stat tiles and the ACWR gauge are anchored on the end of the selected range
  (capped at today), and days past "now" continue with TRIMP = 0 to show the
  projected decay (dashed) as far as the range extends: fatigue falls fast,
  form rebounds — the taper view.

The dashboard's **Training Load Focus** panel reuses the Edwards zone boundaries
(§9, % of HR_max) to split the selected range's training time into low-aerobic
(50–80%), high-aerobic (80–90%) and anaerobic (≥ 90%) shares from
`workout_hr_samples`, with a whole-session fallback to `avg_hr` for workouts
without a stored HR series.
