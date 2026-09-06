# ────────────────────────────  Stage 1 ─ Builder  ────────────────────────────
ARG VARIANT=3.13-slim-bookworm
FROM python:${VARIANT} AS builder

# Stage 1: Install required libraries
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y upgrade \
    && apt-get -y install --no-install-recommends unzip curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy uv and bun from official images (simpler than curl install)
RUN export UV_INSTALL_DIR=/bin && curl -LsSf https://astral.sh/uv/install.sh | sh
RUN export BUN_INSTALL=/usr/local && curl -fsSL https://bun.sh/install | bash

# ───────────────────────────  Stage 2 ─ Runtime  ────────────────────────────
FROM builder AS final

ARG PORT=80
ARG BACKEND_PORT=3030
ARG API_URL
ENV PORT=$PORT REFLEX_API_URL=${API_URL:-http://localhost:$PORT}

# The one deployment that has to answer off-machine, said out loud. rxconfig.py
# binds 127.0.0.1 because the desktop archive has no sign-in and a loopback
# socket is what stands in for one; a container is unreachable on that, so it
# widens the bind here rather than by leaving Reflex's 0.0.0.0 default in place
# for every laptop as well. Reflex applies env overrides after rxconfig's
# kwargs, so this wins. It presumes the usual thing about a container: that
# something in front of it decides who gets to connect.
ENV REFLEX_BACKEND_HOST=0.0.0.0

ENV WORK=/reflexapp
WORKDIR ${WORK}

COPY pyproject.toml uv.lock README.md .python-version ${WORK}/
COPY components ${WORK}/components

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --all-extras

COPY alembic.ini start.sh rxconfig.py ${WORK}/
COPY configuration ${WORK}/configuration
COPY assets ${WORK}/assets
COPY alembic ${WORK}/alembic
COPY app ${WORK}/app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-extras && \
    chmod +x ${WORK}/start.sh

EXPOSE $PORT $BACKEND_PORT

CMD ["./start.sh"]
