"""Blood-test reports parsed by code: values, units, references, the lab's own marks and notes (no patient data)."""

from __future__ import annotations

from datetime import date

import pytest

from jarvis.bot.fmt import strip_tags
from jarvis.bot.intents import Intent, keyword_route
from jarvis.labs.parse import parse_lab_text, parse_reference
from jarvis.labs.service import find, labs_text, lmf_rows

# The layout text of a Gemotest-like report (PDFKit runs joined per baseline); personal lines replaced by «…».
REPORT = """Лабораторный комплекс правообладателя ООО "ЛАБ
ООО "ГЕМОТЕСТ", лицензия Л000-00000-00/00000000 от 01.01.2020
№ заказа 100000002 Фамилия пациента …
Дата регистрации заказа 10.03.2026 Имя пациента …
Исследование Значение Ед. изм. Нормальные
значения
Гемоглобин 138 г/л 108 - 145
Средняя концентрация Hb в эритроцитах (МСНС) 308- г/л 315 - 348
Цветовой показатель 0.78--- 0.85 - 1.00
Дата исследования: 12.03.2026;
Холестерин липопротеинов низкой плотности (ЛПНП) 3.05 ммоль/л Смотри текст
(венозная кровь) A09.05.028 (Приказ МЗ РФ № 804н)
Дата исследования: 12.03.2026;
Нормальный уровень <2,59
Умеренно-повышенный 2,59-3,34
Холестерин липопротеинов высокой плотности (ЛПВП) 1.03- ммоль/л Смотри текст
(венозная кровь) A09.05.004 (Приказ МЗ РФ № 804н)
Индекс инсулинорезистентности HOMA-IR A09.05.056, A09.05.023 (Приказ МЗ РФ № 804н)
Дата исследования: 12.03.2026;
Глюкоза 4.70 ммоль/л 4.11 - 6.1
Индекс инсулинорезистентности HOMA-IR 2.9+ < 2.7
Ферритин A09.05.076 (Приказ МЗ РФ № 804н) 48.0 нг/мл Смотри текст
Дата исследования: 14.03.2026;
Биологические референсные интервалы: 30.0-400 нг/мл
Тропонин I, высокочувствительный A09.05.193 (Приказ МЗ РФ № <1.5 пг/мл < 34.2 (Смотри текст)
Пролактин 21.4++ нг/мл 4.04 - 15.2
Вирус гепатита C - кач. РНК не РНК не обнаружена
Группа крови II(A) - -
"""


def rows_by_name() -> dict[str, object]:
    return {r.name: r for r in parse_lab_text(REPORT).rows}


def test_values_units_references_and_the_labs_marks() -> None:
    report = parse_lab_text(REPORT)
    assert report.taken_on == date(2026, 3, 10) and report.source == "Гемотест №100000002"
    rows = rows_by_name()
    hb = rows["Гемоглобин"]
    assert (hb.value, hb.unit, hb.ref_low, hb.ref_high, hb.flag) == (138, "г/л", 108, 145, None)  # type: ignore[attr-defined]
    assert rows["Средняя концентрация Hb в эритроцитах (МСНС)"].flag == "low"  # type: ignore[attr-defined]
    color = rows["Цветовой показатель"]
    assert (color.value_text, color.unit, color.flag) == ("0.78--", None, "low")  # type: ignore[attr-defined]
    assert rows["Пролактин"].flag == "high"  # type: ignore[attr-defined]
    homa = rows["Индекс инсулинорезистентности HOMA-IR"]
    assert (homa.value, homa.unit, homa.ref_high, homa.flag) == (2.9, None, 2.7, "high")  # type: ignore[attr-defined]


def test_a_name_spanning_lines_and_the_labs_notes() -> None:
    rows = rows_by_name()
    ldl = rows["Холестерин липопротеинов низкой плотности (ЛПНП)"]
    assert ldl.value == 3.05 and ldl.reference == "Смотри текст"  # type: ignore[attr-defined]
    assert "Нормальный уровень <2,59" in ldl.note  # type: ignore[attr-defined]
    ferritin = rows["Ферритин"]
    assert ferritin.value == 48.0 and "30.0-400" in ferritin.note  # type: ignore[attr-defined]
    assert rows["Холестерин липопротеинов высокой плотности (ЛПВП)"].flag == "low"  # type: ignore[attr-defined]


def test_below_detection_limit_and_qualitative_rows() -> None:
    rows = rows_by_name()
    troponin = rows["Тропонин I, высокочувствительный"]
    assert (troponin.value, troponin.value_text, troponin.ref_high) == (None, "<1.5", 34.2)  # type: ignore[attr-defined]
    assert rows["Вирус гепатита C"].value_text == "РНК не обнаружена"  # type: ignore[attr-defined]
    assert rows["Группа крови"].value_text == "II(A)"  # type: ignore[attr-defined]


def test_patient_lines_never_become_rows() -> None:
    names = " ".join(rows_by_name())
    assert "пациента" not in names and "заказа" not in names and "ООО" not in names


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("108 - 145", (108, 145)),
        ("< 15 (Смотри текст)", (None, 15)),
        ("> 30", (30, None)),
        ("Смотри текст", (None, None)),
    ],
)
def test_references(ref: str, expected: tuple[float | None, float | None]) -> None:
    assert parse_reference(ref) == expected


def test_rows_for_lmf_and_the_answers() -> None:
    rows = lmf_rows(parse_lab_text(REPORT), "Гемотест №100000002")
    assert {r["date"] for r in rows} == {"2026-03-10"} and len(rows) == len({r["name"] for r in rows})
    assert [r["name"] for r in find(rows, "ферритином")] == ["Ферритин"]
    text = strip_tags(labs_text(rows))
    assert "10.03.2026" in text and "Пролактин — 21.4 нг/мл ↑ — норма 4.04 - 15.2" in text
    assert "решает врач" in text
    one = strip_tags(labs_text(rows, "ферритин"))
    assert "10.03.2026: 48.0 нг/мл" in one and "30.0-400" in one
    assert "нет «тиреоглобулин»" in strip_tags(labs_text(rows, "тиреоглобулин"))


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("мои анализы", None),
        ("Покажи анализы", None),
        ("что по анализам?", None),
        ("какой ферритин в анализах?", "ферритин"),
        ("что с витамином D по анализам", "витамином d"),
    ],
)
def test_labs_phrases(text: str, query: str | None) -> None:
    routed = keyword_route(text)
    assert routed.intent == Intent.LABS_SHOW and routed.query == query


@pytest.mark.parametrize("text", ["напомни сдать анализы завтра", "сдать анализы в пятницу"])
def test_a_task_about_tests_is_not_a_query(text: str) -> None:
    assert keyword_route(text).intent != Intent.LABS_SHOW


def test_a_query_word_starts_a_name_word() -> None:
    rows = [{"name": n} for n in ("Ферритин", "Трансферрин", "Витамин D суммарный (25-OH D2 и D3)", "Витамин B12")]
    assert [r["name"] for r in find(rows, "ферритином")] == ["Ферритин"]
    assert [r["name"] for r in find(rows, "витамином D")] == ["Витамин D суммарный (25-OH D2 и D3)"]
