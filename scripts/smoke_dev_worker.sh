#!/usr/bin/env bash
# Cross-process smoke test of the Dev Worker boundary (no Claude cost: fake adapter).
# Scratch database + API over real HTTP (uvicorn) + separate dev-worker process + real git repository.
# Requires: compose postgres running (make db) and a local .env with DEV_WORKER_TOKEN/DEV_WORKER_SIGNING_KEY.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${SMOKE_PORT:-8090}"
DB=jarvis_smoke
WORK="$(mktemp -d)"
API_PID=""
psqlc() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-jarvis}" -v ON_ERROR_STOP=1 -At "$@"; }
cleanup() {
  [[ -n "$API_PID" ]] && kill "$API_PID" 2>/dev/null || true
  psqlc -d postgres -c "DROP DATABASE IF EXISTS ${DB} WITH (FORCE);" >/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

val() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
export DATABASE_URL="postgresql+asyncpg://jarvis_test:jarvis_test@localhost:${POSTGRES_HOST_PORT:-55432}/${DB}"
export JARVIS_ENV=dev DEV_REPO_IDS=smoke-repo LOG_JSON=false LOG_LEVEL=WARNING
psqlc -d postgres -c "DROP DATABASE IF EXISTS ${DB} WITH (FORCE);" -c "CREATE DATABASE ${DB} OWNER jarvis_test;" >/dev/null
psqlc -d "$DB" -c "CREATE SCHEMA extensions; GRANT USAGE ON SCHEMA extensions TO jarvis_test; CREATE EXTENSION vector SCHEMA extensions;" >/dev/null  # pgvector is not a trusted extension: created by the superuser (socket), used by the test role
uv run --quiet jarvis migrate >/dev/null

uv run --quiet jarvis api --port "$PORT" >"$WORK/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && break; sleep 0.2; done

REPO="$WORK/smoke-repo"
git init -q -b main "$REPO"
echo "# smoke" > "$REPO/README.md"
git -C "$REPO" add -A && git -C "$REPO" -c user.name=t -c user.email=t@t commit -qm init
MAIN_BEFORE="$(git -C "$REPO" rev-parse main)"
cat > "$WORK/repos.yaml" <<YAML
server_url: http://127.0.0.1:${PORT}
worker_id: smoke-worker
adapter: fake
worktrees_dir: ${WORK}/worktrees
repos:
  smoke-repo:
    path: ${REPO}
    remote: null
    test: ["python3", "-c", "print('1 passed')"]
YAML

read -r JOB_ID BRANCH < <(uv run --quiet python scripts/smoke_seed_dev_job.py)
echo "seeded job ${JOB_ID} (${BRANCH})"

# Forged token must be rejected before anything else.
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer wrong' -H 'X-Worker-Id: x' \
  "http://127.0.0.1:${PORT}/api/dev-worker/v1/claim")"
[[ "$code" == "401" ]] || { echo "expected 401 for forged token, got $code"; exit 1; }

RESULT="$(DEV_WORKER_REPOS="$WORK/repos.yaml" DEV_WORKER_TOKEN="$(val DEV_WORKER_TOKEN)" \
  DEV_WORKER_SIGNING_KEY="$(val DEV_WORKER_SIGNING_KEY)" uv run --quiet jarvis-dev-worker once 2>"$WORK/worker.log" | tail -1)"
echo "worker result: ${RESULT}"
STATUS="$(psqlc -d "$DB" -c "SELECT status FROM development_jobs WHERE id = '${JOB_ID}'")"
echo "server job status: ${STATUS}"
git -C "$REPO" rev-parse --verify -q "$BRANCH" >/dev/null && echo "feature branch exists: ${BRANCH}"
[[ "$(git -C "$REPO" rev-parse main)" == "$MAIN_BEFORE" ]] && echo "main untouched"
[[ "$RESULT" == "succeeded" && "$STATUS" == "succeeded" ]] && echo "DEV WORKER SMOKE TEST PASSED" || {
  echo "DEV WORKER SMOKE TEST FAILED"; cat "$WORK/worker.log" "$WORK/api.log"; exit 1; }
