# syntax=docker/dockerfile:1

# ---------- Stage 1: build dependencies ----------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# libpq5 is the runtime (non-dev) Postgres client library needed by asyncpg's
# C extension fallback paths; build tools from stage 1 are not copied over.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 gateway \
    && useradd --uid 1000 --gid gateway --shell /usr/sbin/nologin --no-create-home gateway

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app/ /app/app/

RUN chown -R gateway:gateway /app

# Workspace storage for project files. Created and chowned here so that
# when docker-compose mounts a named volume at this path, Docker seeds
# the volume with these ownership bits on first use (the gateway process
# runs as a non-root user and needs write access).
RUN mkdir -p /data/workspaces && chown -R gateway:gateway /data/workspaces

USER gateway

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/healthz || exit 1

# Single worker per container: scale horizontally via replicas, not
# in-process workers, since the in-memory rate limiter is per-process.
#
# NOTE: --forwarded-allow-ips is intentionally left at uvicorn's default
# (loopback only). If you terminate TLS at an upstream reverse proxy and
# need X-Forwarded-For honored, set it explicitly to that proxy's IP/CIDR
# via the UVICORN_FORWARDED_ALLOW_IPS env var and add it to CMD — do not
# set it to "*" in production.
CMD ["uvicorn", "app.gateway:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
