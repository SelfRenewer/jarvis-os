"""📓 Дневник (2026-10-14): the hub keyboard, «⋯ Ещё», today's TLT card, filling the day step by step, the address
«Джарвис, …» never stored, several requests answered as one HTML message. Fakes only; clock: Wed 23.09 09:00."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from jarvis.bot.menu import KEYBOARD_VERSION, MAIN_MENU, MORE_BUTTON, main_keyboard, menu_intent
from jarvis.bot.service import BotService
from jarvis.db.models import Idea, Note
from jarvis.integrations.telegram.client import html_to_plain

USER = 1001
pytestmark = pytest.mark.db


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


def buttons(sent: dict[str, Any]) -> dict[str, str]:
    markup = sent.get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def last(c: Any) -> str:
    return str(c.telegram.texts()[-1])


def history(c: Any) -> None:
    """Earlier TLT days: Ice Shower (11) twice, Clean Desk (12) once — the card offers the frequent ones."""
    c.lmf_api.days_store["2026-09-20"] = {"date": "2026-09-20", "tags": [11, 12], "supplements": []}
    c.lmf_api.days_store["2026-09-21"] = {"date": "2026-09-21", "tags": [11], "supplements": []}


# --- keyboard and «⋯ Ещё» ------------------------------------------------------------------------------------------


def test_keyboard_is_a_few_hubs_and_old_buttons_still_work() -> None:
    keyboard = main_keyboard()["keyboard"]
    labels = [b["text"] for row in keyboard for b in row]
    assert len(keyboard) <= 2 and 4 <= len(labels) <= 6  # was 12 buttons in 4 rows («a cockpit»)
    assert labels == ["📅 Сегодня", "📋 Задачи", "📓 Дневник", "🛒 Покупки", MORE_BUTTON]
    assert len(MAIN_MENU[-1]) < 3  # room for one more hub (a location button)
    assert KEYBOARD_VERSION == "3"
    for old in (
        "🗓 Завтра",
        "📆 Неделя",
        "🔥 Главное",
        "🧭 План дня",
        "💡 Идеи",
        "📝 Заметки",
        "💊 Лекарства",
        "📊 Статистика",
    ):
        assert menu_intent(old) is not None  # the v2 keyboard may still be on the user's screen
    assert menu_intent("📓 Дневник") is not None and menu_intent("дневник") is None  # exact match only


async def test_more_menu_opens_the_rest(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg(MORE_BUTTON, 1))
    more = c.telegram.sent[-1]
    assert last(c).startswith("⋯ Ещё")
    options = buttons(more)
    assert list(options) == [
        "🗓 Завтра",
        "📆 Неделя",
        "🔥 Главное",
        "🧭 План дня",
        "💡 Идеи",
        "📝 Заметки",
        "💊 Лекарства",
        "💸 Расходы",
        "📊 Статистика",
        "⚙️ Настройки",
    ]
    assert all(len(row) <= 2 for row in more["reply_markup"]["inline_keyboard"])
    await bot.handle_update(tap(options["🗓 Завтра"], 2))
    assert last(c).startswith("📅 Завтра, чт 24.09")
    assert c.telegram.answered[-1] == ("cb2", "🗓 Завтра")
    await bot.handle_update(tap(options["⚙️ Настройки"], 3))
    assert last(c).startswith("⚙️ Настройки")
    await bot.handle_update(tap("mm:nope", 4))
    assert c.telegram.answered[-1] == ("cb4", "Неизвестное действие")
    await bot.handle_update(msg("меню", 5))
    assert last(c).startswith("⋯ Ещё")


# --- the card --------------------------------------------------------------------------------------------------------


async def test_diary_card_shows_what_is_filled_missing_pills_and_examples(container: Any) -> None:
    c = container
    history(c)
    c.lmf_api.days_store["2026-09-23"] = {
        "date": "2026-09-23", "tags": [12], "supplements": [], "sleep_minutes": 450, "sleep_quality": 85,
    }  # fmt: skip
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, цинк 25 в 9:00", 1))
    await bot.handle_update(msg("📓 Дневник", 2))
    card = c.telegram.sent[-1]
    text = html_to_plain(card["text"])
    assert text.startswith("📓 Дневник · сегодня, ср 23.09\n\n✅ Записано\n🛌 Сон: 7:30 · качество 85\n🏷 CD")
    assert "Осталось: оценка, настроение" in text
    assert "💊 Таблетки\n🌅 Утренние 09:00 —" in text
    assert "«✅ Выпил всё» в напоминании записывает их в TLT само" in text
    assert "💬 Скажите, например\n«оценка 7, настроение хорошее»" in text
    assert "<b>Дневник</b>" in card["text"] and "<i>" in card["text"]
    options = buttons(card)
    assert options["✍️ Заполнить по шагам"] == "tl:f"
    assert options["＋ Холодный душ"] == "tl:t:11"  # the most used tag today does not have yet
    assert "＋ Чистый стол" not in options  # already there
    assert len(options) <= 4
    # one tap → the usual draft; nothing is written before «✅ Записать»
    await bot.handle_update(tap(options["＋ Холодный душ"], 3))
    assert last(c).splitlines() == ["📓 Записать в TLT · сегодня, ср 23.09?", "• Отметки: + IS (холодный душ)"]
    assert c.lmf_api.writes == []
    assert "✅ Записать" in buttons(c.telegram.sent[-1])


async def test_tlt_command_and_words_open_the_card(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("/tlt", 1))
    assert last(c).startswith("📓 Дневник · сегодня, ср 23.09\n\nПока пусто — заполним за минуту?")
    await bot.handle_update(msg("что в дневнике?", 2))
    assert last(c).startswith("📓 Дневник")
    await bot.handle_update(msg("/tlt вчера", 3))  # a date: the summary of that day as before
    assert last(c).startswith("📓 TLT · вт 22.09")


async def test_card_when_lmf_is_down(container: Any) -> None:
    c = container
    c.lmf_api.unavailable = True
    await BotService(c).handle_update(msg("📓 Дневник", 1))
    assert "LMF не отвечает" in last(c)


# --- step by step ----------------------------------------------------------------------------------------------------


async def test_fill_the_day_step_by_step(container: Any, worker: Any) -> None:
    c = container
    history(c)
    bot = BotService(c)
    await bot.handle_update(msg("Джарвис, заполни день", 1))  # the address is not a note (the bug of 2026-10-14)
    assert last(c).startswith("🛌 Сколько спали? · шаг 1 из 4")
    step = buttons(c.telegram.sent[-1])
    assert list(step) == ["6 ч", "7 ч", "7:30", "8 ч", "⏭ Пропустить", "✅ Готово"]
    await bot.handle_update(tap(step["7 ч"], 2))  # a button…
    assert last(c).splitlines()[:2] == ["Уже есть: сон 7:00", "⭐ Оценка дня от 0 до 10? · шаг 2 из 4"]
    await bot.handle_update(msg("на восьмёрку", 3))  # …or words
    assert "🙂 Какое настроение? · шаг 3 из 4" in last(c)
    await bot.handle_update(msg("так себе", 4))
    tags = c.telegram.sent[-1]
    assert "🏷 Что было за день? · шаг 4 из 4" in last(c)
    assert list(buttons(tags))[:2] == ["＋ Холодный душ", "＋ Чистый стол"]
    await bot.handle_update(tap(buttons(tags)["＋ Холодный душ"], 5))  # a tag tap stays on the step
    assert "+ IS" in last(c).splitlines()[0]
    await bot.handle_update(msg("была тренировка", 6))
    assert last(c).splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• Сон: 7:00",
        "• Оценка дня: 8",
        "• Настроение: 🟡 нейтральное",
        "• Отметки: + Tr (Тренировка), IS (холодный душ)",
    ]
    assert c.lmf_api.writes == []
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])["✅ Записать"], 7))
    await worker.drain()
    day = c.lmf_api.days_store["2026-09-23"]
    assert (day["sleep_minutes"], day["score"], day["mood"], sorted(day["tags"])) == (420, 8, "neutral", [11, 23])
    async with c.db.session() as s:
        assert (await s.scalars(select(Note))).all() == []
        assert (await s.scalars(select(Idea))).all() == []


async def test_fill_skips_what_is_there_and_a_new_request_ends_it(container: Any) -> None:
    c = container
    c.lmf_api.days_store["2026-09-23"] = {"date": "2026-09-23", "tags": [], "supplements": [], "sleep_minutes": 480}
    bot = BotService(c)
    await bot.handle_update(tap("tl:f", 1))  # the button of the card / the evening review
    assert last(c).startswith("⭐ Оценка дня от 0 до 10? · шаг 1 из 3")  # sleep is in TLT already
    await bot.handle_update(msg("7:30", 12))  # not a score: asked again, not filed into the inbox
    assert last(c).splitlines()[:2] == [
        "Не понял «7:30». Ответьте кнопкой или словами — «пропусти», «готово», «стоп».",
        "⭐ Оценка дня от 0 до 10? · шаг 1 из 3",
    ]
    await bot.handle_update(msg("спал 7 часов, оценка 6", 2))  # a full report answers several steps at once
    assert "🙂 Какое настроение? · шаг 2 из 3" in last(c)
    await bot.handle_update(msg("что у меня завтра?", 3))  # not an answer: a request as usual, the filling ends
    assert last(c).startswith("📅 Завтра, чт 24.09")
    await bot.handle_update(msg("хорошее", 4))
    assert "Какое настроение" not in last(c)
    assert c.lmf_api.writes == []


async def test_fill_can_finish_early_or_stop(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("заполним TLT", 1))
    await bot.handle_update(msg("7:30, качество 90", 2))
    await bot.handle_update(msg("готово", 3))  # finish now: one draft with what was said
    assert last(c).splitlines() == ["📓 Записать в TLT · сегодня, ср 23.09?", "• Сон: 7:30, качество 90"]
    await bot.handle_update(msg("заполни день", 4))
    await bot.handle_update(msg("стоп", 5))
    assert last(c) == "Ок, остановился — в TLT ничего не записал."
    await bot.handle_update(msg("запиши в дневник: спал 6 часов", 6))  # values said: the usual draft at once
    assert last(c).splitlines()[:2] == ["📓 Записать в TLT · сегодня, ср 23.09?", "• Сон: 6:00"]


# --- the address «Джарвис, …» (bug 2026-10-14: it was saved as a note) -----------------------------------------------


async def test_address_is_never_part_of_a_note_or_an_idea(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Джарвис запиши заметку купить хлеб", 1))
    await bot.handle_update(msg("Эй, Джарвис, запиши идею: бот для спортзала.", 2))
    await bot.handle_update(msg("Jarvis", 3))
    assert last(c) == "Слушаю."
    async with c.db.session() as s:
        notes = [n.text for n in (await s.scalars(select(Note))).all()]
        ideas = [i.raw_input for i in (await s.scalars(select(Idea))).all()]
    assert notes == ["купить хлеб"]
    assert ideas == ["бот для спортзала."]


async def test_spoken_address_is_dropped_before_routing(container: Any) -> None:
    c = container
    c.telegram.files["v1"] = b"OggS-fill"
    c.stt.transcripts[b"OggS-fill"] = "Джарвис, заполни день."
    await BotService(c).handle_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 10,
                "from": {"id": USER},
                "chat": {"id": USER, "type": "private"},
                "voice": {"file_id": "v1", "file_unique_id": "u-v1", "duration": 2, "mime_type": "audio/ogg"},
            },
        }
    )
    assert last(c).startswith("🛌 Сколько спали?")
    async with c.db.session() as s:
        assert (await s.scalars(select(Idea))).all() == []  # it used to become an idea / a note


# --- one HTML message for several requests ---------------------------------------------------------------------------


async def test_several_requests_are_one_html_message(container: Any) -> None:
    c = container
    await BotService(c).handle_update(msg("Добавь купить хлеб. Что у меня завтра?", 1))
    [sent] = c.telegram.sent
    assert sent["parse_mode"] == "HTML"
    assert "🛒 Добавил в покупки: <b>Хлеб</b>\n\n📅 <b>Завтра, чт 24.09</b>" in sent["text"]
    assert "&lt;b&gt;" not in sent["text"]  # parts are not escaped twice


async def test_user_text_is_escaped(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("заметка: a < b & <b>c</b>", 1))
    await bot.handle_update(msg("/notes", 2))
    raw = c.telegram.sent[-1]["text"]
    assert "a &lt; b &amp; &lt;b&gt;c&lt;/b&gt;" in raw
    assert last(c).endswith("a < b & <b>c</b>")


async def test_the_mac_gets_html_and_speaks_plain_text(db: Any, clock: Any) -> None:
    from jarvis.desktop.channel import DesktopChannel
    from jarvis.services.container import build_container
    from tests.conftest import make_settings

    c = build_container(make_settings(voice_replies_enabled=True), db=db, clock=clock)
    reply = await DesktopChannel(c).message("Джарвис, дневник")
    [card] = reply["messages"]
    assert card["format"] == "html" and "📓 <b>Дневник</b>" in card["text"]  # RichText.swift renders the tags
    assert {b["data"] for row in card["buttons"] for b in row} >= {"tl:f"}
    assert c.tts.calls and "<" not in c.tts.calls[0][0]  # the Mac speaks the text, never the markup
