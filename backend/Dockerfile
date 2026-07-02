# HealthLog application image.
# python:3.14-slim + s6-overlay v3 (PID 1: clean signal handling + supervision).
# Two long-run services (uvicorn ingest, APScheduler) plus oneshot init/migrate.
# PUID/PGID + /config follow the LinuxServer/Unraid convention.
FROM python:3.14-slim

ARG S6_OVERLAY_VERSION=3.2.0.2

ENV S6_KEEP_ENV=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend \
    PUID=1000 \
    PGID=1000 \
    TZ=Europe/Vienna

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl xz-utils tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- s6-overlay (noarch + x86_64) ----------------------------------------
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/s6-noarch.tar.xz
RUN tar -C / -Jxpf /tmp/s6-noarch.tar.xz && rm /tmp/s6-noarch.tar.xz
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/s6-x86_64.tar.xz
RUN tar -C / -Jxpf /tmp/s6-x86_64.tar.xz && rm /tmp/s6-x86_64.tar.xz

# --- Application user (uid/gid adjusted at runtime from PUID/PGID) --------
RUN groupadd -g 1000 healthlog \
    && useradd -u 1000 -g 1000 -d /app -s /usr/sbin/nologin healthlog

WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
# Install the package (editable, deps already satisfied) for the `healthlog`
# console script; the source stays at /app/backend (also on PYTHONPATH).
RUN pip install --no-cache-dir --no-deps -e .
COPY root/ /

# Service run/up scripts must be executable.
RUN chmod -R +x /etc/s6-overlay/scripts \
    && find /etc/s6-overlay/s6-rc.d -name run -exec chmod +x {} + \
    && find /etc/s6-overlay/s6-rc.d -name up -type f -exec chmod +x {} +

VOLUME /config
EXPOSE 8000

# /api/health verifies DB connectivity, so "unhealthy" means the stack is
# actually broken (API down or database unreachable), not just slow. The
# start period covers init-config + alembic migrations on first boot.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/init"]
