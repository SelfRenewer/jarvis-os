#!/usr/bin/env bash
# Jarvis OS on demo data — no Jarvis, no Docker, no keys. The system map is built from this code
# (jarvis/system/map.py), the page server runs in fixture mode on 127.0.0.1:${PORT:-39110} and prints a link with
# a demo key. macOS (the server binds itself to the Mac it runs on), uv and Node 20+. Ctrl+C stops it.
set -euo pipefail
cd "$(dirname "$0")/.."
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
JARVIS_ENV=test uv run --quiet python - "$dir/map.json" "${PORT:-39110}" <<'PY'
import hashlib
import hmac
import json
import sys

from jarvis.core.config import Settings
from jarvis.system.map import system_map

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({"graph": system_map(Settings())}, f, ensure_ascii=False)
# the demo login key: the server's fixed fixture token, signed like a real one (auth.mjs siteKey)
key = hmac.new(b"fixture-mode-token", b"jarvis-system-site", hashlib.sha256).hexdigest()[:32]
print(f"Open: http://127.0.0.1:{sys.argv[2]}/?key={key}   (lab: …&to=/lab)")
PY
cd desktop/system-site
JARVIS_SYSTEM_FIXTURE="$dir/map.json" JARVIS_SYSTEM_SESSIONS="$dir/sessions.json" JARVIS_OS_DEVICE_FILE="$dir/os-device.json" \
  JARVIS_TLT_AUTOSTART=0 PORT="${PORT:-39110}" node server.mjs
