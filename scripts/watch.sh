#!/usr/bin/env bash
# Live, readable activity feed from all JARVIS services (Ctrl+C to stop). Secrets are redacted in the logs.
set -euo pipefail
cd "$(dirname "$0")/.."
FOLLOW_FLAG="-f"; [[ "${FOLLOW:-1}" == "0" ]] && FOLLOW_FLAG=""
# shellcheck disable=SC2086
docker compose logs $FOLLOW_FLAG --tail="${TAIL:-30}" api worker scheduler bot 2>/dev/null | python3 -u -c '
import json, sys
from datetime import datetime, timezone, timedelta
tz = timezone(timedelta(hours=4))
noise = ("GET /health", "heartbeat", "setup plugin", "Context impl", "Will assume")
for line in sys.stdin:
    service, _, payload = line.partition("|")
    service = service.strip().split("-")[0] if service else "?"
    payload = payload.strip()
    try:
        rec = json.loads(payload)
    except ValueError:
        if payload and not any(n in payload for n in noise):
            print(f"         {service:<9} {payload}")
        continue
    msg = str(rec.get("msg", ""))
    if any(n in msg for n in noise):
        continue
    try:
        ts = datetime.fromisoformat(rec["ts"]).astimezone(tz).strftime("%H:%M:%S")
    except (KeyError, ValueError):
        ts = "        "
    level = rec.get("level", "")
    mark = {"WARNING": "!", "ERROR": "X", "CRITICAL": "X"}.get(level, " ")
    print(f"{ts} {mark} {service:<9} {msg}")
'
