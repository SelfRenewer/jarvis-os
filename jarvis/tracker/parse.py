"""TLT / TPL phrases → values, by code: «спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT».

Numbers, durations and days are parsed here, never by the LLM. What is left over (free words such as «был холодный
душ») is matched against the LMF dictionary by the service, and only then — if still unclear — shown to the LLM.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.core.numwords import spoken_numbers

TLT_ALIAS = r"(?:tlt|тлт|тилт|tilt|ти\s*эл\s*ти|тиэлти)"
TPL_ALIAS = r"(?:tpl|тпл|ти\s*пи\s*эл\w*|типиэл\w*|12\s+сфер\w*|двенадцать\s+сфер\w*)"

_TRIGGER = re.compile(
    rf"^\s*(?:(?:запиши|отметь|добавь|внеси)\s+)?(?:(?:в|во)\s+)?(?:{TLT_ALIAS}|трекер\w*|дневник\w*)\b\s*[:,\-—]?\s*",
    re.IGNORECASE,
)
_SLEEP_WORD = r"(?:спал[аи]?|проспал[аи]?|поспал[аи]?|сон)"
_SLEEP = re.compile(
    rf"\b{_SLEEP_WORD}\s*[:\-—]?\s*(?:всего|около|примерно|где-?то|почти)?\s*"
    r"(?:(?P<h1>\d{1,2})\s*[:.]\s*(?P<m1>\d{2})(?!\d)"
    r"|(?P<h2>\d{1,2})\s*(?:ч\.?|час\w*)\s*(?:и\s*)?(?P<m2>\d{1,2})\s*(?:мин\w*|м\.?)"
    r"|(?P<h3>\d{1,2})\s*(?:с\s+)?половин\w*(?:\s+час\w*)?"
    r"|(?P<h4>\d{1,2}(?:[.,]\d{1,2})?)\s*(?:ч\b\.?|час\w*)"
    r"|(?P<m5>\d{2,3})\s*(?:м\b\.?|мин\w*)"
    r"|(?P<h6>\d{1,2}(?:[.,]\d)?)(?![\d:%]))"
    r"(?:\s*[,(]?\s*(?:качеств\w*\s*(?:сна\s*)?)?[:\-—]?\s*(?P<q>\d{1,3})\s*%?\)?(?![\d:.])(?!\s*(?:/|из)\s*10))?",
    re.IGNORECASE,
)
# «7 часов сна», «7 ч 20 мин сна»
_SLEEP_AFTER = re.compile(
    r"\b(?P<h4>\d{1,2}(?:[.,]\d{1,2})?)\s*(?:ч\b\.?|час\w*)(?:\s*(?:и\s*)?(?P<mm>\d{1,2})\s*(?:м\b\.?|мин\w*))?"
    r"\s+сна\b",
    re.IGNORECASE,
)
_QUALITY = re.compile(r"\bкачеств\w*(?:\s+сна)?\s*[:\-—]?\s*(?P<q>\d{1,3})\s*%?", re.IGNORECASE)
_SCORE_WORDS = {
    "единиц": 1,
    "двойк": 2,
    "тройк": 3,
    "четвёрк": 4,
    "четверк": 4,
    "пятёрк": 5,
    "пятерк": 5,
    "шестёрк": 6,
    "шестерк": 6,
    "семёрк": 7,
    "семерк": 7,
    "восьмёрк": 8,
    "восьмерк": 8,
    "девятк": 9,
    "десятк": 10,
}
_SCORE = re.compile(
    r"\b(?:оценк\w*(?:\s+(?:дня|за\s+день))?|результат\w*(?:\s+дня)?|день)\s*(?:[:\-—]|на|был\s+на)?\s*"
    r"(?:(?P<s>\d{1,2}(?:[.,]\d)?)(?:\s*(?:/|из)\s*10)?(?![\d:.])"
    rf"|(?P<w>{'|'.join(_SCORE_WORDS)})\w*)"
    r"|(?P<s2>\d{1,2}(?:[.,]\d)?)\s*(?:/|из)\s*10\b",
    re.IGNORECASE,
)
_MOODS = [
    ("good", r"хорош|отличн|прекрасн|супер|класс|зел[её]н|бодр|радостн|позитивн"),
    ("neutral", r"так\s+себе|норм|средн|нейтральн|обычн|ж[её]лт|ровн|спокойн"),
    ("bad", r"плох|ужасн|паршив|красн|грустн|отвратит|подавлен|никак"),
]
_MOOD = re.compile(r"\bнастроени\w*\s*(?:[:\-—]\s*|было\s+|был\w*\s+)?(?P<m>так\s+себе|[а-яё]+)", re.IGNORECASE)
# «талия 94», «обхват талии 94 см» — the waist goes to LMF «тело» (`waist_cm`)
_WAIST = re.compile(r"\b(?:обхват\w*\s+)?тали[яиюе]\w*\s*[:\-—]?\s*(?P<v>\d{2,3}(?:[.,]\d)?)\s*(?:см\b\.?)?", re.I)
_NOTE = re.compile(r"\b(?:заметк\w*(?:\s+(?:дня|к\s+дню))?|коммент\w*)\s*[:\-—]\s*(?P<v>.+)$", re.IGNORECASE | re.S)
_REMOVE = re.compile(r"^\s*(?:убери|сними|удали|отмени|убрать|снять)\b(?:\s+(?:отметк\w*|тег\w*))?", re.IGNORECASE)
_DAY = re.compile(
    r"\b(?:за\s+)?(?P<v>сегодня|вчера|позавчера)\b"
    r"|\bза\s+(?P<d>\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|\d{1,2}\s+[а-я]+|понедельник|вторник|среду|четверг|пятницу"
    r"|субботу|воскресенье)\b"
    r"|^\s*(?P<p>\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\s*[:\-—]",
    re.IGNORECASE,
)
_CODE = re.compile(r"(?<![A-Za-z0-9+\-])(?P<c>[A-Za-z0-9][A-Za-z0-9\-]*\+{0,2})(?![A-Za-z0-9+])")
_NOT_CODES = {"tlt", "tpl", "tilt"}

# Words that carry no data in a report («сегодня была тренировка и CD»).
GLUE = frozenset(
    [
        "и",
        "а",
        "был",
        "была",
        "было",
        "были",
        "есть",
        "сегодня",
        "тоже",
        "ещё",
        "еще",
        "также",
        "запиши",
        "отметь",
        "добавь",
        "внеси",
        "в",
        "во",
        "на",
        "за",
        "с",
        "у",
        "меня",
        "я",
        "мне",
        "мой",
        "моя",
        "мои",
        "день",
        "дня",
        "сделал",
        "сделала",
        "делал",
        "делала",
        "прошла",
        "прошёл",
        "прошел",
        "утром",
        "вечером",
        "днём",
        "днем",
        "пожалуйста",
        "джарвис",
        "поставь",
        "отметки",
        "отметку",
        "отметка",
        "теги",
        "тег",
        "по",
        "итогу",
        "итого",
        "всё",
        "все",
        "вот",
        "плюс",
        "tlt",
        "тлт",
        "тилт",
        "tilt",
        "трекер",
        "трекере",
        "трекера",
        "дневник",
        "дневнике",
        "tpl",
        "тпл",
    ]
)


def same_word(a: str, b: str) -> bool:
    """Russian-friendly: «утреннюю» ~ «утренняя», «зубы» ~ «зубов» (a common start of ≥ 3 letters)."""
    a, b = a.lower().replace("ё", "е"), b.lower().replace("ё", "е")
    if a == b:
        return True
    common = len(os.path.commonprefix([a, b]))
    return common >= 3 and common >= min(len(a), len(b)) - 2


@dataclass
class DayParse:
    day_text: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)  # sleep_minutes sleep_quality score mood (no charge/work)
    note: str | None = None
    codes: list[str] = field(default_factory=list)  # Latin codes as written («CD», «W+»)
    remove: bool = False  # «убери GG за вчера»
    waist: float | None = None  # cm, «талия 94»
    rest: str = ""  # what the patterns above did not consume

    def words(self) -> list[str]:
        """Leftover words that may still mean something (labels: «холодный душ», «тренировка»)."""
        found = re.findall(r"[a-zа-яё]+", self.rest.lower().replace("ё", "е"))
        return [w for w in found if w not in GLUE and len(w) >= 2]


class _Buffer:
    """The message with consumed parts blanked out (positions stay the same)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def take(self, m: re.Match[str]) -> None:
        start, end = m.span()
        self.text = self.text[:start] + " " * (end - start) + self.text[end:]


