# Grafana Dashboards

Three pre-built Grafana dashboards for HealthLog. Import them once via the
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
  -e GF_AUTH_ANONYMOUS_ENABLED=false \
  -v /mnt/user/appdata/grafana:/var/lib/grafana \
  --restart unless-stopped \
  grafana/grafana:11.5.2
```

**2. Read-only database user** (run once via `docker exec healthlog-db psql -U healthlog -d healthlog`):

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
| Name | `HealthLog DB` |
| Host | `healthlog-db:5432` |
| Database | `healthlog` |
| User | `grafana_ro` |
| Password | *(the password set above)* |
| TLS/SSL mode | disable |
| PostgreSQL version | 16 |
| TimescaleDB | ✓ enabled |

Set the datasource UID to `healthlog-db` (under the name field).

**2. Import the dashboards**

Dashboards → New → Import → upload JSON file (repeat for each):

| File | Dashboard |
|---|---|
| `dashboards/overview.json` | Overview — daily summary (14-day window) |
| `dashboards/sleep.json` | Sleep — phases, efficiency, bedtime (30-day window) |
| `dashboards/training.json` | Training & Recovery — TRIMP by sport, HRV, workouts table (30-day window) |

## Updating a dashboard

Edit the JSON file, then re-import it in Grafana (Import → overwrite existing).
