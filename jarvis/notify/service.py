"""Outbound Telegram notifications with an idempotency ledger.

Policy: at-least-once. The ledger row is committed *before* sending; a row already `sent` is never
re-sent. A crash between the Telegram call and the ledger update can produce one duplicate message —
accepted, because a missed reminder is worse than a duplicate one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from jarvis.audit.service import A, audit
from jarvis.bot.fmt import Html
from jarvis.core.clock import Clock
from jarvis.core.errors import NotConfiguredError, TransientError
from jarvis.db.models import Notification, UserPreference
from jarvis.db.session import Database
from jarvis.integrations.telegram.client import TelegramApi

log = logging.getLogger(__name__)

SENDING_LEASE = timedelta(seconds=90)
ON_CALL_KEY = "on_call_until"  # set by the `caller` service while the user talks to Jarvis


@dataclass
class SendResult:
    status: str  # sent|already_sent
    message_id: int | None


class Notifier:
    def __init__(self, db: Database, telegram: TelegramApi | None, clock: Clock, default_chat_id: int | None) -> None:
        self.db = db
        self.telegram = telegram
        self.clock = clock
        self.default_chat_id = default_chat_id

    async def on_call(self, now: datetime) -> bool:
        """The user is talking to Jarvis on a Telegram call (`user_preferences.on_call_until`, set by the caller):
        notifications then arrive without a sound."""
        async with self.db.session() as session:
            pref = await session.get(UserPreference, ON_CALL_KEY)
        until = (pref.value or {}).get("until") if pref is not None else None
        return bool(until) and datetime.fromisoformat(str(until)) > now

    async def send(
        self,
        text: str,
        *,
        dedupe_key: str,
        chat_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """`text` built with `jarvis.bot.fmt` (`Html`) goes out as Telegram HTML; a plain `str` as plain text."""
        if parse_mode is None and isinstance(text, Html):
            parse_mode = "HTML"
        target = chat_id or self.default_chat_id
        if self.telegram is None or target is None:
            raise NotConfiguredError("telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS)")
        now = self.clock.now()
        async with self.db.session() as session:
            await session.execute(
                insert(Notification)
                .values(channel="telegram", dedupe_key=dedupe_key[:300], chat_id=target, text=text, status="pending")
                .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
            )
            row = await session.scalar(
                select(Notification).where(Notification.dedupe_key == dedupe_key[:300]).with_for_update()
            )
            assert row is not None
            if row.status == "sent":
                return SendResult("already_sent", int(row.external_id) if row.external_id else None)
            if row.status == "sending" and row.sending_since and now - row.sending_since < SENDING_LEASE:
                # Another worker is delivering this very message right now: don't send twice, retry later.
                raise TransientError("notification in flight", retry_after_seconds=SENDING_LEASE.total_seconds())
            # A stale "sending" row means a crash mid-send: re-send (at-least-once).
            row.status = "sending"
            row.sending_since = now
            row.attempts += 1
        try:
            silent = await self.on_call(now)
            result = await self.telegram.send_message(
                target, text, reply_markup=reply_markup, parse_mode=parse_mode, silent=silent
            )
        except Exception as exc:
            async with self.db.session() as session:
                row = await session.scalar(select(Notification).where(Notification.dedupe_key == dedupe_key[:300]))
                if row is not None:
                    row.status = "failed"
                    row.error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        message_id = result.get("message_id")
        async with self.db.session() as session:
            row = await session.scalar(select(Notification).where(Notification.dedupe_key == dedupe_key[:300]))
            if row is not None:
                row.status = "sent"
                row.external_id = str(message_id) if message_id is not None else None
                row.sent_at = self.clock.now()
                row.error = None
            await audit(
                session,
                now=self.clock.now(),
                actor="jarvis",
                action=A.TELEGRAM_SENT,
                target_type="notification",
                target_id=dedupe_key[:100],
            )
        return SendResult("sent", message_id)
