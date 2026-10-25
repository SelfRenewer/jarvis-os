"""Pills → TLT: packs on a schedule («Утренние»), «☑ Не всё», «выпил … кроме …», drinks as caffeine portions,
supplements by fact (after «✅ Записать»), unknown names, LMF outages. Fakes only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import Medication
from jarvis.scheduler.queue import enqueue

USER = 1001
NB = " "
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


def tap(data: str, update_id: int, message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": message_id, "chat": {"id": USER, "type": "private"}},
        },
    }


def buttons(markup: dict[str, Any] | None) -> dict[str, str]:
    return {b["text"]: b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row}


def supplements(c: Any, day: str) -> dict[int, Any]:
    return {s["supplement_id"]: s["dose"] for s in c.lmf_api.days_store.get(day, {}).get("supplements", [])}


async def tick(c: Any, worker: Any) -> None:
    async with c.db.session() as s:
        await enqueue(s, job_type="meds.tick", run_at=c.clock.now())
    await worker.drain()


async def test_pack_reminder_all_taken(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, B12 500, цинк 25 в 9:00", 1))
    assert c.telegram.texts()[-1].splitlines() == [
        "🌅 Буду напоминать Утренние · 09:00: Витамин D3 6000 МЕ · Витамин B12 500 мкг · Цинк 25 мг.",
        "Каждый день в 09:00 пришлю напоминание: ✅ Выпил всё · ☑ Не всё · ⏰ 30 мин · ✖️ Пропустить. "
        "Отмеченное запишу в TLT само.",
    ]
    await tick(c, worker)  # the clock is 09:00
    assert c.telegram.texts()[-1] == (
        "🌅 Время: Утренние (09:00)\nВитамин D3 6000 МЕ · Витамин B12 500 мкг · Цинк 25 мг"
    )
    assert c.telegram.sent[-1]["text"].startswith("🌅 Время: <b>Утренние</b> (09:00)\n<i>")
    options = buttons(c.telegram.sent[-1]["reply_markup"])
    assert set(options) == {"✅ Выпил всё", "☑ Не всё", "⏰ 30 мин", "✖️ Пропустить"}
    await bot.handle_update(tap(options["✅ Выпил всё"], 2))
    assert c.telegram.texts()[-1] == (
        "✅ Отметил: Утренние.\n📓 Записал в TLT · сегодня, ср 23.09: "
        "💊 Витамин D3 6000 МЕ, Витамин B12 500 мкг, Цинк 25 мг"
    )
    assert supplements(c, "2026-09-23") == {12: 6000.0, 11: 500.0, 15: 25.0}
    await bot.handle_update(tap(options["✅ Выпил всё"], 3))  # a double tap writes nothing twice
    assert supplements(c, "2026-09-23") == {12: 6000.0, 11: 500.0, 15: 25.0}
    await bot.handle_update(msg("💊 Лекарства", 4))
    assert "🌅 Утренние — Витамин D3 6000 МЕ · Витамин B12 500 мкг · Цинк 25 мг\n09:00 ✅" in c.telegram.texts()[-1]


async def test_not_all_picker(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("вечерние таблетки: магний 300, глицин", 10))
    c.clock.set(datetime(2026, 9, 23, 18, 30, tzinfo=UTC))  # 22:30 local
    await tick(c, worker)
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["☑ Не всё"], 11))
    assert c.telegram.texts()[-1] == "Что выпили из «Вечерние» (22:30)? Отметьте и нажмите «Готово»."
    picker = buttons(c.telegram.sent[-1]["reply_markup"])
    assert set(picker) == {"☐ Магний 300 мг", "☐ Глицин 0,2 г", "✅ Готово", "✖️ Ничего"}
    await bot.handle_update(tap(picker["✅ Готово"], 12, message_id=200))
    assert c.telegram.answered[-1][1] == "Отметьте хотя бы одну — или «✖️ Ничего»"
    await bot.handle_update(tap(picker["☐ Магний 300 мг"], 13, message_id=200))
    assert "☑ Магний 300 мг" in buttons(c.telegram.markup_edits[-1]["reply_markup"])
    await bot.handle_update(tap(picker["✅ Готово"], 14, message_id=200))
    assert c.telegram.texts()[-1].startswith("✅ Отметил: Вечерние — Магний 300 мг.\n📓 Записал в TLT")
    assert supplements(c, "2026-09-23") == {23: 300.0}


async def test_taken_in_words_except(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, B12 500, цинк 25", 20))
    await bot.handle_update(msg("выпил утренние кроме цинка", 21))
    assert c.telegram.texts()[-1].startswith(
        "✅ Отметил приём: Утренние (09:00): Витамин D3 6000 МЕ · Витамин B12 500 мкг.\n📓 Записал в TLT"
    )
    assert supplements(c, "2026-09-23") == {12: 6000.0, 11: 500.0}
    await bot.handle_update(msg("убери из утренних цинк", 22))
    assert c.telegram.texts()[-1].startswith("🌅 Убрал Цинк 25 мг; Утренние · 09:00:")
    async with c.db.session() as s:
        med = (await s.scalars(select(Medication))).one()
    assert [i["supplement"] for i in med.items] == ["Vit. D3", "Vit. B12"]


async def test_energy_drink_portion_and_by_fact(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("выпил энергетик", 30))
    assert c.telegram.texts()[-1].startswith("☕ Сколько кофеина в «энергетик»?")
    choice = buttons(c.telegram.sent[-1]["reply_markup"])
    assert set(choice) == {"80 мг", "160 мг", "200 мг", "✖️ Отмена"}
    await bot.handle_update(tap(choice["160 мг"], 31))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• 💊 Добавки: + Кофеин (энергетик) 160 мг",
    ]
    assert c.lmf_api.writes == []  # by fact = only after the button
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["✅ Записать"], 32))
    await worker.drain()
    await bot.handle_update(msg("выпил 2 энергетика", 33))
    assert c.telegram.texts()[-1].splitlines()[1] == "• 💊 Добавки: + Кофеин (энергетик) 320 мг (за день будет 480 мг)"
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["✅ Записать"], 34))
    await worker.drain()
    assert supplements(c, "2026-09-23") == {2: 480.0}  # doses add up
    await bot.handle_update(msg("кофе — 80 мг кофеина", 35))
    assert c.telegram.texts()[-1] == "☕ Запомнил: кофе = кофеин 80 мг. «Выпил кофе» запишу в TLT как кофеин 80 мг."
    await bot.handle_update(msg("принял креатин 5 г", 36))
    assert c.telegram.texts()[-1].splitlines()[1] == "• 💊 Добавки: + Креатин 5 г"


async def test_unknown_supplement_can_be_added(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("выпил ашваганду", 40))
    assert c.telegram.texts()[-1] == (
        "Не нашёл «ашваганду» ни в напоминаниях, ни в справочнике добавок LMF. Это еда или добавка?"
    )
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["➕ Добавить «Ашваганда»"], 41))
    assert (
        c.telegram.texts()[-1] == "➕ Добавил «Ашваганда» в справочник LMF. Теперь «выпил ашваганда» запишется в TLT."
    )
    await bot.handle_update(msg("выпил ашваганду", 42))
    assert c.telegram.texts()[-1].splitlines()[1] == "• 💊 Добавки: + Ашваганда"


async def test_lmf_down_when_taken(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, цинк 25", 50))
    await tick(c, worker)
    take = buttons(c.telegram.sent[-1]["reply_markup"])["✅ Выпил всё"]
    c.lmf_api.unavailable = True
    c.tracker._dict = None
    await bot.handle_update(tap(take, 51))
    assert (
        c.telegram.texts()[-1] == "✅ Отметил: Утренние.\nTLT сейчас недоступен — запишу туда, как только LMF ответит."
    )
    c.lmf_api.unavailable = False
    c.clock.advance(timedelta(minutes=2))
    await worker.drain()
    assert supplements(c, "2026-09-23") == {12: 6000.0, 15: 25.0}
    assert c.telegram.texts()[-1].startswith("📓 Записал в TLT · сегодня, ср 23.09: 💊 Витамин D3 6000 МЕ, Цинк 25 мг")


async def test_day_report_with_supplements(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg(f"спал 7:30, кофеин 200, D3{NB}", 60))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• Сон: 7:30",
        "• 💊 Добавки: + Кофеин 200 мг, Витамин D3 4000 МЕ",
    ]


# --- dictated lists (found live 2026-10-14: a long voice note with doses in words was not understood) -------------


async def test_dictated_list_is_one_draft_without_the_llm(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    sent = len(c.telegram.sent)
    await bot.handle_update(
        msg(
            "Я сегодня утром выпил витамин Д3 6000 единиц, B12 500 микрограмм, цинк двадцать пять миллиграмм "
            "и магний 400 миллиграмм.",
            70,
        )
    )
    assert len(c.telegram.sent) == sent + 1
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• 💊 Добавки: + Витамин D3 6000 МЕ, Витамин B12 500 мкг, Цинк 25 мг, Магний 400 мг",
    ]
    assert c.llm.calls == []
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["✅ Записать"], 71))
    await worker.drain()
    assert supplements(c, "2026-09-23") == {12: 6000.0, 11: 500.0, 15: 25.0, 23: 400.0}


async def test_several_sentences_are_one_draft(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    sent = len(c.telegram.sent)
    await bot.handle_update(msg("Выпил витамин D3 5000 единиц. Ещё омегу-3, две капсулы. И магний 400 миллиграмм.", 72))
    assert len(c.telegram.sent) == sent + 1
    assert c.telegram.texts()[-1].splitlines()[1] == "• 💊 Добавки: + Витамин D3 5000 МЕ, Омега-3 (2 шт), Магний 400 мг"
    assert list(buttons(c.telegram.sent[-1]["reply_markup"])) == ["✅ Записать", "✖️ Отмена"]
    assert c.llm.calls == []


async def test_an_unknown_name_does_not_block_the_rest(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("выпил магний 400 мг и ашваганду 600 мг", 74))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[1] == "• 💊 Добавки: + Магний 400 мг"
    assert lines[-1] == "❔ Не понял (не запишу): «ашваганду 600 мг»"
    choice = buttons(c.telegram.sent[-1]["reply_markup"])
    assert list(choice) == ["✅ Записать", "✖️ Отмена", "➕ Добавить «Ашваганда» в LMF"]
    await bot.handle_update(tap(choice["✅ Записать"], 75))
    await worker.drain()
    assert supplements(c, "2026-09-23") == {23: 400.0}


async def test_yesterday(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, цинк 25", 76))
    await bot.handle_update(msg("вчера выпил магний 400", 77))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · вт 22.09?",
        "• 💊 Добавки: + Магний 400 мг",
    ]
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["✅ Записать"], 78))
    await worker.drain()
    assert supplements(c, "2026-09-22") == {23: 400.0}
    # a pack for another day: not marked as taken today — its items go into the draft
    await bot.handle_update(msg("вчера выпил утренние", 79))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · вт 22.09?",
        "• 💊 Добавки: + Витамин D3 6000 МЕ, Цинк 25 мг",
    ]
    await bot.handle_update(msg("выпил утренние", 80))
    assert c.telegram.texts()[-1].startswith("✅ Отметил приём: Утренние (09:00).")


async def test_a_pack_and_more_in_one_sentence(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, B12 500", 82))
    await bot.handle_update(msg("выпил утренние и креатин 5 г", 83))
    assert c.telegram.texts()[-2].startswith("✅ Отметил приём: Утренние (09:00).")
    assert c.telegram.texts()[-1].splitlines()[1] == "• 💊 Добавки: + Креатин 5 г"


async def test_a_lost_reply_does_not_add_the_dose_twice(container: Any, worker: Any) -> None:
    # R38: LMF wrote the day but its reply was lost → the job retried, re-read 300 mg and wrote 600 mg.
    c = container
    bot = BotService(c)
    zinc = {"supplement_id": 15, "dose": 25.0, "dose_text": None}
    c.lmf_api.days_store["2026-09-23"] = {"date": "2026-09-23", "tags": [], "body": None, "supplements": [zinc]}
    await bot.handle_update(msg("выпил магний 300 мг", 90))
    await bot.handle_update(tap(buttons(c.telegram.sent[-1]["reply_markup"])["✅ Записать"], 91))
    c.lmf_api.lose_next_reply = True
    await worker.drain()
    assert supplements(c, "2026-09-23") == {15: 25.0, 23: 300.0}  # written once, the reply lost
    c.lmf_api.days_store["2026-09-23"]["supplements"].append({"supplement_id": 12, "dose": 6000.0, "dose_text": None})
    c.clock.advance(timedelta(minutes=5))  # the retry; meanwhile D3 was added in TLT by hand
    await worker.drain()
    assert supplements(c, "2026-09-23") == {15: 25.0, 23: 300.0, 12: 6000.0}
    assert c.telegram.texts()[-1].startswith("📓 Записал в TLT")


async def test_a_lost_reply_of_a_pack_is_not_logged_twice(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Утренние таблетки: D3 6000, цинк 25", 92))
    await tick(c, worker)
    take = buttons(c.telegram.sent[-1]["reply_markup"])["✅ Выпил всё"]
    c.lmf_api.unavailable = True
    c.tracker._dict = None
    await bot.handle_update(tap(take, 93))  # LMF down → job supplements.log
    c.lmf_api.unavailable = False
    c.lmf_api.lose_next_reply = True
    c.clock.advance(timedelta(minutes=2))
    await worker.drain()
    c.clock.advance(timedelta(minutes=5))
    await worker.drain()
    assert supplements(c, "2026-09-23") == {12: 6000.0, 15: 25.0}
