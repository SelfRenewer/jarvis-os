"""Deterministic Russian date / period / time phrases (never delegated to the LLM).

The LLM may only *copy* a phrase such as "в пятницу" from the message; turning it into a date is done here,
relative to the user's local "today". Examples for today = Wed 23.09.2026:

  "завтра" → 24.09             "в пятницу" → 25.09            "в следующую пятницу" → 02.10
  "25.09" / "25 сентября" → 25.09.2026                        "через 3 дня" → 26.09
  "на неделе" → 23.09–29.09    "на этой неделе" → 23.09–27.09 "на следующей неделе" → 28.09–04.10
  "на выходных" → 26.09–27.09  "ближайшие 3 дня" → 23.09–25.09 "с 25.09 по 30.09" → 25.09–30.09
  times: "в 10", "в 10:30", "к 18", "в 7 вечера", "в шесть вечера", "18:00", "утром" (09:00), "на вечер" (19:00)
  shifts (moving a task): "на 2 часа позже", "на полчаса раньше", "на пару дней", "на неделю", "через 2 часа"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

MAX_PERIOD_DAYS = 31

WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_WEEKDAY_FORMS = {
    "понедельник": 0,
    "пн": 0,
    "вторник": 1,
    "вт": 1,
    "среда": 2,
    "среду": 2,
    "ср": 2,
    "четверг": 3,
    "чт": 3,
    "пятница": 4,
    "пятницу": 4,
    "пт": 4,
    "суббота": 5,
    "субботу": 5,
    "сб": 5,
    "воскресенье": 6,
    "вс": 6,
}
_MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}
_NUMBER_WORDS = {
    "один": 1,
    "одну": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}
_DAY_PARTS = {
    "с утра": time(9, 0),
    "утром": time(9, 0),
    "на утро": time(9, 0),
    "днем": time(13, 0),
    "в обед": time(13, 0),
    "на обед": time(13, 0),
    "после обеда": time(14, 0),
    "вечером": time(19, 0),
    "вечерком": time(19, 0),
    "на вечер": time(19, 0),
}


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    @property
    def single(self) -> bool:
        return self.start == self.end


@dataclass(frozen=True)
class When:
    day: date | None
    at: time | None


def _clean(text: str) -> str:
    value = text.lower().replace("ё", "е")
    value = re.sub(r"[?!,;«»\"()]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" .")


def _strip_prepositions(value: str) -> str:
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"^(?:на|в|во|к|ко|за|к концу)\s+", "", value).strip()
    return value


def _number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _month(token: str) -> int | None:
    if not token.isalpha() or len(token) < 3:
        return None
    return _MONTHS.get(token[:3])


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _closest_year(month: int, day: int, today: date) -> date | None:
    """A date without a year: this year or next, whichever is closer to today (25.01 said in December → next year)."""
    candidates = [d for d in (_safe_date(today.year, month, day), _safe_date(today.year + 1, month, day)) if d]
    if not candidates:  # 29.02 in two ordinary years in a row: the next leap year (L9, review 17.10)
        candidates = [d for year in range(today.year + 2, today.year + 5) if (d := _safe_date(year, month, day))][:1]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today).days))


def next_weekday(today: date, weekday: int) -> date:
    """Nearest date on or after today with the given weekday (0 = Monday)."""
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def parse_day(text: str, today: date) -> date | None:
    """A single calendar day, or None when the phrase is not (only) a day."""
    value = _strip_prepositions(_clean(text))
    if not value:
        return None
    relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}
    if value in relative:
        return today + timedelta(days=relative[value])
    if m := re.fullmatch(r"через\s+(?:(\S+)\s+)?(день|дня|дней|неделю|недели|недель)", value):
        count = _number(m.group(1)) if m.group(1) else 1
        if count is None or (m.group(1) is None and m.group(2) == "день"):
            return None  # "через день" is ambiguous (every other day)
        unit = 7 if m.group(2).startswith("недел") else 1
        return today + timedelta(days=count * unit)
    if m := re.fullmatch(r"(?:(следующ\w*|эт\w+)\s+)?([а-я]+)", value):
        weekday = _WEEKDAY_FORMS.get(m.group(2))
        if weekday is not None:
            if m.group(1) and m.group(1).startswith("следующ"):
                next_monday = today + timedelta(days=7 - today.weekday())
                return next_monday + timedelta(days=weekday)
            return next_weekday(today, weekday)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value):
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?", value):
        day_n, month_n = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            return _safe_date(year + 2000 if year < 100 else year, month_n, day_n)
        return _closest_year(month_n, day_n, today)
    m = re.fullmatch(r"(\d{1,2})(?:-?го)?\s+([а-я]+)\.?(?:\s+(\d{4})(?:\s+г(?:ода?|\.)?)?)?", value)
    named_month = _month(m.group(2)) if m else None
    if m and named_month:
        if m.group(3):
            return _safe_date(int(m.group(3)), named_month, int(m.group(1)))
        return _closest_year(named_month, int(m.group(1)), today)
    if m := re.fullmatch(r"(\d{1,2})(?:-?го|\s+числа|-?го\s+числа)", value):
        # "25-го" / "25 числа": this month, or next month when the day already passed.
        day_n = int(m.group(1))
        candidate = _safe_date(today.year, today.month, day_n)
        if candidate is not None and candidate >= today:
            return candidate
        month_n, year = (1, today.year + 1) if today.month == 12 else (today.month + 1, today.year)
        return _safe_date(year, month_n, day_n)
    return None


def _clip(start: date, end: date) -> Period | None:
    if end < start:
        return None
    return Period(start, min(end, start + timedelta(days=MAX_PERIOD_DAYS - 1)))


def parse_period(text: str, today: date) -> Period | None:
    """A day or a span of days ("на неделе", "на выходных", "с 25.09 по 30.09"); None if not recognized."""
    value = _clean(text)
    if not value:
        return None
    day = parse_day(value, today)
    if day is not None:
        return Period(day, day)
    stripped = _strip_prepositions(value)
    monday = today - timedelta(days=today.weekday())
    if re.fullmatch(r"(?:эт\w+|текущ\w+)\s+недел\w*", stripped):
        return Period(today, monday + timedelta(days=6))
    if re.fullmatch(r"следующ\w+\s+недел\w*", stripped):
        return Period(monday + timedelta(days=7), monday + timedelta(days=13))
    if re.fullmatch(r"(?:всю\s+|ближайшую\s+)?недел[юеия]|неделька|7 дней|семь дней", stripped):
        return Period(today, today + timedelta(days=6))
    if re.fullmatch(r"(?:эт\w+\s+)?выходн\w*", stripped):
        if today.weekday() == 6:
            return Period(today, today)
        saturday = next_weekday(today, 5)
        return Period(saturday, saturday + timedelta(days=1))
    if re.fullmatch(r"следующ\w+\s+выходн\w*", stripped):
        saturday = monday + timedelta(days=12)
        return Period(saturday, saturday + timedelta(days=1))
    if m := re.fullmatch(r"(?:ближайш\w+\s+|следующ\w+\s+)?(\S+)\s+(?:дн\w+|день)", stripped):
        count = _number(m.group(1))
        if count and count > 0:
            return _clip(today, today + timedelta(days=count - 1))
    if re.fullmatch(r"(?:ближайш\w+\s+)?месяц", stripped):
        return _clip(today, today + timedelta(days=30))
    if m := re.fullmatch(r"(?:с|от)\s+(.+?)\s+(?:по|до)\s+(.+)", value) or re.fullmatch(
        r"(.+?)\s*(?:[–—]|\s-\s|(?<=\d)-(?=\d))\s*(.+)", value
    ):
        start, end = parse_day(m.group(1), today), parse_day(m.group(2), today)
        if start and end:
            return _clip(start, end)
    if m := re.fullmatch(r"(.+?)\s+и\s+(.+)", value):
        first, second = parse_day(m.group(1), today), parse_day(m.group(2), today)
        if first and second:
            return _clip(min(first, second), max(first, second))
    return None


_TIME_WITH_PREP = re.compile(
    r"(?:^|\s)(?:в|к|около)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(?:ч|час\w*))?(?:\s+(утра|дня|вечера|ночи))?(?=\s|$)"
)
# Hours said as words (voice transcripts): «в шесть вечера», «в час дня», «в полдень».
_HOUR_WORDS = {**{k: v for k, v in _NUMBER_WORDS.items() if k not in ("одну", "одна", "две")}, "час": 1}
_TIME_WORDS = re.compile(
    rf"(?:^|\s)(?:в|к|около)\s+({'|'.join(_HOUR_WORDS)})(?:\s+час\w*)?(?:\s+(утра|дня|вечера|ночи))?(?=\s|$)"
)
_NOON = re.compile(r"(?:^|\s)(?:в|к|около)\s+(полдень|полночь)(?=\s|$)")
_CLOCK = re.compile(r"(?:^|\s)(?:на\s+)?(\d{1,2}):(\d{2})(?:\s+(утра|дня|вечера|ночи))?(?=\s|$)")


def _to_time(hour: int, minute: int, part: str | None) -> time | None:
    if part in ("вечера", "дня") and 1 <= hour < 12:
        hour += 12
    elif part is None and 1 <= hour <= 5:
        hour += 12  # «в 3», «в два» without «утра/ночи» means the afternoon
    elif part == "ночи" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _extract_time(value: str) -> tuple[time | None, str]:
    at: time | None
    if m := _NOON.search(value):
        at = time(12, 0) if m.group(1) == "полдень" else time(0, 0)
        return at, (value[: m.start()] + " " + value[m.end() :]).strip()
    if m := _TIME_WORDS.search(value):
        hour = _HOUR_WORDS[m.group(1)]
        part = m.group(2) or ("дня" if hour == 1 else None)  # «в час» alone is 13:00
        at = _to_time(hour, 0, part)
        if at is not None:
            return at, (value[: m.start()] + " " + value[m.end() :]).strip()
    for pattern in (_TIME_WITH_PREP, _CLOCK):
        for m in pattern.finditer(value):
            following = value[m.end() :].split()
            if following and (_month(following[0]) or following[0] == "числа"):
                continue  # "в 5 октября" is a date, not 05:00
            at = _to_time(int(m.group(1)), int(m.group(2) or 0), m.group(3))
            if at is not None:
                return at, (value[: m.start()] + " " + value[m.end() :]).strip()
    for phrase, part_at in _DAY_PARTS.items():
        if re.search(rf"(?:^|\s){phrase}(?=\s|$)", value):
            return part_at, re.sub(rf"(?:^|\s){phrase}(?=\s|$)", " ", value).strip()
    return None, value


def parse_when(text: str, today: date) -> When | None:
    """A day and/or a time of day ("завтра в 10", "в пятницу", "в 18:00"); None if anything is left unparsed."""
    value = _clean(text)
    if not value:
        return None
    at, rest = _extract_time(value)
    rest = re.sub(r"\s+", " ", rest).strip()
    rest = re.sub(r"(?:^|\s)(?:в|во|на|к)$", "", rest).strip()  # "завтра в" after "в 10" was taken
    day: date | None = None
    if rest:
        day = parse_day(rest, today)
        if day is None:
            return None
    if day is None and at is None:
        return None
    return When(day, at)


@dataclass(frozen=True)
class Shift:
    """Move by an amount: «на 2 часа позже» (+2 h), «на день раньше» (−1 d), «через полчаса» (now + 30 min)."""

    minutes: int = 0
    days: int = 0
    months: int = 0
    from_now: bool = False  # «через 2 часа»: counted from now, not from the task's current time

    @property
    def by_time(self) -> bool:
        return self.minutes != 0

    def describe(self) -> str:
        sign = "−" if (self.minutes or self.days or self.months) < 0 else "+"
        parts = []
        if self.months:
            parts.append(f"{abs(self.months)} мес")
        if self.days:
            parts.append(f"{abs(self.days)} дн")
        if self.minutes:
            hours, minutes = divmod(abs(self.minutes), 60)
            parts += [f"{hours} ч"] * bool(hours) + [f"{minutes} мин"] * bool(minutes)
        return sign + " ".join(parts)


_SHIFT_UNITS = {
    "мин": 1,
    "минут": 1,
    "минуту": 1,
    "минуты": 1,
    "час": 60,
    "часа": 60,
    "часов": 60,
    "часик": 60,
    "день": 1440,
    "дня": 1440,
    "дней": 1440,
    "денек": 1440,
    "сутки": 1440,
    "суток": 1440,
    "неделю": 10080,
    "недели": 10080,
    "недель": 10080,
    "месяц": -1,  # calendar months
    "месяца": -1,
    "месяцев": -1,
}
_UNIT_WORDS = "|".join(sorted(_SHIFT_UNITS, key=len, reverse=True))
_SHIFT = re.compile(
    rf"^(?:(?:еще|все)\s+)?(на|через)\s+(?:(\d{{1,3}}|[а-я]+)\s+)?(полчаса|полтора\s+часа|{_UNIT_WORDS})"
    r"(?:\s+(позже|попозже|вперед|дальше|раньше|пораньше|назад))?$"
)


def parse_shift(text: str) -> Shift | None:
    """«на 2 часа позже», «на полчаса раньше», «на пару дней», «на неделю», «через 2 часа»; None otherwise.

    «на N …» shifts the task's own time (forward unless «раньше»); «через N минут/часов» counts from now.
    «через N дней» is not a shift: `parse_day` turns it into a date.
    """
    m = _SHIFT.fullmatch(_clean(text))
    if not m:
        return None
    prep, amount_word, unit_word, direction = m.groups()
    if unit_word == "полчаса":
        amount, unit = 1, 30
    elif unit_word.startswith("полтора"):
        amount, unit = 1, 90
    else:
        unit = _SHIFT_UNITS[unit_word]
        if amount_word is None:
            amount = 1
        elif amount_word in ("пару", "пара"):
            amount = 2
        elif amount_word in ("полтора", "полторы") and 0 < unit < 1440:
            amount, unit = 1, unit * 3 // 2
        else:
            number = _number(amount_word)
            if number is None or number <= 0:
                return None
            amount = number
    if prep == "через":
        if unit == -1 or unit >= 1440 or direction:
            return None  # «через 3 дня» is a date (parse_day)
        return Shift(minutes=amount * unit, from_now=True)
    sign = -1 if direction in ("раньше", "пораньше", "назад") else 1
    if unit == -1:
        return Shift(months=sign * amount)
    if unit >= 1440:
        return Shift(days=sign * amount * unit // 1440)
    return Shift(minutes=sign * amount * unit)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    for last in (31, 30, 29, 28):
        if (candidate := _safe_date(year, month, min(day.day, last))) is not None:
            return candidate
    raise ValueError("unreachable")


def round_up(moment: datetime, minutes: int = 5) -> datetime:
    """22:13 → 22:15: a time «in 2 hours» is shown and stored on a round minute."""
    moment = moment.replace(second=0, microsecond=0)
    extra = (-moment.minute) % minutes
    return moment + timedelta(minutes=extra)


def looks_like_period(text: str) -> bool:
    """Recognition only (the router has no clock): the result does not depend on the reference day."""
    return parse_period(text, date(2026, 1, 7)) is not None


def human_day(day: date, today: date, *, with_weekday: bool = True) -> str:
    """Human label: "сегодня, ср 23.09", "завтра, чт 24.09", "пт 25.09" (year only when not the current one)."""
    fmt = "%d.%m" if day.year == today.year else "%d.%m.%Y"
    label = f"{WEEKDAY_SHORT[day.weekday()]} {day.strftime(fmt)}" if with_weekday else day.strftime(fmt)
    if day == today:
        return f"сегодня, {label}"
    if day == today + timedelta(days=1):
        return f"завтра, {label}"
    return label
