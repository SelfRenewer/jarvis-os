"""Expense phrases → amounts, currencies, descriptions and categories. Code only, money in Decimal.

«потратил 45 лари на такси» · «заплатил $120 за отель» · «вчера потратил 30 на продукты и 12,5 на кофе» ·
«купил кроссовки за 250 лари» · «расход: аптека 18₾». No currency → the default one (GEL in Tbilisi).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from jarvis.calc.currency import _AMOUNT, CRYPTO_IDS, _mentions, exponent, money_text
from jarvis.calc.expression import parse_amount

SYMBOL = {"GEL": "₾", "USD": "$", "EUR": "€", "RUB": "₽", "TRY": "₺", "AMD": "֏", "GBP": "£"}

VERB = re.compile(
    r"^\s*(?:я\s+)?(?:(?:сегодня|вчера|позавчера)\s+)?"
    r"(?:потратил[аи]?|заплатил[аи]?|оплатил[аи]?|отдал[аи]?|спустил[аи]?|расход\w*|трат[аы]\w*|минус)\b[:\s]*",
    re.IGNORECASE,
)
_BOUGHT = re.compile(r"^\s*(?:я\s+)?(?:(?:сегодня|вчера|позавчера)\s+)?купил[аи]?\s+(?P<what>.+?)\s+за\s+(?P<rest>.+)$")
_DAY_WORD = re.compile(r"\b(?:за\s+)?(?P<w>сегодня|вчера|позавчера)\b")
# «12.10» is a date only next to another amount («35 лари за стрижку 12.10»); «9.50» or «12.10 лари» is money.
_DATE = re.compile(r"(?:\bза\s+)?(?<![\d.,])(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?![\d.,])")
_TIME_UNIT = re.compile(r"\s*(?:час|минут|мин\b|секунд|сек\b|дн[еяи]|день|недел|месяц|год|лет\b|раз\b|ч\b)")
_GLUE = {"на", "за", "в", "во", "по", "с", "у", "для", "и", "а", "ещё", "еще", "это", "всего", "около", "примерно",
         "рублей", "сегодня", "вчера", "позавчера"}  # fmt: skip

# Category by words of the description (the first match wins). The description itself is kept verbatim.
CATEGORIES: list[tuple[str, str]] = [
    (
        "Транспорт",
        r"такси|uber|bolt|yandex|яндекс|maxim|автобус|метро|маршрутк|бензин|топлив|заправк|парковк"
        r"|поезд|билет|самол[её]т|авиа|каршеринг|самокат",
    ),
    (
        "Кафе и рестораны",
        r"кафе|кофе|ресторан|бар\b|обед|ужин|завтрак|доставк|wolt|glovo|пицц|суши|шаурм|бургер"
        r"|кофейн|ланч|фастфуд|макдон",
    ),
    (
        "Продукты",
        r"продукт|магазин|супермаркет|carrefour|spar|nikora|никора|агрохаб|agrohub|2\s*nabiji|ориент"
        r"|рынок|овощ|фрукт|хлеб|молок|мяс|вод[аыу]\b|яйц",
    ),
    ("Здоровье", r"аптек|лекарств|таблет|витамин|врач|клиник|стоматолог|анализ|массаж|бад"),
    ("Жильё", r"аренд|квартир|коммунал|электричеств|свет\b|газ\b|вод[ау]\s+за|интернет|ремонт|мебел"),
    (
        "Связь и подписки",
        r"связ|телефон|magti|магти|geocell|silknet|подписк|netflix|spotify|youtube|apple|icloud"
        r"|claude|openrouter|chatgpt|vpn|хостинг|домен",
    ),
    ("Одежда", r"одежд|кроссовк|обув|куртк|футболк|джинс|рубашк|носк"),
    ("Спорт", r"спорт|зал\b|фитнес|тренировк|бассейн|абонемент"),
    ("Красота", r"стрижк|барбер|парикмахер|маникюр|косметик|салон"),
    ("Путешествия", r"отел|гостиниц|хостел|airbnb|booking|тур\b|экскурс|виз[аыу]\b"),
    ("Подарки", r"подар|цвет[ыо]|букет"),
    ("Развлечения", r"кино|концерт|театр|игр[аыу]|steam|музей|клуб"),
    ("Техника", r"телефон\s+новый|ноутбук|наушник|техник|гаджет|зарядк|кабел"),
]
_CATEGORY_RE = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in CATEGORIES]
OTHER = "Другое"


def category_of(text: str) -> str:
    for name, pattern in _CATEGORY_RE:
        if pattern.search(text):
            return name
    return OTHER


def to_minor(amount: Decimal, currency: str) -> int:
    exp = exponent(currency)
    return int((amount * (Decimal(10) ** exp)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor) / (Decimal(10) ** exponent(currency))


def amount_text(minor: int, currency: str) -> str:
    """«1 234,50 ₾», «120,00 $» → without trailing «,00»: «45 ₾», «12,50 ₾»."""
    text = money_text(from_minor(minor, currency), currency).removesuffix(",00")
    return f"{text} {SYMBOL.get(currency, currency)}"


@dataclass
class ExpenseItem:
    amount: Decimal
    currency: str
    description: str
    category: str


@dataclass
class ExpenseParse:
    items: list[ExpenseItem]
    day_word: str | None = None  # «вчера», «12.10»
    error: str | None = None


def spent_day(word: str | None, today: date) -> date | None:
    if not word:
        return today
    low = word.lower()
    if low == "вчера":
        return today - timedelta(days=1)
    if low == "позавчера":
        return today - timedelta(days=2)
    if low == "сегодня":
        return today
    if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})", low):
        try:
            day = date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return day if day <= today else day.replace(year=today.year - 1)
    return None


def _one(part: str, currency: str | None, default: str) -> tuple[ExpenseItem | None, str | None]:
    """«45 лари на такси» → item; the currency carries over to the next part («и 12 на кофе»)."""
    low = part.lower()
    amounts = list(_AMOUNT.finditer(low))
    if len(amounts) != 1:
        return None, currency
    amount = parse_amount(amounts[0].group())
    if amount is None or amount <= 0:
        return None, currency
    mentions = _mentions(part)
    if not mentions and _TIME_UNIT.match(low[amounts[0].end() :]):  # «потратил 2 часа на уборку» is not money
        return None, currency
    code = mentions[0].code if mentions else (currency or default)
    rest = list(low)
    for start, end in [(m.start, m.end) for m in mentions] + [amounts[0].span()]:
        rest[start:end] = " " * (end - start)
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", "".join(rest)) if w not in _GLUE]
    description = " ".join(words).strip()
    if not description:
        return ExpenseItem(amount, code, "Без описания", OTHER), code
    return ExpenseItem(amount, code, description[:1].upper() + description[1:], category_of(description)), code


def parse_expense(text: str, default_currency: str = "GEL") -> ExpenseParse | None:
    """None when the message is not an expense report. Several parts: «30 на продукты и 12,5 на кофе»."""
    t = text.strip().rstrip(".!")
    low = t.lower()
    day = None
    if m := _DAY_WORD.search(low):
        day = m.group("w")
    if m := _BOUGHT.match(low):
        body = f"{m.group('rest')} {m.group('what')}"  # «купил кроссовки за 250 лари» → «250 лари кроссовки»
    elif m := VERB.match(low):
        body = t[m.end() :]
    else:
        return None
    body = _DAY_WORD.sub(" ", body)
    for m in _DATE.finditer(body):
        others = body[: m.start()] + body[m.end() :]
        if 1 <= int(m.group("d")) <= 31 and 1 <= int(m.group("m")) <= 12 and _AMOUNT.search(others.lower()):
            day = day or f"{m.group('d')}.{m.group('m')}"
            body = others
            break
    # «45 лари на такси. Что у меня сегодня» — a second sentence without an amount makes this not one report
    parts = [p for p in re.split(r",(?!\d)|;|\s+и\s+|\s+а\s+ещё\s+|\s+плюс\s+|\n|(?<!\d)[.!?]\s+", body) if p.strip()]
    items: list[ExpenseItem] = []
    currency: str | None = None
    for part in parts:
        item, currency = _one(part, currency, default_currency)
        if item is None:
            return ExpenseParse([], day, error=f"Не понял сумму или на что: «{part.strip()}».")
        if item.currency in CRYPTO_IDS:
            return ExpenseParse([], day, error="Расходы в криптовалюте я не веду.")
        items.append(item)
    if not items:
        return ExpenseParse([], day, error="Не понял сумму. Например: «потратил 45 лари на такси».")
    return ExpenseParse(items, day)


# --- receipts (the photo reader copies the text; the amount is parsed here) -----------------------------------------

_RECEIPT_NUMBER = re.compile(r"\d{1,3}(?:[  ,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def receipt_amount(printed: str) -> Decimal | None:
    """«45.80», «1 234,50», «1,234.50», «₾ 12.00» → Decimal; None when there is no single clear number."""
    found = _RECEIPT_NUMBER.findall(printed.replace(" ", " "))
    if len(found) != 1:
        return None
    raw = found[0].replace(" ", "").replace(" ", "")
    if "," in raw and "." in raw:  # «1,234.50» or «1.234,50»: the last separator is the decimal one
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        raw = raw.replace("," if decimal_sep == "." else ".", "").replace(decimal_sep, ".")
    elif "," in raw:
        whole, _, frac = raw.rpartition(",")
        raw = f"{whole.replace(',', '')}.{frac}" if len(frac) <= 2 else raw.replace(",", "")
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    return value if value > 0 else None
