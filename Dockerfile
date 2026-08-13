# syntax=docker/dockerfile:1
#
# KTN Enclosure Manager.
#
# The image bundles sg3-utils so nothing has to be installed on the TrueNAS
# host (spec §32). Nothing under /usr, /etc, systemd, middlewared or the
# TrueNAS WebUI is touched.

# ---------------------------------------------------------------- frontend
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
# Neither --omit=optional nor --ignore-scripts here: rollup resolves its native
# binary through a platform-specific optional dependency, and omitting it makes
# `vite build` fail with MODULE_NOT_FOUND on rollup/dist/native.js.
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ----------------------------------------------------------------- runtime
FROM python:3.13-slim AS runtime

# sg3-utils: read-only chassis telemetry.
# util-linux: setpriv, used to drop privileges for the web process.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sg3-utils util-linux \
 && rm -rf /var/lib/apt/lists/*

# The web process runs as this unprivileged user. Only the IDENT helper runs
# as root, and only if a helper socket is configured (§31).
RUN groupadd -g 1000 ktn && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin ktn

WORKDIR /app

COPY backend/pyproject.toml backend/pyproject.toml
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" \
      "pydantic-settings>=2.6" "httpx>=0.27" "websockets>=13.0" \
      "argon2-cffi>=23.1" "itsdangerous>=2.2"

COPY backend/ /app/backend/
COPY helper/ /app/helper/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY --from=frontend /build/dist /app/frontend/dist

RUN chmod +x /app/docker-entrypoint.sh

ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    KTN_DATA_DIR=/data \
    KTN_PORT=8420

EXPOSE 8420
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
