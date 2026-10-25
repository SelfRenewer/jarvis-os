"""What Jarvis knows about the user: «запомни, что мой барбер — Гоги» → a fact (router and agent context),
«что ты обо мне знаешь?», «забудь про барбера» / «↩️ Забыть». Other «запомни …» stay notes."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.agent.service import QuestionAgent
from jarvis.bot.service import BotService

pytestmark = pytest.mark.db
USER = 1001


def msg(text: str, update_id: int) -> dict[str, Any]:
    chat = {"id": USER, "type": "private"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "from": {"id": USER}, "chat": chat},
    }


def tap(data: str, update_id: int) -> dict[str, Any]:
    message = {"message_id": 1, "chat": {"id": USER, "type": "private"}}
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb{update_id}", "data": data, "from": {"id": USER}, "message": message},
    }


def buttons(sent: dict[str, Any]) -> dict[str, str]:
    markup = sent.get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


async def test_remember_show_and_forget(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Запомни, что мой барбер — Гоги", 1))
    assert c.telegram.texts()[-1].startswith("🧠 Запомнил: мой барбер — Гоги")
    undo = buttons(c.telegram.sent[-1])["↩️ Забыть"]
    await bot.handle_update(msg("запомни что мой барбер — Гоги", 2))
    assert c.telegram.texts()[-1].startswith("🧠 Уже помню")
    await bot.handle_update(msg("запомни: у меня аллергия на орехи", 3))
    await bot.handle_update(msg("что ты обо мне знаешь?", 4))
    shown = c.telegram.texts()[-1]
    assert "Что я о вас знаю · 2" in shown and "мой барбер — Гоги" in shown and "аллергия на орехи" in shown
    await bot.handle_update(msg("забудь про аллергию", 5))
    assert c.telegram.texts()[-1] == "🧠 Забыл: у меня аллергия на орехи"
    await bot.handle_update(tap(undo, 6))
    assert c.telegram.texts()[-1] == "🧠 Забыл: мой барбер — Гоги"
    await bot.handle_update(msg("что ты обо мне знаешь", 7))
    assert c.telegram.texts()[-1].startswith("🧠 Пока я ничего о вас не запоминал.")


async def test_other_remember_becomes_a_note_only_after_the_button(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("запомни код домофона 1234", 1))
    assert c.telegram.texts()[-1].startswith("📥 Куда записать «код домофона 1234»?")
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])["📝 Заметка"], 2))
    assert c.telegram.texts()[-1].startswith("📝 Заметка сохранена.")


async def test_facts_reach_the_router_and_the_agent(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("запомни, что мама живёт в Тбилиси", 1))
    context = await bot._llm_context()
    assert context is not None and "- мама живёт в Тбилиси" in context
    c.llm.chat_responder = lambda messages, tools: {"content": "В Тбилиси, сэр."}
    await QuestionAgent(c).answer("где живёт мама?")
    assert "мама живёт в Тбилиси" in c.llm.chats[-1][0]["content"]
