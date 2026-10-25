"""Purchases (2026-10-01): «купить …» goes to the list, purchases leave the tasks, «иду в магазин» shows the list.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import Approval, ShoppingItem
from jarvis.scheduler.queue import enqueue
from jarvis.shopping.parsing import nominative
from jarvis.shopping.service import is_purchase_task

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


def buttons(sent: dict[str, Any]) -> list[tuple[str, str]]:
    markup = sent.get("reply_markup") or {}
    return [(b["text"], b["callback_data"]) for row in markup.get("inline_keyboard", []) for b in row]


async def purchases_in_tasks(c: Any) -> None:
    c.todoist.add(id="FLAT", content="Обустройство квартиры")
    c.todoist.add(id="KNIFE", content="Купить кухонный нож", parent_id="FLAT", due={"date": "2026-09-27"})
    c.todoist.add(id="TABLE", content="Купить стол для балкона и барный стул", parent_id="FLAT")
    c.todoist.add(id="CIGAR", content="Купить подходящую сигару")
    c.todoist.add(id="BED", content="Подобрать и купить решение для работы с ноутбуком в кровати")
    c.todoist.add(id="GYM", content="Купить протеин", due={"date": "2026-09-23", "is_recurring": True})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())


async def names(c: Any, status: str = "open") -> list[str]:
    async with c.db.session() as s:
        rows = await s.scalars(
            select(ShoppingItem.requested_name).where(ShoppingItem.status == status).order_by(ShoppingItem.created_at)
        )
        return list(rows.all())


def test_nominative_and_purchase_titles() -> None:
    assert nominative("подходящую сигару") == "подходящая сигара"
    assert nominative("зубную пасту и колу") == "зубная паста и колу"  # the head only; parse_list splits first
    assert nominative("органайзер для столовых приборов") == "органайзер для столовых приборов"
    assert nominative("2 кг куриного филе") == "2 кг куриного филе"
    assert nominative("Veet Men Kit") == "Veet Men Kit"
    assert is_purchase_task("Купить кухонный нож") and is_purchase_task("докупить батарейки")
    assert not is_purchase_task("Подобрать и купить решение для ноутбука")  # research first
    assert not is_purchase_task("Купить протеин", recurring=True)


@pytest.mark.db
async def test_buy_phrases_go_to_the_list(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("добавь купить сметану и зубную пасту", 1))
    assert c.telegram.texts()[-1] == "🛒 Добавил в покупки: Сметана, Зубная паста"
    await bot.handle_update(msg("купить подарок маме в пятницу", 2))  # with a date it stays a task
    assert ("create", ("Купить подарок маме", "в пятницу")) in c.todoist.calls
    assert await names(c) == ["сметана", "зубная паста"]


@pytest.mark.db
async def test_purchases_move_from_tasks_once(container: Any, worker: Any) -> None:
    c = container
    await purchases_in_tasks(c)
    bot = BotService(c)
    await bot.handle_update(msg("перенеси покупки из задач", 10))
    text = c.telegram.texts()[-1]
    assert text.startswith("🛒 Перенести покупки из задач в список покупок (3)?")
    assert "• Купить кухонный нож" in text and "• Купить подходящую сигару" in text
    assert "решение для работы" not in text and "протеин" not in text  # research and a recurring task stay
    approve = buttons(c.telegram.sent[-1])[0][1]
    assert approve.startswith("ap:a:")
    await bot.handle_update(tap(approve, 11))
    await worker.drain()
    assert await names(c) == [
        "кухонный нож (Обустройство квартиры)",
        "стол для балкона (Обустройство квартиры)",
        "барный стул (Обустройство квартиры)",
        "подходящая сигара",
    ]
    assert {("close", "KNIFE"), ("close", "TABLE"), ("close", "CIGAR")} <= set(c.todoist.calls)
    assert ("close", "BED") not in c.todoist.calls
    assert "🛒 Перенесено в покупки: 3" in c.telegram.texts()[-1]
    # The job runs again: nothing is added twice.
    async with c.db.session() as s:
        approval = await s.scalar(select(Approval).where(Approval.kind == "task_op"))
        assert approval is not None
        await enqueue(s, job_type="tasks.apply_op", run_at=c.clock.now(), payload={"approval_id": str(approval.id)})
    await worker.drain()
    assert len(await names(c)) == 4
    # The knife task is reopened in Todoist and moved again: its item is already on the list.
    await c.todoist.reopen_task("KNIFE", "reopen-1")
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    await bot.handle_update(msg("перенеси покупки из задач", 12))
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])[0][1], 13))
    await worker.drain()
    assert len(await names(c)) == 4
    assert c.todoist.calls.count(("close", "KNIFE")) == 2


@pytest.mark.db
async def test_going_to_the_store_shows_the_list(container: Any) -> None:
    c = container
    await purchases_in_tasks(c)
    bot = BotService(c)
    await bot.handle_update(msg("добавь молоко, хлеб и 2 кг куриного филе", 20))
    await bot.handle_update(msg("Джарвис, я иду в магазин", 21))
    sent = c.telegram.sent[-1]
    text = c.telegram.texts()[-1]
    assert text.startswith("🛒 Не забудьте купить · 3\n\n• Молоко\n• Хлеб\n• 2 кг куриного филе — 2 кг")
    assert "📋 Ещё покупки в задачах: Купить кухонный нож" in text
    labels = [label for label, _ in buttons(sent)]
    assert labels == ["✅ Молоко", "✅ Хлеб", "✅ 2 кг куриного филе", "🧹 Купил всё"]
    milk = buttons(sent)[0][1]
    await bot.handle_update(tap(milk, 22, message_id=sent["message_id"]))
    edit = c.telegram.edits[-1]
    assert edit["message_id"] == sent["message_id"]
    assert c.telegram.edited_texts()[-1].startswith("🛒 Осталось купить · 2\n\n• Хлеб")
    assert edit["parse_mode"] == "HTML"
    await bot.handle_update(tap(milk, 23))  # a second tap changes nothing
    assert c.telegram.answered[-1] == ("cb23", "Уже отмечено")
    await bot.handle_update(tap("sb:all", 24))
    assert c.telegram.edited_texts()[-1] == "🎉 Всё из списка куплено."
    assert await names(c, "bought") == ["молоко", "хлеб", "2 кг куриного филе"]
    await bot.handle_update(msg("что купить?", 25))
    assert c.telegram.texts()[-1].startswith("📋 Ещё покупки в задачах:")


@pytest.mark.db
async def test_morning_briefing_offers_to_move_purchases(container: Any, worker: Any) -> None:
    c = container
    await purchases_in_tasks(c)
    async with c.db.session() as s:
        await c.shopping.add_items(s, "молоко", c.clock.now())
        await enqueue(s, job_type="briefing.morning", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    brief = next(m for m in c.telegram.sent if m["text"].startswith("☀️"))
    assert "🛒 В списке покупок: 1 — скажите «иду в магазин», и я покажу список." in brief["text"]
    assert "🛒 В задачах есть покупки (3): Купить кухонный нож, Купить стол для балкона и барный стул" in brief["text"]
    assert ("🛒 Перенести в покупки (3)", buttons(brief)[-1][1]) == buttons(brief)[-1]


@pytest.mark.db
async def test_listings_are_matched_by_the_model_once(container: Any) -> None:
    # L14 (review 17.10): up to 20 model calls in a row, and the same ones again on the next quote.
    from jarvis.shopping.providers import ImportDocument

    c = container
    asked: list[str] = []

    def verdict(user: str, messages: Any) -> dict[str, Any]:
        asked.append(user)
        return {"same_product": "<wanted>milk</wanted>" in user and "Sante" in user, "confidence": 0.9}

    c.llm.responders["product_matcher"] = verdict
    offer = {"store_key": "a", "package_quantity": "1", "package_unit": "l", "price": "4.50"}
    eggs = {**offer, "product_name": "Eggs 10", "package_unit": "pcs"}
    doc = ImportDocument.model_validate({
        "stores": [{"store_key": "a", "name": "Store A", "delivery_fee": "3.99", "service_fee": "1.00"}],
        "offers": [{**offer, "product_name": "Sante 3.2% 1L"}, eggs],
    })  # fmt: skip
    now = c.clock.now()
    async with c.db.session() as s:
        await c.shopping.add_items(s, "молоко, яйца", now)
        await c.shopping.import_offers(s, doc, now)
    async with c.db.session() as s:
        _, offers, _, _ = await c.shopping.build_inputs(s, now)
    assert {o.product_name: o.key for o in offers} == {"Sante 3.2% 1L": "milk", "Eggs 10": "eggs"}
    assert asked == ["<wanted>eggs</wanted>\n<listing>Sante 3.2% 1L</listing>",
                     "<wanted>milk</wanted>\n<listing>Sante 3.2% 1L</listing>"]  # fmt: skip
    async with c.db.session() as s:
        _, again, _, _ = await c.shopping.build_inputs(s, now)
    assert len(asked) == 2 and {o.key for o in again} == {"milk", "eggs"}  # the next quote: from the cache
