#!/usr/bin/env bash
# Restore a dump into the running database (DESTRUCTIVE for current data). Stops app services first.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?usage: scripts/restore.sh backups/<file>.dump [target_db]}"
TARGET="${2:-${POSTGRES_DB:-jarvis}}"
USER="${POSTGRES_USER:-jarvis}"
if [[ "${TARGET}" == "${POSTGRES_DB:-jarvis}" && "${CONFIRM:-}" != "yes" ]]; then
  echo "This replaces the data in '${TARGET}'. Re-run with CONFIRM=yes to proceed." >&2
  exit 2
fi
if [[ "${TARGET}" == "${POSTGRES_DB:-jarvis}" ]]; then
  docker compose stop api worker scheduler bot 2>/dev/null || true  # only when replacing the live database
fi
docker compose exec -T postgres psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${TARGET}' AND pid <> pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"${TARGET}\";" -c "CREATE DATABASE \"${TARGET}\";"
docker compose exec -T postgres psql -U "$USER" -d "$TARGET" -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker compose exec -T postgres pg_restore -U "$USER" -d "$TARGET" --no-owner --no-privileges --exit-on-error < "$FILE"
echo "restored ${FILE} into ${TARGET}"
if [[ "${TARGET}" == "${POSTGRES_DB:-jarvis}" ]]; then
  docker compose run --rm migrate
  docker compose start api worker scheduler bot 2>/dev/null || true
fi
