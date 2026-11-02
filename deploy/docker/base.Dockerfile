# syntax=docker/dockerfile:1.7
# Shared build for carto service images (spec 6 "Packaging", 14.6, 14.8).
#
# Stage 1 installs the locked workspace with uv into a self-contained virtual environment.
# Stage 2 copies only that environment into a slim runtime image that runs as a non-root user
# with no package manager left behind. M6 moves the runtime stage to a distroless or Chainguard
# base pinned by digest (plan M1 "Deferred"); the layout here is already compatible.
#
# Build args select the member to install and the console script to run:
#   docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=edge --build-arg ENTRY=carto-edge .
#
# Base images are pinned by digest (spec 14.8, ADR 0072); Renovate moves the digests with the
# patch releases (renovate.json, `docker:pinDigests`). The repository's .dockerignore keeps
# caches, tests, secrets and generated data out of the build context.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

FROM ${PYTHON_IMAGE} AS builder
ARG MEMBER
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/carto
COPY --from=ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY edge ./edge
COPY core ./core
COPY simulator ./simulator
COPY eval ./eval
COPY tools ./tools
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package "carto-${MEMBER}"

FROM ${PYTHON_IMAGE} AS runtime
ARG ENTRY
ARG MEMBER
LABEL org.opencontainers.image.source="https://github.com/23spakkerakari/self-writing-integrations" \
      org.opencontainers.image.title="carto-${MEMBER}" \
      org.opencontainers.image.licenses="proprietary"
# Non-root user, no shell login, no package manager (spec 14.6).
RUN groupadd --system --gid 10001 carto \
    && useradd --system --uid 10001 --gid carto --home-dir /var/lib/carto --shell /usr/sbin/nologin carto \
    && mkdir -p /var/lib/carto /etc/carto \
    && chown -R carto:carto /var/lib/carto /etc/carto \
    && apt-get purge -y --auto-remove apt dpkg-dev 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/* /usr/bin/apt* /usr/lib/apt
COPY --from=builder --chown=root:root /opt/carto /opt/carto
ENV PATH="/opt/carto/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CARTO_STATE_DIR=/var/lib/carto
USER 10001:10001
WORKDIR /var/lib/carto
ENV CARTO_ENTRY=${ENTRY}
ENTRYPOINT ["/bin/sh", "-c", "exec \"$CARTO_ENTRY\" \"$@\"", "--"]
