# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY jarvis ./jarvis
COPY dev_worker ./dev_worker
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM base AS runtime
RUN groupadd --system --gid 10001 jarvis && useradd --system --uid 10001 --gid jarvis --home /app jarvis
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=jarvis:jarvis jarvis ./jarvis
COPY --chown=jarvis:jarvis dev_worker ./dev_worker
COPY --chown=jarvis:jarvis migrations ./migrations
COPY --chown=jarvis:jarvis alembic.ini ./
USER jarvis
EXPOSE 8080
CMD ["jarvis", "api", "--host", "0.0.0.0", "--port", "8080"]
