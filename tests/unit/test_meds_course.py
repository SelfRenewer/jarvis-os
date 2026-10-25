"""Courses with an end («курсом 8 недель», «до 15 января»): cut from the phrase by code; «продли … на …»."""

from __future__ import annotations

from datetime import date

import pytest

from jarvis.bot.intents import Intent, keyword_route
from jarvis.meds.service import parse_course


@pytest.mark.parametrize(
    ("phrase", "rest", "ends", "said"),
    [
        ("магний 400 мг в 21:00 курсом 8 недель", "магний 400 мг в 21:00", "2026-12-08", "8 недель"),
        ("железо утром на две недели", "железо утром", "2026-10-27", "2 недели"),
        ("витамин D утром в течение месяца", "витамин D утром", "2026-11-13", "1 месяц"),
        ("цинк в 9:00 до 15 января", "цинк в 9:00", "2027-01-15", "до 15.01.2027"),
        ("D3 на 21 день утром", "D3 утром", "2026-11-03", "21 день"),
        ("омегу 2 раза в день утром и вечером", "омегу 2 раза в день утром и вечером", None, None),
        ("магний на ночь", "магний на ночь", None, None),
    ],
)
def test_course_end_is_cut_from_the_phrase(phrase: str, rest: str, ends: str | None, said: str | None) -> None:
    text, course, ok = parse_course(phrase, date(2026, 10, 14))
    assert ok and text == rest
    assert (course.ends_on.isoformat(), course.said) == (ends, said) if course else ends is None


def test_extend_phrases() -> None:
    routed = keyword_route("продли магний ещё на 2 недели")
    assert (routed.intent, routed.task_text, routed.query) == (Intent.MED_EXTEND, "магний", "2 недели")
