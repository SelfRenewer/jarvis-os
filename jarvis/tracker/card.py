"""📓 Дневник: today's TLT card and the step-by-step filling («заполни день») — rendering and parsing only.

The bot does the I/O; nothing here writes anything. Filling ends in the usual draft with «✅ Записать».
Steps: sleep → day score → mood → what happened; a step already filled in TLT today is skipped. Every step takes a
button or free words («7:30, качество 85», «на семёрку», «тренировка и холодный душ»); the numbers are parsed by
code (`tracker.parse`), never by the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from jarvis.bot import fmt
from jarvis.core.dates import human_day
from jarvis.tracker.parse import mood_key, parse_day_report
from jarvis.tracker.service import MOOD_WORDS, Dictionary, Tag, _num, day_rows, hm

FLOW_STEPS = ("sleep", "score", "mood", "tags")
MOOD_LABEL = MOOD_WORDS
PILL_STATUS = {
    "taken": "✅ выпиты",
    "skipped": "✖️ пропущены",
    "missed": "⚠️ не отмечены",
    "pending": "⏳ ждут отметки",
    "planned": "· впереди",
}


@dataclass
class Pill:
    """One pill reminder of today, for the card."""

    emoji: str
    name: str
    at: str
    status: str


def missing_steps(day: dict[str, Any]) -> list[str]:
    """Steps of the filling that today's TLT still lacks (always ends with «what happened»)."""
    steps = []
    if day.get("sleep_minutes") is None:
        steps.append("sleep")
    if day.get("score") is None:
        steps.append("score")
    if not day.get("mood"):
        steps.append("mood")
    steps.append("tags")
    return steps


def quick_tags(dictionary: Dictionary, have: set[int], limit: int = 3) -> list[Tag]:
    """The most used tags that today does not have yet (one tap → a draft)."""
    pool = sorted((t for t in dictionary.tags if t.uses > 0 and t.id not in have), key=lambda t: (-t.uses, t.code))
    out: list[Tag] = []
    for tag in pool:
        if all(t.code != tag.code for t in out):
            out.append(tag)
        if len(out) == limit:
            break
    return out


def tag_button_label(tag: Tag) -> str:
    """«＋ Холодный душ» (the Russian part of the label), else the code."""
    words = tag.label.split("—")[-1].strip()
    name = words if words and words != tag.code and re.search(r"[а-яё]", words, re.I) else tag.code
    name = name[:1].upper() + name[1:]
    return f"＋ {name[:18]}"


def card_text(
    day: date,
    today: date,
    data: dict[str, Any] | None,
    dictionary: Dictionary | None,
    food: dict[str, Any] | None,
    pills: list[Pill],
) -> fmt.Html:
    """`data` None = LMF is not reachable now (the card still shows the pills)."""
    header = fmt.title("📓", "Дневник", human_day(day, today))
    if data is None:
        filled: fmt.Html | None = fmt.hint("TLT сейчас не отвечает — LMF выключен? Скажите «открой сайт TLT».")
        gaps: list[str] = []
    else:
        filled, gaps = _filled(data, dictionary, food)
    sections: list[object] = [header, filled]
    if data is not None and gaps:
        sections.append(fmt.line(fmt.b("Осталось:"), ", ".join(gaps)))
    elif data is not None:
        sections.append(fmt.lines("✅ " + "День заполнен.", fmt.hint("Добавить ещё — просто скажите.")))
    if pills:
        sections.append(
            fmt.lines(
                fmt.head("Таблетки", "💊"),
                [fmt.line(f"{p.emoji} {p.name} {p.at}", "—", PILL_STATUS.get(p.status, p.status)) for p in pills],
                fmt.hint("«✅ Выпил всё» в напоминании записывает их в TLT само — отдельно ничего говорить не нужно."),
            )
        )
    sections.append(fmt.lines(fmt.head("Скажите, например", "💬"), fmt.say(*_examples(gaps))))
    return fmt.blocks(*sections)


def _filled(
    data: dict[str, Any], dictionary: Dictionary | None, food: dict[str, Any] | None
) -> tuple[fmt.Html | None, list[str]]:
    rows, gaps = day_rows(data, dictionary, food)
    if not rows:
        return fmt.hint("Пока пусто — заполним за минуту?"), gaps
    return fmt.lines(fmt.head("Записано", "✅"), rows), gaps


def _examples(gaps: list[str]) -> list[str]:
    examples = []
    if "сон" in gaps:
        examples.append("спал 7:30, качество 85")
    if "оценка" in gaps or "настроение" in gaps:
        examples.append("оценка 7, настроение хорошее")
    examples.append("сегодня была тренировка и холодный душ")
    if len(examples) < 2:
        examples.append("выпил креатин 5 г")
    return examples[:2]


# --- step by step ------------------------------------------------------------------------------------------------

