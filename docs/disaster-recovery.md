# Backup, restore and migrations

## What must be backed up
* PostgreSQL database (all state: jobs, ideas incl. raw audio, audit, tokens encrypted with `JARVIS_ENCRYPTION_KEY`).
* `.env` — store it separately (password manager). **Without `JARVIS_ENCRYPTION_KEY` the stored Google token
  cannot be decrypted** (re-run `jarvis google-auth` in that case).
* Dev machine: `dev_worker/repos.yaml` and the token files.
* Mac app: nothing to back up — `uv run jarvis desktop-setup` regenerates its config from `.env`.

## Backup
```bash
./scripts/backup.sh            # → backups/jarvis-<UTC timestamp>.dump (pg_dump custom format, mode 600)
```
The script validates the archive (`pg_restore --list`) and keeps the newest 14 dumps. Schedule it on the VPS,
e.g. `crontab -e`: `15 3 * * * cd /opt/jarvis && ./scripts/backup.sh >> backups/backup.log 2>&1`, and copy
`backups/` off-host (restic/rclone to object storage).

## Restore
```bash
# into a scratch database (non-destructive check)
./scripts/restore.sh backups/jarvis-XXXX.dump jarvis_restore_check
# over the live database (stops app services, restores, runs migrations, restarts)
CONFIRM=yes ./scripts/restore.sh backups/jarvis-XXXX.dump
```

## Verified
`./scripts/test_backup_restore.sh` seeds a marker row, dumps, restores into a scratch DB, compares row counts
of core tables (rows existing before the snapshot), checks the marker and the idea-immutability trigger, then
cleans up. It was executed successfully on 2026-09-23 against the running Docker stack (3 consecutive passes).

## Migrations
* Alembic, `migrations/versions/`: 0001 initial schema + triggers + HNSW index, 0002 notification lease,
  0003 notes + pending choices, 0004 medications, 0005 spend snapshots + `shopping_items.source_ref`,
  0006 `mac_actions`.
* `docker compose up` always runs the one-shot `migrate` service before app services start.
* CI runs `upgrade head → downgrade base → upgrade head → alembic check` (no model/migration drift).
* Procedure for a new migration: change models → `uv run alembic revision --autogenerate -m "..."` → review
  (replace custom types with `sa.DateTime(timezone=True)`) → `make test` → back up → deploy.

## Full rebuild on a new VPS
1. Install Docker + Tailscale, clone the repo, restore `.env`.
2. `docker compose up -d postgres`
3. `CONFIRM=yes ./scripts/restore.sh <latest dump>`
4. `docker compose up -d` → `docker compose run --rm api jarvis doctor`
5. Update `JARVIS_API_BIND` / DNS if the Tailscale IP or public IP changed.
