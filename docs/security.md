# Security

## Trust boundaries
| Boundary | Control |
|---|---|
| Internet → API | Only `/health` and `/webhooks/*` are published by Caddy. Every webhook verifies provider authenticity (Todoist HMAC-SHA256, Telegram secret header, Google channel token, Vapi bearer/`X-Vapi-Secret`, Twilio HMAC-SHA1) with constant-time comparison. Missing secret configuration ⇒ `503` (disabled), never "accept all". Bodies are size-limited (1 MB). |
| Admin API | `Authorization: Bearer JARVIS_API_TOKEN`; bound to loopback / Tailscale only. |
| Telegram users | Allowlisted numeric user ids **and** private chat with that same id. Strangers get no reply (no information leak); attempts are audited. Each `update_id` processed once. |
| Inline buttons | Every `callback_data` is HMAC-signed by the Telegram adapter (`CallbackSigner`, 9-byte HMAC-SHA256, key derived from the bot token) and verified first in `handle_callback`: modified clients (AyuGram…) cannot forge or edit ids (`tr:HAIR` → `tr:MILK`); rejections are audited (`CALLBACK_REJECTED`). |
| Forwarded messages | Never executed as commands — only offered to be saved (task / idea / note). |
| Mac app → API | `/api/desktop/v1/*` on loopback only, `Authorization: Bearer <token>` derived (HMAC) from existing secrets; written by `jarvis desktop-setup` into a mode-600 config, never printed. Same allowlisted chat id as Telegram. |
| API → Mac actions | Closed set (`open_app`, `open_url`, `open_path`, `claude_code`) resolved by Jarvis from what the Mac reported; the app re-validates (installed app, http/https only, existing path under home or /Applications, Claude only in a reported project), never runs shell strings; Claude's prompt is passed through a file. Pending actions expire in 10 min; claimed-but-unreported fail after 5 min (at most once). Claude Code with a task needs the «▶️ Запустить» button. |
| Server → dev machine | None. The Dev Worker **pulls**; the server cannot open connections to it. |
| Dev Worker ← server | Bearer token (compared by SHA-256), HMAC-signed job payload (`DEV_WORKER_SIGNING_KEY`), closed job schema (no paths/commands), approval flag + id required, repo allowlist local to the dev machine, branch regex `^jarvis/…`, protected branches refused. |
| Claude Code ← repository | `permission_mode="dontAsk"` + explicit allowlist, `setting_sources=[]` (repo `.claude/` ignored), web/MCP/subagent tools disallowed, PreToolUse hook denying paths outside the worktree, `.git` internals, secret-like files (`.env*`, keys, `.netrc`, credentials…), shell metacharacters and network/exfiltration commands; secret env vars blanked; system prompt states repo content is untrusted. Never `bypassPermissions`/`--dangerously-skip-permissions`. Git hooks disabled (`core.hooksPath=/dev/null`). |
| LLM output | JSON-schema constrained + Pydantic validated; closed intent enum; LLM never supplies database ids (references resolved from `chat_context`); refusal and truncation handled; one bounded repair. |
| Voice agent | Receives only contextual variables; its only server tool is read-only `get_today_schedule`; everything else returns "unavailable". |

## Secrets
* Only via environment / `.env` (git-ignored, mode 600). `.env.example` documents names; no fake secrets are committed.
* `SecretStr` in settings (not printed by `repr`); a log filter redacts configured secret values, bearer tokens,
  `api_key=` pairs and Telegram bot tokens inside URLs; `httpx/httpcore/anthropic` loggers run at WARNING.
* Google refresh tokens are encrypted at rest with Fernet (`JARVIS_ENCRYPTION_KEY`).
* Audit `details` are sanitized (keys containing token/secret/password/phone/card… are masked).
* `jarvis doctor` reports configuration presence and probe results without printing values.
* Dev Worker secrets can be read from files (`DEV_WORKER_TOKEN_FILE`, `DEV_WORKER_SIGNING_KEY_FILE`).

## Cost & side-effect safety
* `JARVIS_ENV=test` builds **only fakes**; the normal test suite also blocks non-loopback sockets/DNS.
* Live calls need `VOICE_CALLS_ENABLED=true` at runtime; live-call tests need `ALLOW_LIVE_CALL_TESTS=true`;
  nested Claude runs in tests need `ALLOW_LIVE_CLAUDE_TESTS=true`; purchases need `ALLOW_LIVE_PURCHASES=true`
  **and** a provider with a permitted ordering API (none exists today).
* Daily budgets: `LLM_MAX_CALLS_PER_DAY`, `TTS_MAX_CALLS_PER_DAY`, `STT_MAX_CALLS_PER_DAY`, `MAPS_MAX_REQUESTS_PER_DAY`,
  `MAX_CALLS_PER_DAY`, `WEATHER_ALERTS_PER_DAY` (atomic DB counters); the OpenRouter key has its own monthly limit.
* Phone calls are at-most-once (ledger committed before dialing; unknown outcome ⇒ Telegram fallback, never re-dial).
* Dev jobs: per-job Claude budget (`max_budget_usd`) and turn limit; not retried automatically.

## Approval boundaries
Planning apply (unless auto-apply policy), development start, purchase/checkout, low-confidence substitutions,
deleting a task, completing a task with subtasks, any change to several tasks at once (incl. moving purchases from
tasks to the shopping list), starting Claude Code with a task on the Mac.
Approval = one conditional `UPDATE … WHERE status='pending'`; duplicate callbacks are no-ops.

## Data integrity
Naive datetimes are rejected at the ORM type; raw ideas are immutable and undeletable (DB trigger);
money never uses floats; job completion requires lease ownership.

## Accepted residual risks
* A compromised VPS can forge approvals and craft spec text for an allowlisted repo. Mitigations: worker-side
  sandboxing, no push/merge/deploy, human review of the branch. Specs are not a security boundary.
* Telegram chat commands are at-most-once: if a process crashes mid-update, the user re-sends.
* A crash between Telegram send and ledger update can duplicate one reminder message (at-least-once by design).
* The Claude CLI fallback adapter has no Python hook (relies on dontAsk + rules); the SDK adapter is the default.
