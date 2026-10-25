"""Food (КБЖУ) → LMF: directory values by code, LLM estimates only for unknown foods (shown with ≈, saved after
«✅ Записать»), product values and daily norms said in words, drinks with calories, an LMF without food. Fakes only."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.bot.service import BotService

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


def buttons(c: Any) -> dict[str, str]:
    markup = c.telegram.sent[-1].get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def products(c: Any, *rows: tuple[str, float, float, float, float, float | None, str | None]) -> None:
    for name, kcal, protein, fat, carbs, portion_g, portion_name in rows:
        key = name.lower()
        c.lmf_api.products[key] = {
            "id": len(c.lmf_api.products) + 1,
            "key": key,
            "name": name,
            "kcal": kcal,
            "protein": protein,
            "fat": fat,
            "carbs": carbs,
            "portion_g": portion_g,
            "portion_name": portion_name,
            "source": "user",
        }


def estimate(*items: dict[str, Any]) -> Any:
    def responder(_user: str, _messages: Any) -> dict[str, Any]:
        return {"items": list(items)}

    return responder


async def test_known_food_is_computed_by_code(container: Any, worker: Any) -> None:
    c = container
    products(c, ("Гречка", 330, 12, 3, 62, None, None))
    c.lmf_api.targets = {"kcal": 2200}
    bot = BotService(c)
    await bot.handle_update(msg("Съел 200 г гречки", 1))
    assert c.telegram.texts()[-1].splitlines() == [
        "🍽 Записать еду · сегодня, ср 23.09?",
        "• Гречка 200 г — 660 ккал · Б 24 · Ж 6 · У 124",
        "Итого: 660 ккал · Б 24 · Ж 6 · У 124 · за день будет 660 из 2200 ккал",
    ]
    assert not [call for call in c.llm.calls if call[0] == "food_estimate"]
    assert c.lmf_api.food == []
    approve = buttons(c)["✅ Записать"]
    await bot.handle_update(tap(approve, 2))
    await worker.drain()
    assert c.telegram.texts()[-1] == (
        "🍽 Записал · сегодня, ср 23.09: Гречка 200 г — 660 ккал · Б 24 · Ж 6 · У 124\n"
        "За день: 660 ккал · Б 24 · Ж 6 · У 124 (норма 2200 ккал)"
    )
    entry = c.lmf_api.food[0]
    assert (entry["name"], entry["grams"], entry["kcal"], entry["time"]) == ("Гречка", 200, 660.0, "09:00")
    await bot.handle_update(tap(approve, 3))  # a double tap writes nothing twice
    await worker.drain()
    assert len(c.lmf_api.food) == 1
    await bot.handle_update(msg("сколько я сегодня съел?", 4))
    assert c.telegram.texts()[-1].splitlines() == [
        "🍽 Сегодня, ср 23.09",
        "",
        "Всего: 660 ккал · Б 24 · Ж 6 · У 124",
        "Норма: ккал осталось 1540",
        "",
        "• 09:00 Гречка 200 г — 660 ккал",
    ]


async def test_unknown_food_is_estimated_then_saved(container: Any, worker: Any) -> None:
    c = container
    products(c, ("Яйцо", 157, 12.7, 10.9, 0.7, 55, "шт"))
    c.llm.responders["food_estimate"] = estimate(
        {
            "said": "тарелку борща",
            "name": "Борщ",
            "grams": 300,
            "kcal_100": 50,
            "protein_100": 2.5,
            "fat_100": 2.5,
            "carbs_100": 5,
        },
    )
    bot = BotService(c)
    await bot.handle_update(msg("На обед тарелку борща и 2 яйца", 10))
    assert c.telegram.texts()[-1].splitlines() == [
        "🍽 Записать еду · сегодня, ср 23.09?",
        "• Яйцо 110 г — 173 ккал · Б 14 · Ж 12 · У 1",
        "• Борщ ≈300 г — 150 ккал · Б 8 · Ж 8 · У 15 ≈ оценка ИИ — сохраню в справочник",
        "Итого: 323 ккал · Б 22 · Ж 20 · У 16",
    ]
    assert "борщ" not in c.lmf_api.products  # the estimate is saved only after the button
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 11))
    await worker.drain()
    saved = c.lmf_api.products["борщ"]
    assert (saved["kcal"], saved["portion_g"], saved["source"]) == (50, 300.0, "estimate")
    assert [(e["name"], e["meal"], e["product_id"]) for e in c.lmf_api.food] == [
        ("Яйцо", "обед", 1),
        ("Борщ", "обед", saved["id"]),
    ]
    await bot.handle_update(msg("съел тарелку борща", 12))  # now from the directory: same numbers, no LLM
    assert c.telegram.texts()[-1].splitlines()[1] == "• Борщ 300 г — 150 ккал · Б 8 · Ж 8 · У 15"
    assert len([call for call in c.llm.calls if call[0] == "food_estimate"]) == 1


async def test_implausible_estimate_is_not_used(container: Any) -> None:
    c = container
    c.llm.responders["food_estimate"] = estimate(
        {
            "said": "пирожок",
            "name": "Пирожок",
            "grams": 80,
            "kcal_100": 900,
            "protein_100": 1,
            "fat_100": 1,
            "carbs_100": 1,
        },
    )
    await BotService(c).handle_update(msg("съел пирожок", 20))
    assert c.telegram.texts()[-1].startswith("Не понял, что записать в еду.")
    assert "пирожок (оценка не сошлась — скажите КБЖУ на 100 г)" in c.telegram.texts()[-1]


async def test_values_and_norm_in_words(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("гречка — 330 ккал, Б 12, Ж 3, У 62", 30))
    assert c.telegram.texts()[-1] == "🍽 Запомнил: Гречка на 100 г — 330 ккал · Б 12 · Ж 3 · У 62."
    await bot.handle_update(msg("энергетик — банка 500 мл, 225 ккал на банку, У 55 на банку", 31))
    assert c.telegram.texts()[-1] == "🍽 Запомнил: Энергетик на 100 г — 45 ккал · Б — · Ж — · У 11 · банка = 500 г."
    await bot.handle_update(msg("норма 2200 ккал, белок 140", 32))
    assert c.telegram.texts()[-1] == "🎯 Норма на день: 2200 ккал · Б 140 · Ж не задано · У не задано"
    assert c.lmf_api.targets == {"kcal": 2200.0, "protein": 140.0}
    await bot.handle_update(msg("норма 2000 ккал", 33))  # R36: one norm said → the others stay
    assert c.telegram.texts()[-1] == "🎯 Норма на день: 2000 ккал · Б 140 · Ж не задано · У не задано"
    assert c.lmf_api.targets == {"kcal": 2000.0, "protein": 140.0}


async def test_drinks(container: Any, worker: Any) -> None:
    c = container
    products(c, ("Энергетик", 45, 0, 0, 11, 500, "банка"), ("Молоко", 60, 3, 3.2, 4.7, 250, "стакан"))
    bot = BotService(c)
    await bot.handle_update(msg("энергетик — 160 мг кофеина", 40))
    await bot.handle_update(msg("выпил энергетик", 41))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• 💊 Добавки: + Кофеин (энергетик) 160 мг",
        "",
        "🍽 Еда",
        "• Энергетик 500 г — 225 ккал · Б 0 · Ж 0 · У 55",
        "Итого: 225 ккал · Б 0 · Ж 0 · У 55",
    ]
    await bot.handle_update(msg("выпил стакан молока", 42))  # food, not a supplement
    assert c.telegram.texts()[-1].splitlines()[:2] == [
        "🍽 Записать еду · сегодня, ср 23.09?",
        "• Молоко 250 г — 150 ккал · Б 8 · Ж 8 · У 12",
    ]


async def test_unknown_drink_can_be_food(container: Any) -> None:
    c = container
    c.llm.responders["food_estimate"] = estimate(
        {
            "said": "кефир",
            "name": "Кефир",
            "grams": 250,
            "kcal_100": 50,
            "protein_100": 3,
            "fat_100": 2.5,
            "carbs_100": 4,
        },
    )
    bot = BotService(c)
    await bot.handle_update(msg("выпил кефир", 50))
    assert (
        c.telegram.texts()[-1]
        == "Не нашёл «кефир» ни в напоминаниях, ни в справочнике добавок LMF. Это еда или добавка?"
    )
    await bot.handle_update(tap(buttons(c)["🍽 Это еда — посчитать КБЖУ"], 51))
    assert c.telegram.texts()[-1].splitlines()[1].startswith("• Кефир ≈250 г — 125 ккал")


async def test_old_lmf_without_food(container: Any) -> None:
    c = container
    c.lmf_api.food_supported = False
    await BotService(c).handle_update(msg("съел 200 г гречки", 60))
    assert c.telegram.texts()[-1] == "📓 LMF без раздела «Питание» — перезапустите сайт TLT в LocalSites"


async def test_evening_food_line(container: Any) -> None:
    c = container
    assert await c.tracker.evening_lines(c.clock.now()) == []  # nothing eaten: no daily nag (2026-10-14)
    products(c, ("Гречка", 330, 12, 3, 62, None, None))
    from datetime import date

    await c.lmf_api.add_food(date(2026, 9, 23), [{"name": "Гречка", "grams": 100, "product_id": 1}])
    c.lmf_api.targets = {"kcal": 2000}
    assert (await c.tracker.evening_lines(c.clock.now()))[-1] == (
        "🍽 Сегодня: 330 ккал · Б 12 · Ж 3 · У 62 (норма 2000 ккал)"
    )


# --- removing an entry by words ----------------------------------------------------------------------------------


def eaten(c: Any, name: str, grams: float, kcal: float, at: str | None, day: str = "2026-09-23") -> None:
    row = {"id": c.lmf_api.next_food_id, "date": day, "name": name, "grams": grams, "kcal": kcal, "protein": 1,
           "fat": 1, "carbs": 1, "time": at, "created_at": f"{day} {at or '00:00'}:00"}  # fmt: skip
    c.lmf_api.food.append(row)
    c.lmf_api.next_food_id += 1


async def test_food_entry_is_removed_by_words_after_the_button(container: Any, worker: Any) -> None:
    c = container
    eaten(c, "Гречка", 200, 660, "09:00")
    eaten(c, "Яйцо", 110, 173, "09:00")
    eaten(c, "Гречка", 150, 495, "13:00")
    bot = BotService(c)
    await bot.handle_update(msg("удали гречку из еды", 300))
    assert c.telegram.texts()[-1].splitlines() == [
        "🗑 Удалить из еды · сегодня, ср 23.09?",
        "• 13:00 Гречка 150 г — 495 ккал · Б 1 · Ж 1 · У 1 (последняя из 2 похожих)",
        "За день станет 833 ккал",
    ]
    assert len(c.lmf_api.food) == 3  # nothing deleted before the button
    await bot.handle_update(tap(buttons(c)["🗑 Удалить"], 301))
    await worker.drain()
    assert [e["id"] for e in c.lmf_api.food] == [1, 2]
    assert c.telegram.texts()[-1].startswith("🗑 Удалил из еды · сегодня, ср 23.09: Гречка 150 г\nЗа день: 833 ккал")


async def test_food_removal_prefers_the_said_grams_and_the_last_entry(container: Any, worker: Any) -> None:
    c = container
    eaten(c, "Гречка", 200, 660, "09:00")
    eaten(c, "Гречка", 150, 495, "13:00")
    eaten(c, "Яйцо", 110, 173, "14:00")
    bot = BotService(c)
    await bot.handle_update(msg("удали 200 г гречки из еды", 310))
    assert "• 09:00 Гречка 200 г" in c.telegram.texts()[-1]
    await bot.handle_update(msg("отмени последнюю запись еды", 311))
    assert "• 14:00 Яйцо 110 г" in c.telegram.texts()[-1]


async def test_food_removal_cancel_and_missing_food(container: Any, worker: Any) -> None:
    c = container
    eaten(c, "Гречка", 200, 660, "09:00")
    bot = BotService(c)
    await bot.handle_update(msg("убери борщ из еды", 320))
    assert c.telegram.texts()[-1] == "🍽 сегодня, ср 23.09: в еде нет «борщ». Записано: Гречка."
    await bot.handle_update(msg("удали гречку из еды за вчера", 321))
    assert c.telegram.texts()[-1] == "🍽 вт 22.09: в еде нет «гречку». Еда за этот день не записана."
    await bot.handle_update(msg("удали гречку из еды", 322))
    await bot.handle_update(tap(buttons(c)["✖️ Отмена"], 323))
    await worker.drain()
    assert len(c.lmf_api.food) == 1
    assert "запись еды не трогаю" in c.telegram.texts()[-1]


async def test_food_removal_never_deletes_another_entry_that_got_the_same_id(container: Any, worker: Any) -> None:
    """LMF can give a deleted id to a new entry: the approved draft must match id, name and creation time."""
    c = container
    eaten(c, "Гречка", 200, 660, "09:00")
    bot = BotService(c)
    await bot.handle_update(msg("удали гречку из еды", 330))
    approve = buttons(c)["🗑 Удалить"]
    c.lmf_api.food.clear()  # deleted in the LMF page meanwhile…
    c.lmf_api.food.append({"id": 1, "date": "2026-09-23", "name": "Борщ", "grams": 300, "kcal": 150,
                           "time": "10:00", "created_at": "2026-09-23 10:00:00"})  # fmt: skip
    await bot.handle_update(tap(approve, 331))
    await worker.drain()
    assert [e["name"] for e in c.lmf_api.food] == ["Борщ"]
    assert c.telegram.texts()[-1] == "🍽 Записи «Гречка» уже нет — ничего не удалял."


# --- pills and food in one phrase (2026-10-14) -------------------------------------------------------------------


def lmf_supplements(c: Any, day: str = "2026-09-23") -> dict[int, Any]:
    return {s["supplement_id"]: s["dose"] for s in c.lmf_api.days_store.get(day, {}).get("supplements", [])}


async def test_pills_and_food_in_one_phrase_are_one_draft(container: Any, worker: Any) -> None:
    c = container
    products(c, ("Кефир", 50, 3, 2.5, 4, 250, "стакан"))
    bot = BotService(c)
    await bot.handle_update(msg("выпил магний 400 и стакан кефира", 60))
    assert c.telegram.texts()[-1].splitlines() == [
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• 💊 Добавки: + Магний 400 мг",
        "",
        "🍽 Еда",
        "• Кефир 250 г — 125 ккал · Б 8 · Ж 6 · У 10",
        "Итого: 125 ккал · Б 8 · Ж 6 · У 10",
    ]
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 61))
    await worker.drain()
    assert lmf_supplements(c) == {23: 400.0}
    assert [(e["name"], e["grams"]) for e in c.lmf_api.food] == [("Кефир", 250)]
    assert c.llm.calls == []


async def test_food_first_then_pills(container: Any) -> None:
    c = container
    products(c, ("Гречка", 330, 12, 3, 62, None, None))
    bot = BotService(c)
    await bot.handle_update(msg("Съел 200 г гречки и выпил D3 шесть тысяч", 62))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[1] == "• 💊 Добавки: + Витамин D3 6000 МЕ"
    assert "• Гречка 200 г — 660 ккал · Б 24 · Ж 6 · У 124" in lines
    assert c.llm.calls == []  # D3 is not estimated as food


async def test_pills_and_breakfast_in_two_sentences(container: Any) -> None:
    c = container
    products(c, ("Гречка", 330, 12, 3, 62, None, None))
    bot = BotService(c)
    sent = len(c.telegram.sent)
    await bot.handle_update(msg("Утром выпил D3 6000 и цинк 25. На завтрак съел 200 г гречки.", 63))
    assert len(c.telegram.sent) == sent + 1  # one message, one «✅ Записать»
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[1] == "• 💊 Добавки: + Витамин D3 6000 МЕ, Цинк 25 мг"
    assert "• Гречка 200 г — 660 ккал · Б 24 · Ж 6 · У 124" in lines
    assert list(buttons(c)) == ["✅ Записать", "✖️ Отмена"]
    assert c.llm.calls == []


async def test_unknown_pill_next_to_food(container: Any) -> None:
    c = container
    products(c, ("Кефир", 50, 3, 2.5, 4, 250, "стакан"))
    bot = BotService(c)
    await bot.handle_update(msg("выпил ашваганду и стакан кефира", 64))
    lines = c.telegram.texts()[-1].splitlines()
    assert "• Кефир 250 г — 125 ккал · Б 8 · Ж 6 · У 10" in lines
    assert lines[-1] == "❔ Не понял (не запишу): «ашваганду»"
