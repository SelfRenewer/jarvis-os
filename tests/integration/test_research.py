"""Researcher: «изучи …» shows the price first; the paid web search runs once, only after «🔎 Искать»; the answer is
plain text with sources and the real price; a daily cap; failures are reported, never retried. Images of the
OpenRouter request are checked with respx (no network)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from jarvis.bot.service import BotService
from jarvis.core.errors import TransientError
from jarvis.integrations.llm.client import OpenRouterLLM, WebAnswer
from jarvis.research.service import plain_text

USER = 1001
OR = "https://openrouter.ai/api/v1/"


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


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


def buttons(c: Any) -> dict[str, str]:
    markup = c.telegram.sent[-1].get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def test_markdown_becomes_plain_text() -> None:
    raw = "## ВНЖ\n**Главное**: нужен [паспорт](https://x.ge) [1].\n- пункт один\n* пункт два [2, 3]\n\n\n\nКонец"
    assert plain_text(raw) == "ВНЖ\nГлавное: нужен паспорт.\n• пункт один\n• пункт два\n\nКонец"


@respx.mock
async def test_web_answer_request_sources_and_price() -> None:
    route = respx.post(OR + "chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Ответ про ВНЖ.",
                            "annotations": [
                                {"type": "url_citation", "url_citation": {"url": "https://a.ge/x", "title": "A"}},
                                {"type": "url_citation", "url_citation": {"url": "https://a.ge/x", "title": "A"}},
                                {"type": "url_citation", "url_citation": {"url": "javascript:alert(1)", "title": "B"}},
                                {"type": "url_citation", "url_citation": {"url": "https://b.ge/y", "title": ""}},
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 600, "cost": 0.0141},
            },
        )
    )
    llm = OpenRouterLLM("k", "google/gemini-3.8-flash")
    answer = await llm.web_answer(system="sys", user="Вопрос", max_results=5)
    body = json.loads(route.calls[0].request.content)
    assert body["plugins"] == [{"id": "web", "engine": "exa", "max_results": 5}]
    assert "response_format" not in body
    assert answer.sources == [("A", "https://a.ge/x"), ("", "https://b.ge/y")]  # deduped, only http(s)
    assert answer.cost_usd == Decimal("0.0141")
    await llm.aclose()


@respx.mock
async def test_web_answer_is_not_retried_on_a_server_error() -> None:
    route = respx.post(OR + "chat/completions").mock(return_value=httpx.Response(502))
    llm = OpenRouterLLM("k", "m/1")
    with pytest.raises(TransientError):
        await llm.web_answer(system="s", user="u")
    assert route.call_count == 1  # a paid search is never sent twice by the client
    await llm.aclose()


@pytest.mark.db
async def test_research_runs_once_after_the_price_button(container: Any, worker: Any) -> None:
    c = container
    c.research.web.answer = WebAnswer(
        "**Кратко**: ВНЖ дают по работе или недвижимости.\n- Срок — 1 год",
        [("Public Service Hall", "https://psh.gov.ge/vnzh"), ("", "https://example.ge/b")],
        Decimal("0.0141"),
        "fake",
    )
    bot = BotService(c)
    await bot.handle_update(msg("изучи ВНЖ Грузии", 1))
    assert c.telegram.texts()[-1].startswith("🔎 Исследовать в интернете: «ВНЖ Грузии»?\nЦена ≈ $0.01–0.03 за запрос")
    assert c.research.web.calls == []
    search = buttons(c)["🔎 Искать"]
    await bot.handle_update(tap(search, 2))
    assert c.telegram.texts()[-1] == "🔎 Ищу в интернете… (обычно 10–40 с)"
    await worker.drain()
    assert c.telegram.texts()[-1].splitlines() == [
        "🔎 ВНЖ Грузии",
        "",
        "Кратко: ВНЖ дают по работе или недвижимости.",
        "• Срок — 1 год",
        "",
        "📚 Источники",
        "1. Public Service Hall · psh.gov.ge",
        "2. example.ge",
        "",
        "Стоимость запроса: $0.014",
    ]
    assert len(c.research.web.calls) == 1 and "Вопрос: ВНЖ Грузии" in c.research.web.calls[0]
    await bot.handle_update(tap(search, 3))  # a second tap neither searches nor pays again
    await worker.drain()
    assert len(c.research.web.calls) == 1


@pytest.mark.db
async def test_research_cancel_cap_and_failure(container: Any, worker: Any) -> None:
    c = container
    c.settings.research_max_per_day = 1
    bot = BotService(c)
    await bot.handle_update(msg("найди в интернете цены на права", 10))
    await bot.handle_update(tap(buttons(c)["✖️ Отмена"], 11))
    assert c.telegram.texts()[-1] == "Ок, не ищу."
    c.research.web.error = TransientError("openrouter: timeout")
    await bot.handle_update(msg("что пишут про новый налог", 12))
    await bot.handle_update(tap(buttons(c)["🔎 Искать"], 13))
    await worker.drain()
    assert c.telegram.texts()[-1] == "🔎 Не получилось найти: openrouter: timeout. Повторите запрос чуть позже."
    assert len(c.research.web.calls) == 1  # not retried by the queue
    c.research.web.error = None
    await bot.handle_update(msg("изучи права в Грузии", 14))
    await bot.handle_update(tap(buttons(c)["🔎 Искать"], 15))
    await worker.drain()
    assert c.telegram.texts()[-1].startswith("🔎 Лимит исследований на сегодня (1) исчерпан")
    assert len(c.research.web.calls) == 1


@pytest.mark.db
async def test_research_needs_openrouter(container: Any) -> None:
    c = container
    c.research.web = None
    await BotService(c).handle_update(msg("изучи ВНЖ Грузии", 20))
    assert c.telegram.texts()[-1].startswith("🔎 Поиск в интернете не подключён")
