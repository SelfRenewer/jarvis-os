"""Calculator and currency answers in the chat: instant, computed by code, the LLM is never asked (R26)."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.bot.intents import Intent, route_message
from jarvis.bot.service import BotService
from jarvis.integrations.llm.client import FakeLLM

USER = 1001


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


async def test_route_never_asks_the_llm() -> None:
    llm = FakeLLM()  # no responders: any call would fail the routing
    for text in ("10 000 000 ÷ 1500", "Сколько 10 000 руб. в долларах?", "курс лари"):
        routed = await route_message(text, llm)
        assert [r.intent for r in routed] in ([Intent.CALCULATE], [Intent.CONVERT_CURRENCY])
        assert routed[0].source == "keyword"
    assert llm.calls == []


@pytest.mark.db
async def test_calculator_and_currency_replies(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("10 000 000 ÷ 1500", 1))
    assert c.telegram.texts()[-1] == "🧮 10 000 000 ÷ 1500 ≈ 6 666,67"
    await bot.handle_update(msg("Сколько 10 000 руб. в долларах?", 2))
    reply = c.telegram.texts()[-1]
    assert reply.startswith("💱 10\u00a0000 RUB ≈ 118,99 USD\nКурс: 1 USD = 84,04 RUB\nКурсы: open.er-api.com")
    await bot.handle_update(msg("1/0", 3))
    assert c.telegram.texts()[-1] == "🧮 Не посчитать: деление на ноль."
    assert c.llm.calls == []
