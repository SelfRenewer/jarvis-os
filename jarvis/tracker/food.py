"""Food for TLT (КБЖУ): what was eaten → grams and KBJU, by code; the LLM only estimates unknown foods.

«съел 200 г гречки и 2 яйца» · «тарелку борща» · «на обед курица 150 г» · «гречка — 330 ккал, Б 12, Ж 3, У 62».
KBJU = grams × values per 100 g from the user's product directory in LMF. A product that is not there yet is
estimated by the LLM (marked «≈», checked for plausibility), shown before writing and saved to the directory only
after «✅ Записать» — so the next time the same food gives the same numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from jarvis.tracker.parse import same_word

MACROS = ("kcal", "protein", "fat", "carbs")
_VERBS = re.compile(
    r"^\s*(?:я\s+)?(?:уже\s+|ещё\s+|еще\s+|только\s+что\s+)?"
    r"(?:съел[аи]?|поел[аи]?|скушал[аи]?|слопал[аи]?|перекусил[аи]?|позавтракал[аи]?|пообедал[аи]?|поужинал[аи]?"
    r"|выпил[аи]?|ел[аи]?)\b\s*",
    re.IGNORECASE,
)
_MEAL = re.compile(r"\b(?:на\s+)?(?P<m>завтрак|обед|ужин|перекус|полдник)\w*\s*[:\-—]?\s*", re.IGNORECASE)
_MEAL_VERB = {"позавтракал": "завтрак", "пообедал": "обед", "поужинал": "ужин", "перекусил": "перекус"}
_GRAMS = re.compile(
    r"(?<![\w.,])(?P<n>\d+(?:[.,]\d+)?)\s*(?P<u>кг|килограмм\w*|г\b|гр\b|грамм\w*|мл\b|л\b|литр\w*)", re.I
)
_NUMBER = re.compile(r"(?<![\w.,])(?P<n>\d+(?:[.,]\d+)?)(?![\w.,])")
_COUNT_WORDS = {
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "пару": 2,
    "полторы": 1.5, "полтора": 1.5, "половину": 0.5, "пол": 0.5,
}  # fmt: skip
# Portion words: «тарелку борща», «стакан молока», «банку энергетика» — a count of the product's portion.
_PORTIONS = (
    r"(?:тарел(?:ка|ку|ки|ок)|порци[яиюй]|стакан\w{0,2}|банк[аиуе]|банок|чашк[аиуе]|чашек|кружк[аиуе]|кус(?:ок|ка|очек|очка)"
    r"|ломтик\w{0,2}|ложк[аиуе]|ложек|бутылк[аиуе]|пачк[аиуе]|шт\.?|штук[аиу]?)"
)
_PORTION = re.compile(rf"(?<![\w])(?:пол\s*)?{_PORTIONS}(?![\w])", re.IGNORECASE)
_FILLER = {"и", "с", "со", "на", "в", "немного", "чуть", "ещё", "еще", "всего", "грамм", "примерно", "около"}


@dataclass
class FoodPhrase:
    said: str
    words: list[str]
    grams: float | None = None
    count: float | None = None  # «2 яйца», «тарелку»
    portion_word: str | None = None


def meal_of(text: str) -> str | None:
    low = text.lower()
    for verb, meal in _MEAL_VERB.items():
        if re.search(rf"\b{verb}", low):
            return meal
    m = _MEAL.search(low)
    return m.group("m") if m else None


def split_food(text: str) -> list[FoodPhrase]:
    """«съел 200 г гречки, 2 яйца и тарелку борща» → phrases with grams / counts."""
    body = _VERBS.sub("", text.strip())
    body = _MEAL.sub(" ", body)
    phrases = []
    for part in re.split(r"(?<!\d),|,(?!\d)|;|\s+и\s+|\s+плюс\s+|\s+а\s+ещё\s+|\n", body):
        said = part.strip(" .!?:—-")
        if not said:
            continue
        phrase = FoodPhrase(said, [])
        rest = said
        if m := _GRAMS.search(rest):
            value = float(m.group("n").replace(",", "."))
            unit = m.group("u").lower()
            phrase.grams = value * 1000 if unit.startswith(("кг", "кил", "л")) and unit != "мл" else value
            rest = rest[: m.start()] + " " + rest[m.end() :]
        if m := _PORTION.search(rest):
            phrase.portion_word = m.group(0).lower()
            phrase.count = 0.5 if phrase.portion_word.startswith("пол") else 1.0
            rest = rest[: m.start()] + " " + rest[m.end() :]
        for token in re.findall(r"[^\s]+", rest):
            low = token.lower().strip(",.()")
            if (number := _NUMBER.fullmatch(low)) and phrase.grams is None:
                value = float(number.group("n").replace(",", "."))
                if value >= 10:  # «курица 150» — grams; «2 яйца» — pieces
                    phrase.grams = value
                else:
                    phrase.count = value * (phrase.count or 1)
            elif low in _COUNT_WORDS:
                phrase.count = _COUNT_WORDS[low] * (phrase.count or 1)
            elif low and low not in _FILLER and re.search(r"[a-zа-яё]", low):
                phrase.words.append(low)
        if phrase.words:
            phrases.append(phrase)
    return phrases


def product_key(name: str) -> str:
    return " ".join(str(name).strip().lower().replace("ё", "е").split())


def find_product(words: list[str], products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The product named by the words («гречки» → Гречка, «куриную грудку» → Куриная грудка); None if unsure."""
    said = [w.replace("ё", "е") for w in words]
    full, partial = [], []
    for product in products:
        names = [w for w in re.findall(r"[a-zа-я0-9]+", product_key(product["name"])) if len(w) >= 2]
        if not names:
            continue
        covers_said = all(any(same_word(w, n) for n in names) for w in said)
        covers_name = all(any(same_word(n, w) for w in said) for n in names)
        if covers_said and covers_name:
            full.append(product)
        elif covers_name:  # «гречка отварная» said as «гречку отварную с маслом» is not «гречка»
            partial.append(product)
    if len(full) == 1:
        return full[0]
    return None if full or len(partial) != 1 else (partial[0] if len(said) <= 2 else None)


