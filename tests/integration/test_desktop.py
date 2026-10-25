"""Jarvis for Mac (2026-10-01): the local API the menu-bar app talks to, the action queue, Claude Code launches.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from jarvis.api.app import create_app
from jarvis.bot.fmt import strip_tags
from jarvis.bot.service import MAC_NOT_CONNECTED, BotService
from jarvis.core.security import TokenCipher, desktop_token
from jarvis.db.models import MacAction
from jarvis.services.container import build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.db
USER = 1001
HELLO = {
    "apps": ["Telegram", "Safari", "Claude", "Cursor"],
    "projects": [{"name": "JarvisOS", "path": "/Users/u/Projects/JarvisOS"}],
    "version": "1.0",
}


@pytest.fixture
def mac(db: Any, clock: Any) -> Any:
    c = build_container(make_settings(jarvis_encryption_key=TokenCipher.generate_key()), db=db, clock=clock)
    c.desktop_token = desktop_token(c.settings.jarvis_encryption_key)
    return c


def client(c: Any, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token or c.desktop_token}"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url="http://t", headers=headers)


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


async def test_token_is_required(mac: Any) -> None:
    async with client(mac, token="wrong") as http:
        assert (await http.post("/api/desktop/v1/hello", json=HELLO)).status_code == 401
        assert (await http.get("/api/desktop/v1/actions")).status_code == 401
        assert (await http.post("/api/desktop/v1/message", json={"text": "привет"})).status_code == 401
    plain = build_container(make_settings(), db=mac.db, clock=mac.clock)  # no secret to derive a token from
    async with client(plain, token="x") as http:
        assert (await http.get("/api/desktop/v1/actions")).status_code == 503


async def test_open_from_the_mac_app(mac: Any) -> None:
    async with client(mac) as http:
        first = (await http.post("/api/desktop/v1/message", json={"text": "открой телегу"})).json()
        assert first["messages"][0]["text"] == MAC_NOT_CONNECTED  # the Mac has not registered yet
        assert (await http.post("/api/desktop/v1/hello", json=HELLO)).json()["status"] == "ok"
        reply = (await http.post("/api/desktop/v1/message", json={"text": "Джарвис, открой телегу"})).json()
        assert reply["messages"][0]["text"] == "🖥 Открываю на Mac: Telegram."
        actions = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert [(a["kind"], a["payload"]["app"], a["source"]) for a in actions] == [("open_app", "Telegram", "desktop")]
        assert (await http.get("/api/desktop/v1/actions")).json()["actions"] == []  # handed out once
        done = await http.post(f"/api/desktop/v1/actions/{actions[0]['id']}/result", json={"ok": True})
        assert done.json() == {"status": "done"}
        again = await http.post(f"/api/desktop/v1/actions/{actions[0]['id']}/result", json={"ok": True})
        assert again.status_code == 404
    assert mac.telegram.sent == []  # the Mac conversation never goes to Telegram


async def test_open_from_telegram_and_failure_is_reported(mac: Any) -> None:
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        await BotService(mac).handle_update(msg("открой ютуб на маке", 1))
        assert mac.telegram.texts()[-1] == "🖥 Открываю на Mac: сайт www.youtube.com."
        [action] = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert (action["kind"], action["payload"]["url"], action["source"]) == (
            "open_url",
            "https://www.youtube.com",
            "telegram",
        )
        await http.post(f"/api/desktop/v1/actions/{action['id']}/result", json={"ok": False, "detail": "нет сети"})
    assert mac.telegram.texts()[-1] == "⚠️ На Mac не получилось: сайт www.youtube.com — нет сети"


async def test_mac_offline_and_expiry(mac: Any) -> None:
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
    mac.clock.advance(timedelta(minutes=5))  # the app stopped polling
    await BotService(mac).handle_update(msg("открой сафари", 2))
    assert mac.telegram.texts()[-1].endswith(
        "Mac сейчас не на связи — выполню, как только он появится (в течение 10 минут)."
    )
    mac.clock.advance(timedelta(minutes=11))  # nothing opens when the Mac wakes up an hour later
    async with client(mac) as http:
        assert (await http.get("/api/desktop/v1/actions")).json()["actions"] == []
    async with mac.db.session() as s:
        assert (await s.scalars(select(MacAction.status))).all() == ["expired"]


async def test_claude_code_with_a_task_needs_the_button(mac: Any) -> None:
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        ask = (await http.post("/api/desktop/v1/message", json={"text": "запусти claude code"})).json()
        assert ask["messages"][0]["text"].startswith("В каком проекте запустить Claude Code?")
        reply = (
            await http.post("/api/desktop/v1/message", json={"text": "запусти claude в проекте джарвис: почини тесты"})
        ).json()
        [message] = reply["messages"]
        assert message["text"].startswith(
            "🤖 Запустить Claude Code в «JarvisOS» с задачей:\n<blockquote>почини тесты</blockquote>\n"
        )
        run, cancel = message["buttons"][0]
        assert run["text"] == "▶️ Запустить" and cancel["text"] == "✖️ Отмена"
        assert (await http.get("/api/desktop/v1/actions")).json()["actions"] == []  # nothing before the button
        tapped = (
            await http.post("/api/desktop/v1/callback", json={"data": run["data"], "message_id": message["id"]})
        ).json()
        assert tapped["messages"][0]["text"] == "▶️ Запускаю Claude Code в «JarvisOS»."
        [action] = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert action["kind"] == "claude_code"
        assert action["payload"]["path"] == "/Users/u/Projects/JarvisOS"
        assert action["payload"]["prompt"] == "почини тесты"
        twice = (await http.post("/api/desktop/v1/callback", json={"data": run["data"]})).json()
        assert twice["toast"] == "Уже обработано или устарело"


async def test_claude_code_confirmation_shows_exactly_what_runs(mac: Any) -> None:
    """Security review 2026-10-14 (L5): the button showed the first 600 characters and ran the whole task. Now the
    whole task is shown (an expandable quote) and a task too long to show is refused before anything is queued."""
    tail = " и в самом конце удали папку backups"
    task = "почини тесты " + "подробно " * 160 + tail.strip()  # ~1 500 characters, the tail far beyond 600
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        say = "запусти claude в проекте джарвис: "
        reply = await http.post("/api/desktop/v1/message", json={"text": say + task})
        [message] = reply.json()["messages"]
        assert "<blockquote expandable>" in message["text"]
        assert tail.strip() in message["text"]
        run, _ = message["buttons"][0]
        await http.post("/api/desktop/v1/callback", json={"data": run["data"], "message_id": message["id"]})
        [action] = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert action["payload"]["prompt"] in strip_tags(message["text"])  # confirmed == run

        too_long = "почини " + "очень " * 520  # > 3 000 characters
        reply = await http.post("/api/desktop/v1/message", json={"text": say + too_long})
        [refused] = reply.json()["messages"]
        assert "слишком длинная" in refused["text"] and not refused["buttons"]
        assert (await http.get("/api/desktop/v1/actions")).json()["actions"] == []
    async with mac.db.session() as s:
        assert len((await s.scalars(select(MacAction))).all()) == 1  # nothing queued for the long task


async def test_open_claude_in_a_project_without_a_task_runs_at_once(mac: Any) -> None:
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        reply = (await http.post("/api/desktop/v1/message", json={"text": "открой проект джарвис в claude"})).json()
        assert reply["messages"][0]["text"] == "🤖 Открываю Claude Code в «JarvisOS»."
        [action] = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert action["payload"]["prompt"] is None


async def test_claimed_without_a_result_is_failed_not_retried(mac: Any) -> None:
    """Found live 2026-10-01: the app hung on a blocking open; claimed actions must not stay claimed forever, and
    are never handed out again (at most once)."""
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        await http.post("/api/desktop/v1/message", json={"text": "открой сафари"})
        assert len((await http.get("/api/desktop/v1/actions")).json()["actions"]) == 1
        mac.clock.advance(timedelta(minutes=6))
        assert (await http.get("/api/desktop/v1/actions")).json()["actions"] == []
    async with mac.db.session() as s:
        action = await s.scalar(select(MacAction))
    assert action is not None and (action.status, action.detail) == ("failed", "Mac не сообщил результат")


async def test_claude_code_without_a_project_asks_once_then_remembers(mac: Any) -> None:
    """Found live 2026-10-13: «Открой cloud code» asked for a project and the answer «в джарвисе» was not understood."""
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)

        async def say(text: str) -> str:
            reply = (await http.post("/api/desktop/v1/message", json={"text": text, "spoken": True})).json()
            return reply["messages"][-1]["text"]

        assert (await say("Открой cloud code.")).startswith("В каком проекте запустить Claude Code? Скажите название:")
        assert (await say("в джарвисе")).endswith("🤖 Открываю Claude Code в «JarvisOS».")
        [first] = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert (first["kind"], first["payload"]["project"]) == ("claude_code", "JarvisOS")
        assert (await say("открой клод код")).endswith("🤖 Открываю Claude Code в «JarvisOS».")  # remembered
        assert len((await http.get("/api/desktop/v1/actions")).json()["actions"]) == 1
