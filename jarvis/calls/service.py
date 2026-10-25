"""Telegram calls (2026-10-14 pilot): Jarvis rings the user's own Telegram account from a second account.

The Bot API cannot call, so a second Telegram account (a «userbot» on the user's Georgian number) does it in the
`caller` service (Telethon + py-tgcalls, `jarvis/integrations/telegram/caller.py`). Everything else stays here:

- a call is a `call_requests` row (`provider = 'telegram'`, unique idempotency key) queued by the worker or the bot
  with what to say and when; the caller claims one row at a time with a conditional UPDATE and dials;
- at most once: a row is dialed only from `queued`; a row left `in_progress` by a crash becomes `unknown` and is
  never dialed again; a queued call found more than `LATE_AFTER` past its time (the Mac slept) expires instead of
  ringing late;
- the daily cap is the shared `phone_calls` budget (`MAX_CALLS_PER_DAY`); reminder calls are skipped during sleep
  hours (a call the user asked for, «позвони мне в 7:30», rings anyway);
- no answer / declined → a missed call is enough (the text reminder is already in the chat); a failure → one short
  Telegram message.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.budget import consume
from jarvis.core.clock import parse_hhmm, to_local
from jarvis.core.config import Settings
from jarvis.core.errors import BudgetExceededError, NotConfiguredError
from jarvis.db.models import CallRequest, Reminder

log = logging.getLogger(__name__)

PROVIDER = "telegram"
LATE_AFTER = timedelta(minutes=15)  # a queued call this late is not rung any more
STUCK_AFTER = timedelta(minutes=10)  # `in_progress` this long = the caller died mid-call → unknown
ANSWERED = "completed"
MISSED = ("no_answer", "declined", "busy")


class Dialer(Protocol):
    """Rings the user and, if they answer, plays the PCM — or, with `talk`, starts a conversation (the PCM is empty).
    Returns completed | no_answer | declined | busy; raises on errors (the call then counts as failed)."""

    async def dial(self, pcm: bytes, sample_rate: int, *, ring_seconds: int, talk: bool = False) -> str: ...


@dataclass
class FakeDialer:
    outcome: str = ANSWERED
    fail_with: Exception | None = None
    calls: list[tuple[int, int]] | None = None
    talks: int = 0

    async def dial(self, pcm: bytes, sample_rate: int, *, ring_seconds: int, talk: bool = False) -> str:
        if self.calls is None:
            self.calls = []
        self.calls.append((len(pcm), sample_rate))
        self.talks += talk
        if self.fail_with is not None:
            raise self.fail_with
        return self.outcome


def enabled(settings: Settings) -> bool:
    """Calls may be queued (bot, worker, scheduler, doctor). The caller account's secrets are not needed for that:
    compose gives TELEGRAM_CALLER_SESSION / _API_HASH only to the `caller` service (security review 2026-10-14, L2);
    `caller-login` sets TELEGRAM_CALLS_ENABLED together with them, and the caller's heartbeat shows its state."""
    return bool(
        settings.telegram_calls_enabled and settings.telegram_caller_api_id and settings.telegram_allowed_user_ids
    )


def configured(settings: Settings) -> bool:
    """The `caller` service can dial: calls are on and the second account is signed in."""
    return bool(enabled(settings) and settings.telegram_caller_api_hash and settings.telegram_caller_session)


def in_sleep_hours(now: datetime, settings: Settings) -> bool:
    local = to_local(now, settings.tz).time()
    start, end = parse_hhmm(settings.sleep_start), parse_hhmm(settings.sleep_end)
    return local >= start or local < end if start > end else start <= local < end


async def request(
    session: AsyncSession,
    settings: Settings,
    *,
    key: str,
    text: str | None,
    at: datetime,
    now: datetime,
    reminder_id: uuid.UUID | None = None,
) -> str:
    """Queue a call: queued | duplicate | off | budget. The budget is taken now (a queued call counts). No text = a
    call to talk: the conversation starts when the user answers."""
    if not enabled(settings):
        return "off"
    inserted = await session.execute(
        insert(CallRequest)
        .values(
            idempotency_key=key,
            reminder_id=reminder_id,
            provider=PROVIDER,
            to_number_masked="telegram",
            requested_for=at,
            status="queued",
            attempts=0,
            spoken_text=text[:1000] if text else None,
        )
        .on_conflict_do_nothing(index_elements=[CallRequest.idempotency_key])
        .returning(CallRequest.id)
    )
    row = inserted.first()
    if row is None:
        return "duplicate"
    try:
        await consume(session, "phone_calls", now, settings.max_calls_per_day)
    except BudgetExceededError:
        await session.execute(
            update(CallRequest)
            .where(CallRequest.id == row[0])
            .values(status="blocked", last_error="daily call budget exhausted")
        )
        return "budget"
    return "queued"