# --- nutrition values said by the user ------------------------------------------------------------------------

_KCAL = re.compile(r"(?P<n>\d+(?:[.,]\d+)?)\s*(?:ккал|кал\b|kcal|калори\w*)", re.IGNORECASE)
_MACRO_WORDS = {
    "protein": r"\bб\b|белк\w*|белок|protein",
    "fat": r"\bж\b|жир\w*|fat",
    "carbs": r"\bу\b|углевод\w*|carbs?",
}
# «белок 12» is read before «12 г белка»: with both forms in one phrase the word-first reading is the intended one.
_MACRO_AFTER = {k: re.compile(rf"(?:{w})\s*[:\-—]?\s*(?P<n>\d+(?:[.,]\d+)?)", re.I) for k, w in _MACRO_WORDS.items()}
_MACRO_BEFORE = {k: re.compile(rf"(?P<n>\d+(?:[.,]\d+)?)\s*г?\s*(?:{w})", re.I) for k, w in _MACRO_WORDS.items()}
_PORTION_SIZE = re.compile(
    rf"(?:1\s*)?(?P<name>{_PORTIONS})\s*(?:[=—:\-]\s*|это\s+|по\s+)?(?P<g>\d+(?:[.,]\d+)?)\s*(?:г|гр|грамм\w*|мл)\b",
    re.IGNORECASE,
)
_PER_PORTION = re.compile(rf"\b(?:на|в|за)\s+(?:1\s+|одн\w+\s+)?{_PORTIONS}", re.IGNORECASE)


