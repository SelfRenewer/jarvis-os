# Architecture

State on 2026-10-14. Runs on the user's Mac (Docker Compose); the same stack is meant to move to a VPS later
(`docs/deployment.md`).

```
   Telegram (text, voice, signed buttons)        Jarvis for Mac (menu bar: ⌥Space, «Джарвис», chat, HUD)
                │ long polling                                │ loopback HTTP, bearer token
┌───────────────▼─────────────────────────────────────────────▼──────────────── Docker Compose ─┐
│  bot ──────────────► BotService ◄──────────── api /api/desktop/v1/* (DesktopChannel)            │
│                      │  router: keyword patterns → LLM only when unclear (RoutedMessage.actions) │
│                      ▼                                                                          │
│   agents (modules): agenda · tasks · planner · reminders · briefings · inbox/notes · shopping ·   │
│   meds · weather · ideas→spec · voice · usage stats · desktop actions · calc/fx · TLT/TPL · finance│
│                      │                                                                          │
│   approvals (signed buttons, single conditional UPDATE) · audit · daily budgets                 │
│                      │                                                                          │
│  scheduler ──► scheduled_jobs ◄── worker   (SKIP LOCKED, leases, retries, dead-letter)           │
│  periodic slots: briefings 08:00/21:00 · reminders.plan 5 min · todoist 2 min · calendar 5 min · │
│  meds.tick 5 min · weather.watch 30 min · usage.snapshot 1 h · tracker.nudge 23:00 · cleanup 04:00│
│                      │                                                                          │
│  postgres (pgvector) — the only durable truth                                                   │
└──────────────────────┼──────────────────────────────────────────────────────────────────────────┘
                       ▼
   Todoist API v1 · Google Calendar v3 · OpenRouter (LLM, vision, web search, TTS, STT, key/credits) · Open-Meteo · open.er-api ·
   CoinGecko · LMF on the Mac (host.docker.internal:4321) · LocalSites «Пульт» (:7788) ·
   (not connected: Google Maps Routes, Vapi/Twilio, Voyage, webhooks/HTTPS, Dev Worker)
```

## Processes
| Service | Role | Scales |
|---|---|---|
| `api` | FastAPI: health/readiness, webhooks, admin API, Dev-Worker pull API, **desktop API** for the Mac app | stateless |
| `worker` | executes `scheduled_jobs` (claims with `FOR UPDATE SKIP LOCKED`) | N instances safe |
| `health` | home network (port 8097): only `POST /webhooks/health` (HMAC) + `GET /health` — watch and scale data from the phone (Health Connect → Life Dashboard Companion) | stateless |
| `scheduler` | materializes periodic work as deduplicated jobs; lease reaper | N instances safe (dedupe) |
| `bot` | Telegram long polling; offset persisted in DB | 1 instance |
| `postgres` | durable state (pgvector) | — |
| `caddy` (profile `public`) | HTTPS for `/health` and `/webhooks/*` only (not used yet) | — |
| Jarvis for Mac | `~/Applications/Jarvis.app` (Swift): chat, small voice window, wake word, executes `mac_actions` | 1 per Mac |
| `dev_worker` | on the dev machine (uv, launchd/systemd) — implemented, **not connected** | 1 per machine |

## Code layout
```
jarvis/core          settings, clock/timezones, dates (Russian phrases → dates/shifts), money, units, http resilience,
                     security (HMAC callback signer, Fernet, desktop token), budgets, logging (redaction), text (title links)
jarvis/db            SQLAlchemy models (UTCDateTime rejects naive datetimes), sessions
jarvis/scheduler     durable queue, runner/handler registry, periodic scheduler
jarvis/jobs          all job handlers + the periodic schedule
jarvis/integrations  todoist · google_calendar (+auth_flow) · telegram · llm (OpenRouter/Anthropic) · tts · stt ·
                     weather (Open-Meteo) · maps · voice (vapi/twilio) · embeddings
jarvis/sync          Todoist cache (+webhook), Calendar window reconcile (+push channels, Strategy-B mirror), task↔event links
jarvis/bot           intents (router), BotService (commands, callbacks), persona, menu keyboard, speech text
jarvis/agenda        day/period agenda, morning briefing text, evening review, «needs attention»
jarvis/tasks         task ops (resolve by name in SQL / by meaning via LLM titles), resolve_due, approvals (task_op)
jarvis/planner       deterministic engine + proposals → approval → apply
jarvis/reminders     timing policy, travel estimation, reminder planning/firing
jarvis/inbox         notes, pending choices (buttons and «answer with the next message»)
jarvis/shopping      parsing (nominative names), list, purchases from tasks, optimizer, providers
jarvis/meds          medication courses, intake state machine
jarvis/weather       categories, texts, change detection
jarvis/sites         LocalSites «Пульт»: find a site by ear, start/stop, LAN / tunnel links
jarvis/calc          safe arithmetic (AST, exact fractions), currency parsing + conversion (open.er-api, CoinGecko)
jarvis/tracker       TLT/TPL reports → drafts → «✅ Записать» → LMF; summaries; fill-in reminders; food (text, photo, removal)
jarvis/photos        one vision LLM call: food label | dish | receipt | other
jarvis/finance       expenses (phrases, receipts), totals by period/category/currency
jarvis/search        search by meaning (embeddings backfill, hybrid ranking)
jarvis/research      web research after a price button (OpenRouter `web` plugin, Exa), summary + sources
jarvis/ideas         capture (immutable raw), LLM structuring, retrieval, DEV_TASK rendering
jarvis/development   dev jobs, approval, signed pull API for the Dev Worker
jarvis/usage         statistics periods, OpenRouter spend per period from hourly key samples
jarvis/desktop       Mac env, «открой …» resolution, mac_actions queue, DesktopChannel
jarvis/voice notify  calls (at-most-once), Telegram notification ledger (at-least-once)
jarvis/api           FastAPI app
desktop/JarvisMac    Swift menu-bar app (built by desktop/build.sh, installed by desktop/install.sh)
dev_worker           policy, git ops, Claude adapters (fake/sdk/cli), checks, pull loop
migrations           Alembic 0001–0009
```

