# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# The line above names the BuildKit frontend, an image that is pulled and
# executed at build time - in the publish job too, which holds packages:write.
# So it is digest-pinned like the base images below. Dependabot's docker
# ecosystem parses only FROM lines and never reads it, so the same reference
# also sits in the `syntax-pin` stage below, where Dependabot does see it. Its
# PR bumps that stage and fails tests/test_image_build_inputs.py until the
# stage's version and digest are copied onto line 1.
#
# KTN Enclosure Manager.
#
# The image bundles sg3-utils so nothing has to be installed on the TrueNAS
# host (spec §32). Nothing under /usr, /etc, systemd, middlewared or the
# TrueNAS WebUI is touched.

# -------------------------------------------------------------- syntax-pin
# Never built: nothing copies from it and it is not the last stage, so BuildKit
# skips it. It exists only so Dependabot proposes new frontend releases (above).
FROM docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e AS syntax-pin

# ---------------------------------------------------------------- frontend
# Pinned by digest: a floating tag makes today's build and next month's differ
# silently, and hides a tampered upstream. Dependabot bumps these.
FROM node:26-slim@sha256:5758d367d7b4f48b73a9bb3530e687e47efb289f3b43f9c0450a25225ae0db5d AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
# Neither --omit=optional nor --ignore-scripts here: rollup resolves its native
# binary through a platform-specific optional dependency, and omitting it makes
# `vite build` fail with MODULE_NOT_FOUND on rollup/dist/native.js.
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ----------------------------------------------------------------- runtime
FROM python:3.15-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0 AS runtime

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

# Dependencies are declared in pyproject.toml and installed from
# backend/requirements.txt, a hash-pinned lock compiled from them for this
# image's Python on both published architectures (CONTRIBUTING.md, "Dependency
# lock"). Without it every build resolved whatever PyPI served that day, so two
# builds of the same commit - and a rollback - shipped different libraries.
COPY backend/ /app/backend/
# Install the lock (every file checked against its recorded hash) from wheels
# only: every pin has a cp315 wheel for amd64 and arm64, so nothing in it is
# built here, and a future pin without one fails the build rather than running
# a build backend nobody hashed. The project's own build backend, setuptools,
# comes from a second hash-pinned lock (backend/build-requirements.txt, same
# rules), so the project is built with --no-build-isolation: without it pip
# would fetch the newest setuptools matching [build-system].requires into a
# throwaway environment, unhashed, as root, and the wheel it builds ships. The
# project goes in with --no-deps so no runtime dependency outside the lock can
# come in. Then setuptools is removed (a build tool, not a runtime need), then
# `pip check` fails the build if the lock no longer covers the names and
# versions pyproject declares (pyproject uses no extras, so that is every
# runtime package). Then remove pip itself.
# Nothing needs it at runtime - the image is immutable and rebuilt from
# source - and it is not dead weight but live attack surface: pip vendors its
# own copies of libraries (msgpack, setuptools) which show up as HIGH findings
# in an image scan and would be usable by anyone who got code execution in the
# container.
#
# This stays one RUN, so setuptools never persists in a lower layer, and the
# lock installs do not get a cached layer of their own (CI builds the runtime
# stage with no-cache-filters anyway). Only the last step may fail, hence the
# parentheses. Written as `A && B || true`, the `|| true` also swallowed a
# failed install: the build succeeded and the image had no fastapi or uvicorn.
RUN pip install --no-cache-dir --require-hashes --only-binary :all: \
    -r /app/backend/requirements.txt \
 && pip install --no-cache-dir --require-hashes --only-binary :all: \
    -r /app/backend/build-requirements.txt \
 && pip install --no-cache-dir --no-deps --no-build-isolation /app/backend \
 && pip uninstall -y setuptools \
 && pip check \
 && (pip uninstall -y pip 2>/dev/null || true)
COPY helper/ /app/helper/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
# Opt-in TLS (KTN_TLS_ENABLED): the pair check the entrypoint runs before
# serving, the healthcheck that follows the port and scheme, and the boolean
# spelling table the entrypoint shares with the app.
COPY docker-healthcheck.py docker-tlscheck.py /app/
COPY scripts/tls-bool.sh /app/scripts/tls-bool.sh
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

# A script rather than an inline URL: it reads KTN_PORT and KTN_TLS_ENABLED,
# so a changed port or a TLS deployment is probed where it actually listens.
# Docker runs it as root every 30 s, so Python starts isolated: -I ignores
# PYTHON* variables and the user site, -S skips site-packages and every .pth
# and sitecustomize there. The script needs only the standard library.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -I -S /app/docker-healthcheck.py

ENTRYPOINT ["/app/docker-entrypoint.sh"]
