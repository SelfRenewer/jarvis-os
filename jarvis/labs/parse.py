"""Blood-test reports → rows, by code (no LLM: lab results and the patient's data never leave the Mac).

Built on the text layer of a Gemotest PDF (macOS PDFKit), the usual layout of Russian labs: «Название значение
единицы норма», e.g. «Гемоглобин 131 г/л 108 - 145». The lab's own out-of-range marks are kept: «310-» (below),
«1.16+» / «29.9++» (above). A name may span lines and come before its value («Холестерин ЛПНП» / «(венозная кровь)
A09.05.028 …» / «Дата исследования …» / «2.92 ммоль/л Смотри текст»); the lab's notes («Нормальный уровень <2,59»)
go to the row before them. Lines with the patient's name, address, birth date, the lab's licences and footers are
dropped. Qualitative rows whose columns the PDF scrambles (infection panels) are reported as «not parsed».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_UNIT = (
    r"(?:-|%|сек\.?|фл|пг|г/л|мг/л|мкг/л|нг/мл|пг/мл|нг/дл|мкг/дл|мм/час|ммоль/л|мкмоль/л|нмоль/л|пмоль/л|МЕ/мл|"
    r"мкМЕ/мл|мкЕд/мл|Ед/л|x10\*\d+/л|10\*\d+/л)"
)
_ROW = re.compile(
    rf"^(?P<name>.*?[A-Za-zА-Яа-яЁё)].*?)\s+(?P<text>(?P<sign>[<>])?(?P<num>\d+(?:[.,]\d+)?)(?P<mark>\+{{1,2}}|-{{1,2}})?)"
    rf"(?:\s+|(?=-))(?P<unit>{_UNIT})(?:\s+(?P<ref>.+))?$"
)
_VALUE_ONLY = re.compile(
    rf"^(?P<text>(?P<sign>[<>])?(?P<num>\d+(?:[.,]\d+)?)(?P<mark>\+{{1,2}}|-{{1,2}})?)(?:\s+|(?=-))(?P<unit>{_UNIT})"
    rf"(?:\s+(?P<ref>.+))?$"
)
_NO_UNIT = re.compile(  # «Индекс инсулинорезистентности HOMA-IR 3.0+ < 2.7»: a ratio has no unit
    r"^(?P<name>.*?[A-Za-zА-Яа-яЁё)].*?)\s+(?P<text>(?P<sign>[<>])?(?P<num>\d+(?:[.,]\d+)?)(?P<mark>\+{1,2}|-{1,2})?)"
    r"\s+(?P<ref>[<>≤≥]\s*\d.*|\d+(?:[.,]\d+)?\s*-\s*\d.*)$"
)
_QUALITATIVE = re.compile(
    r"^(?P<name>[^\d<>].*?)\s+(?P<text>(?:(?:ДНК|РНК)\s+не\s+)?(?:не\s+обнаружен[аоы]?|обнаружен[аоы]?|"
    r"отрицательный|положительный|III\(B\)|II\(A\)|I\(0\)|IV\(AB\)))(?=\s|-|$)"
)
_CODE = re.compile(r"\s*\b[AB]\d{2}\.\d{2,3}\.\d{3}(?:\.\d{3})?(?:,\s*[AB]\d{2}\.\d{2,3}\.\d{3}(?:\.\d{3})?)*")
_ORDER = re.compile(r"\(\s*Приказ МЗ РФ № ?\s*804н\s*\)|\(\s*Приказ[^)]*$|^(?:МЗ\s+)?(?:РФ\s+№\s*)?804н\)\s*")
_DATE = re.compile(r"Дата регистрации заказа\s+(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})")
_ORDER_NO = re.compile(r"№ заказа\s*:?\s*(?P<no>\d{5,})")
_LAB = re.compile(r'^ООО\s+["«](?P<lab>[А-ЯЁA-Z][^"»]*)["»]', re.M)
_SKIP = re.compile(
    r"^(?:Лабораторный комплекс|Л\d{3}-|ГОСТ|Сертификат|ООО|8 800|№ заказа|Дата регистрации|Пол пациента|"
    r"Адрес|Номер истории|пациента|Краснодар|Палата|Диагноз|Исследование|значения|Заведующий|Порваткина|Гумеровна|"
    r"Результат лабораторных|За интерпретацией|Получая данный|ПЕЧАТЬ|Дата исследования|Метод:|Набор реагентов|"
    r"Полный чек-ап|\d+ инфекций|ИММУНОГЕМАТОЛОГИЧЕСКИЕ|\f)"
)
_NOTE = re.compile(
    r"^(?:[<>=≤≥]|\d|(?:Нормальный|Умеренно-?\w*|Повышенный|Высокий|Низкий|Средний)(?:\s+уровень)?\s*[<>\d]|"
    r"Для людей|Биологические|"
    r"В соответствии|При |утро|день|Расчетный|Рекомендуется|К данному|крови\.|Клинические|до \d|Исследование проведено|"
    r"Стратификация|Концентрация железа|суточным|организме|текст\)|\*)",
    re.I,
)
_QUAL_GARBLE = re.compile(r"(?:не обнаружен|обнаружена|РНК не|ДНК не)", re.I)


@dataclass
class LabRow:
    name: str
    value_text: str  # as printed, the lab's mark included: «310-», «<1.3», «положительный»
    value: float | None
    unit: str | None
    reference: str | None
    ref_low: float | None
    ref_high: float | None
    flag: str | None  # low | high
    note: str = ""


@dataclass
class LabReport:
    taken_on: date | None
    rows: list[LabRow] = field(default_factory=list)
    skipped: int = 0  # qualitative lines whose columns the PDF scrambled
    source: str | None = None  # «Гемотест №100000001»: the lab and the order number, when printed


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_reference(ref: str | None) -> tuple[float | None, float | None]:
    """«108 - 145» → (108, 145); «< 15 (Смотри текст)» → (None, 15); «> 30» → (30, None); «Смотри текст» → none."""
    if not ref:
        return None, None
    text = ref.replace("*", " ")
    if m := re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)", text):
        return _num(m.group(1)), _num(m.group(2))
    if m := re.match(r"^\s*[<≤]\s*(\d+(?:[.,]\d+)?)", text):
        return None, _num(m.group(1))
    if m := re.match(r"^\s*[>≥]\s*(\d+(?:[.,]\d+)?)", text):
        return _num(m.group(1)), None
    return None, None


def _clean_name(name: str) -> str:
    name = re.sub(r"Дата исследования:\s*\d{2}\.\d{2}\.\d{4};?", " ", name)
    name = _ORDER.sub(" ", _CODE.sub(" ", name))
    name = re.sub(r"\(венозная кровь\),?|\s-\s*кач\.|,?\s*кач\.$|\s+(?:ДНК|РНК)\s+не$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" ,.;:")
    return name


def _row(name: str, m: re.Match[str]) -> LabRow:
    ref = (m.group("ref") or "").strip() or None
    if ref:
        ref = re.sub(r"\s*\(Смотри\s*$", " (Смотри текст)", ref)
    low, high = parse_reference(ref)
    value = _num(m.group("num")) if not m.group("sign") else None  # «<1.3» is below the detection limit
    mark = m.group("mark") or ""
    flag: str | None = "high" if mark.startswith("+") else ("low" if mark.startswith("-") else None)
    if flag is None and value is not None:
        if low is not None and value < low:
            flag = "low"
        elif high is not None and value > high:
            flag = "high"
    unit = m.groupdict().get("unit")
    return LabRow(
        _clean_name(name), m.group("text"), value, None if unit in (None, "-") else unit, ref, low, high, flag
    )


def parse_lab_text(text: str) -> LabReport:
    """The text of a lab report → its rows (numbers, references and marks parsed by code)."""
    taken = _DATE.search(text)
    report = LabReport(date(int(taken["y"]), int(taken["m"]), int(taken["d"])) if taken else None)
    lab, order = _LAB.search(text), _ORDER_NO.search(text)
    if lab and order:
        report.source = f"{lab['lab'].split()[0].capitalize()} №{order['no']}"
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _SKIP.match(line):
            if line.startswith(("Дата исследования", "Метод:", "Набор реагентов")):
                continue  # inside a row's block: keep its pending name
            pending = []
            continue
        if m := _QUALITATIVE.match(line):
            report.rows.append(LabRow(_clean_name(m["name"]), m["text"], None, None, None, None, None, None))
            pending = []
            continue
        if _QUAL_GARBLE.search(line):
            report.skipped += 1
            pending = []
            continue
        if (m := _VALUE_ONLY.match(line)) and pending and _is_reference(m["ref"]):
            report.rows.append(_row(" ".join(pending), m))
            pending = []
            continue
        if (m := _ROW.match(line) or _NO_UNIT.match(line)) and not _NOTE.match(line) and _is_reference(m["ref"]):
            report.rows.append(_row(m["name"], m))
            pending = []
            continue
        if _NOTE.match(line) or (report.rows and not pending and _looks_like_note(line)):
            if report.rows:
                report.rows[-1].note = (report.rows[-1].note + " " + line).strip()[:500]
            pending = []
            continue
        pending.append(line)
    return report


def _is_reference(ref: str | None) -> bool:
    """«108 - 145», «< 15 (Смотри текст)», «Смотри текст» or nothing; «- является диагностическим …» is a note."""
    return ref is None or bool(re.match(r"^\s*(?:[<>≤≥]\s*)?\d|^\s*Смотри", ref))


def _looks_like_note(line: str) -> bool:
    """Free text right after a row («Рекомендуется оценивать …») — not a name: names carry a lab code or a value."""
    return bool(re.search(r"[а-яё]{3,}\s+[а-яё]{3,}\s+[а-яё]{3,}\s+[а-яё]{3,}", line)) and not _CODE.search(line)
