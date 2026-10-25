"""Supplement names, doses and units → LMF, by code; packs; caffeine portions."""

from __future__ import annotations

from datetime import time

import pytest

from jarvis.bot.intents import Intent, keyword_route, route_message
from jarvis.integrations.llm.client import FakeLLM
from jarvis.integrations.lmf.client import FakeLifeTracker
from jarvis.meds.service import parse_pack_command
from jarvis.tracker.service import merge_supplements
from jarvis.tracker.supplements import has_dose, resolve, split_items, supplements_from

SUPPS = supplements_from(FakeLifeTracker().data)
PORTIONS = {"энергетик": {"supplement": "Caffeine", "dose": 160}}


def taken(text: str) -> list[str]:
    out = []
    for phrase in split_items(text):
        t = resolve(phrase, SUPPS, PORTIONS)
        if t.supplement is not None:
            out.append(t.text())
        elif t.needs_portion:
            out.append(f"? {t.needs_portion}")
        else:
            out.append(f"! {t.said}")
    return out


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("D3 6000, B12 500, цинк 25", ["Витамин D3 6000 МЕ", "Витамин B12 500 мкг", "Цинк 25 мг"]),
        ("витамин Д3", ["Витамин D3 4000 МЕ"]),  # Cyrillic look-alike, LMF default dose
        ("магний 300 мг и глицин", ["Магний 300 мг", "Глицин 0,2 г"]),
        ("кофеин 0,2 г", ["Кофеин 200 мг"]),  # converted to the LMF unit; the comma is a decimal
        ("5 г креатина", ["Креатин 5 г"]),
        ("омега 3", ["Омега-3"]),  # the number belongs to the name
        ("омегу 2 капсулы", ["Омега-3 (2 шт)"]),
        ("выпил 2 энергетика", ["Кофеин 320 мг (2 × энергетик)"]),
        ("кофе", ["? кофе"]),  # a drink: its caffeine is asked, never guessed
        ("ашваганду", ["! ашваганду"]),
        # dictated (found live 2026-10-14: units and numbers in words, a dose after a comma, several sentences)
        (
            "витамин Д3 6000 единиц, B12 500 микрограмм, цинк 25 миллиграмм и магний 400 миллиграмм",
            ["Витамин D3 6000 МЕ", "Витамин B12 500 мкг", "Цинк 25 мг", "Магний 400 мг"],
        ),
        ("цинк двадцать пять миллиграмм и магний четыреста", ["Цинк 25 мг", "Магний 400 мг"]),
        ("D3 шесть тысяч международных единиц", ["Витамин D3 6000 МЕ"]),
        ("D3 5 000 МЕ", ["Витамин D3 5000 МЕ"]),
        ("омегу-3, две капсулы", ["Омега-3 (2 шт)"]),
        ("омегу-3 две капсулы", ["Омега-3 (2 шт)"]),
        ("магний, 400 мг", ["Магний 400 мг"]),
        ("креатин пять грамм", ["Креатин 5 г"]),
        ("пару капсул омеги", ["Омега-3 (2 шт)"]),
        (
            "витамин D3 5000 единиц. Ещё омегу-3, две капсулы. И магний 400 миллиграмм.",
            ["Витамин D3 5000 МЕ", "Омега-3 (2 шт)", "Магний 400 мг"],
        ),
        ("Vit. D3 6000. B12 500 мкг", ["Витамин D3 6000 МЕ", "Витамин B12 500 мкг"]),  # «Vit.» is not a sentence end
        ("я сегодня утром выпил цинк 25", ["Цинк 25 мг"]),
    ],
)
def test_resolve(text: str, result: list[str]) -> None:
    assert taken(text) == result


def test_merge_adds_doses_and_keeps_the_rest() -> None:
    current = [
        {"supplement_id": 2, "dose": 160.0, "dose_text": None},
        {"supplement_id": 16, "dose": 600.0, "dose_text": "ЭПК 600мг"},
    ]
    merged = merge_supplements(current, [{"id": 2, "dose": 160.0}, {"id": 12, "dose": 6000.0}], [16])
    assert merged == [
        {"supplement_id": 2, "dose": 320.0, "dose_text": None},
        {"supplement_id": 12, "dose": 6000.0, "dose_text": None},
    ]


