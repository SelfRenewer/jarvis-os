"""Numbers said in words → digits, for dictated text: «двадцать пять» → «25», «шесть тысяч» → «6000».

Speech recognition (Whisper in Telegram, Apple on the Mac) writes doses either way: «25 мг» or «двадцать пять
миллиграмм», «6 000» or «шесть тысяч». Only cardinal numbers in the nominative/accusative are converted; any other
word ends the number. Fractions keep the decimal comma: «полтора» → «1,5», «два с половиной» → «2,5».
"""

from __future__ import annotations

import re
from decimal import Decimal

_UNITS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
}
_TEENS = {
    "десять": 10,
    "десяти": 10,  # «семь из десяти»
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
}
_TENS = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}
_HUNDREDS = {
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
}
_HALF = {"полтора": Decimal("1.5"), "полторы": Decimal("1.5")}
# word → (value, rank): a number is said from the highest rank down («сто двадцать пять»)
_WORDS: dict[str, tuple[Decimal, int]] = {
    **{w: (Decimal(v), 3) for w, v in _HUNDREDS.items()},
    **{w: (Decimal(v), 2) for w, v in _TENS.items()},
    **{w: (Decimal(v), 1) for w, v in _TEENS.items()},
    **{w: (Decimal(v), 1) for w, v in _UNITS.items()},
    **{w: (v, 1) for w, v in _HALF.items()},
}
_THOUSAND = r"тысяч[аиу]?"
_WORD = "|".join(sorted(map(re.escape, _WORDS), key=len, reverse=True))
_NUMBER = re.compile(
    rf"(?<![\w-])(?:(?P<digits>\d+(?:[.,]\d+)?)\s+(?P<k>{_THOUSAND})"
    rf"|(?P<words>(?:{_WORD}|{_THOUSAND})(?:\s+(?:{_WORD}|{_THOUSAND}))*))"
    r"(?P<half>\s+с\s+половиной)?(?![\w-])",
    re.IGNORECASE,
)
_HALF_AFTER_DIGITS = re.compile(r"(?<![\w.,])(?P<n>\d+)\s+с\s+половиной(?![\w-])", re.IGNORECASE)
_GROUPED = re.compile(r"(?<![\w.,])\d{1,3}(?:[   ]\d{3})+(?![\w.,]|\s\d)")


def _fmt(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text.replace(".", ",")


def _parse(words: list[str]) -> list[Decimal]:
    """Number words → one or more numbers («два три» are two numbers, «сто двадцать» one)."""
    numbers: list[Decimal] = []
    total = group = Decimal(0)
    rank, started, thousands = 9, False, False

    def flush() -> None:
        nonlocal total, group, rank, started, thousands
        if started:
            numbers.append(total + group)
        total = group = Decimal(0)
        rank, started, thousands = 9, False, False

    for word in words:
        low = word.lower()
        if re.fullmatch(_THOUSAND, low):
            if thousands:
                flush()
            total += (group if started and group else Decimal(1)) * 1000
            group, rank, started, thousands = Decimal(0), 4, True, True
            continue
        value, word_rank = _WORDS[low]
        teen_after_tens = 10 <= value < 20 and rank == 2
        if word_rank >= rank or teen_after_tens or (low in _HALF and group):
            flush()
        group += value
        rank, started = word_rank, True
    flush()
    return numbers


def spoken_numbers(text: str, *, halves: bool = True) -> str:
    """Digits instead of number words; «6 000» → «6000». `halves`: «семь с половиной» → «7,5» (else «7 с половиной»,
    for parsers that read the half themselves)."""

    def number(m: re.Match[str]) -> str:
        if m.group("digits"):
            values = [Decimal(m.group("digits").replace(",", ".")) * 1000]
        else:
            values = _parse(m.group("words").split())
        half = m.group("half") or ""
        if half and halves:
            values[-1] += Decimal("0.5")
            half = ""
        return " ".join(_fmt(v) for v in values) + half

    value = _GROUPED.sub(lambda m: re.sub(r"\D", "", m.group(0)), text)
    value = _NUMBER.sub(number, value)
    if halves:
        value = _HALF_AFTER_DIGITS.sub(lambda m: f"{m.group('n')},5", value)
    return value
