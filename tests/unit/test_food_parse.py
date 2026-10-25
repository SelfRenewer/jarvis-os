"""Food phrases, nutrition values and estimate checks — by code."""

from __future__ import annotations

import pytest

from jarvis.bot.intents import Intent, keyword_route
from jarvis.tracker.food import FoodEstimateItem, find_product, meal_of, parse_nutrition, plausible, split_food


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("съел 200 г гречки и 2 яйца", [(["гречки"], 200.0, None), (["яйца"], None, 2.0)]),
        ("тарелку борща", [(["борща"], None, 1.0)]),
        ("съел пол тарелки супа", [(["супа"], None, 0.5)]),
        ("на обед: курица 150, рис 200", [(["курица"], 150.0, None), (["рис"], 200.0, None)]),
        ("съел 0,5 кг клубники", [(["клубники"], 500.0, None)]),
        ("перекусил бананом", [(["бананом"], None, None)]),  # «бан…» is not «банка»
        ("выпил стакан молока", [(["молока"], None, 1.0)]),
    ],
)
def test_split_food(text: str, parsed: list[tuple[list[str], float | None, float | None]]) -> None:
    assert [(p.words, p.grams, p.count) for p in split_food(text)] == parsed


def test_meal() -> None:
    assert meal_of("на обед: курица 150") == "обед"
    assert meal_of("поужинал пастой") == "ужин"
    assert meal_of("съел яблоко") is None


def test_nutrition_values() -> None:
    per100 = parse_nutrition("330 ккал, Б 12, Ж 3, У 62")
    assert (per100.kcal, per100.protein, per100.fat, per100.carbs) == (330, 12, 3, 62)
    words = parse_nutrition("157 ккал, белка 12.7 жиров 10.9 углеводов 0.7")  # word first, then the number
    assert (words.protein, words.fat, words.carbs) == (12.7, 10.9, 0.7)
    after = parse_nutrition("120 ккал, 18 г белка, 5 г жира, 3 г углеводов")
    assert (after.protein, after.fat, after.carbs) == (18, 5, 3)
    can = parse_nutrition("банка 500 мл, 225 ккал на банку, У 55 на банку")  # per portion → per 100 g
    assert (can.kcal, can.carbs, can.portion_g, can.portion_name) == (45, 11, 500, "банка")


def test_find_product() -> None:
    products = [{"id": 1, "name": "Гречка"}, {"id": 2, "name": "Куриная грудка"}, {"id": 3, "name": "Яйцо"}]
    assert find_product(["гречки"], products) == products[0]
    assert find_product(["куриную", "грудку"], products) == products[1]
    assert find_product(["гречку", "отварную", "с", "маслом"], products) is None  # a different dish


def test_plausible_estimates() -> None:
    good = FoodEstimateItem(said="борщ", name="Борщ", grams=300, kcal_100=50, protein_100=2.5, fat_100=2.5, carbs_100=5)
    bad = FoodEstimateItem(
        said="пирожок", name="Пирожок", grams=80, kcal_100=900, protein_100=1, fat_100=1, carbs_100=1
    )
    assert plausible(good) and not plausible(bad)


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("съел 200 г гречки и 2 яйца", Intent.FOOD_LOG),
        ("на обед борщ", Intent.FOOD_LOG),
        ("сколько я сегодня съел?", Intent.FOOD_SHOW),
        ("КБЖУ за вчера", Intent.FOOD_SHOW),
        ("гречка — 330 ккал, Б 12, Ж 3, У 62", Intent.FOOD_PRODUCT),
        ("яйцо — 1 шт 55 г", Intent.FOOD_PRODUCT),
        ("энергетик — 160 мг кофеина", Intent.MED_PORTION),  # caffeine, not calories
        ("норма 2200 ккал, белок 140", Intent.FOOD_TARGETS),
        ("напомни на обед позвонить маме", Intent.REMIND),
    ],
)
def test_routing(text: str, intent: Intent) -> None:
    assert keyword_route(text).intent == intent