def test_pack_commands() -> None:
    cmd = parse_pack_command("утренние таблетки: D3 6000, B12 500, цинк 25 в 9:00")
    assert cmd is not None and (cmd.action, cmd.name, cmd.items, cmd.times) == (
        "set", "Утренние", "D3 6000, B12 500, цинк 25", [time(9, 0)],
    )  # fmt: skip
    night = parse_pack_command("таблетки на ночь: мелатонин 2 мг")
    assert night is not None and (night.name, night.default_at) == ("На ночь", time(22, 30))
    add = parse_pack_command("добавь в утренние таблетки омегу 1000")
    assert add is not None and (add.action, add.items) == ("add", "омегу 1000")
    when = parse_pack_command("утренние таблетки в 8:30")
    assert when is not None and (when.action, when.times) == ("time", [time(8, 30)])


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("утренние таблетки: D3 6000, B12 500", Intent.MED_PACK),
        ("утром пью D3 и омегу", Intent.MED_PACK),
        ("добавь в утренние таблетки омегу", Intent.MED_PACK),  # not a subtask of a task «утренние…»
        ("убери из утренних цинк", Intent.MED_PACK),
        ("выпил утренние кроме цинка", Intent.MED_TAKEN),
        ("выпил 2 энергетика", Intent.MED_TAKEN),
        ("энергетик — 160 мг кофеина", Intent.MED_PORTION),
        ("в энергетике 160 мг кофеина", Intent.MED_PORTION),
        ("напоминай пить омегу-3 в 9:00 и 21:00", Intent.MED_ADD),
        ("спал 7:30, кофеин 200", Intent.TRACKER_LOG),
    ],
)
def test_routing(text: str, intent: Intent) -> None:
    assert keyword_route(text).intent == intent


@pytest.mark.parametrize(
    ("text", "dose"),
    [
        ("Ещё омегу-3, две капсулы", True),
        ("И магний 400 миллиграмм", True),
        ("позвонить маме в 5", False),  # a number without a unit is not a dose
        ("Ещё теанин 200", False),
        ("купить хлеб", False),
    ],
)
def test_has_dose(text: str, dose: bool) -> None:
    assert has_dose(text) is dose


@pytest.mark.parametrize(
    ("text", "what", "day"),
    [
        (
            "я сегодня утром выпил витамин Д3 6000 единиц, B12 500 микрограмм",
            "витамин Д3 6000 единиц, B12 500 микрограмм",
            None,
        ),
        ("вчера выпил магний 400", "магний 400", "вчера"),
        ("утром принял креатин 5 г", "креатин 5 г", None),
        ("после завтрака выпила омегу", "омегу", None),
    ],
)
def test_taken_with_when_words(text: str, what: str, day: str | None) -> None:
    routed = keyword_route(text)
    assert (routed.intent, routed.task_text, routed.date_text) == (Intent.MED_TAKEN, what, day)


async def test_sentences_with_doses_continue_the_list_without_the_llm() -> None:
    llm = FakeLLM()
    actions = await route_message(
        "Выпил витамин D3 5000 единиц. Ещё омегу-3, две капсулы. И магний 400 миллиграмм.", llm
    )
    assert [(a.intent, a.task_text) for a in actions] == [
        (Intent.MED_TAKEN, "витамин D3 5000 единиц; Ещё омегу-3, две капсулы; И магний 400 миллиграмм")
    ]
    assert llm.calls == []


async def test_a_sentence_without_a_dose_is_not_a_supplement() -> None:
    actions = await route_message("Выпил D3 6000. Позвонить маме в 5", None)
    assert [a.intent for a in actions] == [Intent.MED_TAKEN, Intent.UNKNOWN]
    assert actions[0].task_text == "D3 6000"


async def test_llm_sentences_become_one_list() -> None:
    llm = FakeLLM(
        responders={
            "intent_router": lambda *_: {
                "actions": [
                    {"intent": "med_taken", "task_text": "витамин D3 5000 единиц"},
                    {"intent": "med_taken", "task_text": "теанин 200"},
                    {"intent": "show_today"},
                ]
            }
        }
    )
    actions = await route_message("Выпил витамин D3 5000 единиц. Ещё теанин 200. Что у меня сегодня?", llm)
    assert [(a.intent, a.task_text) for a in actions] == [
        (Intent.MED_TAKEN, "витамин D3 5000 единиц; выпил теанин 200"),
        (Intent.SHOW_TODAY, None),
    ]
