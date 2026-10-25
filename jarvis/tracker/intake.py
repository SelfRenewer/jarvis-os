"""Pills and food in one phrase: «выпил магний 400 и стакан кефира», «съел 2 яйца и выпил D3 6000»,
«утром выпил D3. На завтрак овсянка 200 г и кофе» → one draft with supplements and food.

The phrase is cut at its verbs: after «съел» / «на завтрак:» comes food, after «выпил» / «принял» — pills (a drink
may be either). A name from the LMF supplement dictionary is a supplement anywhere, and so is a dose in мг/мкг/МЕ;
in a «выпил» part a glass, grams or a product of the food directory make it food. Everything here is code, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from jarvis.core.numwords import spoken_numbers
from jarvis.tracker.food import find_product, split_food
from jarvis.tracker.supplements import TAKEN_VERB, TAKEN_WHEN, Supplement, Taken, resolve, split_items

Kind = Literal["pill", "food"]

_FOOD_VERB = (
    r"(?:съел[аи]?|поел[аи]?|скушал[аи]?|слопал[аи]?|перекусил[аи]?|позавтракал[аи]?|пообедал[аи]?|поужинал[аи]?"
    r"|на\s+(?:завтрак|обед|ужин|перекус|полдник)\w*\s*[:\-—]?|(?:завтрак|обед|ужин|перекус|полдник)\w*\s*[:\-—])"
)
_CLAUSE = re.compile(rf"(?<![\w-])(?:(?P<food>{_FOOD_VERB})|(?P<pill>{TAKEN_VERB}))(?![\w-])", re.IGNORECASE)
_WHEN = re.compile(rf"(?<![\w-])(?:я|{TAKEN_WHEN})(?![\w-])", re.IGNORECASE)
# What only food is measured in: grams, kilograms, millilitres, litres; and the dishes it is served in.
_FOOD_AMOUNT = re.compile(r"\d\s*(?:кг|г|гр|грамм\w*|мл|л|литр\w*)(?![\w])|(?<![\w])(?:кило|литр)\w*", re.IGNORECASE)
_FOOD_PORTION = re.compile(
    r"(?<![\w])(?:тарел\w*|порци\w*|стакан\w*|чашк\w*|чашек|кружк\w*|кус(?:ок|ка|очек|очка)|ломтик\w*|бутылк\w*"
    r"|пачк\w*|миск\w*)(?![\w])",
    re.IGNORECASE,
)
_PILL_UNITS = ("мг", "мкг", "МЕ")


@dataclass
class Intake:
    taken: list[Taken] = field(default_factory=list)  # supplements, drinks as caffeine, unknown pills
    food: list[str] = field(default_factory=list)  # food phrases as said, for the food parser


def _is_food(said: str, products: list[dict[str, Any]]) -> bool:
    if _FOOD_AMOUNT.search(said) or _FOOD_PORTION.search(said):
        return True
    phrases = split_food(said)
    return bool(phrases) and find_product(phrases[0].words, products) is not None


def split_intake(
    text: str, supplements: list[Supplement], portions: dict[str, Any], products: list[dict[str, Any]], kind: Kind
) -> Intake:
    """`kind`: what the message is about before any verb (routed as «выпил …» → pill, «съел …» → food)."""
    body = _WHEN.sub(" ", spoken_numbers(text))
    clauses: list[tuple[Kind, str]] = []
    start, current = 0, kind
    for m in _CLAUSE.finditer(body):
        clauses.append((current, body[start : m.start()]))
        current = "food" if m.group("food") else "pill"
        start = m.end()
    clauses.append((current, body[start:]))
    out = Intake()
    for clause_kind, clause in clauses:
        for phrase in split_items(clause):
            taken = resolve(phrase, supplements, portions)
            # a drink whose caffeine is not set yet is asked about after «выпил», not in a meal («на ужин суп и чай»)
            asks = taken.needs_portion is not None and clause_kind == "pill"
            if taken.supplement is not None or asks or taken.candidates or phrase.unit in _PILL_UNITS:
                out.taken.append(taken)
            elif clause_kind == "food" or _is_food(phrase.said, products):
                out.food.append(phrase.said)
            else:
                out.taken.append(taken)  # not known: the pill flow asks or offers to add it to LMF
    return out
