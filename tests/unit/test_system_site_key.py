"""Jarvis OS page access key: Python (`jarvis system-link`) and the page's server (desktop/system-site/auth.mjs,
checked by desktop/system-site/check.mjs) derive the same key from the Mac app's token."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis import cli
from jarvis.system.site import site_key, site_link
from tests.conftest import make_settings

# The same vector is asserted in desktop/system-site/check.mjs.
TOKEN = "jarvis-test-token-0123456789abcdef"
KEY = "97af05405566fe0a1b7685f4a1aa8cc5"  # gitleaks:allow (derived from TOKEN above)


def test_key_vector() -> None:
    assert site_key(TOKEN) == KEY
    assert site_link(TOKEN) == f"http://127.0.0.1:39110/?key={KEY}"
    assert site_link(TOKEN, 5190, "localhost") == f"http://localhost:5190/?key={KEY}"
    assert site_key(TOKEN + "x") != KEY


CHECK = Path(__file__).resolve().parents[2] / "desktop" / "system-site" / "check.mjs"


def test_check_script_carries_the_same_vector() -> None:
    check = CHECK.read_text(encoding="utf-8")
    assert TOKEN in check and KEY in check


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_server_door() -> None:
    """The page server itself (loopback only, a throwaway token): no session → 401, a session → 200, a cross-origin
    «Спросить» → 403, the rate limit, sessions across a restart, and the same key as here."""
    done = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "checks passed" in done.stdout


def test_system_link_prints_the_link_never_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"base_url": "http://127.0.0.1:8080", "token": TOKEN}), encoding="utf-8")
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", config)
    assert cli.cmd_system_link(make_settings(), argparse.Namespace(port=39110)) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"http://127.0.0.1:39110/?key={KEY}"
    assert TOKEN not in out


def test_system_link_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", tmp_path / "missing.json")
    assert cli.cmd_system_link(make_settings(), argparse.Namespace(port=39110)) == 2
    assert "desktop-setup" in capsys.readouterr().out
