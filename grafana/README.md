# Grafana Provisioning for HealthLog Phase 4

This directory contains Grafana provisioning configuration and pre-built dashboards for HealthLog. On container start, Grafana automatically loads the datasource and all three dashboards — no manual import needed.

## What's included

- `provisioning/datasources/timescaledb.yaml` — PostgreSQL/TimescaleDB datasource (`healthlog-db`)
- `provisioning/dashboards/dashboards.yaml` — dashboard provider (file-based, auto-reload every 60 s)
- `dashboards/overview.json` — Morgenübersicht (14-day overview)
- `dashboards/sleep.json` — Schlaf (30-day sleep detail)
- `dashboards/training.json` — Training & Erholung (30-day training & recovery)

## Prerequisites

### 1. Grafana on the same Docker network as `healthlog-db`

Make sure both containers are on the same Docker network (e.g. `health`).

### 2. Read-only database user

Connect to your HealthLog PostgreSQL instance and run:

```sql
CREATE USER grafana_ro WITH PASSWORD 'change-me-grafana-ro';
GRANT CONNECT ON DATABASE healthlog TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
```

## Deploying on Unraid

1. Copy the `grafana/` directory to your host, e.g.:

   ```
   /mnt/user/appdata/healthlog/grafana/
   ```

2. Start Grafana:

   ```bash
   docker run -d \
     --name grafana \
     --network health \
     -p 3000:3000 \
     -e GF_SECURITY_ADMIN_PASSWORD=change-me \
     -e GF_HEALTHLOG_DB_HOST=healthlog-db:5432 \
     -e GF_HEALTHLOG_DB_NAME=healthlog \
     -e GF_HEALTHLOG_DB_USER=grafana_ro \
     -e GF_HEALTHLOG_DB_PASSWORD=change-me-grafana-ro \
     -e GF_AUTH_ANONYMOUS_ENABLED=false \
     -v /mnt/user/appdata/grafana:/var/lib/grafana \
     -v /mnt/user/appdata/healthlog/grafana/provisioning:/etc/grafana/provisioning:ro \
     -v /mnt/user/appdata/healthlog/grafana/dashboards:/var/lib/grafana/dashboards:ro \
     --restart unless-stopped \
     grafana/grafana:11.5.2
   ```

   Replace all `change-me` values with your own passwords.

## Updating dashboards

Pull the updated files from the repo and restart Grafana:

```bash
docker restart grafana
```

Alternatively, the dashboard provider polls `/var/lib/grafana/dashboards` every 60 seconds — simply updating the JSON files on the host is enough for dashboard changes to appear without a restart.

## Dashboards

| Dashboard | UID | Default range | Description |
|---|---|---|---|
| Morgenübersicht | `healthlog-overview` | Last 14 days | Quick morning check: last night's sleep stats (total, deep, HRV), stacked sleep phases, HRV & resting heart rate trend, step count, and an anomaly/recovery-alert table. |
| Schlaf | `healthlog-sleep` | Last 30 days | Detailed sleep view: stacked phases, total duration with 7 h target line, sleep efficiency, bedtime trend, and a sleep-findings table. |
| Training & Erholung | `healthlog-training` | Last 30 days | Training load (TRIMP total and by sport), HRV & resting heart rate, heart-rate recovery, workout log table, and a training/recovery-findings table. |
