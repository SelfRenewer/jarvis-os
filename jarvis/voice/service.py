"""Phone-call orchestration with at-most-once semantics.

1. A `call_requests` row (unique idempotency key) is committed in state `requesting` BEFORE the
   provider is contacted.
2. If that row already exists, no new call is made: a completed/requested call is final, and a row
   stuck in `requesting` (crash mid-request → unknown outcome) is converted to `unknown` and the user
   gets a Telegram fallback instead of a possible second call.
3. Any failure → Telegram fallback. Calls are never retried automatically.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from jarvis.audit.service import A, audit
from jarvis.core.budget import consume
from jarvis.core.clock import Clock
from jarvis.core.config import Settings
from jarvis.core.errors import BudgetExceededError, LiveActionBlockedError
from jarvis.db.models import CallRequest
from jarvis.db.session import Database
from jarvis.integrations.voice.providers import CallProvider, CallSpec, mask_number
from jarvis.notify.service import Notifier

log = logging.getLogger(__name__)

FINAL_OK = {"requested", "in_progress", "completed"}
VAPI_ENDED_FAILURE_HINTS = ("no-answer", "busy", "failed", "error", "voicemail", "rejected", "did-not-answer")


@dataclass
class CallOutcome:
    status: str  # requested|duplicate|blocked|failed|unknown|budget
    call_request_id: uuid.UUID | None
    fallback_sent: bool


class CallService:
    def __init__(
        self, db: Database, provider: CallProvider, notifier: Notifier, settings: Settings, clock: Clock
    ) -> None:
        self.db = db
        self.provider = provider
        self.notifier = notifier
        self.settings = settings
        self.clock = clock

    async def _fallback(self, key: str, text: str) -> bool:
        try:
            await self.notifier.send(f"📞 Не удалось позвонить.\n{text}", dedupe_key=f"call-fallback:{key}")
            return True
        except Exception:
            log.exception("telegram fallback failed")
            return False

    async def request_call(
        self,
        *,
        idempotency_key: str,
        reminder_id: uuid.UUID | None,
        variables: dict[str, str],
        spoken_text: str,
        fallback_text: str,
    ) -> CallOutcome:
        now = self.clock.now()
        number = self.settings.user_phone_number
        if not self.settings.voice_calls_enabled or not number:
            sent = await self._fallback(idempotency_key, fallback_text)
            return CallOutcome("blocked", None, sent)

        async with self.db.session() as session:
            inserted = await session.execute(
                insert(CallRequest)
                .values(
                    idempotency_key=idempotency_key,
                    reminder_id=reminder_id,
                    provider=self.provider.name,
                    to_number_masked=mask_number(number),
                    requested_for=now,
                    status="requesting",
                    attempts=1,
                )
                .on_conflict_do_nothing(index_elements=[CallRequest.idempotency_key])
                .returning(CallRequest.id)
            )
            row = inserted.first()
            if row is None:
                existing = await session.scalar(
                    select(CallRequest).where(CallRequest.idempotency_key == idempotency_key).with_for_update()
                )
                assert existing is not None
                if existing.status == "requesting":
                    existing.status = "unknown"
                    existing.last_error = "previous attempt interrupted; outcome unknown — not re-dialing"
                    call_id = existing.id
                else:
                    return CallOutcome("duplicate", existing.id, False)
            else:
                call_id = row[0]
                try:
                    await consume(session, "phone_calls", now, self.settings.max_calls_per_day)
                except BudgetExceededError:
                    req = await session.get(CallRequest, call_id)
                    assert req is not None
                    req.status = "blocked"
                    req.last_error = "daily call budget exhausted"
                    call_id = None
        if row is None:
            sent = await self._fallback(idempotency_key, fallback_text)
            return CallOutcome("unknown", call_id, sent)
        if call_id is None:
            sent = await self._fallback(idempotency_key, fallback_text)
            return CallOutcome("budget", None, sent)

        spec = CallSpec(
            idempotency_key=idempotency_key,
            to_number=number,
            variables=variables,
            spoken_text=spoken_text,
            metadata={"call_request_id": str(call_id)},
        )
        status: str
        placed = None
        error: str | None = None
        try:
            placed = await self.provider.place_call(spec)
            status = "requested"
        except LiveActionBlockedError as exc:
            status, error = "blocked", str(exc)
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        async with self.db.session() as session:
            req = await session.get(CallRequest, call_id)
            assert req is not None
            req.status = status
            req.last_error = error[:500] if error else None
            if placed is not None:
                req.external_call_id = placed.external_call_id
                req.provider_response = _sanitize(placed.response)
            await audit(
                session,
                now=self.clock.now(),
                actor="reminders",
                action=A.PHONE_CALL_REQUESTED
                if placed
                else (A.PHONE_CALL_BLOCKED if status == "blocked" else A.PHONE_CALL_FAILED),
                result="ok" if placed else "error",
                target_type="call_request",
                target_id=str(call_id),
                details={"provider": self.provider.name, "error": error or ""},
            )
        if placed is None:
            sent = await self._fallback(idempotency_key, fallback_text)
            return CallOutcome(status, call_id, sent)
        return CallOutcome("requested", call_id, False)

    async def on_provider_status(
        self, external_call_id: str, status: str, ended_reason: str | None, fallback_text_builder: Any = None
    ) -> dict[str, str]:
        async with self.db.session() as session:
            req = await session.scalar(select(CallRequest).where(CallRequest.external_call_id == external_call_id))
            if req is None:
                return {"status": "unknown_call"}
            if status == "ended":
                failed = bool(ended_reason) and any(h in (ended_reason or "") for h in VAPI_ENDED_FAILURE_HINTS)
                req.status = "failed" if failed else "completed"
                req.ended_reason = (ended_reason or "")[:100]
                await audit(
                    session,
                    now=self.clock.now(),
                    actor="vapi",
                    action=A.PHONE_CALL_FAILED if failed else A.PHONE_CALL_COMPLETED,
                    target_type="call_request",
                    target_id=str(req.id),
                    details={"reason": ended_reason or ""},
                )
                key = req.idempotency_key
            else:
                if req.status in ("requesting", "requested"):
                    req.status = "in_progress" if status in ("ringing", "in-progress") else req.status
                return {"status": "updated"}
        if failed:
            await self._fallback(key, "Я звонил, но не дозвонился. Проверь напоминание в Telegram.")
        return {"status": "ended"}


def _sanitize(response: dict[str, Any]) -> dict[str, Any]:
    allowed = {"id", "sid", "status"}
    return {k: v for k, v in response.items() if k in allowed}
