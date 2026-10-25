"""Photos → food (КБЖУ): a label is copied as printed, a dish is estimated (≈); nothing is written before
«✅ Записать»; the photo goes to the LLM once, as an image. Fakes only."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import RawMedia
from jarvis.integrations.llm.client import RawOutput

USER = 1001
pytestmark = pytest.mark.db
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def photo(update_id: int, caption: str | None = None, *, as_file: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": USER},
        "chat": {"id": USER, "type": "private"},
    }
    if as_file:
        message["document"] = {"file_id": "f1", "file_unique_id": f"u{update_id}", "mime_type": "image/png",
                               "file_size": 2000}  # fmt: skip
    else:
        message["photo"] = [
            {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90, "file_size": 900},
            {"file_id": "f1", "file_unique_id": f"u{update_id}", "width": 1280, "height": 960, "file_size": 2000},
        ]
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


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


LABEL = {
    "kind": "food_label",
    "label": {
        "name": "Протеиновый батончик",
        "kcal_100": 380,
        "protein_100": 30,
        "fat_100": 12,
        "carbs_100": 35,
        "package_g": 60,
        "portion_g": None,
    },
    "dishes": [],
    "receipt": None,
    "note": None,
}


def reads(c: Any, reading: dict[str, Any]) -> None:
    c.telegram.files["f1"] = JPEG
    c.llm.responders["photo_reader"] = lambda _user, _messages: reading


async def test_label_photo_is_logged_as_the_whole_package_after_the_button(container: Any, worker: Any) -> None:
    c = container
    reads(c, LABEL)
    bot = BotService(c)
    await bot.handle_update(photo(1))
    assert c.telegram.texts()[-1].splitlines() == [
        "📷 Этикетка: Протеиновый батончик — 380 ккал · Б 30 · Ж 12 · У 35 на 100 г, упаковка 60 г",
        "Считаю, что съедена вся упаковка; если нет — подпишите фото: «съел 30 г».",
        "",
        "🍽 Записать еду · сегодня, ср 23.09?",
        "• Протеиновый батончик ≈60 г — 228 ккал · Б 18 · Ж 7 · У 21",
        "Итого: 228 ккал · Б 18 · Ж 7 · У 21",
    ]
    assert c.llm.images == [("photo_reader", [("image/jpeg", JPEG)])]  # the largest size, sent as an image
    assert c.lmf_api.food == [] and c.lmf_api.products == {}
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 2))
    await worker.drain()
    assert c.lmf_api.food[0]["name"] == "Протеиновый батончик" and c.lmf_api.food[0]["kcal"] == 228
    product = c.lmf_api.products["протеиновый батончик"]
    assert (product["kcal"], product["portion_g"], product["portion_name"], product["source"]) == (
        380,
        60,
        "упаковка",
        "label",
    )
    async with c.db.session() as s:
        media = (await s.scalars(select(RawMedia))).all()
    assert [(m.kind, m.data) for m in media] == [("photo", JPEG)]


async def test_label_caption_says_how_much_was_eaten(container: Any) -> None:
    c = container
    reads(c, LABEL)
    bot = BotService(c)
    await bot.handle_update(photo(10, "съел половину"))
    assert "• Протеиновый батончик ≈30 г — 114 ккал" in c.telegram.texts()[-1]
    await bot.handle_update(photo(11, "вчера 45 г"))
    text = c.telegram.texts()[-1]
    assert "🍽 Записать еду · вт 22.09?" in text and "• Протеиновый батончик 45 г — 171 ккал" in text
    assert "вся упаковка" not in text


async def test_label_without_weight_is_only_saved_to_the_directory(container: Any, worker: Any) -> None:
    c = container
    reads(c, {**LABEL, "label": {**LABEL["label"], "package_g": None}})
    bot = BotService(c)
    await bot.handle_update(photo(20))
    assert c.telegram.texts()[-1].splitlines()[-1] == (
        "💾 В справочник продуктов: Протеиновый батончик — 380 ккал · Б 30 · Ж 12 · У 35 на 100 г"
    )
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 21))
    await worker.drain()
    assert c.lmf_api.food == []
    assert "протеиновый батончик" in c.lmf_api.products
    assert c.telegram.texts()[-1] == "💾 Сохранил в справочник продуктов: Протеиновый батончик"


async def test_dish_photo_is_estimated_and_the_directory_wins(container: Any) -> None:
    c = container
    c.lmf_api.products["гречка"] = {"id": 1, "key": "гречка", "name": "Гречка", "kcal": 330, "protein": 12,
                                    "fat": 3, "carbs": 62, "portion_g": None, "portion_name": None}  # fmt: skip
    reads(
        c,
        {
            "kind": "dish",
            "label": None,
            "dishes": [
                {
                    "said": "гречка",
                    "name": "Гречка",
                    "grams": 200,
                    "kcal_100": 110,
                    "protein_100": 4,
                    "fat_100": 1,
                    "carbs_100": 21,
                },
                {
                    "said": "котлета",
                    "name": "Котлета куриная",
                    "grams": 100,
                    "kcal_100": 190,
                    "protein_100": 18,
                    "fat_100": 11,
                    "carbs_100": 5,
                },
            ],
            "receipt": None,
            "note": None,
        },
    )
    await BotService(c).handle_update(photo(30, "на обед"))
    assert c.telegram.texts()[-1].splitlines() == [
        "📷 На фото: Гречка, Котлета куриная",
        "",
        "🍽 Записать еду · сегодня, ср 23.09?",
        "• Гречка ≈200 г — 660 ккал · Б 24 · Ж 6 · У 124",
        "• Котлета куриная ≈100 г — 190 ккал · Б 18 · Ж 11 · У 5 ≈ оценка ИИ — сохраню в справочник",
        "Итого: 850 ккал · Б 42 · Ж 17 · У 129",
    ]


async def test_image_sent_as_a_file_is_read_too(container: Any) -> None:
    c = container
    reads(c, LABEL)
    await BotService(c).handle_update(photo(40, "100 г", as_file=True))
    assert c.llm.images[0][1][0][0] == "image/png"
    assert "• Протеиновый батончик 100 г — 380 ккал" in c.telegram.texts()[-1]


async def test_not_food_and_a_broken_reading(container: Any) -> None:
    c = container
    reads(c, {"kind": "other", "label": None, "dishes": [], "receipt": None, "note": "кот на диване"})
    bot = BotService(c)
    await bot.handle_update(photo(50))
    assert c.telegram.texts()[-1] == "📷 Вижу: кот на диване. Я понимаю фото еды, этикеток и чеков."
    c.llm.responders["photo_reader"] = lambda _u, _m: RawOutput("not json", "end_turn")
    await bot.handle_update(photo(51))
    assert "Не смог разобрать фото (ИИ не ответил как надо)" in c.telegram.texts()[-1]


async def test_label_without_calories_asks_for_the_values(container: Any) -> None:
    c = container
    reads(c, {**LABEL, "label": {**LABEL["label"], "kcal_100": None}})
    await BotService(c).handle_update(photo(60))
    assert c.telegram.texts()[-1].startswith("📷 «Протеиновый батончик»: не разобрал калорийность на этикетке.")
