"""TLT / TPL phrases are parsed by code: values, days, periods, TPL marks and routing."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from jarvis.bot.intents import Intent, keyword_route
from jarvis.integrations.lmf.client import FakeLifeTracker
from jarvis.tracker.parse import parse_day_report, parse_month, parse_tpl_marks
from jarvis.tracker.service import Dictionary, TplItem, parse_past, same_word

TODAY = date(2026, 10, 13)


@pytest.mark.parametrize(
    ("text", "day", "fields", "codes", "words"),
    [
        ("спал 7:30, качество 85, оценка 7, CD LT", None, {"sleep_minutes": 450, "sleep_quality": 85, "score": 7},
         ["CD", "LT"], []),
        ("TLT: сон 7:30 85, день на семёрку, настроение хорошее", None,
         {"sleep_minutes": 450, "sleep_quality": 85, "score": 7, "mood": "good"}, [], []),
        ("вчера спал 7 с половиной часов, оценка дня 6", "вчера", {"sleep_minutes": 450, "score": 6}, [], []),
        ("спал 8 часов, 7 из 10", None, {"sleep_minutes": 480, "score": 7}, [], []),
        ("7 часов 20 минут сна, качество сна 80", None, {"sleep_minutes": 440, "sleep_quality": 80}, [], []),
        ("качество сна 85, спал 7:30", None, {"sleep_minutes": 450, "sleep_quality": 85}, [], []),
        ("спал 7,5 часа", None, {"sleep_minutes": 450}, [], []),
        ("сегодня была тренировка, холодный душ и W+", "сегодня", {}, ["W+"], ["тренировка", "холодный", "душ"]),
        ("зарядка 5 минут", None, {}, [], ["зарядка", "минут"]),  # not recorded since 2026-10-14
        ("работал 6 часов, настроение так себе", None, {"mood": "neutral"}, [], ["работал", "часов"]),
        ("за 12.10: оценка 8", "12.10", {"score": 8}, [], []),
        # dictated: numbers in words (found 2026-10-14), «минут» fully consumed
        ("спал семь с половиной часов, оценка восемь, настроение хорошее", None,
         {"sleep_minutes": 450, "score": 8, "mood": "good"}, [], []),
        ("спал семь часов двадцать минут, качество восемьдесят пять", None,
         {"sleep_minutes": 440, "sleep_quality": 85}, [], []),
        ("спал 7 часов 20 минут", None, {"sleep_minutes": 440}, [], []),
        ("сегодня спал шесть часов, оценка семь из десяти", "сегодня", {"sleep_minutes": 360, "score": 7}, [], []),
    ],
)  # fmt: skip
def test_day_report(text: str, day: str | None, fields: dict[str, Any], codes: list[str], words: list[str]) -> None:
    parsed = parse_day_report(text)
    assert (parsed.day_text, parsed.fields, parsed.codes, parsed.words()) == (day, fields, codes, words)


def test_note_and_removal() -> None:
    parsed = parse_day_report("за 12.10: оценка 8, заметка: был у врача, всё ок")
    assert (parsed.fields, parsed.note) == ({"score": 8}, "был у врача, всё ок")
    assert parse_day_report("заметка: был один дома, пять часов спал").note == "был один дома, пять часов спал"
    removal = parse_day_report("убери GG за вчера")
    assert (removal.remove, removal.codes, removal.day_text) == (True, ["GG"], "вчера")


def test_tpl_marks_and_month() -> None:
    marks = parse_tpl_marks("TPL: отметь утреннюю рутину и чтение, сними медитацию")
    assert (marks.check, marks.uncheck) == (["утреннюю рутину", "чтение"], ["медитацию"])
    assert parse_tpl_marks("отметь в TPL тренировки").check == ["тренировки"]
    assert parse_month("покажи TPL за сентябрь", 2026, 10)[0] == "2026-09"
    assert parse_month("TPL за ноябрь", 2026, 10)[0] == "2025-11"  # a later month is last year's
    assert parse_month("tpl", 2026, 10)[0] == "2026-10"


@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("как я спал на неделе?", (date(2026, 10, 7), TODAY)),
        ("что в TLT за вчера", (date(2026, 10, 12), date(2026, 10, 12))),
        ("TLT за 10.10", (date(2026, 10, 10), date(2026, 10, 10))),
        ("TLT за пятницу", (date(2026, 10, 9), date(2026, 10, 9))),  # a report day is in the past
        ("оценка за сентябрь", (date(2026, 9, 1), date(2026, 9, 30))),
        ("как я спал на прошлой неделе", (date(2026, 10, 5), date(2026, 10, 11))),
        ("сколько я спал в этом месяце", (date(2026, 10, 1), TODAY)),
        ("tlt с 01.10 по 05.10", (date(2026, 10, 1), date(2026, 10, 5))),
        ("tlt", None),
    ],
)
def test_past_periods(text: str, span: tuple[date, date] | None) -> None:
    assert parse_past(text, TODAY) == span


@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("tlt с 25.12 по 01.01", (date(2025, 12, 25), date(2026, 1, 1))),  # L8: last December, not the coming one
        ("сон с 20.12 по 28.12", (date(2025, 12, 20), date(2025, 12, 28))),
        ("TLT за 25.12", (date(2025, 12, 25), date(2025, 12, 25))),
        ("tlt с 30.12 по 05.01", (date(2025, 12, 30), date(2026, 1, 2))),  # cut at today
        ("tlt с 25.12.2025 по 01.01.2026", (date(2025, 12, 25), date(2026, 1, 1))),
    ],
)
def test_past_spans_over_new_year(text: str, span: tuple[date, date]) -> None:
    assert parse_past(text, date(2026, 1, 2)) == span


def test_word_matching_and_tpl_candidates() -> None:
    assert same_word("утреннюю", "утренняя") and same_word("зубы", "зубов") and not same_word("сон", "сна")
    data = FakeLifeTracker().data
    items = [TplItem(i["id"], i["name"], c["name"], c["idx"]) for c in data["categories"] for i in c["items"]]
    d = Dictionary([], items, data["categories"], {})
    assert [i.title for i in d.tpl_candidates("сон")] == ["Здоровье · Сон"]  # not «Качественный сон»
    assert [i.title for i in d.tpl_candidates("утреннюю рутину")] == ["Привычки · Утренняя рутина"]
    assert [i.title for i in d.tpl_candidates("тренировки")] == ["Внешность · Тренировки", "Привычки · Тренировки"]
    assert [i.title for i in d.tpl_candidates("медитацию в отдыхе")] == ["Отдых · Медитация"]
    assert [i.title for i in d.tpl_candidates("отдых")] == ["Здоровье · Отдых"]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("спал 7:30, качество 85, оценка 7, CD LT", Intent.TRACKER_LOG),
        ("TLT: сон 7:30 85. День на семёрку. Настроение хорошее", Intent.TRACKER_LOG),
        ("убери GG за вчера в TLT", Intent.TRACKER_LOG),
        ("запиши в трекер: тренировка и холодный душ", Intent.TRACKER_LOG),
        ("спал шесть часов, оценка семь из десяти", Intent.TRACKER_LOG),  # dictated numbers
        ("оценка дня девять", Intent.TRACKER_LOG),
        ("как я спал на неделе?", Intent.TRACKER_SHOW),
        ("TLT", Intent.DIARY),  # not an ISO currency code; the 📓 card of today
        ("открой TLT", Intent.OS_OPEN),  # TLT in Jarvis OS on the Mac (16.10), not the card
        ("заполни TLT", Intent.DIARY_FILL),
        ("что в TLT за вчера", Intent.TRACKER_SHOW),
        ("TPL: отметь утреннюю рутину и чтение", Intent.TPL_MARK),
        ("отметь в TPL медитацию", Intent.TPL_MARK),
        ("покажи TPL", Intent.TPL_SHOW),
        ("отметь стрижку выполненной", Intent.COMPLETE_TASK),
        ("что у меня завтра?", Intent.SHOW_AGENDA),
    ],
)
def test_routing(text: str, intent: Intent) -> None:
    assert keyword_route(text).intent == intent
