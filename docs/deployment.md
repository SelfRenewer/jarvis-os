# Deployment

## Today: the user's Mac (2026-10)
The Compose stack runs on the Mac with ports bound to loopback (`127.0.0.1:8080` API, `127.0.0.1:55432` Postgres):
`make up` / `make restart`, `make doctor`. Docker Desktop autostart is **off** (the user's choice), so after a reboot
Jarvis is down until Docker starts (Docker Desktop, or «Запустить» in the Jarvis for Mac window).
Jarvis for Mac: `./desktop/install.sh` → `~/Applications/Jarvis.app`; launch at login is a toggle in its menu.
Limits of this setup: everything sleeps with the Mac; no HTTPS, so webhooks run as polling.

## A. Linux VPS (always-on, planned)
Requirements: Docker Engine + Compose v2, 1 vCPU / 1–2 GB RAM is enough, Tailscale.

```bash
# 1. code + config
git clone <your repo> jarvis && cd jarvis
cp .env.example .env && chmod 600 .env
docker compose run --rm --no-deps api jarvis gen-key   # paste the output into .env
#    set POSTGRES_PASSWORD (before the first start!) and make DATABASE_URL match it,
#    set TELEGRAM_*, TODOIST_*, OPENROUTER_API_KEY, GOOGLE_*, … (see .env.example)

# 2. private network for the Dev Worker
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
#    set JARVIS_API_BIND=<VPS Tailscale IP 100.x.y.z> in .env

# 3. start
docker compose up -d --build
docker compose run --rm api jarvis doctor
curl -s http://127.0.0.1:8080/health
```

### Public HTTPS (optional, enables webhooks)
Point a DNS A/AAAA record at the VPS, open ports 80/443, then:
```bash
# .env: JARVIS_DOMAIN=jarvis.example.com  JARVIS_PUBLIC_BASE_URL=https://jarvis.example.com
docker compose --profile public up -d
```
Caddy obtains certificates automatically and publishes only `/health` and `/webhooks/*`.
Without a domain everything works in polling/reconciliation mode.

### Updates / migrations
```bash
git pull && docker compose build && ./scripts/backup.sh && docker compose up -d   # `migrate` runs first
```

## B. Trusted development machine (Dev Worker)
```bash
brew install uv            # or the official installer on Linux/Windows
uv sync --all-extras       # installs claude-agent-sdk
claude --version           # Claude Code must be installed and logged in
mkdir -p ~/.config/jarvis && chmod 700 ~/.config/jarvis
printf '%s' '<DEV_WORKER_TOKEN>'       > ~/.config/jarvis/dev_worker_token
printf '%s' '<DEV_WORKER_SIGNING_KEY>' > ~/.config/jarvis/dev_worker_signing_key
chmod 600 ~/.config/jarvis/*
cp dev_worker/repos.example.yaml dev_worker/repos.yaml   # edit: server_url (Tailscale name), repos, commands
DEV_WORKER_TOKEN_FILE=~/.config/jarvis/dev_worker_token \
DEV_WORKER_SIGNING_KEY_FILE=~/.config/jarvis/dev_worker_signing_key \
  uv run jarvis-dev-worker check-config
```
Run it as a service: macOS `deploy/launchd/com.jarvis.dev-worker.plist`, Linux
`deploy/systemd/jarvis-dev-worker.service` (user unit). On Windows use Task Scheduler with the same command.
On the server, list the same repo ids in `DEV_REPO_IDS`.

Never expose the development machine to the internet; it only needs outbound access to the VPS over Tailscale.
