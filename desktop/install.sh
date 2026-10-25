#!/bin/zsh
# Installs Jarvis for Mac: builds the app, writes its config (local API address + token, mode 600),
# copies it to ~/Applications and starts it. Usage (from the repo root): ./desktop/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
./desktop/build.sh
uv run jarvis desktop-setup
mkdir -p "$HOME/Applications"
pkill -x Jarvis 2>/dev/null || true
rm -rf "$HOME/Applications/Jarvis.app"
# Moved, not copied: a second Jarvis.app with the same bundle id shows up twice in Spotlight/Launchpad.
mv desktop/build/Jarvis.app "$HOME/Applications/Jarvis.app"
# Right after the move LaunchServices may not know the new bundle yet (seen live: error -600) — retry a few times.
for attempt in 1 2 3; do
  open "$HOME/Applications/Jarvis.app" --args --show 2>/dev/null && break
  sleep 2
done
sleep 2
pgrep -x Jarvis >/dev/null || { echo "⚠️ Jarvis did not start — open ~/Applications/Jarvis.app manually"; exit 1; }
echo "✓ Jarvis is in the menu bar (⌥Space). Installed to ~/Applications/Jarvis.app"
