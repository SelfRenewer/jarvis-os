"""Natural-language planning constraints → normalized enums.

Deterministic keyword parsing only; slot computation never depends on free-form LLM output.
"""

from __future__ import annotations

import re

from jarvis.planner.engine import Period

_KEYWORDS: list[tuple[Period, tuple[str, ...]]] = [
    (Period.MORNING, ("утр", "morning", "с утра")),
    (Period.AFTERNOON, ("днём", "днем", "после обеда", "afternoon", "в обед")),
    (Period.EVENING, ("вечер", "evening", "после работы")),
]


def parse_preferences_keywords(text: str | None) -> tuple[Period, ...]:
    if not text:
        return ()
    lowered = text.lower()
    found = [period for period, words in _KEYWORDS if any(w in lowered for w in words)]
    return tuple(found)


_SPLIT = re.compile(r"(можно (разбить|частями)|по частям|splittable)", re.IGNORECASE)


def parse_splittable(text: str | None) -> bool:
    return bool(text and _SPLIT.search(text))