def _hours(value: str) -> float:
    return float(value.replace(",", "."))


def sleep_minutes(m: re.Match[str]) -> int | None:
    g = m.groupdict()
    if g.get("h1") is not None:
        total = int(g["h1"]) * 60 + int(g["m1"])
    elif g.get("h2") is not None:
        total = int(g["h2"]) * 60 + int(g["m2"])
    elif g.get("h3") is not None:
        total = int(g["h3"]) * 60 + 30
    elif g.get("h4") is not None:
        total = round(_hours(g["h4"]) * 60) + int(g.get("mm") or 0)
    elif g.get("m5") is not None:
        total = int(g["m5"])
    elif g.get("h6") is not None:
        total = round(_hours(g["h6"]) * 60)
    else:
        return None
    return total if 0 < total <= 24 * 60 else None


def parse_sleep(text: str) -> tuple[int | None, int | None]:
    """«7:30», «7 с половиной часов», «сон 7:30 85» → (minutes, quality)."""
    value = text if re.match(rf"\s*{_SLEEP_WORD}\b", text, re.IGNORECASE) else f"сон {text}"
    for m in [*_SLEEP.finditer(value), *_SLEEP_AFTER.finditer(text)]:
        minutes = sleep_minutes(m)
        if minutes is not None:
            quality = m.groupdict().get("q")
            return minutes, int(quality) if quality and int(quality) <= 100 else None
    return None, None


