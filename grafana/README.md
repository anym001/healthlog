# Grafana Dashboards

Four pre-built Grafana dashboards for HealthLog. Import them once via the
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
| `dashboards/sleep.json` | Sleep — phases, efficiency, bedtime (30-day window) |
| `dashboards/training.json` | Training & Recovery — TRIMP by sport, HRV, workouts table (30-day window) |
| `dashboards/metrics.json` | Metrics Explorer — raw values for any metric, Apple-Health-style (30-day window) |

The **Metrics Explorer** is metric-agnostic: two cascading dropdowns at the top —
`Category` first, then `Metric` (only the metrics that belong to the chosen
category) — select what to inspect, and every panel follows. Latest reading,
7-day average, 30-day min/max, the agg-aware daily trend (sum / min / avg / max
per the metric registry), the intraday min–avg–max range, daily sample count,
and the raw `metric_samples` rows. The canonical unit is shown in panel titles
and chart axes. The bottom **Data Catalog** lists every metric that has data with
its unit, category, tier, day count, and last sample — so newly auto-registered
metrics show up automatically without editing the dashboard.

## Updating a dashboard

Edit the JSON file, then re-import it in Grafana (Import → overwrite existing).
