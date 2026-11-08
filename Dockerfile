# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app

RUN mkdir -p /media/telegram /var/lib/telegram-bot-api /app/models

ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock .python-version ./

FROM base AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra worker --extra postgres --no-dev --no-install-project
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra worker --extra postgres --no-dev --no-editable
ENTRYPOINT ["/app/.venv/bin/python", "-m", "nova_transcriber.worker"]

FROM base AS app
ARG COMPONENT_EXTRA="postgres"

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra "$COMPONENT_EXTRA" --no-dev --no-install-project

COPY README.md ./
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra "$COMPONENT_EXTRA" --no-dev --no-editable

ENTRYPOINT ["/app/.venv/bin/python", "-m", "nova_transcriber.api"]
