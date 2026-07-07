# Grafana Dashboards

Six pre-built Grafana dashboards for HealthLog. Import them once via the
Grafana UI — no provisioning required.

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

## Setup

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
| `dashboards/overview.json` | Overview — daily summary (14-day window) |
| `dashboards/sleep.json` | Sleep — phases, efficiency, bedtime, overnight vitals (30-day window) |
| `dashboards/training.json` | Training & Recovery — TRIMP by sport, HRV, workouts table (30-day window) |
| `dashboards/fitness.json` | Fitness — CTL/ATL/TSB performance management chart, ACWR gauge, 28-day training-load focus (90-day window + 14-day projection) |
| `dashboards/workout-detail.json` | Workout Detail — single-session drill-down: intra-workout HR curve, KPIs, metadata |
| `dashboards/metrics.json` | Metrics Explorer — raw values for any metric, Apple-Health-style (30-day window) |

The **Fitness** dashboard is a performance-management view of the training load
(the classic CTL/ATL/TSB "fitness & form" chart known from Garmin/TrainingPeaks):
**Fitness (CTL)** is a 42-day and **Fatigue (ATL)** a 7-day exponentially
weighted average of the daily TRIMP estimate, **Form (TSB)** their difference.
Everything is computed live in SQL from the `workouts` table — the same relative
TRIMP formula as the Training dashboard, so the same two dashboard variables
apply: HR Base is auto-derived from your measured resting HR, **HR Max must be
set once** (dashboard variable, default 190) to match your physiology. Every
panel follows the time picker: the stat tiles and the ratio gauge read off the
**end of the selected range** (capped at today, so scrubbing back in time shows
the historical values), the load focus covers exactly the selected range, and
the chart's dashed projection (fitness/fatigue decay assuming no further
training) extends as far past "now" as the range does — the default range runs
14 days into the future for that reason. Two companion panels: the **Training
Load Ratio** gauge (ACWR = 7-day / 28-day mean load, the same ratio the nightly
analysis stores as a `training_load` finding when it leaves the safe band, see
`docs/workout-analysis.md` §5) and the **Training Load Focus** table (the
selected range's training time split into low-aerobic / high-aerobic /
anaerobic HR zones from the intra-workout HR samples, falling back to a
session's average HR when no samples are stored).

The **Metrics Explorer** is metric-agnostic: two cascading dropdowns at the top —
`Category` first, then `Metric` (only the metrics that belong to the chosen
category) — select what to inspect, and every panel follows. Latest reading,
7-day average, 30-day min/max, the agg-aware daily trend (sum / min / avg / max
per the metric registry), the intraday min–avg–max range, daily sample count,
and the raw `metric_samples` rows. The canonical unit is shown in panel titles
and chart axes.

The **Workout Detail** dashboard is a single-session drill-down for the
intra-workout HR samples (`workout_hr_samples`) that the ingest already collects.
The fastest way in is the **Workouts** table on the Training dashboard: click a
`Date` cell and Grafana opens Workout Detail with that session pre-selected and
the time range zoomed to the workout. You can also pick a session from the
`Workout` dropdown or from the Recent Workouts table at the bottom (both list
only workouts that carry HR samples). Panels: duration / avg HR / max HR / active
energy KPIs, the second-by-second HR curve with dashed average and maximum
reference lines, a **GPS route map**, and a full session-metadata table.

The route map is populated from `workout_route_points`. That data only arrives
for **outdoor GPS workouts** and only when **Include Route Data** is enabled in
Health Auto Export (Workouts → Datenart-Einstellungen → *Routendaten
einschließen*); indoor sessions and pre-existing workouts exported without it show
an empty map. Enabling the toggle affects future exports only — to add routes to
past workouts, re-export that date range from HAE with the toggle on.

## Updating a dashboard

Edit the JSON file, then re-import it in Grafana (Import → overwrite existing).
