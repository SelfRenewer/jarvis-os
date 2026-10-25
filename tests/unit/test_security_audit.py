"""Security review 2026-10-14: LMF's machine token (M2), per-service secrets in compose (L2). No network, no DB."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from jarvis.calls.service import configured, enabled
from jarvis.cli import cmd_lmf_setup
from jarvis.core.errors import AuthError, NotFoundError
from jarvis.doctor import FAIL, OK, WARN, lmf_access_row
from jarvis.integrations.lmf.client import HttpLifeTracker
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "T" * 43  # the shape of LMF's token (32 random bytes, base64url)


# --- M2: Jarvis → LMF with the machine token --------------------------------------------------------------------


@respx.mock
async def test_lmf_requests_carry_the_token() -> None:
    route = respx.get("http://lmf.test/api/day/2026-10-14").mock(return_value=httpx.Response(200, json={}))
    api = HttpLifeTracker("http://lmf.test", token=TOKEN)
    await api.day(date(2026, 10, 14))
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    await api.aclose()


@respx.mock
async def test_no_token_no_header_and_a_refusal_is_an_auth_error() -> None:
    route = respx.get("http://lmf.test/api/bootstrap").mock(return_value=httpx.Response(401, json={"login": True}))
    api = HttpLifeTracker("http://lmf.test")
    with pytest.raises(AuthError):
        await api.bootstrap()
    assert "authorization" not in route.calls[0].request.headers
    await api.aclose()


@respx.mock
async def test_auth_status_and_an_old_lmf() -> None:
    respx.get("http://new.test/api/auth").mock(
        return_value=httpx.Response(200, json={"password": True, "access": "token"})
    )
    respx.get("http://old.test/api/auth").mock(return_value=httpx.Response(404, json={"error": "Не найдено"}))
    new, old = HttpLifeTracker("http://new.test", token=TOKEN), HttpLifeTracker("http://old.test")
    assert (await new.auth_status())["access"] == "token"
    with pytest.raises(NotFoundError):
        await old.auth_status()
    await new.aclose()
    await old.aclose()


@pytest.mark.parametrize(
    ("answer", "token_set", "status", "says"),
    [
        ({"password": True, "access": "token"}, True, OK, "token accepted; UI password set"),
        ({"password": False, "access": "token"}, True, WARN, "no UI password yet"),
        ({"password": True, "access": "none"}, True, FAIL, "rejected"),
        ({"password": False, "access": "open"}, False, WARN, "open to any container"),
        ({"password": True, "access": "none"}, False, FAIL, "requires a token"),
        (None, True, WARN, "old version"),
    ],
)
def test_doctor_lmf_access_row(answer: dict[str, Any] | None, token_set: bool, status: str, says: str) -> None:
    got_status, detail = lmf_access_row(answer, token_set=token_set)
    assert got_status == status and says in detail
    assert TOKEN not in detail


def _setup_args(token_file: Path) -> argparse.Namespace:
    return argparse.Namespace(token_file=str(token_file), check_url="")


def test_lmf_setup_copies_the_token_into_env_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TODOIST_API_TOKEN=keep-me\nLMF_API_TOKEN=old\n", encoding="utf-8")
    token_file = tmp_path / "api-token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    assert cmd_lmf_setup(make_settings(), _setup_args(token_file)) == 0
    env = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    assert env == ["TODOIST_API_TOKEN=keep-me", f"LMF_API_TOKEN={TOKEN}"]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_lmf_setup_refuses_without_a_token_or_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cmd_lmf_setup(make_settings(), _setup_args(tmp_path / "api-token")) == 2  # no .env here
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    assert cmd_lmf_setup(make_settings(), _setup_args(tmp_path / "missing")) == 1
    bad = tmp_path / "bad-token"
    bad.write_text("short value; rm -rf\n", encoding="utf-8")
    assert cmd_lmf_setup(make_settings(), _setup_args(bad)) == 1
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "A=1\n"
    assert "rm -rf" not in capsys.readouterr().out


# --- L2: secrets only where they are needed ---------------------------------------------------------------------


def _services() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return {name: svc for name, svc in compose["services"].items() if svc.get("image") == "jarvis-app:local"}


def test_caller_secrets_reach_only_the_caller() -> None:
    services = _services()
    assert {"api", "worker", "scheduler", "bot", "health", "caller", "migrate"} <= set(services)
    for name, svc in services.items():
        env = svc.get("environment") or {}
        if name == "caller":
            assert "TELEGRAM_CALLER_SESSION" not in env and svc["env_file"]  # from .env, untouched
        elif name != "health":
            assert env["TELEGRAM_CALLER_SESSION"] == "" and env["TELEGRAM_CALLER_API_HASH"] == "", name


def test_the_lan_service_gets_an_allowlist_not_the_env_file() -> None:
    health = _services()["health"]
    assert health["env_file"] == []
    assert set(health["environment"]) == {
        "DATABASE_URL",
        "HEALTH_WEBHOOK_SECRET",
        "JARVIS_ENV",
        "LOG_LEVEL",
        "LOG_JSON",
        "DEFAULT_USER_TIMEZONE",
    }


def test_calls_are_queued_without_the_session_but_dialled_only_with_it() -> None:
    base = {"telegram_calls_enabled": True, "telegram_caller_api_id": 12345}
    blanked = make_settings(**base, telegram_caller_api_hash="", telegram_caller_session="")
    assert enabled(blanked) and not configured(blanked)  # bot / worker / scheduler: queue, never dial
    caller = make_settings(**base, telegram_caller_api_hash="hash-123456", telegram_caller_session="session-123456")
    assert enabled(caller) and configured(caller)
    assert not enabled(make_settings(telegram_caller_api_id=12345))  # switched off
