"""JARVIS's manner: a polite, slightly formal butler («Да, сэр»). Fixed phrases (no LLM), rotated so that replies
do not repeat word for word. Facts in replies are never changed — the manner only adds a short first line."""

from __future__ import annotations

import itertools
from datetime import datetime

ACKS = ("Да, {a}.", "Слушаюсь, {a}.", "Будет сделано, {a}.", "Как скажете, {a}.", "Сию минуту, {a}.")
QUERIES = ("Разумеется, {a}.", "Вот, {a}.", "Минуту, {a}. Готово.", "Извольте, {a}.")
SORRY = "Прошу прощения, {a}."


class Persona:
    def __init__(self, enabled: bool, address: str = "сэр") -> None:
        self.enabled = enabled
        self.address = address
        self._acks = itertools.cycle(ACKS)
        self._queries = itertools.cycle(QUERIES)

    def _fmt(self, phrase: str) -> str:
        return phrase.format(a=self.address)

    def ack(self) -> str | None:
        """For a command that changes something: «Да, сэр.»"""
        return self._fmt(next(self._acks)) if self.enabled else None

    def query(self) -> str | None:
        """For a question about the schedule/tasks: «Разумеется, сэр.»"""
        return self._fmt(next(self._queries)) if self.enabled else None

    def sorry(self, text: str) -> str:
        if not self.enabled:
            return text
        if text[:1].islower():  # «Прошу прощения, сэр, ничего не нашёл …»
            return f"{self._fmt(SORRY)[:-1]}, {text}"
        return f"{self._fmt(SORRY)} {text}"

    def is_apology(self, text: str) -> bool:
        return self.enabled and text.startswith(self._fmt(SORRY)[:-1])

    def greeting(self, local_now: datetime) -> str:
        hour = local_now.hour
        if 5 <= hour < 12:
            hello = "Доброе утро"
        elif 12 <= hour < 18:
            hello = "Добрый день"
        elif 18 <= hour < 23:
            hello = "Добрый вечер"
        else:
            hello = "Доброй ночи"
        return f"{hello}, {self.address}. JARVIS к вашим услугам." if self.enabled else f"{hello}! Я — JARVIS."
