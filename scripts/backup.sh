#!/usr/bin/env bash
# PostgreSQL logical backup (custom format) from the compose `postgres` service.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB="${POSTGRES_DB:-jarvis}"
USER="${POSTGRES_USER:-jarvis}"
OUT="backups/${DB}-${STAMP}.dump"
docker compose exec -T postgres pg_dump -U "$USER" -d "$DB" --format=custom --no-owner --no-privileges > "$OUT"
# integrity check: the archive must list its table of contents
docker compose exec -T postgres pg_restore --list < "$OUT" > /dev/null
chmod 600 "$OUT"
echo "$OUT"
# retention: keep the newest 14 dumps
ls -1t backups/*.dump 2>/dev/null | tail -n +15 | xargs -r rm -f
