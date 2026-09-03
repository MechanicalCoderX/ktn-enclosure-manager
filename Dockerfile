# syntax=docker/dockerfile:1
#
# KTN Enclosure Manager.
#
# The image bundles sg3-utils so nothing has to be installed on the TrueNAS
# host (spec §32). Nothing under /usr, /etc, systemd, middlewared or the
# TrueNAS WebUI is touched.

# ---------------------------------------------------------------- frontend
# Pinned by digest: a floating tag makes today's build and next month's differ
# silently, and hides a tampered upstream. Dependabot bumps these.
FROM node:26-slim@sha256:c0753125a3789977aefe869cbebccf70e3cfd7ea84ca48547458f02e4f1d7146 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
# Neither --omit=optional nor --ignore-scripts here: rollup resolves its native
# binary through a platform-specific optional dependency, and omitting it makes
# `vite build` fail with MODULE_NOT_FOUND on rollup/dist/native.js.
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ----------------------------------------------------------------- runtime
FROM python:3.14-slim@sha256:83ff1d245a3d57d04152252d3ef9cb361494d0b3395abd65a5ebe91c401c8e83 AS runtime

# sg3-utils: read-only chassis telemetry.
# util-linux: setpriv, used to drop privileges for the web process.
# The upgrade line applies Debian security point-releases that land between
# base-image rebuilds: the digest pin freezes the OS snapshot, and Docker Hub
# rebuilds slim images on its own schedule, so a fixed CVE (e.g. the openssl
# deb13u2 -> u3 line) can sit unpatched in the newest pinned digest for days.
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends sg3-utils util-linux \
 && rm -rf /var/lib/apt/lists/*

# The web process runs as this unprivileged user. Only the IDENT helper runs
# as root, and only if a helper socket is configured (§31).
RUN groupadd -g 1000 ktn && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin ktn

WORKDIR /app

# Dependencies come from pyproject.toml, which is the single source of truth.
# A hand-maintained duplicate list here silently drifts out of sync with it.
COPY backend/ /app/backend/
# Install, then remove pip itself. Nothing needs it at runtime - the image is
# immutable and rebuilt from source - and it is not dead weight but live
# attack surface: pip vendors its own copies of libraries (msgpack, setuptools)
# which show up as HIGH findings in an image scan and would be usable by
# anyone who got code execution in the container.
RUN pip install --no-cache-dir /app/backend \
 && pip uninstall -y pip 2>/dev/null || true
COPY helper/ /app/helper/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY --from=frontend /build/dist /app/frontend/dist

RUN chmod +x /app/docker-entrypoint.sh

# Set in the image, not only exported by the entrypoint, so every process in
# the container agrees on it - including one started later with `docker exec`.
# A process that resolves a different lock path does not merely fail to
# synchronise, it silently believes it has.
ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    KTN_DATA_DIR=/data \
    KTN_PORT=8420 \
    KTN_ENCLOSURE_LOCK=/run/ktn/enclosure.lock

EXPOSE 8420
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
