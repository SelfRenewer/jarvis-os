"""TLT / TPL tracker (LMF): report → «✅ Записать» → write; TPL marks; summaries; reminders. Fakes only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jarvis.bot.service import BotService
from jarvis.scheduler.queue import enqueue

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


async def test_report_confirm_and_write(container: Any, worker: Any) -> None:
    c = container
    lmf = c.lmf_api
    lmf.days_store["2026-09-23"] = {"date": "2026-09-23", "tags": [19], "supplements": [], "score": 5}
    bot = BotService(c)
    await bot.handle_update(msg("Спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT", 1))
    text = c.telegram.texts()[-1]
    assert text.splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• Сон: 7:30, качество 85",
        "• Оценка дня: 7 (было 5)",
        "• Настроение: 🟢 хорошее",
        "• Отметки: + CD (чистый стол), LT (светотерапия)",
    ]
    assert lmf.writes == []  # nothing before the button
    assert not [call for call in c.llm.calls if call[0] == "tracker_extract"]  # parsed by code
    options = buttons(c.telegram.sent[-1])
    assert set(options) == {"✅ Записать", "✖️ Отмена"}
    await bot.handle_update(tap(options["✅ Записать"], 2))
    await worker.drain()
    day = lmf.days_store["2026-09-23"]
    assert (day["sleep_minutes"], day["sleep_quality"], day["score"], day["mood"]) == (450, 85, 7, "good")
    assert day["tags"] == [12, 13, 19]  # merged with what was there
    assert c.telegram.texts()[-1] == (
        "📓 Записал в TLT · сегодня, ср 23.09: сон 7:30 (85) · оценка 7 · настроение хорошее · + CD, LT"
    )
    await bot.handle_update(tap(options["✅ Записать"], 3))  # a second tap is a no-op
    await worker.drain()
    assert len(lmf.writes) == 1


async def test_labels_yesterday_and_cancel(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Вчера была тренировка и холодный душ, в TLT", 10))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[0] == "📓 Записать в TLT · вт 22.09?"
    assert lines[1] == "• Отметки: + IS (холодный душ), Tr (Тренировка)"
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])["✖️ Отмена"], 11))
    await worker.drain()
    assert c.telegram.texts()[-1] == "Отменено — в TLT ничего не пишу."
    assert c.lmf_api.writes == []


async def test_duplicate_code_and_removal(container: Any, worker: Any) -> None:
    c = container
    lmf = c.lmf_api
    for day in ("2026-09-01", "2026-09-02"):  # «FC» of the routine group is the one in use
        lmf.days_store[day] = {"date": day, "tags": [17], "supplements": []}
    lmf.days_store["2026-09-22"] = {"date": "2026-09-22", "tags": [6, 17], "supplements": [], "score": 6}
    bot = BotService(c)
    await bot.handle_update(msg("TLT: FC", 20))
    assert c.telegram.texts()[-1].splitlines()[1] == "• Отметки: + FC (уход за лицом)"
    await bot.handle_update(msg("убери GG и оценку за вчера в TLT", 21))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[1:] == ["• Оценка дня: очистить", "• Снять отметки: − GG"]
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])["✅ Записать"], 22))
    await worker.drain()
    day = lmf.days_store["2026-09-22"]
    assert (day["tags"], day["score"]) == ([17], None)


async def test_tpl_ambiguous_item_then_write(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("TPL: отметь утреннюю рутину и тренировки", 30))
    assert c.telegram.texts()[-1] == "«тренировки» есть в нескольких сферах TPL — какой пункт отметить?"
    options = buttons(c.telegram.sent[-1])
    assert set(options) == {"Внешность · Тренировки", "Привычки · Тренировки", "☑ Все", "✖️ Отмена"}
    await bot.handle_update(tap(options["Привычки · Тренировки"], 31))
    assert c.telegram.texts()[-1].splitlines() == [
        "📊 Отметить в TPL · сентябрь 2026?",
        "• ☑ Привычки · Утренняя рутина",
        "• ☑ Привычки · Тренировки",
    ]
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])["✅ Записать"], 32))
    await worker.drain()
    assert c.lmf_api.checks["2026-09"] == {14: True, 17: True}
    assert c.telegram.texts()[-1] == "📊 TPL · сентябрь 2026: Привычки 0% → 35%, Total 0% → 7%"


async def test_lmf_down(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    c.lmf_api.unavailable = True
    await bot.handle_update(msg("спал 8 часов", 40))
    assert c.telegram.texts()[-1].startswith("Трекер TLT сейчас недоступен: LMF не отвечает")
    c.lmf_api.unavailable = False
    await bot.handle_update(msg("спал 8 часов", 41))
    approve = buttons(c.telegram.sent[-1])["✅ Записать"]
    c.lmf_api.unavailable = True
    c.tracker._dict = None  # the job re-reads the dictionary
    await bot.handle_update(tap(approve, 42))
    await worker.drain()
    assert c.telegram.texts()[-1] == "⏳ LMF не отвечает — запишу в TLT автоматически, как только он поднимется."
    c.lmf_api.unavailable = False
    c.clock.advance(timedelta(minutes=5))
    await worker.drain()
    assert c.lmf_api.days_store["2026-09-23"]["sleep_minutes"] == 480
    assert c.telegram.texts()[-1].startswith("📓 Записал в TLT · сегодня, ср 23.09: сон 8:00")


async def test_free_speech_goes_to_the_llm_with_closed_lists(container: Any) -> None:
    c = container
    seen: list[str] = []

    def extract(user_text: str, _messages: Any) -> dict[str, Any]:
        seen.append(user_text)
        return {
            "day_text": None,
            "sleep_text": None,
            "sleep_quality": None,
            "score": None,
            "mood": None,
            "tags": ["F", "XX"],
            "remove_tags": [],
            "tpl_items": ["Здоровье: Медитация"],
            "note": None,
        }

    c.llm.responders["tracker_extract"] = extract
    bot = BotService(c)
    await bot.handle_update(msg("в TLT: к вечеру немного вымотался, зато помедитировал", 50))
    assert "F — Лёгкое ощущение усталости" in seen[0] and "Здоровье: Медитация" in seen[0]
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• Отметки: + F (Лёгкое ощущение усталости)",
        "",
        "📊 TPL · сентябрь 2026",
        "• ☑ Здоровье · Медитация",
        "",
        "❔ Не понял (не запишу): «XX»",
    ]


async def test_summaries(container: Any) -> None:
    c = container
    lmf = c.lmf_api
    lmf.days_store["2026-09-22"] = {
        "date": "2026-09-22", "tags": [12, 23], "supplements": [], "sleep_minutes": 420, "sleep_quality": 80,
        "score": 6, "mood": "neutral",
    }  # fmt: skip
    lmf.days_store["2026-09-21"] = {"date": "2026-09-21", "tags": [12], "supplements": [], "sleep_minutes": 480}
    bot = BotService(c)
    await bot.handle_update(msg("/tlt вчера", 60))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 TLT · вт 22.09",
        "",
        "🛌 Сон: 7:00 · качество 80",
        "⭐ Оценка: 6",
        "🟡 Настроение: нейтральное",
        "🏷 CD (чистый стол), Tr (Тренировка)",
    ]
    await bot.handle_update(msg("как я спал на неделе?", 61))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[0] == "📓 TLT · 17.09–23.09: заполнено 2 из 7 дн."
    assert lines[2] == "🛌 сон в среднем 7:30 (качество 80) · ⭐ оценка 6"
    assert lines[-1] == "🏷 Чаще всего: CD ×2, Tr ×1"
    lmf.checks["2026-09"] = {14: True}
    await bot.handle_update(msg("/tpl", 62))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[0] == "📊 TPL · сентябрь 2026 · Total 4%"
    assert "🔴 Привычки 20% (1/4): Утренняя рутина" in lines


async def test_reminders(container: Any, worker: Any) -> None:
    c = container
    tracker = c.tracker
    now = c.clock.now()
    assert (
        await tracker.morning_line(now) == "🛌 Как спалось? Скажите, например: «спал 7:30, качество 85» — запишу в TLT."
    )
    assert await tracker.evening_lines(now) == []  # score and mood: the check-in buttons; no food nag
    month_end = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)  # 21:00 local on the last day of September
    assert (await tracker.evening_lines(month_end))[-1].startswith("📊 Завтра новый месяц — отметьте TPL за сентябрь")
    # 23:00: an empty day → one nudge; a filled day → nothing.
    c.clock.set(datetime(2026, 9, 23, 19, 0, tzinfo=UTC))
    async with c.db.session() as s:
        await enqueue(s, job_type="tracker.nudge", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    assert c.telegram.texts()[-1].startswith("📓 В TLT за сегодня пусто.")
    assert [b["callback_data"] for row in c.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for b in row] == [
        "tl:f"
    ]  # one tap → filling step by step
    sent = len(c.telegram.sent)
    c.lmf_api.days_store["2026-09-24"] = {"date": "2026-09-24", "tags": [12], "supplements": []}
    c.clock.set(datetime(2026, 9, 24, 19, 0, tzinfo=UTC))
    async with c.db.session() as s:
        await enqueue(s, job_type="tracker.nudge", run_at=c.clock.now(), payload={"slot": "2026-09-24"})
    await worker.drain()
    assert len(c.telegram.sent) == sent


async def test_labs_answer_from_lmf(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("мои анализы", 1))
    assert "анализов пока нет" in c.telegram.texts()[-1]
    row = {"name": "Ферритин", "value": 48.0, "value_text": "48.0", "unit": "нг/мл", "reference": "Смотри текст",
           "ref_low": None, "ref_high": None, "flag": None, "note": None, "source": "Лаб №1"}  # fmt: skip
    await c.lmf_api.save_labs(
        [
            {**row, "date": "2025-09-01", "value": 25.0, "value_text": "25.0"},
            {**row, "date": "2026-03-10"},
            {
                **row,
                "date": "2026-03-10",
                "name": "Пролактин",
                "value_text": "21.4++",
                "flag": "high",
                "unit": "нг/мл",
                "reference": "4.04 - 15.2",
            },
        ]
    )
    await bot.handle_update(msg("какой ферритин в анализах?", 2))
    text = c.telegram.texts()[-1]
    assert "10.03.2026: 48.0 нг/мл" in text and "01.09.2025: 25.0 нг/мл" in text
    await bot.handle_update(msg("/labs", 3))
    text = c.telegram.texts()[-1]
    assert "Анализы · 10.03.2026" in text and "Пролактин — 21.4 нг/мл ↑" in text and "решает врач" in text
    c.lmf_api.unavailable = True
    await bot.handle_update(msg("мои анализы", 4))
    assert c.telegram.texts()[-1].startswith("Трекер TLT сейчас недоступен: LMF не отвечает")
