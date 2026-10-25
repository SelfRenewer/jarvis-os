"""The persistent reply keyboard under the input field, the «⋯ Ещё» menu and the settings panel.

The keyboard is a few hubs (the user found 12 buttons «an airplane cockpit», 2026-10-14): today, tasks, the diary
(TLT), shopping and «⋯ Ещё» — an inline menu with the rest. Row 2 has room for one more hub (a location button).
Tapping a keyboard button sends its text as an ordinary message; `menu_intent` maps it to an intent exactly (no
LLM). Buttons of older layouts that may still be on screen keep working. Bump KEYBOARD_VERSION when the layout
changes: the keyboard is then re-sent once automatically.
"""

from __future__ import annotations

from typing import Any

from jarvis.bot.intents import Intent, RoutedIntent
from jarvis.integrations.telegram.client import InlineButton, inline_keyboard

KEYBOARD_VERSION = "3"
KEYBOARD_PREF_KEY = "menu_keyboard"

MORE_BUTTON = "⋯ Ещё"
MAIN_MENU: list[list[str]] = [
    ["📅 Сегодня", "📋 Задачи", "📓 Дневник"],
    ["🛒 Покупки", MORE_BUTTON],
]
SETTINGS_BUTTON = "⚙️ Настройки"

_ACTIONS: dict[str, RoutedIntent] = {
    "📅 Сегодня": RoutedIntent(intent=Intent.SHOW_TODAY),
    "📋 Задачи": RoutedIntent(intent=Intent.SHOW_TASKS),
    "📓 Дневник": RoutedIntent(intent=Intent.DIARY),
    "🛒 Покупки": RoutedIntent(intent=Intent.SHOPPING_SHOW),
    MORE_BUTTON: RoutedIntent(intent=Intent.MENU),
    # older layouts (keyboard v2) — still on the user's screen until the new keyboard arrives
    "🗓 Завтра": RoutedIntent(intent=Intent.SHOW_AGENDA, date_text="завтра"),
    "📆 Неделя": RoutedIntent(intent=Intent.SHOW_AGENDA, date_text="на неделе"),
    "🔥 Главное": RoutedIntent(intent=Intent.SHOW_FOCUS),
    "🧭 План дня": RoutedIntent(intent=Intent.PLAN),
    "💡 Идеи": RoutedIntent(intent=Intent.SEARCH_IDEAS),
    "📝 Заметки": RoutedIntent(intent=Intent.SHOW_NOTES),
    "💊 Лекарства": RoutedIntent(intent=Intent.MED_LIST),
    "📊 Статистика": RoutedIntent(intent=Intent.SHOW_USAGE),
}

# «⋯ Ещё»: (callback key, label, what it opens). Keys are a closed set checked in `more_intent`.
MORE_ITEMS: list[tuple[str, str, RoutedIntent | None]] = [
    ("tmr", "🗓 Завтра", RoutedIntent(intent=Intent.SHOW_AGENDA, date_text="завтра")),
    ("week", "📆 Неделя", RoutedIntent(intent=Intent.SHOW_AGENDA, date_text="на неделе")),
    ("focus", "🔥 Главное", RoutedIntent(intent=Intent.SHOW_FOCUS)),
    ("plan", "🧭 План дня", RoutedIntent(intent=Intent.PLAN)),
    ("ideas", "💡 Идеи", RoutedIntent(intent=Intent.SEARCH_IDEAS)),
    ("notes", "📝 Заметки", RoutedIntent(intent=Intent.SHOW_NOTES)),
    ("meds", "💊 Лекарства", RoutedIntent(intent=Intent.MED_LIST)),
    ("money", "💸 Расходы", RoutedIntent(intent=Intent.EXPENSE_SHOW, query="расходы")),
    ("stats", "📊 Статистика", RoutedIntent(intent=Intent.SHOW_USAGE)),
    ("set", SETTINGS_BUTTON, None),  # the settings panel (not an intent)
]


def main_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": label} for label in row] for row in MAIN_MENU],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Напишите или скажите…",
    }


def menu_intent(text: str) -> RoutedIntent | None:
    routed = _ACTIONS.get(text.strip())
    return routed.model_copy() if routed is not None else None


def more_keyboard() -> dict[str, Any]:
    """Two per row: short labels stay readable on a phone."""
    buttons = [InlineButton(label, f"mm:{key}") for key, label, _ in MORE_ITEMS]
    return inline_keyboard([buttons[n : n + 2] for n in range(0, len(buttons), 2)])


def more_intent(key: str) -> tuple[str, RoutedIntent | None] | None:
    """(label, intent) of a «⋯ Ещё» button; the settings panel has no intent; unknown keys → None."""
    for item_key, label, routed in MORE_ITEMS:
        if item_key == key:
            return label, routed.model_copy() if routed is not None else None
    return None


def settings_keyboard(voice_on: bool, voice: str, voices: tuple[str, ...]) -> dict[str, Any]:
    """Voice on/off, other voices only while voice is on; status (it also syncs) and help."""
    rows = [
        [InlineButton("🔇 Отвечать текстом", "st:voff") if voice_on else InlineButton("🔊 Отвечать голосом", "st:von")]
    ]
    others = [v for v in voices if v != voice][:3] if voice_on else []
    if others:
        rows.append([InlineButton(f"🎙 {name}", f"st:v:{name}") for name in others])
    rows.append([InlineButton("🩺 Статус и синхронизация", "st:status"), InlineButton("❓ Помощь", "st:help")])
    return inline_keyboard(rows)