def mood_key(word: str) -> str | None:
    for key, pattern in _MOODS:
        if re.match(pattern, word.lower()):
            return key
    return None


def parse_day_report(text: str) -> DayParse:
    """Everything a TLT report can carry, parsed without the LLM."""
    out = DayParse()
    buf = _Buffer(_TRIGGER.sub("", text.strip(), count=1))
    if m := _NOTE.search(buf.text):
        out.note = m.group("v").strip()
        buf.take(m)
    # Dictated reports say numbers in words («спал семь с половиной часов, оценка восемь»); the note stays verbatim.
    buf.text = spoken_numbers(buf.text, halves=False)
    if m := _REMOVE.search(buf.text):
        out.remove = True
        buf.take(m)
    if (m := _WAIST.search(buf.text)) and 40 <= (waist := _hours(m.group("v"))) <= 200:
        out.waist = waist
        buf.take(m)
    if m := _DAY.search(buf.text):
        out.day_text = (m.group("v") or m.group("d") or m.group("p") or "").lower()
        buf.take(m)
    for m in [*_SLEEP.finditer(buf.text), *_SLEEP_AFTER.finditer(buf.text)]:
        minutes = sleep_minutes(m)
        if minutes is None:
            continue
        out.fields["sleep_minutes"] = minutes
        quality = m.groupdict().get("q")
        if quality and int(quality) <= 100:
            out.fields["sleep_quality"] = int(quality)
        buf.take(m)
        break
    if "sleep_quality" not in out.fields and (m := _QUALITY.search(buf.text)) and int(m.group("q")) <= 100:
        out.fields["sleep_quality"] = int(m.group("q"))
        buf.take(m)
    if m := _SCORE.search(buf.text):
        raw = m.group("s") or m.group("s2")
        if raw is not None:
            score = _hours(raw)
        else:
            stem = next(stem for stem in _SCORE_WORDS if m.group("w").lower().startswith(stem))
            score = float(_SCORE_WORDS[stem])
        if 0 <= score <= 10:
            out.fields["score"] = int(score) if score.is_integer() else score
            buf.take(m)
    if (m := _MOOD.search(buf.text)) and (mood := mood_key(m.group("m"))):
        out.fields["mood"] = mood
        buf.take(m)
    for m in list(_CODE.finditer(buf.text)):
        code = m.group("c")
        if code.isdigit() or code.lower() in _NOT_CODES:
            continue
        out.codes.append(code)
        buf.take(m)
    out.rest = buf.text
    return out


