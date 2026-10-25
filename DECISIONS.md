# Architecture Decision Records

Status of all records: **Accepted** (ADR-001…007 on 2026-09-23, ADR-008…010 on 2026-10-01/13). Each record is short
on purpose; details live in `docs/`.

---

## ADR-001 — Todoist ↔ Calendar synchronization model

**Context.** Todoist owns task identity/metadata; Google Calendar owns events/availability. Two-way
custom sync between them invites update loops and duplicates.

**Decision.**
- Exactly one strategy is active, selected by `CALENDAR_STRATEGY` (`native` | `mirror`), default `native`.
- **Strategy A (`native`)** — Todoist's own Calendar integration renders time-blocked tasks. Jarvis only
  writes `due_datetime` + `duration` to Todoist. Jarvis never creates Google events for tasks. The
  "Todoist" calendar (id in `GOOGLE_TODOIST_CALENDAR_ID`, or auto-detected by summary `Todoist`) is
  **excluded** from availability, because the same task is already known from Todoist.
- **Strategy B (`mirror`)** — only when the native integration is off. A one-way job mirrors
  Todoist → Google into `GOOGLE_MIRROR_CALENDAR_ID`, every event carrying
  `extendedProperties.private = {jarvis_managed: "true", todoist_task_id: …}`. Jarvis-managed events are
  excluded from availability and are **never** read back into Todoist.
- `jarvis doctor` fails if `mirror` is selected while a calendar named "Todoist" is visible (probable
  double rendering), so both strategies can't silently run together.
- Sync handlers (webhooks, reconciliation) are **read-only toward external systems**. The only writers
  are: planner "apply" (after approval/auto-apply policy) and the Strategy-B mirror job. Writes are
  skipped when the target already equals the desired state → no ping-pong.

**Consequences.** No loop is structurally possible: nothing that reacts to an external change writes to
the system that produced it.

## ADR-002 — Durable scheduler

**Decision.** PostgreSQL table `scheduled_jobs` is the only source of truth for future work.
- Enqueue with optional `dedupe_key UNIQUE` (`INSERT … ON CONFLICT DO NOTHING`).
- Claim with `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING` → safe for N workers.
- Each claim sets a lease (`lock_expires_at`). A reaper returns expired leases to `pending`
  (crash recovery) or to `dead` if attempts are exhausted.
- Retries: deterministic bounded exponential backoff `min(base * 2^(n-1), cap)` without jitter in
  tests (jitter is derived deterministically from the job id so schedules are reproducible).
- Permanent errors (auth/validation) go straight to `dead`; transient errors retry.
- Every attempt is recorded in `job_attempts`.
- Lateness policy: handlers receive `lateness = now - run_at`; reminder handlers apply per-channel grace
  windows (e.g. call ≤ 5 min late, Telegram ≤ 30 min, otherwise a recovery notification).
- Periodic work ("cron") is materialized as ordinary jobs by the scheduler process using
  `dedupe_key = periodic:<name>:<slot>` so several schedulers never double-enqueue.
- Time comes from an injectable `Clock` (tests advance time deterministically).

**Rejected.** APScheduler/celery-beat memory state, Vapi `schedulePlan`, n8n — none is durable truth.

### ADR-002b — Calendar change detection by bounded windows
Google sync tokens cannot be combined with a time window and an unbounded first sync would expand
every recurring event. Jarvis polls a bounded window (`now-1d … now+CALENDAR_SYNC_DAYS`,
`singleEvents=true`, `showDeleted=true`) every few minutes and on push notification; changes are detected by
`etag`. Push channels (when HTTPS exists) only trigger an early reconciliation; channels are persisted
and renewed before expiry.

## ADR-003 — Dev Worker security boundary

**Decision.** The Dev Worker **pulls** work; the server can never initiate a connection to the
development machine.
- Worker authenticates with a bearer token (`DEV_WORKER_TOKEN`, only its SHA-256 is compared on the server).
- Claimed job payloads are HMAC-signed (`DEV_WORKER_SIGNING_KEY`); the worker rejects unsigned/altered jobs.
- Job schema is closed: `job_id, repo_id, spec_id, branch_name, dev_task_markdown, approved, approval_id`.
  No paths, no commands, no shell strings.
- `repo_id → path` allowlist lives only on the dev machine (`dev_worker/repos.yaml`) together with the
  per-repo test/lint/typecheck **argv lists** (never passed through a shell).
- Work happens in a fresh `git worktree` on branch `jarvis/<slug>`; `main`/`master`/the base branch is
  never checked out for writing, never force-pushed, never merged; pushing is off by default.
- Claude Code runs with `permission_mode="dontAsk"`, an explicit tool allowlist, `setting_sources=[]`
  (repository `.claude/` settings ignored), web tools disallowed, a PreToolUse hook that denies paths outside
  the worktree and secret-like files, and a scrubbed environment (no Jarvis secrets). Never `bypassPermissions`.
