"""R27: a slow LLM router must not hold a chat reply (it once took 53 s) — after the limit Jarvis answers without it."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jarvis.bot.intents import Intent, route_message
from jarvis.bot.service import BotService
from jarvis.integrations.llm.client import FakeLLM, RawOutput

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


def slow(llm: FakeLLM, seconds: float = 5.0) -> FakeLLM:
    async def hang(*_args: Any) -> RawOutput:
        llm.calls.append(("intent_router", "slow"))
        await asyncio.sleep(seconds)
        return RawOutput('{"actions": [{"intent": "unknown"}]}', "end_turn")

    llm._raw_call = hang  # type: ignore[method-assign]
    return llm


async def test_router_gives_up_after_the_limit() -> None:
    llm = slow(FakeLLM())
    started = time.monotonic()
    actions = await route_message("какой-то непонятный текст про жизнь", llm, timeout=0.05)
    assert time.monotonic() - started < 1.0
    assert [(a.intent, a.source) for a in actions] == [(Intent.UNKNOWN, "llm_timeout")]
    assert llm.calls  # it was asked, not skipped


async def test_keyword_messages_never_wait_for_the_llm() -> None:
    llm = slow(FakeLLM())
    actions = await route_message("что у меня сегодня", llm, timeout=0.05)
    assert actions[0].intent == Intent.SHOW_TODAY
    assert llm.calls == []


async def test_known_parts_survive_a_router_timeout() -> None:
    llm = slow(FakeLLM())
    actions = await route_message("какая погода. И ещё что-то непонятное про жизнь", llm, timeout=0.05)
    assert [a.intent for a in actions] == [Intent.WEATHER, Intent.UNKNOWN]
    assert actions[1].source == "llm_timeout"


async def test_no_limit_waits_for_the_answer() -> None:
    llm = slow(FakeLLM(), seconds=0.1)
    actions = await route_message("какой-то непонятный текст про жизнь", llm, timeout=0)
    assert actions[0].source == "llm"


@pytest.mark.db
async def test_bot_offers_inbox_buttons_after_a_timeout(container: Any) -> None:
    c = container
    c.settings.llm_router_timeout_seconds = 0.05
    slow(c.llm)
    started = time.monotonic()
    await BotService(c).handle_update(msg("велосипед на балкон", 1))
    assert time.monotonic() - started < 2.0
    assert c.telegram.texts()[-1].startswith("📥 Куда записать «велосипед на балкон»?")


@pytest.mark.db
async def test_bot_says_honestly_that_the_llm_was_too_slow_for_a_question(container: Any) -> None:
    c = container
    c.settings.llm_router_timeout_seconds = 0.05
    slow(c.llm)
    await BotService(c).handle_update(msg("почему небо голубое?", 2))
    assert "ИИ сейчас отвечает слишком долго" in c.telegram.texts()[-1]
