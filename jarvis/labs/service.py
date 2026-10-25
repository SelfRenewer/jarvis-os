"""Blood tests in LMF: rows for its «Анализы» section and the answers to «мои анализы» / «какой ферритин в анализах».

Reference only: the out-of-range marks are the lab's; Jarvis never interprets results or suggests medicines or doses
(a doctor's plan can be put on the schedule as a course with an end date).
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.bot import fmt
from jarvis.labs.parse import LabReport

ARROW = {"high": "↑", "low": "↓"}
_STOP = {"и", "в", "на", "по", "с", "мой", "мои", "моя", "мое", "моё", "у", "меня", "уровень", "анализ"}


def lmf_rows(report: LabReport, source: str) -> list[dict[str, Any]]:
    """A parsed report → rows for `POST /api/labs` (one per analyte; upserted by date + name + source)."""
    if report.taken_on is None:
        raise ValueError("no «Дата регистрации заказа» in the report")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for r in report.rows:
        if r.name in seen:  # the same analyte printed twice: the first row wins
            continue
        seen.add(r.name)
        rows.append(
            {
                "date": report.taken_on.isoformat(),
                "name": r.name,
                "value": r.value,
                "value_text": r.value_text,
                "unit": r.unit,
                "reference": r.reference,
                "ref_low": r.ref_low,
                "ref_high": r.ref_high,
                "flag": r.flag,
                "note": r.note or None,
                "source": source,
            }
        )
    return rows


def _day(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def _value(row: dict[str, Any]) -> str:
    text = re.sub(r"[+-]+$", "", str(row["value_text"]))  # the lab's mark is shown as an arrow
    unit = f" {row['unit']}" if row.get("unit") else ""
    arrow = f" {ARROW[row['flag']]}" if row.get("flag") in ARROW else ""
    return f"{text}{unit}{arrow}"


def _norm(row: dict[str, Any]) -> str | None:
    ref = str(row.get("reference") or "")
    if not ref:
        return None
    if ref.lower().startswith("смотри"):
        return "норма в пояснении лаборатории"
    return "норма " + re.sub(r"\s*\(Смотри текст\)", "", ref)


def find(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Analytes where every word of the query (its first 5 letters) starts a word of the name: «ферритином» →
    «Ферритин» (not «Трансферрин»), «витамин D» → «Витамин D суммарный…»."""
    words = [w[:5] for w in _words(query) if w not in _STOP]
    if not words:
        return []
    return [r for r in rows if all(any(n.startswith(w) for n in _words(str(r["name"]))) for w in words)]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))


def labs_text(rows: list[dict[str, Any]], query: str | None = None) -> fmt.Html:
    if not rows:
        return fmt.lines(
            fmt.title("🧪", "Анализы"),
            "В LMF анализов пока нет.",
            fmt.hint("Импорт из PDF лаборатории: ./scripts/labs_import.sh <файл.pdf>"),
        )
    if query:
        found = sorted(find(rows, query), key=lambda r: (str(r["name"]), str(r["date"])), reverse=True)
        if not found:
            return fmt.lines(f"🧪 В анализах нет «{query}».", fmt.hint("Все показатели — в LMF, раздел «Анализы»."))
        items: list[fmt.Html] = []
        for name in dict.fromkeys(str(r["name"]) for r in found):
            items.append(fmt.b(name))
            for r in (x for x in found if x["name"] == name):
                items.append(fmt.bullet(f"{_day(str(r['date']))}: {_value(r)}", _norm(r)))
                if r.get("note"):
                    items.append(fmt.quote(str(r["note"])[:400], expandable=True))
        return fmt.blocks(
            fmt.lines(fmt.title("🧪", "Анализы", query), items),
            fmt.hint("Справка, не диагноз: что делать с отклонениями — решает врач."),
        )
    latest = max(str(r["date"]) for r in rows)
    current = [r for r in rows if r["date"] == latest]
    off = [r for r in current if r.get("flag")]
    return fmt.blocks(
        fmt.lines(
            fmt.title("🧪", "Анализы", _day(latest)),
            f"Показателей: {len(current)} · вне нормы по отметкам лаборатории: {len(off)}",
        ),
        fmt.section("Вне нормы", (fmt.bullet(f"{r['name']} — {_value(r)}", _norm(r)) for r in off)),
        fmt.lines(
            fmt.hint(
                "Справка, не диагноз: что означают отклонения и что принимать — решает врач. "
                "Его план можно поставить в расписание курсом: «напоминай пить … курсом 8 недель»."
            ),
            fmt.hint("Пояснения к каждому показателю — в LMF, раздел «Анализы»."),
            fmt.say("какой ферритин в анализах?"),
        ),
    )