- Transport: Tailscale (WireGuard) between VPS and dev machine; worker endpoint set via `JARVIS_SERVER_URL`.

## ADR-004 — LLM vs deterministic responsibilities

LLM (via one `LLMClient`, several prompts/schemas): intent routing, idea structuring, spec writing,
fuzzy product-name matching, natural-language constraint interpretation ("лучше вечером" → enum),
conversational answers. Every machine-readable output is JSON-schema constrained **and** Pydantic
validated; malformed output ⇒ at most one repair retry ⇒ typed failure.

Deterministic code only: time/timezone math, slot search and scoring, reminder/leave times, travel
math, money (Decimal, GEL minor units), unit conversion, basket totals and optimization, retries,
dedupe, authorization, approvals, DB consistency, entity resolution (LLM never supplies DB ids —
references are resolved against the database).

## ADR-005 — Shopping provider limitations

Wolt and Bolt expose merchant-side APIs only and their consumer terms forbid bots/scraping (checked
against their public terms on 2026-09-23). Their providers report `AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE`.
Implemented: provider abstraction, `ManualImportProvider` (JSON/CSV offers), `UserAssistedProvider`
(Jarvis prepares a checklist, the user enters prices), optimizer, history, preferences. A permitted
official API can be added as a new `ShoppingProvider` without changing the shopping service.

## ADR-006 — Approval boundaries

Human approval (Telegram inline button or explicit command) is required for: applying a planning
proposal (unless the configured auto-apply policy allows it), starting a development job, any
checkout/purchase, low-confidence product substitutions, deleting a task, closing a task with subtasks, any
change to several tasks at once (approval kind `task_op`, incl. `to_shopping`), and starting Claude Code with a
task on the Mac (`mac_actions` `proposed → pending`). Every button's data is HMAC-signed and verified first. Approvals are rows with a unique nonce; the
state transition `pending → approved` is a single conditional `UPDATE`, so duplicate callbacks are no-ops.
LLM output can *request* an approval, never grant one. `ALLOW_LIVE_PURCHASES=false` additionally blocks
checkout at the provider layer.

## ADR-007 — Deployment topology

- VPS (Linux, Docker Compose): `postgres` (pgvector), `migrate` (one-shot Alembic), `api` (FastAPI:
  health, webhooks, internal + dev-worker API), `worker` (executes jobs), `scheduler` (materializes
  periodic jobs + lease reaper), `bot` (Telegram long polling). Optional `caddy` (profile `public`) for HTTPS.
- Dev machine: `dev_worker` process (uv, not Docker — it needs the real repos and `claude`).
- No Kubernetes, Kafka, agent frameworks. n8n optional and never on the critical path.
- Today (2026-10) the same Compose stack runs on the user's Mac (loopback ports); the VPS move is pending.
- Secrets only via environment (`.env` not committed; `.env.example` documents them).

## ADR-008 — Jarvis for Mac (2026-10-01)

**Decision.** A native menu-bar app (Swift, `desktop/JarvisMac`) is a second chat channel and the only executor of
actions on the Mac.
- It talks to the core over loopback (`/api/desktop/v1/*`) with a bearer token derived from existing secrets
  (no new secret in `.env`); the core runs the same `BotService` with a capturing adapter (`DesktopChannel`), so
  the Mac and Telegram share one conversation and the same signed buttons.
- Actions follow the Dev Worker pattern: the core never reaches into the Mac; the app **pulls** `mac_actions`.
  The set is closed (`open_app`, `open_url`, `open_path`, `claude_code`), values are resolved by Jarvis from what
  the Mac reported, and the app re-validates them; no shell strings. Pending actions expire (10 min).
- Starting Claude Code with a task needs a button (ADR-006); an interactive session without a task opens at once.
- Speech is recognised on the Mac (Apple, on-device `ru-RU`); voice opens a small window, the big chat only on click.

**Rejected.** Running actions from Docker (no access to the Mac's apps), AppleScript automation (extra privacy
prompts), sending audio to the cloud for the wake word.

## ADR-009 — OpenRouter spend per period (2026-10-01)

A normal OpenRouter key exposes only cumulative `usage` and calendar counters; `/activity` needs a management key.
Jarvis samples the key hourly (`usage.snapshot`, free) into `spend_snapshots` (+ derived day/week/month starts) and
computes spend for a period as `now − sample at its start`. The value is exact when no paid call happened between the
sample and the start (the key is Jarvis's only), otherwise shown as an upper bound «≤». Money is integer
micro-dollars.

## ADR-010 — Words, not buttons, for moving tasks (2026-10-01)

Dates come from speech and text: `TaskOpsService.resolve_due` (shift «на 2 часа позже» → day/time → period start),
always computed by code. A missing date is asked in words and the next message answers it (`pending_choices`
`await_due`); a name that matches no title is resolved by the LLM choosing among open-task **titles**, then by
exact title in SQL. Several tasks at once still need a confirmation button.

