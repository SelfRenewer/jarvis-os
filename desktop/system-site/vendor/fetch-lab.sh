#!/usr/bin/env bash
# The lab's two big binaries are not in git: MediaPipe's WebAssembly (11 MB) and the hand model (7.8 MB).
# Pinned and verified (vetted 2026-10-14: tasks-vision 0.10.35 has no telemetry — 1.x posts usage stats to
# odml.pa.googleapis.com every minute; the lab page also forbids outside connections with CSP connect-src 'self').
set -euo pipefail
cd "$(dirname "$0")"
get() {  # url file algo expected(base64)
  local url=$1 file=$2 algo=$3 want=$4
  if [[ -f "$file" ]] && [[ "$(openssl dgst -"$algo" -binary "$file" | base64)" == "$want" ]]; then
    echo "ok   $file"; return
  fi
  local tmp; tmp="$(mktemp)"
  curl -fsSL "$url" -o "$tmp"
  local got; got="$(openssl dgst -"$algo" -binary "$tmp" | base64)"
  if [[ "$got" != "$want" ]]; then rm -f "$tmp"; echo "HASH MISMATCH for $file ($got)" >&2; exit 1; fi
  mkdir -p "$(dirname "$file")"; mv "$tmp" "$file"; chmod 644 "$file"; echo "got  $file"
}
get https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm/vision_wasm_internal.wasm \
  mediapipe/wasm/vision_wasm_internal.wasm sha256 alxkWEwqthx2O24gSvvbx84cr39SFhhzIryo35T2Rrw=
get https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task \
  mediapipe/hand_landmarker.task md5 FTGEMOo4UWcP6ZFBFqnPrQ==
