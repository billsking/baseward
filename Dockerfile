FROM python:3.12-slim AS base

# Defaults to PyPI; override with --build-arg PIP_INDEX_URL=... (e.g. an internal mirror)
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=$PIP_INDEX_URL \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Test image: CI runs pytest in it on the server side; never shipped in the runtime image
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Runtime image (the default target, built when docker compose build has no target)
FROM base AS runtime
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# Quick start stores the management database in SQLite here; mount a volume to keep it
ENV GATEWAY_DB_PATH=/data/gateway.db

RUN groupadd --system gateway \
    && useradd --system --gid gateway --uid 10001 gateway \
    && mkdir -p /data \
    && chown -R gateway:gateway /app /data

USER gateway

# 3307 is the v1.4 export channel (disabled by default); exposure is decided by the compose port mapping
EXPOSE 8080 3307

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
