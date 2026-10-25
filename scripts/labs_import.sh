#!/usr/bin/env bash
# Import a lab report (PDF) into LMF «Анализы»: macOS PDFKit reads the text, the `api` container parses it by code
# (no LLM) and upserts the rows. The text (it holds the patient's name and address) lives in a private temp folder
# that is deleted on exit; nothing leaves the Mac.  Usage: ./scripts/labs_import.sh <report.pdf> [--dry-run]
set -euo pipefail
pdf="${1:?usage: ./scripts/labs_import.sh <report.pdf> [--dry-run]}"
shift
cd "$(dirname "$0")/.."
work="$(mktemp -d)"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
swift scripts/pdf_text.swift "$pdf" > "$work/report.txt"
docker compose run -T --rm --no-deps -v "$work:/in:ro" api jarvis labs-import --text /in/report.txt "$@"
