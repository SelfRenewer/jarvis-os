"""Jarvis Lab from Jarvis: «покажи / разбери макбук», «покажи плату макбука» open the lab on the Mac; «Что это за
деталь?» — one camera frame → a vision reading (markings as printed), nothing stored."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from jarvis.api.app import create_app
from jarvis.bot.intents import Intent, keyword_route
from jarvis.core.security import TokenCipher, desktop_token
from jarvis.photos.parts import PartReading
from jarvis.services.container import build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.db
HELLO = {"apps": ["Safari"], "projects": [], "version": "1.0"}


@pytest.fixture
def mac(db: Any, clock: Any) -> Any:
    c = build_container(make_settings(jarvis_encryption_key=TokenCipher.generate_key()), db=db, clock=clock)
    c.desktop_token = desktop_token(c.settings.jarvis_encryption_key)
    return c


def client(c: Any, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token or c.desktop_token}"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url="http://t", headers=headers)


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("покажи макбук", ""),
        ("Разбери макбук", "explode"),
        ("собери макбук про", "assemble"),
        ("покажи аккумулятор макбука", "part=battery"),
        ("покажи материнку макбука", "part=board"),
        ("открой лабораторию", ""),
    ],
)
def test_lab_phrases(text: str, command: str) -> None:
    routed = keyword_route(text)
    assert (routed.intent, routed.query or "") == (Intent.LAB_OPEN, command)


@pytest.mark.parametrize(
    ("text", "page"),
    [
        ("открой интерфейс", "/"),
        ("Джарвис, открой джарвис ос", "/"),
        ("покажи граф", "/"),
        ("открой сайты", "/sites"),
        ("открой TLT", "/tlt"),
        ("запусти тлт", "/tlt"),
        ("открой сайт TLT", "/tlt"),
        ("открой CRM", "/crm"),  # 24.10: the CRM on its own server
        ("Джарвис, открой срм", "/crm"),
        ("открой сайт CRM", "/crm"),
        ("покажи мою воронку продаж", "/crm"),
    ],
)
def test_jarvis_os_phrases(text: str, page: str) -> None:
    routed = keyword_route(text)
    assert (routed.intent, routed.query) == (Intent.OS_OPEN, page)


def test_only_jarvis_os_pages_can_be_opened() -> None:
    from jarvis.desktop.service import JARVIS_PAGES

    assert all(JARVIS_PAGES.match(p) for p in ("/", "/lab", "/lab#part=battery", "/sites", "/tlt"))
    assert not any(JARVIS_PAGES.match(p) for p in ("/api/system/ask", "//evil.com", "/lab#<x>", "http://x", "/tlt/../"))


async def test_the_lab_opens_on_the_mac(mac: Any) -> None:
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        reply = (await http.post("/api/desktop/v1/message", json={"text": "Джарвис, разбери макбук"})).json()
        assert reply["messages"][0]["text"].startswith("🧪 Открываю лабораторию на Mac: разбираю MacBook.")
        await http.post("/api/desktop/v1/message", json={"text": "открой интерфейс"})
        await http.post("/api/desktop/v1/message", json={"text": "открой сайты"})
        actions = (await http.get("/api/desktop/v1/actions")).json()["actions"]
        assert [(a["kind"], a["payload"]["path"]) for a in actions] == [
            ("open_jarvis", "/lab#explode"),  # the app signs in with its own key: no link travels through here
            ("open_jarvis", "/"),
            ("open_jarvis", "/sites"),
        ]


async def test_the_crm_opens_from_its_server(mac: Any) -> None:
    from jarvis.bot.service import BotService

    def msg(text: str, update_id: int) -> dict[str, Any]:
        user = mac.settings.telegram_allowed_user_ids[0]
        chat = {"id": user, "type": "private"}
        message = {"message_id": update_id, "text": text, "from": {"id": user}, "chat": chat}
        return {"update_id": update_id, "message": message}

    bot = BotService(mac)
    await bot.handle_update(msg("открой CRM", 1))  # no Mac connected yet: the link only
    url = "https://crm.example.com/"
    first = mac.telegram.sent[-1]
    assert mac.telegram.texts()[-1] == "💼 CRM — по кнопке."
    assert first["reply_markup"] == {"inline_keyboard": [[{"text": "💼 Открыть CRM", "url": url}]]}
    async with client(mac) as http:
        await http.post("/api/desktop/v1/hello", json=HELLO)
        await bot.handle_update(msg("открой срм", 2))
        assert mac.telegram.texts()[-1] == "💼 Открываю CRM на Mac."
        actions = (await http.get("/api/desktop/v1/actions")).json()["actions"]
    assert [(a["kind"], a["payload"]["url"]) for a in actions] == [("open_url", url)]


async def test_what_is_this_part(mac: Any) -> None:
    def responder(_user: str, messages: list[dict[str, Any]]) -> PartReading:
        return PartReading(
            name="Микросхема памяти LPDDR5", kind="chip", markings=["SEC 318", "K3LKBKB0BM"],
            purpose="Оперативная память.", device=None, confidence="high", caution=None,
        )  # fmt: skip

    mac.llm.responders["part_reader"] = responder
    frame = base64.b64encode(b"\xff\xd8" + b"0" * 2000).decode()
    async with client(mac) as http:
        answer = await http.post("/api/desktop/v1/system/identify", json={"image": frame, "mime": "image/jpeg"})
        assert answer.status_code == 200
        assert answer.json()["markings"] == ["SEC 318", "K3LKBKB0BM"]
        assert mac.llm.images[-1][0] == "part_reader"  # the frame went to the vision call
        assert (
            await http.post("/api/desktop/v1/system/identify", json={"image": "!!", "mime": "image/gif"})
        ).status_code == 422
        big = base64.b64encode(b"0" * (2 * 1024 * 1024 + 10)).decode()
        assert (
            await http.post("/api/desktop/v1/system/identify", json={"image": big, "mime": "image/jpeg"})
        ).status_code in (413, 422)
    async with client(mac, token="wrong") as http:
        assert (
            await http.post("/api/desktop/v1/system/identify", json={"image": frame, "mime": "image/jpeg"})
        ).status_code == 401