## Key flows
**A message** (Telegram or Mac) → `normalize_command` (fillers, spoken verb forms, «cloud code» → «Claude Code») →
`split_requests` → keyword router; unclear parts → LLM (`RoutedMessage`, sees open task *titles*, never ids) →
`dispatch_all` (one reply for several requests) → agent handler → reply. A pending question («На когда перенести …?»,
«В каком проекте?») takes the next message first. Mac replies are captured by `DesktopChannel` (same chat id →
shared context) and returned to the app; voice is attached only to conversation replies.

**Buttons** → every inline `callback_data` is HMAC-signed by the Telegram adapter → `handle_callback` verifies before
anything else → approvals flip with one conditional `UPDATE` → side effects run as jobs (`tasks.apply_op`, …).

**Todoist** → polling reconcile every 2 min (+ before answers about tasks, + before each reminder) → upsert, stale
snapshots ignored via `remote_updated_at`. Webhooks (with HTTPS) only notify.

**Calendar** → bounded window (`now-1d … +14d`, `singleEvents`, `showDeleted`) every 5 min → etag diff → cache;
vanished events → cancelled. Tasks and hand-made events with the same title are linked (`jarvis/sync/links.py`):
one reminder per item; a task closed anywhere silences its event.

**Planner** → tasks with a date but no time + busy blocks → deterministic slot search → proposal → approval →
`planner.apply` writes `due_datetime` + `duration` (never into the past).

**Reminders** → every 5 min: desired reminder set per upcoming event/task, diffed against stored reminders →
`reminder.fire` jobs → re-validate (task re-read from Todoist) → grace windows (late → recovery notice).

**Briefings** → 08:00: agenda + weather + meds + shopping + «needs attention» + plan proposal; 21:00: what is not done,
calendar duplicates likely done. Slots older than the grace window are skipped.

**Mac actions** → a resolved `Target` (installed app / http(s) URL / reported path / Claude Code in a reported project)
→ `mac_actions` (Claude with a task: `proposed` until «▶️ Запустить») → the app long-polls `/actions`, re-validates,
executes (Claude Code via a `.command` script, prompt in a file), reports the result.

**Tracker** → a TLT/TPL report is parsed by code (sleep, score, mood, codes, days), label words are matched to the
LMF dictionary, only leftovers go to the LLM (closed lists of codes/items) → draft in an approval → «✅ Записать» →
job `tracker.apply` re-reads the day, merges tags, `PUT /api/day` / `PUT /api/tpl` (LMF down → queue retries).

**Spend** → `usage.snapshot` hourly stores the key's cumulative usage (+ derived day/week/month starts) →
spend for a period = now − the sample at its start; exact when no paid call happened in between, otherwise «≤».

## Data model (main tables)
Queue and safety: `scheduled_jobs`, `job_attempts`, `approvals`, `audit_events`, `notifications`, `webhook_deliveries`,
`telegram_updates`, `usage_counters`, `integration_accounts` (encrypted OAuth), `integration_sync_state`,
`user_preferences`, `chat_context`, `pending_choices`, `agent_runs`.
Domain: `todoist_tasks`, `calendar_events`, `calendar_channels`, `planner_runs`, `planning_proposals(+_items)`,
`reminders`, `travel_estimates`, `call_requests`, `notes`, `medications`, `medication_intakes`, `shopping_lists`,
`shopping_items` (`source_ref`), `shopping_preferences`, `shopping_stores`, `shopping_offers`, `shopping_quotes`,
`purchase_requests`, `raw_media`, `ideas` (immutable raw input), `idea_versions`, `memory_embeddings` (HNSW),
`dev_specs`, `development_jobs`, `development_runs`, `development_artifacts`, `dev_worker_heartbeats`,
`spend_snapshots`, `mac_actions`, `expenses`.

All timestamps are `timestamptz` in UTC; money is integer minor units + `Decimal` (OpenRouter spend: micro-dollars).
