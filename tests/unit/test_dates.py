"""Deterministic Russian date phrases (agenda periods, reschedule targets)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from jarvis.core.dates import (
    Shift,
    When,
    add_months,
    human_day,
    looks_like_period,
    parse_day,
    parse_period,
    parse_shift,
    parse_when,
    round_up,
)

WED = date(2026, 9, 23)  # Wednesday


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("сегодня", "2026-09-23", "2026-09-23"),
        ("завтра", "2026-09-24", "2026-09-24"),
        ("на завтра?", "2026-09-24", "2026-09-24"),
        ("послезавтра", "2026-09-25", "2026-09-25"),
        ("в пятницу", "2026-09-25", "2026-09-25"),
        ("пятницу", "2026-09-25", "2026-09-25"),
        ("в среду", "2026-09-23", "2026-09-23"),  # today is Wednesday
        ("в следующую пятницу", "2026-10-02", "2026-10-02"),
        ("25.09", "2026-09-25", "2026-09-25"),
        ("25.09.2026", "2026-09-25", "2026-09-25"),
        ("25 сентября", "2026-09-25", "2026-09-25"),
        ("1 октября", "2026-10-01", "2026-10-01"),
        ("2026-10-01", "2026-10-01", "2026-10-01"),
        ("25-го", "2026-09-25", "2026-09-25"),
        ("через 3 дня", "2026-09-26", "2026-09-26"),
        ("через неделю", "2026-09-30", "2026-09-30"),
        ("на неделе", "2026-09-23", "2026-09-29"),
        ("на этой неделе", "2026-09-23", "2026-09-27"),
        ("на следующей неделе", "2026-09-28", "2026-10-04"),
        ("на выходных", "2026-09-26", "2026-09-27"),
        ("ближайшие 3 дня", "2026-09-23", "2026-09-25"),
        ("с 25.09 по 30.09", "2026-09-25", "2026-09-30"),
        ("25.09-30.09", "2026-09-25", "2026-09-30"),
        ("сегодня и завтра", "2026-09-23", "2026-09-24"),
    ],
)
def test_parse_period(phrase: str, start: str, end: str) -> None:
    period = parse_period(phrase, WED)
    assert period is not None, phrase
    assert (period.start.isoformat(), period.end.isoformat()) == (start, end)


@pytest.mark.parametrize("phrase", ["", "привет", "я говорил про inbox", "через день", "10", "стрижку"])
def test_not_a_period(phrase: str) -> None:
    assert parse_period(phrase, WED) is None


def test_year_rollover_and_limits() -> None:
    assert parse_day("25.01", date(2026, 12, 20)) == date(2027, 1, 25)  # closest occurrence
    assert parse_day("20.09", WED) == date(2026, 9, 20)  # a recent past date stays in the past
    assert parse_day("31.02", WED) is None
    assert parse_day("29.02", date(2026, 10, 17)) == date(2028, 2, 29)  # L9: two ordinary years in a row
    assert parse_day("29.02", date(2027, 3, 1)) == date(2028, 2, 29)
    assert parse_day("31.04", date(2026, 10, 17)) is None
    weekend_on_sunday = parse_period("на выходных", date(2026, 9, 27))
    assert weekend_on_sunday is not None
    assert weekend_on_sunday.start == weekend_on_sunday.end == date(2026, 9, 27)
    month = parse_period("с 01.10 по 30.12", WED)
    assert month is not None
    assert (month.end - month.start).days == 30  # clipped to 31 days


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("завтра в 10", When(date(2026, 9, 24), time(10, 0))),
        ("в пятницу в 18:30", When(date(2026, 9, 25), time(18, 30))),
        ("на пятницу", When(date(2026, 9, 25), None)),
        ("в 7 вечера", When(None, time(19, 0))),
        ("в 12 ночи", When(None, time(0, 0))),
        ("на 18:00", When(None, time(18, 0))),
        ("завтра утром", When(date(2026, 9, 24), time(9, 0))),
        ("25.09 в 9 утра", When(date(2026, 9, 25), time(9, 0))),
        ("в 5 октября", When(date(2026, 10, 5), None)),  # a date, not 05:00
    ],
)
def test_parse_when(phrase: str, expected: When) -> None:
    assert parse_when(phrase, WED) == expected


@pytest.mark.parametrize("phrase", ["работу на завтра", "10", "в 25:00", "как-нибудь"])
def test_parse_when_rejects_leftovers(phrase: str) -> None:
    assert parse_when(phrase, WED) is None


def test_human_day_and_recognition() -> None:
    assert human_day(WED, WED) == "сегодня, ср 23.09"
    assert human_day(date(2026, 9, 24), WED) == "завтра, чт 24.09"
    assert human_day(date(2027, 1, 2), WED) == "сб 02.01.2027"
    assert looks_like_period("на выходных")
    assert not looks_like_period("я говорил про inbox")


# --- moving a task by voice (2026-10-01): shifts, spoken hours, day parts ------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("на 2 часа позже", Shift(minutes=120)),
        ("на час позже", Shift(minutes=60)),
        ("на полчаса раньше", Shift(minutes=-30)),
        ("на полтора часа", Shift(minutes=90)),
        ("ещё на 15 минут", Shift(minutes=15)),
        ("на два часа", Shift(minutes=120)),
        ("на пару дней", Shift(days=2)),
        ("на 3 дня вперёд", Shift(days=3)),
        ("на день раньше", Shift(days=-1)),
        ("на неделю", Shift(days=7)),
        ("на месяц", Shift(months=1)),
        ("через 2 часа", Shift(minutes=120, from_now=True)),
        ("через полчаса", Shift(minutes=30, from_now=True)),
    ],
)
def test_parse_shift(phrase: str, expected: Shift) -> None:
    assert parse_shift(phrase) == expected


@pytest.mark.parametrize(
    "phrase", ["на завтра", "на пятницу", "на 18:00", "на 5 октября", "через 3 дня", "на выходные", "на минут 20"]
)
def test_not_a_shift(phrase: str) -> None:
    assert parse_shift(phrase) is None  # dates stay dates («через 3 дня» is parse_day's)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("в шесть вечера", When(None, time(18, 0))),
        ("завтра в девять утра", When(date(2026, 9, 24), time(9, 0))),
        ("в час дня", When(None, time(13, 0))),
        ("в полдень", When(None, time(12, 0))),
        ("в два", When(None, time(14, 0))),  # 1…5 without «утра/ночи» is the afternoon
        ("завтра в 3", When(date(2026, 9, 24), time(15, 0))),
        ("в 5 утра", When(None, time(5, 0))),
        ("в пятницу на вечер", When(date(2026, 9, 25), time(19, 0))),
        ("завтра после обеда", When(date(2026, 9, 24), time(14, 0))),
    ],
)
def test_spoken_times(phrase: str, expected: When) -> None:
    assert parse_when(phrase, WED) == expected


def test_add_months_and_round_up() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert round_up(datetime(2026, 9, 23, 22, 13, 40)) == datetime(2026, 9, 23, 22, 15)
    assert round_up(datetime(2026, 9, 23, 22, 15)) == datetime(2026, 9, 23, 22, 15)
