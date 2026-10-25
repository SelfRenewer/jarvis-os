"""Numbers said in words → digits (dictated doses: Whisper writes «двадцать пять миллиграмм» as often as «25 мг»)."""

from __future__ import annotations

import pytest

from jarvis.core.numwords import spoken_numbers


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("цинк двадцать пять миллиграмм и магний четыреста", "цинк 25 миллиграмм и магний 400"),
        ("шесть тысяч единиц", "6000 единиц"),
        ("6 тысяч", "6000"),
        ("1,5 тысячи", "1500"),
        ("тысяча двести", "1200"),
        ("две тысячи двадцать шесть", "2026"),
        ("сто одиннадцать", "111"),
        ("6 000 МЕ", "6000 МЕ"),  # a grouped number
        ("B12 500 и D3 5 000", "B12 500 и D3 5000"),  # «B12 500» is not «12 500»
        ("два три", "2 3"),  # two numbers, not five
        ("одну капсулу", "1 капсулу"),
        ("полтора грамма", "1,5 грамма"),
        ("два с половиной грамма", "2,5 грамма"),
        ("7 с половиной", "7,5"),
        ("трико и тридцать", "трико и 30"),  # only whole words
        ("без чисел", "без чисел"),
    ],
)
def test_spoken_numbers(text: str, result: str) -> None:
    assert spoken_numbers(text) == result


def test_halves_can_be_left_to_the_parser() -> None:
    assert spoken_numbers("семь с половиной часов", halves=False) == "7 с половиной часов"
