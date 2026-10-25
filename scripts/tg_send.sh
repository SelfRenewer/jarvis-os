#!/usr/bin/env bash
# Send a message through the Jarvis bot (the running `api` container) to the owner and/or added recipients.
#
#   ./scripts/tg_send.sh "<b>Готово</b>: …"           to the owner. Telegram HTML: <b> <i> <u> <s> <code> <pre> <a> <blockquote>
#   ./scripts/tg_send.sh --to анна "текст"             to a recipient (repeat --to for several; --to всем = owner + all)
#   ./scripts/tg_send.sh -f report.html                the text from a file
#   echo "текст" | ./scripts/tg_send.sh -              from stdin (only with «-»: an agent's open stdin must not hang it)
#   ./scripts/tg_send.sh --plain "a < b & c"           plain text, no HTML
#   ./scripts/tg_send.sh --list                        who can be written to
#
# Recipients: `docker compose exec api jarvis tg-recipient add|remove|list` — by numeric Telegram id; the person must
# press Start in the bot once, or Telegram refuses. In HTML mode escape & < > in ordinary text (&amp; &lt; &gt;).
# Needs Docker running and `make up`. Prints «sent <who> <message id>» per person; exit 1 with Telegram's reason.
set -euo pipefail
cd "$(dirname "$0")/.."

plain=()
to=()
text=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plain) plain=(--plain); shift ;;
    --to) to+=(--to "$2"); shift 2 ;;
    --list) exec docker compose exec -T api jarvis tg-recipient list ;;
    -f|--file) text="$(cat "$2")"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    -) text="$(cat)"; shift ;;
    *) text="$1"; shift ;;
  esac
done
if [[ -z "${text//[[:space:]]/}" ]]; then
  echo "Нечего отправлять: дайте текст аргументом, через -f файл или «-» для stdin." >&2
  exit 1
fi

exec docker compose exec -T -e JARVIS_TG_TEXT="$text" api jarvis tg-send ${to[@]+"${to[@]}"} ${plain[@]+"${plain[@]}"} </dev/null
