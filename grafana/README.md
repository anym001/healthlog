# Grafana Dashboards

Five pre-built Grafana dashboards for HealthLog. Import them once via the
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
| `dashboards/workout-detail.json` | Workout Detail — single-session drill-down: intra-workout HR curve, KPIs, metadata |
| `dashboards/stress.json` | Stress & Body Battery — Garmin-style daily score gauge, time-in-zone, intraday timeline, long-term trend, plus a Body-Battery reserve gauge, daily wake/high/low, intraday battery timeline and charged/drained bars (7-day window) |
| `dashboards/metrics.json` | Metrics Explorer — raw values for any metric, Apple-Health-style (30-day window) |

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

The **Stress** dashboard reads the `stress_daily` / `stress_intraday` tables the
nightly analysis fills (a Garmin-style proxy from the heart-rate elevation above
your resting baseline, HRV-calibrated — see `docs/ARCHITECTURE.md` §4.9). For a
usable **intraday timeline**, set Health Auto Export's **Time Grouping** to
**Minute** (Gesundheitsmetriken → *Zeitgruppierung* → *Minute*): the default
hourly grouping yields only ~24 heart-rate points per day, far too coarse for the
timeline. The score is derived, not measured — Apple Health exports no
beat-to-beat RR intervals — so read it relative to your own baseline, **not** as a
Garmin-comparable number. After changing the grouping (or a bulk backfill),
rebuild history with `healthlog rederive-stress`.

The same dashboard also carries the **Body Battery** panels, fed by the
`body_battery_daily` / `body_battery_intraday` tables (see `docs/ARCHITECTURE.md`
§4.10). Body Battery integrates the stress timeline against recovery — stress and
workouts drain the 0-100 reserve, calm rest and sleep recharge it — so it needs
the same **Minute** grouping as the stress timeline. It is a proxy on that proxy,
read relative to your own baseline, not a Garmin value. When rebuilding history,
run `healthlog rederive-body-battery` **after** `rederive-stress` (the battery
reads the freshly recomputed stress rows).

## Updating a dashboard

Edit the JSON file, then re-import it in Grafana (Import → overwrite existing).
