FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/models \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock .python-version README.md ./
COPY src/ ./src/

RUN uv sync --locked --no-dev --no-editable --no-cache

ENTRYPOINT ["/app/.venv/bin/nova-transcriber"]