STEP_FIELD = {"sleep": "sleep_minutes", "score": "score", "mood": "mood"}
# option → value. One character each (the choice callback carries it); «n» skips a step, «d» finishes now.
SLEEP_OPTIONS = {"a": 360, "b": 420, "c": 450, "e": 480}
SCORE_OPTIONS = {str(n): n for n in (5, 6, 7, 8, 9)}
MOOD_OPTIONS = {"g": "good", "u": "neutral", "b": "bad"}


def step_options(step: str, tags: list[Tag]) -> list[tuple[str, str]]:
    """(option, button label) of a step (without «skip» / «done»)."""
    if step == "sleep":
        return [(key, hm(minutes).replace(":00", " ч")) for key, minutes in SLEEP_OPTIONS.items()]
    if step == "score":
        return [(key, key) for key in SCORE_OPTIONS]
    if step == "mood":
        return [("g", "🟢 Хорошее"), ("u", "🟡 Так себе"), ("b", "🔴 Плохое")]
    return [(str(n), tag_button_label(tag)) for n, tag in enumerate(tags[:3])]


def option_value(step: str, option: str) -> tuple[str, Any] | None:
    """A tapped option → (field, value); tag options are resolved by the caller."""
    if step == "sleep" and option in SLEEP_OPTIONS:
        return "sleep_minutes", SLEEP_OPTIONS[option]
    if step == "score" and option in SCORE_OPTIONS:
        return "score", SCORE_OPTIONS[option]
    if step == "mood" and option in MOOD_OPTIONS:
        return "mood", MOOD_OPTIONS[option]
    return None


def step_prompt(
    step: str, number: int, total: int, collected: dict[str, Any], chosen: list[str] | None = None
) -> fmt.Html:
    progress = f"шаг {number} из {total}"
    if step == "sleep":
        body = fmt.lines(
            fmt.title("🛌", "Сколько спали?", progress), fmt.hint("Можно с качеством: «7:30, качество 85».")
        )
    elif step == "score":
        body = fmt.lines(fmt.title("⭐", "Оценка дня от 0 до 10?", progress), fmt.hint("Или словами: «на семёрку»."))
    elif step == "mood":
        body = fmt.title("🙂", "Какое настроение?", progress)
    else:
        body = fmt.lines(
            fmt.title("🏷", "Что было за день?", progress),
            fmt.hint("Своими словами: «тренировка, холодный душ, читал» — или кнопкой. Нечего добавить — «✅ Готово»."),
        )
    done = collected_line(collected, chosen)
    return fmt.lines(fmt.hint(done) if done else None, body)


def collected_line(fields: dict[str, Any], chosen: list[str] | None = None) -> str:
    """«Уже есть: сон 7:30 · оценка 7 · + CD» — what the steps collected so far (`chosen`: tag codes)."""
    bits = []
    if fields.get("sleep_minutes"):
        quality = fields.get("sleep_quality")
        bits.append(f"сон {hm(fields['sleep_minutes'])}" + (f" ({quality})" if quality else ""))
    if fields.get("score") is not None:
        bits.append(f"оценка {_num(fields['score'])}")
    if fields.get("mood"):
        bits.append(f"настроение {MOOD_LABEL.get(fields['mood'], fields['mood'])}")
    if chosen:
        bits.append("+ " + ", ".join(chosen))
    return ("Уже есть: " + " · ".join(bits)) if bits else ""


_PREFIX = {"sleep": "сон", "score": "оценка", "mood": "настроение"}


@dataclass
class StepAnswer:
    fields: dict[str, Any]
    keep_text: bool  # the words carry more (tags, a full report): parsed again with the dictionary at the end


def parse_answer(step: str, text: str) -> StepAnswer | None:
    """Free words as the answer to a step; None when they are not an answer (then they are a new request).

    Sleep / score / mood need that value («7:30», «на семёрку», «так себе» — said without the word too); «what
    happened» takes any words (the caller first rules out other commands)."""
    report = parse_day_report(text)
    fields = dict(report.fields)
    extra = bool(report.codes or report.words())
    field = STEP_FIELD.get(step)
    if field is None:
        return StepAnswer(fields, keep_text=True) if fields or extra else None
    if field not in fields:
        prefixed = parse_day_report(f"{_PREFIX[step]} {text}")  # the value said without its word
        if field in prefixed.fields:
            fields.update(prefixed.fields)
            extra = bool(prefixed.codes or prefixed.words())
        elif step == "mood" and text.strip() and (key := mood_key(text.strip().split()[0])):
            fields["mood"], extra = key, False
    if not fields:
        return None
    return StepAnswer(fields, keep_text=extra)


def flow_payload(day: date, steps: list[str]) -> dict[str, Any]:
    """The state of a filling, kept in the pending choice of each step."""
    return {"day": day.isoformat(), "steps": steps, "index": 0, "fields": {}, "tags": [], "texts": []}
