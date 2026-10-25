"""«Джарвис, …» is how the user addresses Jarvis, not part of the request (R28, 2026-10-14: it was saved as a note)."""

from __future__ import annotations

import pytest

from jarvis.bot.intents import Intent, keyword_route, normalize_command, strip_address


@pytest.mark.parametrize(
    ("said", "rest"),
    [
        ("Джарвис, заполни день", "заполни день"),
        ("джарвис запиши заметку купить хлеб", "запиши заметку купить хлеб"),
        ("Эй, Джарвис! Что у меня завтра?", "Что у меня завтра?"),
        ("Jarvis. Открой телегу.", "Открой телегу."),  # the rest is kept verbatim (final period too)
        ("Окей, Джарвис, идея: бот.", "идея: бот."),
        ("спасибо, Джарвис!", "спасибо!"),
        ("Джарвис", ""),
        ("Эй, Джарвис", ""),
        ("Джарвисон пришёл", "Джарвисон пришёл"),  # another word
        ("купи джарвису корм", "купи джарвису корм"),  # not an address
    ],
)
def test_strip_address(said: str, rest: str) -> None:
    assert strip_address(said) == rest


def test_routing_ignores_the_address() -> None:
    assert keyword_route("Джарвис, заполни день").intent == Intent.DIARY_FILL
    note = keyword_route("Джарвис запиши заметку купить хлеб")
    assert (note.intent, note.note_text) == (Intent.CAPTURE_NOTE, "купить хлеб")
    assert normalize_command("Эй, Джарвис, удалим задачу сигара") == "удали задачу сигара"


@pytest.mark.parametrize(
    ("said", "intent", "note"),
    [
        ("Заметка: код домофона 4521", Intent.CAPTURE_NOTE, "код домофона 4521"),
        ("создай заметку купить хлеб", Intent.CAPTURE_NOTE, "купить хлеб"),
        ("Джарвис, запиши номер парковки 17", Intent.CAPTURE_NOTE, "номер парковки 17"),
        ("запиши идею бот для заметок", Intent.CAPTURE_IDEA, None),
        ("запиши в дневник спал 7 часов", Intent.TRACKER_LOG, None),
        ("запиши задачу купить хлеб", Intent.UNKNOWN, None),  # a task: the router decides, never a note
        ("где-то гигабайт оперативки ест ладно", Intent.UNKNOWN, None),
    ],
)
def test_notes_only_on_an_explicit_command(said: str, intent: Intent, note: str | None) -> None:
    """16.10: a remark became a note; now only «заметка …», «создай заметку …», «(Джарвис,) запиши …» make one."""
    routed = keyword_route(said)
    assert (routed.intent, routed.note_text) == (intent, note)
