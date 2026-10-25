"""Expenses: record (a phrase → at once with «↩️ Отменить»; a receipt photo → after «✅ Записать»), totals for a
period by category and currency, soft removal. Money: integer minor units + Decimal; conversion to the default
currency uses the same rates as the currency answers (open.er-api) and is shown as «≈».
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.agenda.service import plural
from jarvis.bot import fmt
from jarvis.calc.currency import FxService
from jarvis.core.clock import to_local
from jarvis.core.config import Settings
from jarvis.core.dates import human_day
from jarvis.db.models import Expense
from jarvis.finance.parse import (
    CATEGORIES,
    OTHER,
    ExpenseItem,
    amount_text,
    category_of,
    from_minor,
    receipt_amount,
    to_minor,
)
from jarvis.photos.reader import ReceiptReading
from jarvis.tracker.parse import same_word
from jarvis.tracker.service import parse_past

log = logging.getLogger(__name__)

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь"]  # fmt: skip
_PERIOD = re.compile(
    r"(?:\b(?:за|в|на)\s+|(?<![а-яё]))(?P<p>(?:эт\w+|прошл\w+|текущ\w+|последн\w+)\s+(?:недел\w*|месяц\w*)|недел\w*|месяц\w*"
    r"|сегодня|вчера|\d{1,3}\s+дн\w+|январ\w*|феврал\w*|март\w*|апрел\w*|ма[йея]\b|июн\w*|июл\w*|август\w*"
    r"|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
    r"|\b(?P<t>сегодня|вчера)\b"
)
_NOISE = re.compile(
    r"\b(?:сколько|я|ушло|уходит|трачу|потратил\w*|покажи|мои|мой|расход\w*|трат\w*|финанс\w*|итог\w*|всего|денег"
    r"|по|категори\w*|на|за|в|и|а|money)\b"
)
_RECEIPT_DATE = re.compile(r"(?P<a>\d{1,4})[./-](?P<b>\d{1,2})[./-](?P<c>\d{2,4})")


@dataclass
class SpendQuery:
    start: date
    end: date
    words: list[str]  # «на такси» → only matching expenses
    label: str


def period_label(start: date, end: date, today: date) -> str:
    if start == end:
        return human_day(start, today)
    if start.day == 1 and (end == today or (end + timedelta(days=1)).day == 1) and start.month == end.month:
        return (
            f"{MONTHS[start.month - 1].capitalize()} {start.year}"
            if start.year != today.year
            else (MONTHS[start.month - 1].capitalize())
        )
    return f"{start:%d.%m}–{end:%d.%m}"


def parse_spend_query(text: str, today: date) -> SpendQuery:
    """«сколько ушло на такси за неделю» → last 7 days, words [такси]; no period → this month."""
    low = text.lower().replace("ё", "е")
    span = None
    rest = low
    if m := _PERIOD.search(low):
        span = parse_past(m.group("p") or m.group("t"), today)
        rest = low[: m.start()] + " " + low[m.end() :]
    start, end = span or (today.replace(day=1), today)
    words = [w for w in re.findall(r"[a-zа-я0-9]+", _NOISE.sub(" ", rest)) if len(w) >= 3]
    return SpendQuery(start, end, words, period_label(start, end, today))


def receipt_day(printed: str | None, today: date) -> date | None:
    """«14.10.2026», «14/10/26», «2026-10-14» → a date within the last 60 days (else None: use today)."""
    if not printed or not (m := _RECEIPT_DATE.search(printed)):
        return None
    a, b, c = m.group("a"), m.group("b"), m.group("c")
    try:
        if len(a) == 4:
            day = date(int(a), int(b), int(c))
        else:
            year = int(c) + 2000 if len(c) == 2 else int(c)
            day = date(year, int(b), int(a))
    except ValueError:
        return None
    return day if today - timedelta(days=60) <= day <= today else None


class FinanceService:
    def __init__(self, settings: Settings, fx: FxService | None) -> None:
        self.settings = settings
        self.fx = fx

    @property
    def currency(self) -> str:
        return self.settings.finance_default_currency

    def today(self, now: datetime) -> date:
        return to_local(now, self.settings.tz).date()

    # --- writing ---------------------------------------------------------------------------------------------

    async def add(
        self,
        s: AsyncSession,
        items: list[ExpenseItem],
        day: date,
        *,
        source: str,
        source_ref: str,
        merchant: str | None = None,
        raw_media_id: uuid.UUID | None = None,
    ) -> list[Expense]:
        """Idempotent per `source_ref` (a retried message or a second tap adds nothing)."""
        refs = [f"{source_ref}:{n}" for n in range(len(items))]
        for item, ref in zip(items, refs, strict=True):
            await s.execute(
                insert(Expense)
                .values(
                    id=uuid.uuid4(),
                    spent_on=day,
                    amount_minor=to_minor(item.amount, item.currency),
                    currency=item.currency,
                    category=item.category[:30],
                    description=item.description[:300],
                    merchant=(merchant or None) and merchant[:120],
                    source=source,
                    source_ref=ref,
                    raw_media_id=raw_media_id,
                )
                .on_conflict_do_nothing(index_elements=[Expense.source_ref])
            )
        rows = await s.scalars(select(Expense).where(Expense.source_ref.in_(refs)).order_by(Expense.source_ref))
        return list(rows.all())

    async def undo(self, s: AsyncSession, expense_id: uuid.UUID, now: datetime) -> list[Expense]:
        """«↩️ Отменить» under a recorded message: hides every expense of that message (soft)."""
        expense = await s.get(Expense, expense_id)
        if expense is None:
            return []
        group = (expense.source_ref or "").rpartition(":")[0]
        query = select(Expense).where(Expense.deleted_at.is_(None))
        query = query.where(Expense.source_ref.like(f"{group}:%")) if group else query.where(Expense.id == expense_id)
        rows = list((await s.scalars(query)).all())
        for row in rows:
            row.deleted_at = now
        return rows

    async def remove(self, s: AsyncSession, expense_id: uuid.UUID, now: datetime) -> Expense | None:
        result = await s.execute(
            update(Expense)
            .where(Expense.id == expense_id, Expense.deleted_at.is_(None))
            .values(deleted_at=now)
            .returning(Expense)
        )
        return result.scalar_one_or_none()

    # --- texts -----------------------------------------------------------------------------------------------

    @staticmethod
    def line(expense: Expense, *, with_day: bool = False, today: date | None = None) -> str:
        day = ""
        if with_day and today:
            day = expense.spent_on.strftime("%d.%m " if expense.spent_on.year == today.year else "%d.%m.%Y ")
        where = f" · {expense.merchant}" if expense.merchant and expense.merchant != expense.description else ""
        return f"• {day}{expense.description}{where} — {amount_text(expense.amount_minor, expense.currency)}"

    @staticmethod
    def html_line(
        expense: Expense, *, with_day: bool = False, today: date | None = None, category: bool = False
    ) -> fmt.Html:
        """«• <i>14.10</i> Такси · Bolt — <b>45 ₾</b> <i>(Транспорт)</i>»."""
        day = None
        if with_day and today:
            day = fmt.i(expense.spent_on.strftime("%d.%m" if expense.spent_on.year == today.year else "%d.%m.%Y"))
        where = f" · {expense.merchant}" if expense.merchant and expense.merchant != expense.description else ""
        return fmt.line(
            "•",
            day,
            f"{expense.description}{where} —",
            fmt.b(amount_text(expense.amount_minor, expense.currency)),
            fmt.i(f"({expense.category})") if category else None,
        )

    async def added_text(self, s: AsyncSession, rows: list[Expense], now: datetime) -> fmt.Html:
        today = self.today(now)
        when = f" · {human_day(rows[0].spent_on, today)}" if rows[0].spent_on != today else ""
        month = await self._rows(s, today.replace(day=1), today)
        return fmt.lines(
            fmt.line("💸", fmt.b(f"Записал в расходы{when}:")),
            [self.html_line(r, category=True) for r in rows],
            fmt.hint(f"За {MONTHS[today.month - 1]}: {await self._total_text(month)}"),
        )

    async def _rows(self, s: AsyncSession, start: date, end: date) -> list[Expense]:
        rows = await s.scalars(
            select(Expense)
            .where(Expense.deleted_at.is_(None), Expense.spent_on >= start, Expense.spent_on <= end)
            .order_by(Expense.spent_on, Expense.created_at)
        )
        return list(rows.all())

    async def _per_usd(self) -> dict[str, Decimal] | None:
        if self.fx is None:
            return None
        try:
            rates, _stale = await self.fx.fiat()
        except Exception as exc:  # totals per currency are still exact without rates
            log.info("finance: no rates for the total: %s", exc)
            return None
        return dict(rates.per_usd)

    async def _total_text(self, rows: list[Expense]) -> str:
        sums: dict[str, int] = defaultdict(int)
        for r in rows:
            sums[r.currency] += r.amount_minor
        if not sums:
            return "0"
        parts = [amount_text(minor, cur) for cur, minor in sorted(sums.items(), key=lambda kv: kv[0] != self.currency)]
        if len(sums) == 1:
            return parts[0]
        converted = await self.convert(sums)
        return " + ".join(parts) + (f" ≈ {amount_text(converted, self.currency)}" if converted is not None else "")

    async def convert(self, sums: dict[str, int]) -> int | None:
        """Per-currency minor sums → minor units of the default currency (Decimal through USD rates)."""
        per_usd = await self._per_usd()
        if per_usd is None or self.currency not in per_usd or any(c not in per_usd for c in sums):
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            total = sum(
                (from_minor(minor, cur) / per_usd[cur] * per_usd[self.currency] for cur, minor in sums.items()),
                Decimal(0),
            )
        return to_minor(total, self.currency)

    def _matches(self, row: Expense, words: list[str]) -> bool:
        if not words:
            return True
        names = re.findall(r"[a-zа-я0-9]+", f"{row.description} {row.merchant or ''} {row.category}".lower())
        return all(any(same_word(w, n) for n in names) or category_of(w) == row.category for w in words)

    async def period_text(self, s: AsyncSession, query: SpendQuery, now: datetime) -> fmt.Html:
        today = self.today(now)
        rows = [r for r in await self._rows(s, query.start, query.end) if self._matches(r, query.words)]
        about = f" на «{' '.join(query.words)}»" if query.words else ""
        if not rows:
            return fmt.lines(
                fmt.line("💸", fmt.b(f"{query.label}{about}:"), "расходов не записано."),
                fmt.hint("Скажите, например: «потратил 45 лари на такси»."),
            )
        title = fmt.line(
            "💸",
            fmt.b(f"{query.label}{about}:"),
            fmt.b(await self._total_text(rows)),
            f"· {plural(len(rows), 'трата', 'траты', 'трат')}",
        )
        categories: list[fmt.Html] = []
        if not query.words:
            by_cat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for r in rows:
                by_cat[r.category][r.currency] += r.amount_minor
            ranked = []
            for cat, sums in by_cat.items():
                value = sums[self.currency] if set(sums) == {self.currency} else await self.convert(dict(sums))
                ranked.append((value if value is not None else 0, cat, sums))
            order = {name: n for n, (name, _) in enumerate(CATEGORIES)} | {OTHER: len(CATEGORIES)}
            ranked.sort(key=lambda x: (-x[0], order.get(x[1], 99)))
            for _value, cat, sums in ranked:
                parts = " + ".join(amount_text(minor, cur) for cur, minor in sums.items())
                categories.append(fmt.line("•", f"{cat} —", fmt.b(parts)))
        shown = rows[-8:] if not query.words else rows[-15:]
        latest = [self.html_line(r, with_day=True, today=today) for r in reversed(shown)]
        return fmt.blocks(
            title,
            fmt.section("По категориям", categories, emoji="📊"),
            fmt.section("Последние" if not query.words else "Траты", latest, emoji="🧾"),
        )

    async def today_line(self, s: AsyncSession, now: datetime) -> str | None:
        today = self.today(now)
        rows = await self._rows(s, today, today)
        if not rows:
            return None
        return f"💸 Сегодня потрачено: {await self._total_text(rows)} ({len(rows)})"

    async def find_for_removal(self, s: AsyncSession, text: str, now: datetime) -> Expense | None:
        """«удали последний расход» → the latest; «удали расход такси» → the latest matching (last 60 days)."""
        today = self.today(now)
        words = [
            w
            for w in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
            if len(w) >= 3
            and not re.fullmatch(r"удал\w*|убер\w*|убра\w*|отмен\w*|сотр\w*|последн\w*|расход\w*|трат\w*|запис\w*", w)
        ]
        rows = await self._rows(s, today - timedelta(days=60), today)
        matching = [r for r in rows if self._matches(r, words)]
        return max(matching, key=lambda r: (r.created_at, r.spent_on)) if matching else None

    # --- receipts --------------------------------------------------------------------------------------------

    def receipt_draft(self, receipt: ReceiptReading, caption: str, now: datetime) -> dict[str, Any] | str:
        """The receipt as read → an expense draft (amount parsed into Decimal by code) or an explanation."""
        amount = receipt_amount(receipt.total or "")
        if amount is None:
            return "🧾 Не разобрал итоговую сумму на чеке — скажите словами: «потратил 45,80 лари в магазине»."
        currency = (receipt.currency or "").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            currency = self.currency
        merchant = (receipt.merchant or "").strip()[:120] or None
        names = " ".join(line.name for line in receipt.lines)
        category = category_of(f"{merchant or ''} {names}")
        today = self.today(now)
        day = receipt_day(receipt.date, today) or today
        if re.search(r"\bвчера\b", caption.lower()):
            day = today - timedelta(days=1)
        return {
            "amount": str(amount),
            "currency": currency,
            "description": merchant or "Чек",
            "category": category,
            "merchant": merchant,
            "day": day.isoformat(),
            "lines": [f"{line.name[:40]} {line.amount}" for line in receipt.lines[:8]],
        }

    def receipt_text(self, draft: dict[str, Any], now: datetime) -> fmt.Html:
        today = self.today(now)
        minor = to_minor(Decimal(draft["amount"]), draft["currency"])
        return fmt.blocks(
            fmt.lines(
                fmt.line(
                    fmt.title("🧾", f"Чек: {draft['description']}"), "—", fmt.b(amount_text(minor, draft["currency"]))
                ),
                fmt.hint(f"{draft['category']} · {human_day(date.fromisoformat(draft['day']), today)}"),
                fmt.hint("Позиции: " + " · ".join(draft["lines"])) if draft.get("lines") else None,
            ),
            "Записать в расходы?",
        )
