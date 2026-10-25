"""AI Inbox: notes storage and "ask with buttons" choices.

A message without an explicit command is classified by the router (LLM, closed set: task / idea / shopping /
note). When the router is unsure — or no LLM is available — the user picks with inline buttons. A choice row
stores the original message, so a button carries only the choice id and an option letter.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Note, PendingChoice
from jarvis.integrations.telegram.client import InlineButton

CHOICE_TTL = timedelta(hours=24)

INBOX_OPTIONS = {"t": "✅ Задача", "i": "💡 Идея", "s": "🛒 Покупка", "n": "📝 Заметка", "x": "✖️ Ничего"}


def choice_button(choice: PendingChoice, option: str, label: str) -> InlineButton:
    return InlineButton(label[:60], f"pc:{choice.id.hex}:{option}")


async def offer_choice(
    session: AsyncSession,
    *,
    chat_id: int,
    kind: str,
    payload: dict[str, Any],
    now: datetime,
    ttl: timedelta = CHOICE_TTL,
) -> PendingChoice:
    choice = PendingChoice(chat_id=chat_id, kind=kind, payload=payload, status="pending", expires_at=now + ttl)
    session.add(choice)
    await session.flush()
    return choice


async def take_choice(
    session: AsyncSession, *, choice_id: uuid.UUID, chat_id: int, resolution: str, now: datetime
) -> PendingChoice | None:
    """Resolve a pending choice exactly once (double taps and stale buttons return None)."""
    result = await session.execute(
        update(PendingChoice)
        .where(
            PendingChoice.id == choice_id,
            PendingChoice.chat_id == chat_id,
            PendingChoice.status == "pending",
            or_(PendingChoice.expires_at.is_(None), PendingChoice.expires_at > now),
        )
        .values(status="resolved", resolution=resolution[:40], resolved_at=now)
        .returning(PendingChoice.id)
    )
    if result.first() is None:
        return None
    return await session.scalar(
        select(PendingChoice).where(PendingChoice.id == choice_id).execution_options(populate_existing=True)
    )


class NoteService:
    async def save(
        self,
        session: AsyncSession,
        *,
        text_value: str,
        source: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> tuple[Note, bool]:
        """Store verbatim. The same Telegram message is saved once (retries, repeated button taps)."""
        dedupe = f"tg:{chat_id}:{message_id}" if chat_id is not None and message_id is not None else None
        stmt = insert(Note).values(
            id=uuid.uuid4(),
            text=text_value[:10000],
            source=source,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
            dedupe_key=dedupe,
        )
        if dedupe is not None:
            stmt = stmt.on_conflict_do_nothing(index_elements=[Note.dedupe_key])
        row = (await session.execute(stmt.returning(Note.id))).first()
        if row is None:
            existing = await session.scalar(select(Note).where(Note.dedupe_key == dedupe))
            assert existing is not None
            return existing, False
        note = await session.get(Note, row[0])
        assert note is not None
        return note, True

    async def recent(self, session: AsyncSession, limit: int = 10) -> Sequence[Note]:
        return (await session.scalars(select(Note).order_by(Note.created_at.desc()).limit(limit))).all()

    async def search(self, session: AsyncSession, query: str, limit: int = 5) -> Sequence[Note]:
        """Russian full-text search, plus a plain substring match (codes, numbers, names)."""
        tsq = func.websearch_to_tsquery(text("'russian'"), query)
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return (
            await session.scalars(
                select(Note)
                .where(or_(Note.text_tsv.op("@@")(tsq), Note.text.ilike(pattern)))
                .order_by(func.ts_rank(Note.text_tsv, tsq).desc(), Note.created_at.desc())
                .limit(limit)
            )
        ).all()
