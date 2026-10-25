"""Voice on the Mac is its own setting (2026-10-14): every reply is spoken there — typed or said, lists and
results of buttons too — while Telegram keeps /voice and its conversation-only policy. Links, keys and paths are
never read aloud."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from jarvis.api.app import create_app
from jarvis.bot.service import BotService
from jarvis.core.security import TokenCipher, desktop_token
from jarvis.services.container import build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.db
USER = 1001


@pytest.fixture
def mac(db: Any, clock: Any) -> Any:
    c = build_container(
        make_settings(jarvis_encryption_key=TokenCipher.generate_key(), voice_replies_enabled=True), db=db, clock=clock
    )
    c.desktop_token = desktop_token(c.settings.jarvis_encryption_key)
    return c


def client(c: Any) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {c.desktop_token}"}
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


async def test_the_mac_speaks_every_reply_even_when_telegram_voice_is_off(mac: Any) -> None:
    c = mac
    await BotService(c).handle_update(msg("/voice off", 1))  # Telegram: text only
    async with client(c) as http:
        hello = (await http.post("/api/desktop/v1/hello", json={"version": "1.0"})).json()
        assert hello["voice_mac"] is True
        reply = (await http.post("/api/desktop/v1/message", json={"text": "мои задачи"})).json()  # typed, a list
    assert reply["voice_mac"] is True
    first = reply["messages"][0]
    assert first["audio"] is not None and base64.b64decode(first["audio"]).startswith(b"ID3fake-Charon")
    assert c.tts.calls  # one synthesis for the whole answer
    await BotService(c).handle_update(msg("Как дела?", 2))
    assert "voice" not in c.telegram.sent[-1]  # Telegram is still text only


async def test_voice_switches_are_separate(mac: Any) -> None:
    c = mac
    async with client(c) as http:
        reply = (await http.post("/api/desktop/v1/message", json={"text": "отвечай текстом", "spoken": True})).json()
        assert reply["messages"][0]["text"] == "🔇 На Mac отвечаю текстом. В Telegram — без изменений."
        assert reply["voice_mac"] is False and reply["messages"][0]["audio"] is None
        quiet = (await http.post("/api/desktop/v1/message", json={"text": "как дела?", "spoken": True})).json()
        assert all(m["audio"] is None for m in quiet["messages"])
        assert (await http.post("/api/desktop/v1/voice", json={"enabled": True})).json() == {"voice_mac": True}
    bot = BotService(c)
    assert await bot.mac_voice_enabled() is True
    assert (await bot.voice_settings())[0] is True  # Telegram untouched by the Mac switches
    await bot.handle_update(msg("Как дела?", 3))
    assert c.telegram.sent[-1]["voice"].startswith(b"ID3fake")


async def test_links_and_keys_are_not_spoken_on_the_mac(mac: Any) -> None:
    c = mac
    async with client(c) as http:
        await http.post("/api/desktop/v1/hello", json={"version": "1.0"})
        await http.post(
            "/api/desktop/v1/message",
            json={"text": "запиши заметку сайт https://example.ge/a ключ sk-or-v1-abcdef1234567890abcdef"},
        )
        reply = (await http.post("/api/desktop/v1/message", json={"text": "мои заметки"})).json()
    spoken, _voice = c.tts.calls[-1]
    assert "example.ge" not in spoken and "https" not in spoken and "sk-or" not in spoken
    assert "сайт https://example.ge/a" in reply["messages"][0]["text"]  # the text keeps everything