@dataclass
class Nutrition:
    kcal: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    portion_g: float | None = None
    portion_name: str | None = None

    def per100(self) -> dict[str, float | None]:
        return {k: getattr(self, k) for k in MACROS}


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def parse_nutrition(text: str) -> Nutrition:
    """«330 ккал, Б 12, Ж 3, У 62» (per 100 g) · «банка 500 мл, 225 ккал на банку» (per portion → per 100 g)."""
    out = Nutrition()
    value = text
    if m := _PORTION_SIZE.search(value):
        out.portion_g = _num(m.group("g"))
        out.portion_name = _portion_name(m.group("name"))
        value = value[: m.start()] + " " + value[m.end() :]
    if m := _KCAL.search(value):
        out.kcal = _num(m.group("n"))
    for name in MACROS[1:]:
        if m := _MACRO_AFTER[name].search(value) or _MACRO_BEFORE[name].search(value):
            setattr(out, name, _num(m.group("n")))
    if _PER_PORTION.search(value):  # the numbers are for one portion
        size = out.portion_g or 100.0
        out.portion_g = size
        for name in MACROS:
            if (v := getattr(out, name)) is not None:
                setattr(out, name, round(v / size * 100, 2))
    return out


def _portion_name(word: str) -> str:
    low = word.lower()
    for stem, name in (("тарел", "тарелка"), ("стакан", "стакан"), ("бан", "банка"), ("чаш", "чашка"),
                       ("круж", "кружка"), ("кус", "кусок"), ("ломт", "ломтик"), ("ложк", "ложка"),
                       ("бутыл", "бутылка"), ("пачк", "пачка"), ("порци", "порция")):  # fmt: skip
        if low.startswith(stem):
            return name
    return "шт"


# --- LLM estimates (only for foods missing from the directory) ---------------------------------------------------

FOOD_SYSTEM = (
    "You estimate nutrition for foods a person reports eating (Russian). For each item return: name — the food in "
    "Russian nominative case, short (e.g. 'Борщ', 'Куриная грудка отварная'); grams — the weight of the amount "
    "eaten (use the grams given; otherwise a typical portion for the words, e.g. a plate of soup ~300, one egg ~55); "
    "kcal_100, protein_100, fat_100, carbs_100 — typical values per 100 g of that food as eaten. Use standard "
    "reference values; never invent unusual numbers. Keep the items in the same order as given."
)


class FoodEstimateItem(BaseModel):
    said: str = Field(description="The item as given")
    name: str = Field(description="Food name, Russian nominative")
    grams: float = Field(gt=0, le=3000)
    kcal_100: float = Field(ge=0, le=900)
    protein_100: float = Field(ge=0, le=100)
    fat_100: float = Field(ge=0, le=100)
    carbs_100: float = Field(ge=0, le=100)


class FoodEstimate(BaseModel):
    items: list[FoodEstimateItem] = Field(max_length=12)


def plausible(item: FoodEstimateItem) -> bool:
    """Energy from macros must roughly match the calories (4/9/4 kcal per gram)."""
    if item.protein_100 + item.fat_100 + item.carbs_100 > 100.5:
        return False
    energy = 4 * item.protein_100 + 9 * item.fat_100 + 4 * item.carbs_100
    return abs(energy - item.kcal_100) <= max(30.0, 0.3 * item.kcal_100)


@dataclass
class EatenItem:
    """One food in a draft: values for the eaten amount; `new_product` is saved to LMF after «✅ Записать»."""

    name: str
    grams: float | None
    kcal: float | None
    protein: float | None
    fat: float | None
    carbs: float | None
    product_id: int | None = None
    estimate: bool = False  # values estimated by the LLM
    grams_estimate: bool = False  # the amount was not said
    new_product: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, name: str, grams: float | None, per100: dict[str, Any], **extra: Any) -> EatenItem:
        """Values for `grams` from values per 100 g."""
        v = scaled(per100, grams)
        return cls(name, grams, v["kcal"], v["protein"], v["fat"], v["carbs"], **extra)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "grams": self.grams,
            "kcal": self.kcal,
            "protein": self.protein,
            "fat": self.fat,
            "carbs": self.carbs,
            "product_id": self.product_id,
            "estimate": self.estimate,
            "grams_estimate": self.grams_estimate,
            "new_product": self.new_product,
        }


