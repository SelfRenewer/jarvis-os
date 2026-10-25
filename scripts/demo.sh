#!/usr/bin/env bash
# Simulated day with the real code and fake external services, on a scratch database (dropped afterwards).
set -euo pipefail
cd "$(dirname "$0")/.."
psqlc() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-jarvis}" -v ON_ERROR_STOP=1 -At "$@"; }
trap 'psqlc -d postgres -c "DROP DATABASE IF EXISTS jarvis_demo WITH (FORCE);" >/dev/null 2>&1 || true' EXIT
psqlc -d postgres -c "DROP DATABASE IF EXISTS jarvis_demo WITH (FORCE);" -c "CREATE DATABASE jarvis_demo OWNER jarvis_test;" >/dev/null 2>&1
psqlc -d jarvis_demo -c "CREATE SCHEMA extensions; GRANT USAGE ON SCHEMA extensions TO jarvis_test; CREATE EXTENSION vector SCHEMA extensions;" >/dev/null  # pgvector is not a trusted extension: created by the superuser (socket), used by the test role
export DATABASE_URL="postgresql+asyncpg://jarvis_test:jarvis_test@localhost:${POSTGRES_HOST_PORT:-55432}/jarvis_demo"
uv run --quiet alembic upgrade head >/dev/null 2>&1
uv run --quiet python scripts/demo.py
