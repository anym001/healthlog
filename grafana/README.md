# Grafana Dashboards

Three pre-built Grafana dashboards for HealthLog. Two ways to get them into
Grafana:

- **[Option A — manual import](#option-a--manual-import):** click through the
  UI once. Lowest friction to try things out, but every dashboard update means
  re-importing the JSON by hand.
- **[Option B — provisioning](#option-b--provisioning-recommended) (recommended
  for long-term use):** Grafana reads the datasource and the dashboards from
  this repo's files. Updates arrive with a `git pull`, and the datasource UID
  can never be wrong.

> **Upgrading from the earlier seven-dashboard layout?** Sleep, Fitness, Stress
> and Workout Detail were folded into Overview and Training (their UIDs
> `healthlog-sleep`, `healthlog-fitness`, `healthlog-stress` and
> `healthlog-workout-detail` no longer exist). Provisioned setups (Option B)
> clean up on their own — Grafana removes dashboards whose JSON disappeared from
> the folder. Manually imported copies of the four retired dashboards have to be
> deleted by hand in the Grafana UI.

## Prerequisites

**1. Grafana running on the same Docker network as `healthlog-db`**

Unraid `docker run` example:

```bash
docker run -d \
  --name grafana \
  --network health \
  -p 3000:3000 \
  -e GF_SECURITY_ADMIN_PASSWORD=change-me \
  -e GF_SERVER_ROOT_URL=http://YOUR-UNRAID-IP:3000 \
  -v /mnt/user/appdata/grafana:/var/lib/grafana \
  --restart unless-stopped \
  grafana/grafana:11.5.2
```

**2. Read-only database user** (run once via `docker exec -it healthlog-db psql -U healthlog -d healthlog`):

```sql
CREATE USER grafana_ro WITH PASSWORD 'change-me-grafana-ro';
GRANT CONNECT ON DATABASE healthlog TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
```

## Option A — manual import

**1. Add the datasource in Grafana**

Connections → Data sources → Add → PostgreSQL:

| Field | Value |
|---|---|
| Name | `healthlog-db` |
| Host | `healthlog-db:5432` |
| Database | `healthlog` |
| User | `grafana_ro` |
| Password | *(the password set above)* |
| TLS/SSL mode | disable |
| PostgreSQL version | 16 |
| TimescaleDB | ✓ enabled |

The datasource UID must be `healthlog-db` — set it in the UID field directly
below the Name. The dashboards reference this exact UID; a different value
means all panels show "No data".

**2. Import the dashboards**

Dashboards → New → Import → upload JSON file (repeat for each):

| File | Dashboard |
|---|---|
| `dashboards/overview.json` | Overview — daily summary, sleep (phases, efficiency, bedtime, overnight vitals), stress & Body Battery, activity, findings (30-day window) |
| `dashboards/training.json` | Training — TRIMP by sport, fitness & form (CTL/ATL/TSB, ACWR), recovery, workouts table with a per-session Workout Detail drill-down (intra-workout HR curve, route map, metadata; 30-day window) |
| `dashboards/metrics.json` | Metrics Explorer — raw values for any metric, Apple-Health-style (30-day window) |

## Option B — provisioning (recommended)

Instead of clicking the datasource together and importing JSONs, let Grafana
read both from this repo (a checkout on the Docker host). Two mounts and one
environment variable on the **Grafana** container do it — the
[prerequisites](#prerequisites) (network + `grafana_ro` user) are the same:

```bash
docker run -d \
  --name grafana \
  --network health \
  -p 3000:3000 \
  -e GF_SECURITY_ADMIN_PASSWORD=change-me \
  -e GF_SERVER_ROOT_URL=http://YOUR-UNRAID-IP:3000 \
  -e GRAFANA_RO_PASSWORD=change-me-grafana-ro \
  -v /mnt/user/appdata/grafana:/var/lib/grafana \
  -v /path/to/healthlog/grafana/provisioning:/etc/grafana/provisioning:ro \
  -v /path/to/healthlog/grafana/dashboards:/var/lib/grafana/dashboards/healthlog:ro \
  --restart unless-stopped \
  grafana/grafana:11.5.2
```

`GRAFANA_RO_PASSWORD` is the `grafana_ro` password from the prerequisites — the
provisioning file reads it from the environment, so no secret lands in the repo.
On startup Grafana creates the `healthlog-db` datasource (with the exact UID the
dashboards expect) and loads every dashboard into a **HealthLog** folder. It
keeps watching the mounted files: after a release, `git pull` in the checkout is
all it takes — the dashboards refresh within ~30 seconds, no re-import.

Two things to know:

- **Provisioned dashboards are read-only in the UI** — the repo files are the
  source of truth. To customise one, use *Save as* under a new title into
  another folder (your copy, never overwritten), or export the JSON and commit
  it.
- Provisioning only manages its own folder and datasource; an existing Grafana
  with other dashboards is not touched, and previously hand-imported HealthLog
  dashboards simply remain next to the provisioned folder (delete the manual
  copies to avoid confusion).

## The dashboards in detail

The **Overview** dashboard is the daily read. A top row shows last night's
sleep (duration, efficiency), today's HRV and resting heart rate, and the
current stress-score and Body-Battery gauges. Below it: a **Sleep** section
(7-day averages, the nightly phases chart, duration / efficiency / bedtime /
wake-up trends), two collapsed rows — **Overnight Vitals** (respiratory rate,
wrist temperature, SpO₂) and **Stress & Body Battery** (details below) — an
**Activity** section (steps, active energy), and the nightly analysis'
**findings** of every kind plus the top correlations.

The **Stress & Body Battery** section (a collapsed row on the Overview
dashboard) reads the `stress_daily` / `stress_intraday` tables the nightly
analysis fills (a Garmin-style proxy from the heart-rate elevation above
your resting baseline, HRV-calibrated — see `docs/ARCHITECTURE.md` §4.9). For a
usable **intraday timeline**, set Health Auto Export's **Time Grouping** to
**Minute** (Gesundheitsmetriken → *Zeitgruppierung* → *Minute*): the default
hourly grouping yields only ~24 heart-rate points per day, far too coarse for the
timeline. The score is derived, not measured — Apple Health exports no
beat-to-beat RR intervals — so read it relative to your own baseline, **not** as a
Garmin-comparable number. After changing the grouping (or a bulk backfill),
rebuild history with `healthlog rederive-stress`.

The same section also carries the **Body Battery** panels, fed by the
`body_battery_daily` / `body_battery_intraday` tables (see `docs/ARCHITECTURE.md`
§4.10). Body Battery integrates the stress timeline against recovery — stress and
workouts drain the 0-100 reserve, calm rest and sleep recharge it — so it needs
the same **Minute** grouping as the stress timeline. It is a proxy on that proxy,
read relative to your own baseline, not a Garmin value. When rebuilding history,
run `healthlog rederive-body-battery` **after** `rederive-stress` (the battery
reads the freshly recomputed stress rows).

The **Training** dashboard covers everything workout-related: weekly KPIs
(TRIMP, workout count, HRV, resting HR), the daily training load and its split
by sport, a **Fitness & Form** section, recovery and performance trends
(HRV & resting HR, cardio recovery, active energy, VO2 max), the **Workouts**
table, a per-session **Workout Detail** section, and the training-related
findings.

The **Fitness & Form** section is a performance-management view of the training
load (the classic CTL/ATL/TSB "fitness & form" chart known from
Garmin/TrainingPeaks): **Fitness (CTL)** is a 42-day and **Fatigue (ATL)** a
7-day exponentially weighted average of the daily TRIMP estimate, **Form (TSB)**
their difference — with background bands grading the Form scale (overreaching /
productive / fresh / detraining). Everything is computed live in SQL from the
`workouts` table and works with **zero setup**: TRIMP is *sample-resolved* where
intra-workout HR series exist (the Banister formula per HR sample, so intervals
cost more than a steady run with the same average HR; sessions without samples
fall back to the average-HR formula on the same scale), the HR base is a per-day
28-day rolling median of your measured resting HR, and the **HR Max** variable
defaults to `auto` (highest recorded workout HR, clamped to 160–210) — type a
number to override it with a tested value. The chart also overlays the nightly
analysis' `training_load` and `recovery_alert` findings as **annotation markers**
(toggles under the dashboard title). The PMC panel pins its start to the **last
90 days** regardless of the dashboard time range, so the slow CTL trend always
has context; its dashed projection (fitness/fatigue decay assuming no further
training) appears when the time range ends past "now" — set the range end to
e.g. `now+14d` to see it. The stat tiles and the ratio gauge read off the **end
of the selected range** (capped at today, so scrubbing back in time shows the
historical values), and the load focus and the ratio history cover exactly the
selected range. Three companion panels: the **Training Load
Ratio** gauge (ACWR = 7-day / 28-day mean load, the same ratio the nightly
analysis stores as a `training_load` finding when it leaves the safe band, see
`docs/workout-analysis.md` §5), the **Training Load Ratio – History** curve
(the same ratio per day against colored risk bands, showing how a spike built
up) and the **Training Load Focus** table (the selected range's training time
split into low-aerobic / high-aerobic / anaerobic HR zones from the
intra-workout HR samples, falling back to a session's average HR when no
samples are stored).

The **Workout Detail** section is a single-session drill-down for the
intra-workout HR samples (`workout_hr_samples`) that the ingest already collects.
The fastest way in is the **Workouts** table above it: click a `Date` cell and
the session is selected in the detail section below. You can also pick a session
from the `Workout` dropdown (it lists only workouts that carry HR samples). The
detail panels are scoped to the selected workout by ID, not by the dashboard's
time range. Panels: duration / avg HR / max HR / active energy KPIs, the
second-by-second HR curve with dashed average and maximum reference lines, a
**GPS route map**, and a full session-metadata table.

The route map is populated from `workout_route_points`. That data only arrives
for **outdoor GPS workouts** and only when **Include Route Data** is enabled in
Health Auto Export (Workouts → Datenart-Einstellungen → *Routendaten
einschließen*); indoor sessions and pre-existing workouts exported without it show
an empty map. Enabling the toggle affects future exports only — to add routes to
past workouts, re-export that date range from HAE with the toggle on.

The **Metrics Explorer** is metric-agnostic: two cascading dropdowns at the top —
`Category` first, then `Metric` (only the metrics that belong to the chosen
category) — select what to inspect, and every panel follows. Latest reading,
7-day average, 30-day min/max, the agg-aware daily trend (sum / min / avg / max
per the metric registry), the intraday min–avg–max range, daily sample count,
and the raw `metric_samples` rows. The canonical unit is shown in panel titles
and chart axes.

## Updating a dashboard

- **Provisioned (Option B):** update the checkout (`git pull` — or edit the
  JSON); Grafana picks the change up within ~30 seconds.
- **Manually imported (Option A):** edit/update the JSON file, then re-import
  it in Grafana (Import → overwrite existing).
