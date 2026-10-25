"""Supplements and caffeine for TLT: what was taken → LMF supplements with doses, by code.

«D3 6000, B12 500, цинк 25» · «витамин Д3» · «омегу 2 капсулы» · «5 г креатина» · «2 энергетика».
Names are matched to the LMF dictionary (name and Russian label, Cyrillic look-alikes in codes: «Д3» = D3); doses
and units are parsed and converted here (mg ↔ g ↔ mcg). Drinks are «portions» the user sets once
(«энергетик — 160 мг кофеина»): Jarvis never guesses how much caffeine a drink has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from jarvis.core.numwords import spoken_numbers
from jarvis.tracker.parse import same_word

_MASS_MG = {"мкг": Decimal("0.001"), "мг": Decimal(1), "г": Decimal(1000)}
# Units as written and as dictated («500 микрограмм», «25 миллиграмм», «6000 международных единиц»).
_UNITS = [
    (r"мкг|mcg|µg|микрограм+\w*", "мкг"),
    (r"мг|mg|мил+играм+\w*", "мг"),
    (r"гр?|g|грам+\w*", "г"),
    (r"ме|me|iu|ед\.?|единиц\w*", "МЕ"),
    (r"мл|ml", "мл"),
    (r"капс\w*|таб\w*|шт\w*|драже|пакетик\w*|ложк\w*|капел\w*", "шт"),
]
_UNIT = re.compile(
    r"^(?:" + "|".join(f"(?P<u{i}>{pattern})" for i, (pattern, _) in enumerate(_UNITS)) + r")$", re.IGNORECASE
)
_NUMBER = re.compile(r"^(\d+(?:[.,]\d+)?)([a-zа-яёµ.]*)$", re.IGNORECASE)
_COUNT_WORDS = {"один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "пару": 2}
# «я сегодня утром выпил …»: when it was taken, said before the verb (the day itself is read by the router).
TAKEN_WHEN = (
    r"(?:сегодня|вчера|позавчера|сейчас|только\s+что|утром|с\s+утра|дн[её]м|вечером|ночью|на\s+ночь|перед\s+сном"
    r"|после\s+(?:еды|завтрака|обеда|ужина|тренировки)|уже|ещё|еще|тоже|также)"
)
TAKEN_VERB = r"(?:выпил[аи]?|принял[аи]?|закинул[аи]?)"
_VERBS = re.compile(
    rf"^\s*(?:я\s+)?(?:{TAKEN_WHEN}\s+)*(?:я\s+)?(?:{TAKEN_VERB}|пью|принимаю|съел[аи]?)\s+",
    re.IGNORECASE,
)
_ADJECTIVE_UNIT = re.compile(r"^междунар\w*$")  # «6000 международных единиц»
_FILLER = {"ещё", "еще", "уже", "банку", "банка", "чашку", "чашка", "стакан", "кружку", "порцию", "по", "мне"}
# Cyrillic letters that speech recognition writes in vitamin codes: «Д3» = D3, «В12»/«Б12» = B12, «С» = C.
_LOOKALIKE = str.maketrans({"д": "d", "б": "b", "в": "b", "с": "c", "к": "k", "е": "e", "а": "a"})
# Drinks that are portions of caffeine (their size is asked once and remembered): name → word forms.
CAFFEINE_DRINKS = {
    "энергетик": r"энергетик\w{0,3}|энергос\w*",
    "кофе": r"кофе|кофейк\w{0,2}",
    "эспрессо": r"эспрессо",
    "американо": r"американо",
    "капучино": r"капучино",
    "латте": r"латте",
    "редбул": r"ред\s*бу?лл?\w{0,2}|redbull|red\s*bull",
    "монстр": r"монстр\w{0,2}|monster",
    "чай": r"ча[йяю]|чайк\w{0,2}",
}


def _norm(value: str) -> str:
    return value.lower().replace("ё", "е").replace("’", "'")


def _tokens(value: str) -> set[str]:
    """«Vit. D3» → {vit, d3}; «Omega-3» → {omega, 3, omega-3}; «5-HTP» → {5, htp, 5-htp}."""
    text = _norm(value)
    found = set(re.findall(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)*", text))
    for word in list(found):
        found.update(part for part in word.split("-") if part)
    return found


@dataclass(frozen=True)
class Supplement:
    id: int
    name: str  # «Vit. D3»
    label: str  # «Витамин D3»
    unit: str  # «МЕ», «мг», «г», «мкг» or ""
    default_dose: float | None

    @property
    def title(self) -> str:
        return (self.label or self.name).split("—")[0].strip()

    @property
    def tokens(self) -> set[str]:
        return _tokens(self.name) | _tokens(self.label)


def supplements_from(boot: dict[str, Any]) -> list[Supplement]:
    return [
        Supplement(
            int(s["id"]),
            str(s["name"]),
            str(s.get("label") or s["name"]),
            str(s.get("unit") or ""),
            float(s["default_dose"]) if s.get("default_dose") is not None else None,
        )
        for s in boot.get("supplements") or []
        if s.get("active", 1)
    ]


@dataclass
class ItemPhrase:
    said: str
    words: list[str]
    count: int = 1
    amount: Decimal | None = None
    unit: str | None = None  # canonical: мкг мг г МЕ мл шт


def _unit(token: str) -> str | None:
    m = _UNIT.match(token.strip("."))
    if not m:
        return None
    index = int(next(name for name, value in m.groupdict().items() if value)[1:])
    return _UNITS[index][1]


def _unit_after(tokens: list[str], i: int) -> tuple[str | None, int]:
    """The unit written after a number («500 микрограмм», «6000 международных единиц») and the tokens it took."""
    for skip in (0, 1):
        if i + 1 + skip >= len(tokens):
            break
        if skip and not _ADJECTIVE_UNIT.match(_norm(tokens[i + 1].strip(",.()"))):
            break
        unit = _unit(_norm(tokens[i + 1 + skip].strip(",.()")))
        if unit is not None:
            return unit, 1 + skip
    return None, 0


def split_items(text: str) -> list[ItemPhrase]:
    """«D3 6000, B12 500 и цинк 25» → three phrases with amounts; verbs and fillers dropped.

    Dictated text works too: numbers in words («двадцать пять миллиграмм»), several sentences (a period after a
    Latin abbreviation like «Vit. D3» is not a sentence end), and a dose after a comma that belongs to the name
    before it («омегу-3, две капсулы»)."""
    body = _VERBS.sub("", spoken_numbers(text.strip()))
    items: list[ItemPhrase] = []
    for part in re.split(r"(?<!\d),|,(?!\d)|;|(?<=[а-яёА-ЯЁ\d][.!?])\s+|\s+и\s+|\s+плюс\s+|\s+а\s+также\s+|\n", body):
        said = part.strip(" .!?:—-")
        said = re.sub(r"^(?:и|а|ещё|еще|также|плюс|потом|а\s+ещё|а\s+еще)\s+", "", said, flags=re.IGNORECASE)
        if not said:
            continue
        tokens = re.findall(r"[^\s]+", said)
        phrase = ItemPhrase(said, [])
        i = 0
        while i < len(tokens):
            token = tokens[i].strip(",.()")
            low = _norm(token)
            number = _NUMBER.match(low)
            if number and phrase.amount is None:
                amount = Decimal(number.group(1).replace(",", "."))
                unit = _unit(number.group(2)) if number.group(2) else None
                if unit is None and number.group(2):  # «омега-3» is a name, «200мг» is a dose
                    phrase.words.append(token)
                    i += 1
                    continue
                if unit is None:
                    unit, took = _unit_after(tokens, i)
                    i += took
                phrase.amount, phrase.unit = amount, unit
            elif low in _COUNT_WORDS and phrase.amount is None:
                phrase.amount = Decimal(_COUNT_WORDS[low])
                phrase.unit, took = _unit_after(tokens, i)
                i += took
            elif low not in _FILLER:
                phrase.words.append(token)
            i += 1
        if phrase.words:
            items.append(phrase)
        elif phrase.amount is not None and items and items[-1].amount is None:
            # «омегу-3, две капсулы»: a dose alone belongs to the name said just before it
            items[-1].amount, items[-1].unit = phrase.amount, phrase.unit
            items[-1].said = f"{items[-1].said}, {said}"
    return items


def has_dose(text: str) -> bool:
    """«И магний 400 миллиграмм», «омегу-3, две капсулы»: a sentence names a dose with its unit (a supplement list
    goes on); «позвонить маме в 5» does not."""
    return any(p.amount is not None and p.unit is not None for p in split_items(text))


@dataclass
class Taken:
    """One thing taken, resolved: a supplement with a dose in its LMF unit (or `dose_text` when not convertible)."""

    said: str
    supplement: Supplement | None = None
    dose: float | None = None
    dose_text: str | None = None
    portion: str | None = None  # «энергетик»
    count: int = 1
    candidates: list[Supplement] = field(default_factory=list)  # ambiguous name
    needs_portion: str | None = None  # a drink whose caffeine is not set yet

    def text(self) -> str:
        if self.supplement is None:
            return self.said
        unit = f" {self.supplement.unit}" if self.supplement.unit else ""
        dose = f" {_fmt(self.dose)}{unit}" if self.dose is not None else ""
        extra = f" ({self.dose_text})" if self.dose_text else ""
        what = (
            f" ({self.count} × {self.portion})"
            if self.portion and self.count > 1
            else (f" ({self.portion})" if self.portion else "")
        )
        return f"{self.supplement.title}{dose}{extra}{what}"

    def item(self) -> dict[str, Any]:
        """Stored in a pack (`Medication.items`)."""
        return {
            "said": self.said,
            "supplement": self.supplement.name if self.supplement else None,
            "label": self.supplement.title if self.supplement else self.said,
            "dose": self.dose,
            "unit": self.supplement.unit if self.supplement else None,
            "dose_text": self.dose_text,
        }


def _fmt(value: float | Decimal | None) -> str:
    if value is None:
        return ""
    number = Decimal(str(value)).normalize()
    text = f"{number:f}"
    return text.replace(".", ",")


def find_supplement(words: list[str], supplements: list[Supplement]) -> list[Supplement]:
    """Supplements named by the words («витамин Д3» → Vit. D3; «омегу» → Omega-3); several when ambiguous."""
    said = [_norm(w) for w in words]
    said = [w.translate(_LOOKALIKE) if re.fullmatch(r"[а-я]\d+|[а-я]", w) else w for w in said]
    significant = [w for w in said if not re.fullmatch(r"витамин\w*|vit\.?|vitamin", w)] or said
    matches = []
    for s in supplements:
        tokens = s.tokens
        if all(any(w == t or (len(w) >= 4 and len(t) >= 4 and same_word(w, t)) for t in tokens) for w in significant):
            matches.append(s)
    if len(matches) > 1:  # «C» in «Vit. C» vs the letter elsewhere: prefer an exact whole-name match
        exact = [s for s in matches if _norm(s.title) == " ".join(said) or _norm(s.name) == " ".join(said)]
        matches = exact or matches
    return matches


def portion_key(words: list[str], portions: dict[str, Any]) -> str | None:
    """«энергетика» → «энергетик» for a known caffeine drink or a portion the user named («протеиновый батончик»).
    Word forms are strict here: «кофеин» is a supplement, «кофе» is a drink."""
    said = " ".join(_norm(w) for w in words)
    for name, forms in CAFFEINE_DRINKS.items():
        if re.fullmatch(rf"(?:{forms})", said) or any(re.fullmatch(rf"(?:{forms})", _norm(w)) for w in words):
            return name
    for name in portions:
        if name not in CAFFEINE_DRINKS and (said == name or (len(name) >= 5 and same_word(said, name))):
            return name
    return None


def resolve(phrase: ItemPhrase, supplements: list[Supplement], portions: dict[str, Any]) -> Taken:
    taken = Taken(phrase.said)
    portion = portion_key(phrase.words, portions) if len(phrase.words) <= 2 else None
    if portion is not None:
        count = int(phrase.amount) if phrase.amount is not None and phrase.unit is None else 1
        taken.portion, taken.count = portion, max(count, 1)
        spec = portions.get(portion)
        if not spec:
            taken.needs_portion = portion
            return taken
        target = next((s for s in supplements if s.name == spec.get("supplement")), None)
        if target is None:
            taken.needs_portion = portion
            return taken
        taken.supplement = target
        taken.dose = float(Decimal(str(spec["dose"])) * taken.count)
        return taken
    found = find_supplement(phrase.words, supplements)
    if not found and phrase.amount is not None and phrase.unit is None:
        # «омега 3»: the number is part of the name when that is the only way to read it
        found = find_supplement([*phrase.words, _fmt(phrase.amount)], supplements)
        if found:
            phrase.amount = None
    if len(found) != 1:
        taken.candidates = found
        return taken
    taken.supplement = target = found[0]
    if phrase.amount is not None and phrase.unit is None and _fmt(phrase.amount) in target.tokens:
        phrase.amount = None  # «омега 3» / «витамин B 12»: the number belongs to the name
    taken.dose, taken.dose_text = dose_in_unit(phrase.amount, phrase.unit, target)
    return taken


def dose_in_unit(amount: Decimal | None, unit: str | None, target: Supplement) -> tuple[float | None, str | None]:
    """The said amount in the supplement's LMF unit (mass units converted); not convertible → dose_text."""
    if amount is None:
        return target.default_dose, None
    try:
        if unit is None or unit == target.unit or (unit == "МЕ" and target.unit.lower() == "ме"):
            return float(amount), None
        if unit in _MASS_MG and target.unit in _MASS_MG:
            return float(amount * _MASS_MG[unit] / _MASS_MG[target.unit]), None
        if unit == "шт" and target.default_dose is not None:
            return float(amount * Decimal(str(target.default_dose))), f"{_fmt(amount)} шт"
    except (InvalidOperation, ZeroDivisionError):
        pass
    return None, f"{_fmt(amount)} {unit}"