def scaled(per100: dict[str, Any], grams: float | None) -> dict[str, float | None]:
    if grams is None:
        return {k: None for k in MACROS}
    return {k: (round(float(per100[k]) * grams / 100, 1) if per100.get(k) is not None else None) for k in MACROS}


def kbju(values: dict[str, Any]) -> str:
    """«660 ккал · Б 25 · Ж 7 · У 124»."""

    def fmt(v: Any) -> str:
        return "—" if v is None else str(round(float(v))).replace(".", ",")

    return (
        f"{fmt(values.get('kcal'))} ккал · Б {fmt(values.get('protein'))} · Ж {fmt(values.get('fat'))} · "
        f"У {fmt(values.get('carbs'))}"
    )


# --- removing an entry by words («удали гречку из еды», «убери последнюю запись еды») ----------------------------

_REMOVE_WORDS = {
    "удали", "удалить", "убери", "убрать", "сотри", "стереть", "вычеркни", "отмени", "отменить", "из", "в", "еды",
    "еде", "еду", "питания", "питание", "кбжу", "съеденного", "запись", "записи", "записей", "про", "о", "об", "за",
    "мне", "пожалуйста", "последнюю", "последний", "последнее", "последней", "эту", "этот", "это", "приём", "прием",
    "пищи", "сегодня", "вчера", "позавчера", "г", "гр", "грамм", "граммов",
}  # fmt: skip


@dataclass
class FoodRemoval:
    words: list[str]  # what was named («гречку»); empty = the last entry
    grams: float | None = None  # «удали 200 г гречки» prefers the entry with these grams


def parse_food_remove(text: str) -> FoodRemoval:
    body = re.sub(r"\bза\s+\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?", " ", text.lower())
    grams = None
    if m := _GRAMS.search(body):
        grams = float(m.group("n").replace(",", "."))
        body = body[: m.start()] + " " + body[m.end() :]
    words = [w for w in re.findall(r"[a-zа-яё]+", body) if w not in _REMOVE_WORDS and len(w) >= 2]
    return FoodRemoval(words, grams)


def pick_entry(entries: list[dict[str, Any]], removal: FoodRemoval) -> tuple[dict[str, Any] | None, int]:
    """The entry to delete and how many entries matched (the latest of them is taken; the button shows which)."""

    def order(entry: dict[str, Any]) -> tuple[str, int]:
        return (str(entry.get("time") or ""), int(entry.get("id") or 0))

    said = [w.replace("ё", "е") for w in removal.words]
    matched = []
    for entry in entries:
        names = [w for w in re.findall(r"[a-zа-я0-9]+", product_key(entry.get("name") or "")) if len(w) >= 2]
        if all(any(same_word(w, n) for n in names) for w in said):
            matched.append(entry)
    if removal.grams is not None:
        exact = [e for e in matched if e.get("grams") is not None and abs(float(e["grams"]) - removal.grams) < 0.5]
        matched = exact or matched
    if not matched:
        return None, 0
    return max(matched, key=order), len(matched)


# --- how much of a photographed food was eaten (the photo caption) ------------------------------------------------

_SHARES = (
    (r"\bпол(?:овин\w*|\s*упаковк\w*|\s*пачк\w*|\s*порци\w*)?\b", 0.5),
    (r"\bтрет\w*\b", 1 / 3),
    (r"\bчетверт\w*\b", 0.25),
    (r"\b(?:вс[юеёя]|весь|целиком|цел\w+)\b", 1.0),
)


@dataclass
class EatenAmount:
    grams: float | None = None  # «200 г»
    share: float | None = None  # «половину» of the package / the dish


def eaten_amount(text: str) -> EatenAmount:
    low = text.lower()
    if m := _GRAMS.search(low):
        value = float(m.group("n").replace(",", "."))
        unit = m.group("u").lower()
        return EatenAmount(grams=value * 1000 if unit.startswith(("кг", "кил", "л")) and unit != "мл" else value)
    for pattern, share in _SHARES:
        if re.search(pattern, low):
            return EatenAmount(share=share)
    return EatenAmount()
