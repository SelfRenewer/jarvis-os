"""Deterministic text helpers."""

from __future__ import annotations

import re

_SPACE = re.compile(r"\s+")
_EDGE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def normalize_title(value: str | None) -> str:
    """Case/whitespace/edge-punctuation-insensitive key used to link a Todoist task with a calendar event."""
    if not value:
        return ""
    text = value.lower().replace("ё", "е")
    text = _SPACE.sub(" ", text).strip()
    return _EDGE.sub("", text)


_WORD = re.compile(r"[\w-]+", re.UNICODE)


def same_item_titles(a: str | None, b: str | None) -> bool:
    """Exact normalized match, or one title's words all contained in the other's (at least 3 words).

    Only used together with an independent time match, so a near-duplicate like
    "Уточнить у владельца вызов мастера" / "Уточнить у владельца квартиры вызов мастера" links.
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = set(_WORD.findall(na)), set(_WORD.findall(nb))
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(small) >= 3 and small <= big


def covered_by(task_title: str | None, event_title: str | None, min_words: int = 2) -> bool:
    """Every word of the task title is in the event title: «Купить пластыри» ⊂ «Купить батарейки и пластыри».

    The user often merges several tasks into one calendar event. Only used together with a same-day match.
    """
    nt, ne = normalize_title(task_title), normalize_title(event_title)
    if not nt or not ne:
        return False
    if nt == ne:
        return True
    words = set(_WORD.findall(nt))
    return len(words) >= min_words and words <= set(_WORD.findall(ne))


PRONOUNS = {"это", "эту", "его", "ее", "её", "эту задачу", "это дело", "её задачу", "ее задачу"}
_WEEKDAYS_ACC = {"понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"}


def is_pronoun(value: str | None) -> bool:
    return not value or normalize_title(value) in PRONOUNS


def word_stems(value: str | None, length: int = 4) -> list[str]:
    """Crude Russian-friendly stems: first `length` letters of long words, whole short words (>= 3 letters)."""
    words = [w for w in _WORD.findall(normalize_title(value)) if len(w) >= 3]
    return [w[:length] if len(w) > length else w for w in words]


def matches_stems(query: str | None, candidate: str | None, length: int = 4) -> bool:
    stems = word_stems(query, length)
    target = normalize_title(candidate)
    return bool(stems) and all(stem in target for stem in stems)


def normalize_due_phrase(value: str | None) -> str | None:
    """'пятницу' → 'в пятницу' so Todoist's Russian date parser understands a bare weekday."""
    if not value:
        return value
    text = value.strip()
    return f"в {text}" if text.lower() in _WEEKDAYS_ACC else text