_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}
_MONTH = re.compile(
    r"\b(?:за|в|на)\s+(?P<m>январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\b|июн\w*|июл\w*|август\w*|сентябр\w*"
    r"|октябр\w*|ноябр\w*|декабр\w*)|\b(?P<prev>прошл\w+\s+месяц\w*)",
    re.IGNORECASE,
)


def parse_month(text: str, year: int, month: int) -> tuple[str, re.Match[str] | None]:
    """«за сентябрь» → «2026-09» (a later month than now means last year); default: the current month."""
    m = _MONTH.search(text)
    if not m:
        return f"{year:04d}-{month:02d}", None
    if m.group("prev"):
        return (f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"), m
    name = m.group("m").lower()
    number = next(n for stem, n in _MONTHS.items() if name.startswith(stem))
    return (f"{year - 1 if number > month else year:04d}-{number:02d}"), m


_TPL_TRIGGER = re.compile(
    rf"^\s*(?:(?:в|во)\s+)?{TPL_ALIAS}\s*[:,\-—]?\s*|\s*(?:в|во)\s+{TPL_ALIAS}\b|\b{TPL_ALIAS}\b", re.IGNORECASE
)
_TPL_VERB = re.compile(
    r"\b(?:отметь|отметить|отмечу|поставь(?:\s+галочк\w*)?|галочк\w*|сделал[аи]?|выполнил[аи]?)\b", re.IGNORECASE
)
_TPL_UNVERB = re.compile(r"\b(?:сними|снять|убери|убрать|отмени|отменить)\b(?:\s+галочк\w*)?", re.IGNORECASE)


@dataclass
class TplParse:
    period_match: bool
    check: list[str]  # item phrases to check («утреннюю рутину»)
    uncheck: list[str]


def parse_tpl_marks(text: str, period_match: re.Match[str] | None = None) -> TplParse:
    """«TPL: отметь утреннюю рутину и чтение, сними медитацию» → phrases (items are resolved by the service)."""
    value = text
    if period_match is not None:
        value = value[: period_match.start()] + " " + value[period_match.end() :]
    value = _TPL_TRIGGER.sub(" ", value)
    check: list[str] = []
    uncheck: list[str] = []
    # Split into «verb + list» runs: «отметь A и B, сними C».
    pieces = re.split(rf"({_TPL_UNVERB.pattern}|{_TPL_VERB.pattern})", value, flags=re.IGNORECASE)
    target = check
    for piece in pieces:
        if not piece or not piece.strip():
            continue
        if _TPL_UNVERB.fullmatch(piece.strip()):
            target = uncheck
            continue
        if _TPL_VERB.fullmatch(piece.strip()):
            target = check
            continue
        for part in re.split(r",|;|\bи\b|\bа\s+также\b", piece):
            phrase = part.strip(" .:!?-—")
            if phrase and re.search(r"[a-zа-яё]", phrase, re.IGNORECASE):
                target.append(phrase)
    return TplParse(period_match is not None, check, uncheck)
