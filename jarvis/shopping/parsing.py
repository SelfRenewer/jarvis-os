"""Deterministic shopping-list parsing and product-key normalization.

Examples handled:
  "яйца, молоко и 2 кг куриного филе"  → eggs; milk; chicken fillet 2 kg
  "Milk 2 × 1L"                         → milk 2 L (2 packages of 1 L)
  "Eggs 20"                             → eggs 20 pcs
  "Chicken fillet 2kg"                  → chicken fillet 2 kg
The LLM may help with odd phrasing, but quantities and units are always parsed/validated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from jarvis.core.units import Quantity, UnitError, lookup_unit

_UNIT = r"(?:кг|килограмм[а-я]*|кило|г|гр|грамм[а-я]*|л|литр[а-я]*|мл|шт|штук[а-я]*|kg|g|gr|l|lt|ml|pcs|pc)\.?"
_NUM = r"\d+(?:[.,]\d+)?"
_LEADING = re.compile(rf"^(?P<qty>{_NUM})\s*(?P<unit>{_UNIT})?\s+(?P<name>.+)$", re.IGNORECASE)
_MULTI = re.compile(rf"^(?P<name>.+?)\s+(?P<count>\d+)\s*[x×х*]\s*(?P<size>{_NUM})\s*(?P<unit>{_UNIT})$", re.IGNORECASE)
_TRAILING = re.compile(rf"^(?P<name>.+?)\s+(?P<qty>{_NUM})\s*(?P<unit>{_UNIT})?$", re.IGNORECASE)
_SPLIT = re.compile(r"\s*(?:,|;|\n|\s+и\s+|\s+and\s+)\s*", re.IGNORECASE)
_COMMAND = re.compile(
    r"^(?:(?:добавь|добавить)\s+(?:в\s+(?:список|покупки)\s+)?)?(?:(?:нужно|надо)\s+)?"
    r"(?:(?:купить|докупить|закупить|прикупить|купи|add|buy)\b)?\s*[:,-]?\s*",
    re.IGNORECASE,
)

# product key → keyword groups (all words of one group must be present, stems allowed)
PRODUCT_KEYS: list[tuple[str, tuple[str, ...]]] = [
    ("chicken fillet", ("кур", "филе")),
    ("chicken fillet", ("chicken", "fillet")),
    ("chicken fillet", ("chicken", "breast")),
    ("milk", ("молок",)),
    ("milk", ("milk",)),
    ("eggs", ("яйц",)),
    ("eggs", ("яйк",)),
    ("eggs", ("egg",)),
    ("coca-cola", ("кока", "кол")),
    ("coca-cola", ("coca", "cola")),
    ("coca-cola", ("coke",)),
    ("bread", ("хлеб",)),
    ("bread", ("bread",)),
    ("butter", ("масл", "сливоч")),
    ("butter", ("butter",)),
    ("cheese", ("сыр",)),
    ("cheese", ("cheese",)),
    ("water", ("вод",)),
    ("water", ("water",)),
]


def product_key(name: str) -> str:
    lowered = name.lower().replace("ё", "е")
    for key, words in PRODUCT_KEYS:
        if all(w in lowered for w in words):
            return key
    cleaned = re.sub(r"[^\w\s-]", "", lowered).strip()
    return re.sub(r"\s+", " ", cleaned)[:120] or "unknown"


@dataclass(frozen=True)
class ParsedItem:
    requested_name: str
    name: str
    quantity: Decimal | None
    unit: str | None
    key: str

    def base_quantity(self) -> Quantity | None:
        if self.quantity is None:
            return None
        unit = self.unit or "pcs"
        try:
            return Quantity.of(self.quantity, unit)
        except UnitError:
            return None


def _dec(value: str) -> Decimal:
    return Decimal(value.replace(",", "."))


def _norm_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    unit = unit.lower().rstrip(".")
    try:
        lookup_unit(unit)
        return unit
    except UnitError:
        for prefix, canonical in (("килограмм", "кг"), ("грамм", "г"), ("литр", "л"), ("штук", "шт")):
            if unit.startswith(prefix):
                return canonical
    return None


_PREPOSITIONS = {"для", "в", "во", "на", "с", "со", "из", "к", "по", "от", "без", "под", "над", "и", "или"}
_CONSONANT_U = re.compile(r"[бвгджзклмнпрстфхцчшщ]у$")


def nominative(phrase: str) -> str:
    """«подходящую сигару» → «подходящая сигара», «зубную пасту» → «зубная паста» (after «купить/добавь»).

    Only the head of the phrase changes: leading adjectives in -ую/-юю and the first noun in -у/-ию; words after a
    preposition or a number, and Latin names, stay as said."""
    words = phrase.split(" ")
    for i, word in enumerate(words):
        low = word.lower()
        if not re.fullmatch(r"[а-яё-]+", low) or low in _PREPOSITIONS:
            break
        if low.endswith(("ую", "юю")):
            words[i] = word[:-2] + ("ая" if low.endswith("ую") else "яя")
            continue
        if _CONSONANT_U.search(low):
            words[i] = word[:-1] + "а"
        elif low.endswith("ию"):
            words[i] = word[:-1] + "я"
        break  # the first noun ends the head
    return " ".join(words)


def parse_item(fragment: str) -> ParsedItem | None:
    text = nominative(fragment.strip().strip(".").strip())
    if not text:
        return None
    if m := _MULTI.match(text):
        count = _dec(m["count"])
        size = _dec(m["size"])
        unit = _norm_unit(m["unit"])
        name = m["name"].strip()
        return ParsedItem(text, name, count * size, unit, product_key(name))
    if m := _LEADING.match(text):
        name = m["name"].strip()
        return ParsedItem(text, name, _dec(m["qty"]), _norm_unit(m["unit"]) or "шт", product_key(name))
    if m := _TRAILING.match(text):
        name = m["name"].strip()
        return ParsedItem(text, name, _dec(m["qty"]), _norm_unit(m["unit"]) or "шт", product_key(name))
    return ParsedItem(text, text, None, None, product_key(text))


def parse_list(text: str) -> list[ParsedItem]:
    body = _COMMAND.sub("", text.strip())
    items = [parse_item(part) for part in _SPLIT.split(body)]
    return [item for item in items if item is not None]
