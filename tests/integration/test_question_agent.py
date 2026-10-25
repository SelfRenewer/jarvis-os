"""The question agent (intent `ask`): general questions answered by the model, questions about the user's life
answered from read-only looks into Jarvis's own commands; nothing is changed, nothing extra is sent.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jarvis.agent.service import QuestionAgent, look_intent
from jarvis.bot.intents import Intent, RoutedIntent
from jarvis.bot.service import BotService

pytestmark = pytest.mark.db
USER = 1001


def msg(text: str, update_id: int) -> dict[str, Any]:
    chat = {"id": USER, "type": "private"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "from": {"id": USER}, "chat": chat},
    }


def router_says(c: Any, **fields: Any) -> None:
    item = RoutedIntent(**fields).model_dump(mode="json")
    c.llm.responders["intent_router"] = lambda _user, _messages: {"actions": [item]}


def tool_call(what: str, about: str = "", call_id: str = "c1") -> dict[str, Any]:
    args = json.dumps({"what": what, "about": about}, ensure_ascii=False)
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "look", "arguments": args}}],
    }


async def test_a_general_question_is_answered_directly(container: Any) -> None:
    c = container
    router_says(c, intent=Intent.ASK, query="почему небо голубое")
    c.llm.chat_responder = lambda messages, tools: {
        "content": "Из-за рассеяния Рэлея, сэр: синий свет рассеивается сильнее."
    }
    await BotService(c).handle_update(msg("почему небо голубое", 1))
    assert c.telegram.texts()[-1] == "Из-за рассеяния Рэлея, сэр: синий свет рассеивается сильнее."
    [chat] = c.llm.chats
    assert chat[0]["role"] == "system" and chat[-1] == {"role": "user", "content": "почему небо голубое"}


async def test_a_question_about_the_day_looks_into_the_agenda_and_sends_nothing_else(container: Any) -> None:
    c = container
    c.todoist.add(id="T1", content="Постричься", due={"date": "2026-09-24", "datetime": "2026-09-24T14:00:00Z"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    seen: list[str] = []

    def responder(messages: list[dict[str, Any]], tools: Any) -> dict[str, Any]:
        if messages[-1]["role"] == "user":
            return tool_call("agenda", "завтра")
        seen.append(messages[-1]["content"])  # the look's text
        return {"content": "Завтра после пяти вы свободны, сэр: стрижка в 18:00 — единственное дело."}

    c.llm.chat_responder = responder
    router_says(c, intent=Intent.ASK, query="свободен ли я завтра после пяти")
    await BotService(c).handle_update(msg("свободен ли я завтра после пяти", 2))
    assert "Постричься" in seen[0] and "Завтра" in seen[0]  # the agenda as Jarvis would show it
    assert c.telegram.texts() == ["Завтра после пяти вы свободны, сэр: стрижка в 18:00 — единственное дело."]


async def test_the_dialogue_is_remembered_for_follow_ups(container: Any) -> None:
    c = container
    answers = iter(["Завтра свободно, сэр.", "Послезавтра тоже, сэр."])
    c.llm.chat_responder = lambda messages, tools: {"content": next(answers)}
    agent = QuestionAgent(c)
    await agent.answer("что у меня завтра")
    await agent.answer("а послезавтра?")
    second = c.llm.chats[-1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == "Завтра свободно, сэр."


async def test_agent_failure_falls_back_to_the_routers_reply(container: Any) -> None:
    c = container
    router_says(c, intent=Intent.ASK, query="как лучше учить английский", reply="Понемногу, но каждый день, сэр.")
    c.llm.chat_responder = None  # the agent is unavailable
    await BotService(c).handle_update(msg("как лучше учить английский", 3))
    assert c.telegram.texts()[-1] == "Понемногу, но каждый день, сэр."


def test_every_look_is_a_read_only_command() -> None:
    read_only = {
        Intent.SHOW_TODAY, Intent.SHOW_AGENDA, Intent.SHOW_TASKS, Intent.SEARCH_ALL, Intent.WEATHER,
        Intent.HEALTH_SHOW, Intent.TRACKER_SHOW, Intent.TPL_SHOW, Intent.FOOD_SHOW, Intent.EXPENSE_SHOW,
        Intent.LABS_SHOW, Intent.MED_LIST, Intent.SHOPPING_SHOW, Intent.SHOW_NOTES, Intent.SEARCH_IDEAS,
        Intent.STATUS, Intent.SHOW_USAGE,
    }  # fmt: skip
    from jarvis.agent.service import LOOK

    for what in LOOK:
        assert look_intent(what, "завтра")[0].intent in read_only
    with pytest.raises(ValueError, match="unknown look"):
        look_intent("delete_task", "всё")
