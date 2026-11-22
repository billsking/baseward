FROM python:3.12-slim AS base

# Defaults to PyPI; override with --build-arg PIP_INDEX_URL=... (e.g. an internal mirror)
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=$PIP_INDEX_URL \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Test image: CI runs pytest in it on the server side; never shipped in the runtime image
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Runtime image (the default target, built when docker compose build has no target)
FROM base AS runtime

RUN groupadd --system gateway \
    && useradd --system --gid gateway --uid 10001 gateway \
    && mkdir -p /data \
    && chown gateway:gateway /data

# Copy as the runtime user so /app stays read-only when compose mounts a read-only rootfs.
COPY --chown=gateway:gateway app ./app
COPY --chown=gateway:gateway migrations ./migrations
COPY --chown=gateway:gateway scripts ./scripts

# Quick start stores the management database in SQLite here; mount a volume to keep it
ENV GATEWAY_DB_PATH=/data/gateway.db

USER gateway

# 3307 is the v1.4 export channel (disabled by default); exposure is decided by the compose port mapping
EXPOSE 8080 3307

# Python-only healthcheck: the runtime image carries no shell utilities beyond the base image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/healthz', timeout=3).getcode() == 200 else 1)" 2>/dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
