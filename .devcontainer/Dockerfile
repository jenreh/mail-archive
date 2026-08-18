ARG VARIANT=3.14-bookworm
FROM mcr.microsoft.com/devcontainers/python:${VARIANT} AS base

ENV PATH="/root/.local/bin/:$PATH" \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# The base image ships a stale Yarn apt repo whose GPG key has expired, which
# makes `apt-get update` fail (exit 100) and breaks devcontainer feature installs
# (e.g. common-utils). We don't use this repo, so remove it.
RUN rm -f /etc/apt/sources.list.d/yarn.list

# Install uv (Astral) — Python package/project manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install go-task — task runner used via Taskfile.dist.yml
RUN sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin
