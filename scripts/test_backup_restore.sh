#!/usr/bin/env bash
# Proves that a backup is restorable: seed marker rows, dump, restore into a scratch DB, compare counts.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${POSTGRES_DB:-jarvis}"
USER="${POSTGRES_USER:-jarvis}"
SCRATCH="jarvis_restore_check"
psqlc() { docker compose exec -T postgres psql -U "$USER" -v ON_ERROR_STOP=1 -At "$@"; }
MARKER="restore-check-$(date +%s)"
psqlc -d "$DB" -c "INSERT INTO audit_events (occurred_at, actor, action, result, details) VALUES (now(), 'backup-test', 'BACKUP_TEST', 'ok', jsonb_build_object('marker', '${MARKER}'));" >/dev/null
# The stack may be running: compare only rows that existed before the dump snapshot was taken.
CUTOFF="$(psqlc -d "$DB" -c "SELECT now()")"
sleep 1
DUMP="$(./scripts/backup.sh | tail -1)"
./scripts/restore.sh "$DUMP" "$SCRATCH" >/dev/null
FAIL=0
for spec in audit_events:occurred_at scheduled_jobs:created_at ideas:created_at idea_versions:created_at \
            reminders:created_at development_jobs:created_at alembic_version:; do
  table="${spec%%:*}"; col="${spec#*:}"
  where=""; [[ -n "$col" ]] && where="WHERE ${col} < '${CUTOFF}'"
  a="$(psqlc -d "$DB" -c "SELECT count(*) FROM ${table} ${where}")"
  b="$(psqlc -d "$SCRATCH" -c "SELECT count(*) FROM ${table} ${where}")"
  printf '%-18s source=%-6s restored=%-6s %s\n' "$table" "$a" "$b" "$([[ "$a" == "$b" ]] && echo OK || echo MISMATCH)"
  [[ "$a" == "$b" ]] || FAIL=1
done
found="$(psqlc -d "$SCRATCH" -c "SELECT count(*) FROM audit_events WHERE details->>'marker' = '${MARKER}'")"
echo "marker row restored: ${found}"
[[ "$found" == "1" ]] || FAIL=1
trig="$(psqlc -d "$SCRATCH" -c "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_ideas_protect_raw'")"
echo "idea immutability trigger restored: ${trig}"
[[ "$trig" == "1" ]] || FAIL=1
psqlc -d postgres -c "DROP DATABASE ${SCRATCH};" >/dev/null
psqlc -d "$DB" -c "DELETE FROM audit_events WHERE actor = 'backup-test';" >/dev/null
[[ "$FAIL" == "0" ]] && echo "BACKUP/RESTORE CHECK PASSED" || { echo "BACKUP/RESTORE CHECK FAILED"; exit 1; }
