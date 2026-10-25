# Operations runbook

| Task | Command |
|---|---|
| Readable snapshot (services, next reminders, sent, errors) | `make status` |
| Live log feed (readable) | `make watch` |
| Status of containers | `docker compose ps` |
| Diagnostics | `make doctor` (no secrets printed, no billable calls) |
| Liveness / readiness | `curl -s localhost:8080/health` · `curl -s localhost:8080/ready` |
| Logs (JSON, correlation ids) | `docker compose logs -f --tail=200 worker` |
| Queue overview | `curl -s -H "Authorization: Bearer $JARVIS_API_TOKEN" localhost:8080/api/v1/status` |
| Force a sync | Telegram `/sync`, or `docker compose run --rm api jarvis run-job todoist.reconcile` |
| Re-authorize Google | `uv run jarvis google-auth` on the Mac, Docker running (see integrations) |
| Install / update Jarvis for Mac | `./desktop/install.sh` (rebuild may re-ask microphone/speech access) |
| Start the core after a Mac reboot | Docker Desktop autostart is off (user setting): open Docker, or «Запустить» in the Jarvis window |
| Backup now | `./scripts/backup.sh` |
| Verify backups restore | `./scripts/test_backup_restore.sh` |
| Dev Worker boundary smoke test | `./scripts/smoke_dev_worker.sh` (scratch DB, fake Claude) |

## Dead jobs
```sql
SELECT job_type, attempts, last_error, finished_at FROM scheduled_jobs WHERE status = 'dead' ORDER BY finished_at DESC;
SELECT * FROM job_attempts WHERE job_id = '<id>' ORDER BY attempt_no;
```
Re-queue one after fixing the cause (safe: handlers are idempotent):
```sql
UPDATE scheduled_jobs SET status='pending', attempts=0, run_at=now(), last_error=NULL WHERE id='<id>';
```

## Common situations
* **"авторизация недействительна" in Telegram** — credentials revoked/expired; the affected periodic jobs are
  skipped (one alert per provider per day) until fixed.
* **Reminders without travel time** — `DEFAULT_ORIGIN` empty, address not geocodable, Maps quota reached or key
  missing. Jarvis then reminds relative to the event start and says so; it never guesses travel time.
* **Missed reminders after downtime** — on restart overdue reminders are evaluated with grace windows: calls only
  ≤ 5 min late; later → Telegram recovery notice; after event start → one "missed" notice.
* **Processes** — every service writes a heartbeat (`integration_sync_state.provider='process'`); Docker healthchecks
  use `jarvis heartbeat-check`. Workers and schedulers can be scaled; the bot must be a single instance.
* **Costs** — `usage_counters` holds per-day counts (`llm`, `tts`, `stt`, `google_maps`, `phone_calls`,
  `weather_alert`); OpenRouter spend per period comes from `spend_snapshots` (📊 in the chat).
* **Dead periodic jobs after an outage** — a network outage opens the circuit breakers (`CircuitOpenError`) and the
  periodic jobs of that window end `dead`; the next slots run normally, so no re-queue is needed (e.g. 05–08.10.2026).
* **Restart inside a briefing window** (08:00–11:00 / 21:00–24:00) sends that briefing at once if it was not sent
  yet — during development mark the slot `skipped` first.

## Retention
Daily `maintenance.cleanup` deletes idempotency ledgers older than 30 days (`telegram_updates`,
`webhook_deliveries`) and finished periodic jobs. Ideas, audit events, reminders and dev results are kept.