async def sweep(session: AsyncSession, now: datetime) -> tuple[int, int]:
    """(expired, unknown): late queued calls are dropped, calls stuck mid-dial are never redialed."""
    expired = await session.execute(
        update(CallRequest)
        .where(
            CallRequest.provider == PROVIDER,
            CallRequest.status == "queued",
            CallRequest.requested_for < now - LATE_AFTER,
        )
        .values(status="expired", ended_reason="not rung: too late (Jarvis was down)")
    )
    stuck = await session.execute(
        update(CallRequest)
        .where(
            CallRequest.provider == PROVIDER,
            CallRequest.status == "in_progress",
            CallRequest.updated_at < now - STUCK_AFTER,
        )
        .values(status="unknown", last_error="caller stopped mid-call; outcome unknown — not redialing")
    )
    return getattr(expired, "rowcount", 0) or 0, getattr(stuck, "rowcount", 0) or 0


async def claim(session: AsyncSession, now: datetime) -> CallRequest | None:
    """The next due call, moved to `in_progress` in the same statement (two callers never dial the same row)."""
    due = (
        select(CallRequest.id)
        .where(CallRequest.provider == PROVIDER, CallRequest.status == "queued", CallRequest.requested_for <= now)
        .order_by(CallRequest.requested_for)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    claimed = await session.scalar(
        update(CallRequest)
        .where(CallRequest.id == due, CallRequest.status == "queued")
        .values(status="in_progress", attempts=CallRequest.attempts + 1, updated_at=now)
        .returning(CallRequest)
    )
    return claimed


async def finish(session: AsyncSession, call_id: uuid.UUID, status: str, reason: str | None = None) -> None:
    await session.execute(
        update(CallRequest)
        .where(CallRequest.id == call_id, CallRequest.status == "in_progress")
        .values(status=status, ended_reason=(reason or status)[:100], last_error=reason[:500] if reason else None)
    )


async def ring_next(c: Any, dialer: Dialer, now: datetime) -> dict[str, str] | None:
    """One step of the `caller` loop: sweep, claim the next due call, speak it, record the outcome."""
    from jarvis.bot.speech import to_speech

    async with c.db.session() as session:
        await sweep(session, now)
        call = await claim(session, now)
        if call is None:
            return None
        call_id, text = call.id, call.spoken_text
        talk = not text and call.reminder_id is None  # «позвони мне, поболтаем»
        reminder = await session.get(Reminder, call.reminder_id) if call.reminder_id else None
        if reminder is not None and (reminder.payload or {}).get("ack_at"):
            await finish(session, call_id, "skipped", "«Понял» pressed before the call")
            return {"id": str(call_id), "status": "skipped"}
    reason: str | None = None
    try:
        if talk:
            status = await dialer.dial(b"", 0, ring_seconds=c.settings.telegram_call_ring_seconds, talk=True)
        else:
            text = text or "Сэр, это Джарвис."
            speech = to_speech(text, c.settings.tts_max_chars) or text
            pcm, rate = await c.tts.synthesize_pcm(speech, model=c.settings.call_tts_model)
            status = await dialer.dial(pcm, rate, ring_seconds=c.settings.telegram_call_ring_seconds)
    except Exception as exc:  # TTS, Telegram, WebRTC: the call is over either way — never redialed
        status, reason = "failed", f"{type(exc).__name__}: {exc}"
        log.warning("telegram call %s failed: %s", call_id, reason)
    async with c.db.session() as session:
        await finish(session, call_id, status, reason)
    if status == "failed":
        with contextlib.suppress(NotConfiguredError):
            await c.notifier.send(
                f"📞 Не смог позвонить в Telegram: {(reason or '')[:200]}", dedupe_key=f"tg-call-failed:{call_id}"
            )
    return {"id": str(call_id), "status": status}


_SAY = re.compile(
    r"(?:,\s*|\s+)(?:и\s+)?(?P<verb>скажи|напомни|разбуди)(?:\s+(?:мне|меня))?(?:[\s,]+(?P<prep>про|о|об|что))?"
    r"(?:[\s,]+(?P<say>.+))?$"
)
_TALK = re.compile(
    r"(?:^|,\s*|\s+)(?:и\s+|давай\s+|хочу\s+)?(?:поболта(?:ем|ть)|поговори(?:м|ть)|пообща(?:емся|ться))\b.*$"
)
MAX_AHEAD = timedelta(days=7)


def parse_call_me(rest: str, now: datetime, tz: Any) -> tuple[datetime | None, str | None]:
    """«через 10 минут», «в 7:30», «завтра в 8 и напомни про врача» → (when, the sentence to say or None); (None, …)
    if the time is not understood or more than a week ahead. Nothing → now."""
    from jarvis.core.clock import local_datetime
    from jarvis.core.dates import parse_shift, parse_when

    say: str | None = None
    rest = f" {rest}"  # «и скажи …» right after «позвони мне»: the separator _SAY expects
    if m := _SAY.search(rest):
        rest, what = rest[: m.start()], (m["say"] or "").strip(" .!")
        if m["verb"] == "разбуди":
            say = "Сэр, пора вставать." + (f" {what}." if what else "")
        elif m["verb"] == "напомни" and what:
            about = m["prep"] in ("про", "о", "об")
            say = f"Сэр, напоминаю {m['prep']} {what}." if about else f"Сэр, напоминаю: {what}."
        elif what:
            say = f"Сэр, вы просили сказать: {what}."
    rest = _TALK.sub("", rest).strip(" ,.!")  # «…, поболтаем»: a call to talk = a call without a message
    if not rest:
        return now, say
    if (shift := parse_shift(rest)) is not None:
        if not shift.from_now or shift.minutes <= 0:
            return None, say
        return now + timedelta(minutes=shift.minutes), say
    today = to_local(now, tz).date()
    when = parse_when(rest, today)
    if when is None or when.at is None:
        return None, say
    at = local_datetime(when.day or today, when.at, tz)
    if when.day is None and at <= now:
        at += timedelta(days=1)  # «в 7:30» said in the evening = tomorrow morning
    if at <= now - timedelta(minutes=1) or at - now > MAX_AHEAD:
        return None, say
    return max(at, now), say


async def converse(c: Any, line: Any, *, outgoing: bool = False) -> int:
    """A conversation on a connected call. Incoming (the user called): one ledger row afterwards (history; no budget).
    Outgoing (Jarvis rang to talk): the queued call's own row is finished by `ring_next`."""
    from jarvis.calls.conversation import CALLING, GREETING, Conversation, PhoneChannel

    started = c.clock.now()
    phone = PhoneChannel(c)
    await _on_call(c, started + timedelta(minutes=11))  # notifications arrive silently meanwhile
    try:
        greeting = CALLING if outgoing else GREETING
        turns = await Conversation(
            line, phone.ask, c.stt, c.tts, tts_model=c.settings.call_tts_model, greeting=greeting
        ).run()
    finally:
        await _on_call(c, None)
    minutes = max(1, round((c.clock.now() - started).total_seconds() / 60))
    with contextlib.suppress(Exception):  # the chat record is a courtesy; the call itself is over
        await phone.summary(minutes)
    if outgoing:
        log.info("talk call: %d phrase(s)", len(turns))
        return len(turns)
    async with c.db.session() as session:
        await session.execute(
            insert(CallRequest).values(
                idempotency_key=f"tg-call:in:{uuid.uuid4().hex}",
                provider=PROVIDER,
                to_number_masked="telegram",
                requested_for=started,
                status="completed",
                attempts=1,
                ended_reason=f"incoming: {len(turns)} phrase(s)",
            )
        )
    log.info("incoming call: %d phrase(s)", len(turns))
    return len(turns)


async def _on_call(c: Any, until: datetime | None) -> None:
    """`user_preferences.on_call_until`: the notifier sends silently while it is in the future."""
    from jarvis.db.models import UserPreference
    from jarvis.notify.service import ON_CALL_KEY

    async with c.db.session() as session:
        value = {"until": until.isoformat() if until else None}
        await session.execute(
            insert(UserPreference)
            .values(key=ON_CALL_KEY, value=value)
            .on_conflict_do_update(index_elements=[UserPreference.key], set_={"value": value})
        )
