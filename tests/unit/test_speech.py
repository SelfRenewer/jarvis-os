"""Reply text → speech text (deterministic)."""

from __future__ import annotations

from jarvis.bot.speech import TAIL, to_speech


def test_agenda_is_spoken_naturally() -> None:
    text = (
        "📅 Завтра, чт 24.09\n"
        "• 10:00–11:00 Врач 📍Rustaveli 5\n"
        "☐ 13:00 🔴 Снять 2 000 $\n"
        "⚠️ Просрочено (1):\n"
        "☐ 22.09 Купить сигару\n"
        "• — календарь, ☐ — задачи Todoist"
    )
    assert to_speech(text) == (
        "Завтра, четверг, 24 сентября. С 10:00 до 11:00 — Врач, Rustaveli 5. В 13:00 — Снять 2 000 $. "
        "Просрочено (1): 22 сентября Купить сигару."
    )


def test_move_money_and_open_ranges() -> None:
    assert to_speech("🗓 «Заказать цветы» → пт 25.09 16:00") == "Заказать цветы — пятница, 25 сентября, в 16:00."
    assert to_speech("Итого: 42.99 GEL, сборы 5.10 GEL") == "Итого: 42.99 лари, сборы 5.10 лари."  # not dates
    assert to_speech("• …–14:00 Конференция\n• 22:00–… Поезд") == "До 14:00 — Конференция. С 22:00 — Поезд."


def test_nothing_to_say_and_truncation() -> None:
    assert to_speech("👌") is None
    assert to_speech("   \n\n") is None
    long = to_speech("Очень длинная фраза для проверки. " * 60, max_chars=200)
    assert long is not None
    assert long.endswith(TAIL)
    assert len(long) <= 200 + len(TAIL)
    assert to_speech("Кнопки ниже меняют задачи в Todoist.\nГотово") == "Готово."
