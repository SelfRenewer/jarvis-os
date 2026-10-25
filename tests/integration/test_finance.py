"""Finance: expenses said in words (recorded at once, «↩️ Отменить»), totals by period and category, removal after
a button, receipts read from a photo (written after «✅ Записать»). Money as integer minor units. Fakes only.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from jarvis.bot.intents import Intent, route_message
from jarvis.bot.service import BotService
from jarvis.db.models import Expense
from jarvis.finance.parse import amount_text, parse_expense, receipt_amount, to_minor
from jarvis.finance.service import parse_spend_query, receipt_day

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


async def expenses(c: Any) -> list[Expense]:
    async with c.db.session() as s:
        return list((await s.scalars(select(Expense).order_by(Expense.source_ref))).all())


# --- parsing (no DB) -----------------------------------------------------------------------------------------------


def test_phrases_are_parsed_by_code_in_decimal() -> None:
    def short(text: str) -> list[tuple[str, str, str, str]]:
        parsed = parse_expense(text)
        assert parsed is not None and parsed.error is None, text
        return [(str(i.amount), i.currency, i.description, i.category) for i in parsed.items]

    assert short("потратил 45 лари на такси") == [("45", "GEL", "Такси", "Транспорт")]
    assert short("заплатил $120 за отель") == [("120", "USD", "Отель", "Путешествия")]
    assert short("вчера потратил 30 на продукты и 12,5 на кофе") == [
        ("30", "GEL", "Продукты", "Продукты"),
        ("12.5", "GEL", "Кофе", "Кафе и рестораны"),
    ]
    assert short("купил кроссовки за 250 лари") == [("250", "GEL", "Кроссовки", "Одежда")]
    assert short("я сегодня заплатил 9.50 за обед в кафе")[0][:2] == ("9.5", "GEL")  # 9.50 is money, not a date
    assert short("потратил 12.10 лари на такси")[0][0] == "12.1"
    assert parse_expense("заплатил 35 лари за стрижку 12.10").day_word == "12.10"  # type: ignore[union-attr]
    assert parse_expense("потратил 2 часа на уборку").items == []  # type: ignore[union-attr]
    assert parse_expense("перенеси стрижку на завтра") is None
    assert parse_expense("потратил 0.001 btc на кофе").error == "Расходы в криптовалюте я не веду."  # type: ignore[union-attr]


def test_money_is_minor_units_and_receipts_are_parsed_by_code() -> None:
    assert to_minor(Decimal("12.5"), "GEL") == 1250
    assert to_minor(Decimal("0.1") + Decimal("0.2"), "USD") == 30
    assert to_minor(Decimal("1500"), "JPY") == 1500
    assert amount_text(4500, "GEL") == "45 ₾" and amount_text(123450, "GEL") == "1\u00a0234,50 ₾"
    assert receipt_amount("45.80") == Decimal("45.80")
    assert receipt_amount("1 234,50") == Decimal("1234.50")
    assert receipt_amount("1,234.50") == Decimal("1234.50")
    assert receipt_amount("12 и 13") is None
    today = date(2026, 9, 23)
    assert receipt_day("22.09.2026 14:31", today) == date(2026, 9, 22)
    assert receipt_day("2026-09-21", today) == date(2026, 9, 21)
    assert receipt_day("01.01.2020", today) is None  # too old: use today


def test_spend_queries() -> None:
    today = date(2026, 9, 23)
    q = parse_spend_query("сколько ушло на такси за неделю", today)
    assert (q.start, q.end, q.words) == (date(2026, 9, 17), today, ["такси"])
    q = parse_spend_query("сколько я потратил в августе", today)
    assert (q.start, q.end, q.words, q.label) == (date(2026, 8, 1), date(2026, 8, 31), [], "Август")
    q = parse_spend_query("мои расходы", today)
    assert (q.start, q.end, q.label) == (date(2026, 9, 1), today, "Сентябрь")


async def test_routing_keeps_ai_usage_and_tasks_apart() -> None:
    async def first(text: str) -> Intent:
        return (await route_message(text, None))[0].intent

    assert await first("потратил 45 лари на такси") == Intent.EXPENSE_ADD
    assert await first("сколько я потратил в октябре") == Intent.EXPENSE_SHOW
    assert await first("сколько я потратил на ИИ") == Intent.SHOW_USAGE
    assert await first("статистика") == Intent.SHOW_USAGE
    assert await first("удали последний расход") == Intent.EXPENSE_REMOVE
    assert await first("купить хлеб") == Intent.SHOPPING_ADD
    assert await first("10 000 рублей в долларах") == Intent.CONVERT_CURRENCY
    actions = await route_message("потратил 45 лари на такси. Что у меня сегодня", None)
    assert [a.intent for a in actions] == [Intent.EXPENSE_ADD, Intent.SHOW_TODAY]


# --- the bot -------------------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_expense_is_recorded_at_once_and_can_be_undone(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("потратил 45 лари на такси и 12,5 на кофе", 1))
    assert c.telegram.texts()[-1].splitlines() == [
        "💸 Записал в расходы:",
        "• Такси — 45 ₾ (Транспорт)",
        "• Кофе — 12,50 ₾ (Кафе и рестораны)",
        "За сентябрь: 57,50 ₾",
    ]
    rows = await expenses(c)
    assert [(r.amount_minor, r.currency, r.spent_on, r.source) for r in rows] == [
        (4500, "GEL", date(2026, 9, 23), "text"),
        (1250, "GEL", date(2026, 9, 23), "text"),
    ]
    await bot.handle_update(msg("потратил 45 лари на такси и 12,5 на кофе", 1))  # the same update again
    assert len(await expenses(c)) == 2
    undo = buttons(c)["↩️ Отменить"]
    await bot.handle_update(tap(undo, 2))
    assert c.telegram.texts()[-1] == "↩️ Отменил: Такси, Кофе"
    assert all(r.deleted_at is not None for r in await expenses(c))
    await bot.handle_update(tap(undo, 3))
    assert c.telegram.answered[-1][1] == "Уже отменено"


@pytest.mark.db
async def test_totals_by_period_category_and_currency(container: Any) -> None:
    c = container
    c.fx_api.per_usd = {"USD": Decimal(1), "GEL": Decimal("2.70"), "RUB": Decimal("84")}
    bot = BotService(c)
    await bot.handle_update(msg("вчера потратил 30 на такси", 10))
    await bot.handle_update(msg("потратил 20 лари на продукты", 11))
    await bot.handle_update(msg("заплатил $10 за подписку claude", 12))
    await bot.handle_update(msg("потратил 100 лари на такси 15.08", 13))
    await bot.handle_update(msg("мои расходы", 14))
    assert c.telegram.texts()[-1].splitlines() == [
        "💸 Сентябрь: 50 ₾ + 10 $ ≈ 77 ₾ · 3 траты",
        "",
        "📊 По категориям",
        "• Транспорт — 30 ₾",
        "• Связь и подписки — 10 $",
        "• Продукты — 20 ₾",
        "",
        "🧾 Последние",
        "• 23.09 Подписку claude — 10 $",
        "• 23.09 Продукты — 20 ₾",
        "• 22.09 Такси — 30 ₾",
    ]
    await bot.handle_update(msg("сколько ушло на такси за 60 дней", 15))
    assert c.telegram.texts()[-1].splitlines()[0] == "💸 26.07–23.09 на «такси»: 130 ₾ · 2 траты"
    await bot.handle_update(msg("/money август", 16))
    assert c.telegram.texts()[-1].startswith("💸 Август: 100 ₾ · 1 трата")


@pytest.mark.db
async def test_expense_is_removed_only_after_the_button(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("потратил 45 лари на такси", 20))
    await bot.handle_update(msg("потратил 8 лари на кофе", 21))
    await bot.handle_update(msg("удали расход такси", 22))
    assert c.telegram.texts()[-1] == "🗑 Удалить расход?\n• 23.09 Такси — 45 ₾"
    assert all(r.deleted_at is None for r in await expenses(c))
    await bot.handle_update(tap(buttons(c)["🗑 Удалить"], 23))
    assert c.telegram.texts()[-1] == "🗑 Удалил расход: Такси"
    await bot.handle_update(msg("удали последний расход", 24))
    assert "• 23.09 Кофе — 8 ₾" in c.telegram.texts()[-1]
    await bot.handle_update(tap(buttons(c)["✖️ Отмена"], 25))
    assert [r.description for r in await expenses(c) if r.deleted_at is None] == ["Кофе"]


@pytest.mark.db
async def test_receipt_photo_is_recorded_after_the_button(container: Any) -> None:
    c = container
    c.telegram.files["f1"] = b"\xff\xd8receipt"
    c.llm.responders["photo_reader"] = lambda _u, _m: {
        "kind": "receipt",
        "label": None,
        "dishes": [],
        "receipt": {"merchant": "Nikora", "total": "45.80", "currency": "GEL", "date": "22.09.2026 18:04",
                    "lines": [{"name": "Хлеб", "amount": "2.50"}, {"name": "Молоко", "amount": "4.20"}]},
        "note": None,
    }  # fmt: skip
    bot = BotService(c)
    update = {
        "update_id": 30,
        "message": {"message_id": 30, "from": {"id": USER}, "chat": {"id": USER, "type": "private"},
                    "photo": [{"file_id": "f1", "file_unique_id": "r1", "file_size": 500}]},
    }  # fmt: skip
    await bot.handle_update(update)
    assert c.telegram.texts()[-1].splitlines() == [
        "🧾 Чек: Nikora — 45,80 ₾",
        "Продукты · вт 22.09",
        "Позиции: Хлеб 2.50 · Молоко 4.20",
        "",
        "Записать в расходы?",
    ]
    assert await expenses(c) == []
    approve = buttons(c)["✅ Записать"]
    await bot.handle_update(tap(approve, 31))
    await bot.handle_update(tap(approve, 32))
    rows = await expenses(c)
    assert [(r.amount_minor, r.merchant, r.category, r.spent_on, r.source) for r in rows] == [
        (4580, "Nikora", "Продукты", date(2026, 9, 22), "receipt")
    ]
    assert rows[0].raw_media_id is not None
    assert c.telegram.texts()[-1].startswith("💸 Записал в расходы · вт 22.09:")


@pytest.mark.db
async def test_unreadable_receipt_total_asks_for_words(container: Any) -> None:
    c = container
    c.telegram.files["f1"] = b"\xff\xd8receipt"
    c.llm.responders["photo_reader"] = lambda _u, _m: {
        "kind": "receipt", "label": None, "dishes": [], "note": None,
        "receipt": {"merchant": "Кафе", "total": None, "currency": None, "date": None, "lines": []},
    }  # fmt: skip
    update = {
        "update_id": 40,
        "message": {"message_id": 40, "from": {"id": USER}, "chat": {"id": USER, "type": "private"},
                    "photo": [{"file_id": "f1", "file_unique_id": "r2", "file_size": 500}]},
    }  # fmt: skip
    await BotService(c).handle_update(update)
    assert c.telegram.texts()[-1].startswith("🧾 Не разобрал итоговую сумму на чеке")
