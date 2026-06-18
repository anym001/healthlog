# Grafana Provisioning for HealthLog

This directory contains Grafana provisioning configuration and dashboards for the HealthLog project.

## What this is

Automated provisioning for a Grafana instance that visualises HealthLog data directly from the TimescaleDB/PostgreSQL database. Three dashboards are included:

- **Morgenübersicht** (`overview.json`) — 14-day overview: last night's sleep stats, HRV, sleep phases chart, steps, and anomaly table.
- **Schlaf** (`sleep.json`) — 30-day sleep deep-dive: phases, duration, efficiency, sleep-onset time, and sleep-related findings.
- **Training & Erholung** (`training.json`) — 30-day training view: TRIMP load, load by sport, HRV/resting heart rate, cardio recovery, workout log, and training findings.

## Prerequisites

Create a read-only database user in your HealthLog PostgreSQL/TimescaleDB instance:

```sql
CREATE USER grafana_ro WITH PASSWORD 'change-me-grafana-ro';
GRANT CONNECT ON DATABASE healthlog TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
```

## Running Grafana on Unraid

Mount the provisioning and dashboard directories from your appdata share and pass the database credentials via environment variables:

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

Replace `change-me` and `change-me-grafana-ro` with strong passwords before deploying.

## Updating dashboards

The dashboard provider polls for changes every 60 seconds (`updateIntervalSeconds: 60`). To update a dashboard:

1. Edit the relevant JSON file in `grafana/dashboards/`.
2. Copy the updated file to `/mnt/user/appdata/healthlog/grafana/dashboards/` on the Unraid host.
3. Grafana picks up the change automatically within 60 seconds — no restart required.

Dashboard deletion is disabled (`disableDeletion: true`); removing a file from the provisioning path will not delete the dashboard from Grafana's UI until the instance is restarted.
