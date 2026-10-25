"""Jarvis Lab (hands + a schematic MacBook): the gesture rules run under node; the page server serves the lab with
a camera-friendly CSP that still forbids outside connections (MediaPipe 1.x posts usage stats — we pin 0.10.35)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2] / "desktop" / "system-site"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_gesture_rules() -> None:
    done = subprocess.run(
        ["node", str(SITE / "lab-gestures.check.mjs")], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_lab_csp_allows_wasm_and_the_camera_but_no_outside_connections() -> None:
    server = (SITE / "server.mjs").read_text(encoding="utf-8")
    assert "'wasm-unsafe-eval'" in server and "camera=(self)" in server
    assert "connect-src 'self'" in server  # no frame and no telemetry can leave the page
    assert "'/lab':" in server


def test_pinned_mediapipe_has_no_telemetry_endpoint() -> None:
    bundle = (SITE / "vendor" / "mediapipe" / "vision_bundle.mjs").read_text(encoding="utf-8")
    assert "odml.pa.googleapis.com" not in bundle and "https://" not in bundle
    fetch = (SITE / "vendor" / "fetch-lab.sh").read_text(encoding="utf-8")
    assert "tasks-vision@0.10.35" in fetch
