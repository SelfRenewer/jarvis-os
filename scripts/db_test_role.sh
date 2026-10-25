#!/usr/bin/env bash
# Test databases are used by the role jarvis_test (password jarvis_test, not a superuser, no access to the live
# database `jarvis`); the superuser `jarvis` has a strong password (POSTGRES_PASSWORD in .env) since 2026-10-14
# (security audit M3: `jarvis/jarvis` was reachable from any container on the Mac via host.docker.internal:55432).
# pgvector is not a trusted extension, so it is created by the superuser over the container socket, in schema
# `extensions` (tests drop and recreate `public`).  Usage: ./scripts/db_test_role.sh [database ...]
set -euo pipefail
cd "$(dirname "$0")/.."
psqlc() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-jarvis}" -v ON_ERROR_STOP=1 -At "$@"; }
psqlc -d postgres -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jarvis_test') THEN
    CREATE ROLE jarvis_test LOGIN PASSWORD 'jarvis_test';
  END IF;
END \$\$;" -c "ALTER ROLE jarvis_test SET search_path = public, extensions;" \
  -c "REVOKE CONNECT ON DATABASE jarvis FROM PUBLIC;" -c "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;" >/dev/null
for db in "${@:-jarvis_test}"; do
  psqlc -d postgres -c "DROP DATABASE IF EXISTS ${db} WITH (FORCE);" -c "CREATE DATABASE ${db} OWNER jarvis_test;" >/dev/null
  psqlc -d "$db" -c "CREATE SCHEMA extensions; GRANT USAGE ON SCHEMA extensions TO jarvis_test; CREATE EXTENSION vector SCHEMA extensions;" >/dev/null
  echo "test database ${db}: owner jarvis_test, pgvector in schema extensions"
done
