"""Telegram calls (pilot, 2026-10-14): a second Telegram account rings the user after the first unanswered repeat of
a reminder, or when asked («позвони мне через 10 минут»). Queued in `call_requests`, dialed at most once by the
`caller` service (here: a fake dialer), never at night, within the daily cap.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from jarvis.bot.intents import Intent, keyword_route
from jarvis.bot.service import BotService
from jarvis.calls.service import FakeDialer, parse_call_me, ring_next, sweep
from jarvis.db.models import CallRequest, ScheduledJob
from jarvis.scheduler.queue import enqueue

USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


def local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def msg(text: str, update_id: int) -> dict[str, Any]:
    chat = {"id": USER, "type": "private"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "from": {"id": USER}, "chat": chat},
    }


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}", "data": data, "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }  # fmt: skip


def calls_on(c: Any, **extra: Any) -> None:
    c.settings.telegram_calls_enabled = True
    c.settings.telegram_caller_api_id = 12345
    c.settings.telegram_caller_api_hash = "hash"  # type: ignore[assignment]
    c.settings.telegram_caller_session = "session"  # type: ignore[assignment]
    for key, value in extra.items():
        setattr(c.settings, key, value)


async def drain_until(c: Any, worker: Any, until: datetime) -> None:
    while True:
        async with c.db.session() as s:
            next_run = await s.scalar(select(func.min(ScheduledJob.run_at)).where(ScheduledJob.status == "pending"))
        if next_run is None or next_run > until:
            break
        c.clock.set(max(next_run, c.clock.now()))
        await worker.drain()
    c.clock.set(until)


async def meeting(c: Any, worker: Any, start: datetime) -> None:
    c.settings.voice_calls_enabled = False
    c.settings.reminder_offsets_minutes = [30]
    end = {"dateTime": iso(start + timedelta(hours=1))}
    c.calendar.add_event("primary", summary="Встреча с Ниной", start={"dateTime": iso(start)}, end=end)
    async with c.db.session() as s:
        await enqueue(s, job_type="calendar.reconcile", run_at=c.clock.now())
    await worker.drain()


async def calls(c: Any) -> list[CallRequest]:
    async with c.db.session() as s:
        return list((await s.scalars(select(CallRequest).order_by(CallRequest.requested_for))).all())


def buttons(message: dict[str, Any]) -> dict[str, str]:
    markup = message.get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


@pytest.mark.db
async def test_an_unanswered_reminder_rings_once_after_the_first_repeat(container: Any, worker: Any) -> None:
    c = container
    calls_on(c)
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 48))  # the reminder at 10:45, the first repeat at 10:48: no call yet
    assert await calls(c) == []
    await drain_until(c, worker, local(10, 53))  # the second repeat (8 min): the call is queued with it
    [call] = await calls(c)
    assert (call.provider, call.status, call.reminder_id is not None) == ("telegram", "queued", True)
    assert call.spoken_text and call.spoken_text.startswith("Сэр, напоминаю.") and "Встреча с Ниной" in call.spoken_text
    assert any("📞 Звоню в Telegram" in (m.get("text") or "") for m in c.telegram.sent)
    dialer = FakeDialer()
    assert await ring_next(c, dialer, c.clock.now()) == {"id": str(call.id), "status": "completed"}
    assert dialer.calls == [(4800, 24000)]  # the PCM of the fake TTS
    assert await ring_next(c, dialer, c.clock.now()) is None  # dialed once
    await drain_until(c, worker, local(11, 10))  # the 15-min repeat: no second call
    assert [x.status for x in await calls(c)] == ["completed"]


@pytest.mark.db
async def test_understood_before_the_call_cancels_it(container: Any, worker: Any) -> None:
    c = container
    calls_on(c)
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 53))
    first = next(m for m in c.telegram.sent if (m.get("text") or "").startswith("⏰ Через"))
    await BotService(c).handle_update(tap(buttons(first)["👌 Понял"], 1))
    dialer = FakeDialer()
    result = await ring_next(c, dialer, c.clock.now())
    assert result is not None and result["status"] == "skipped" and dialer.calls is None


@pytest.mark.db
async def test_no_call_at_night_when_off_or_over_the_cap(container: Any, worker: Any) -> None:
    c = container
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 53))  # calls are off by default
    assert await calls(c) == []

    calls_on(c, sleep_start="10:00", sleep_end="12:00")  # «night» around this meeting
    await meeting(c, worker, local(11, 30))
    await drain_until(c, worker, local(11, 23))
    assert await calls(c) == []

    calls_on(c, sleep_start="23:30", sleep_end="07:30", max_calls_per_day=1)
    bot = BotService(c)
    await bot.handle_update(msg("позвони мне через 10 минут", 2))
    assert c.telegram.texts()[-1].splitlines()[0] == "📞 Позвоню в 11:33."
    await bot.handle_update(msg("позвони мне через 20 минут", 3))
    assert c.telegram.texts()[-1] == "📞 Лимит звонков на сегодня исчерпан (1)."


@pytest.mark.db
async def test_call_me_later_rings_at_its_time_and_a_late_one_expires(container: Any) -> None:
    c = container
    calls_on(c)
    bot = BotService(c)
    await bot.handle_update(msg("Позвони мне через полчаса и скажи выключить плиту", 1))
    assert c.telegram.texts()[-1].splitlines() == ["📞 Позвоню в 09:30.", "Сэр, вы просили сказать: выключить плиту."]
    dialer = FakeDialer(outcome="no_answer")
    assert await ring_next(c, dialer, c.clock.now()) is None  # not yet
    c.clock.advance(timedelta(minutes=30))
    result = await ring_next(c, dialer, c.clock.now())
    assert result is not None and result["status"] == "no_answer"
    assert c.tts.calls[-1][0] == "Сэр, вы просили сказать: выключить плиту."

    await bot.handle_update(msg("набери меня в 10:00", 2))  # the Mac slept through it
    c.clock.set(local(10, 20))
    async with c.db.session() as s:
        assert await sweep(s, c.clock.now()) == (1, 0)
    assert [x.status for x in await calls(c)] == ["no_answer", "expired"]


@pytest.mark.db
async def test_a_failed_call_says_so_and_is_never_redialed(container: Any) -> None:
    c = container
    calls_on(c)
    await BotService(c).handle_update(msg("позвони мне", 1))
    assert c.telegram.texts()[-1].splitlines()[0] == "📞 Звоню."
    result = await ring_next(c, FakeDialer(fail_with=RuntimeError("WebRTC down")), c.clock.now())
    assert result is not None and result["status"] == "failed"
    assert c.telegram.texts()[-1] == "📞 Не смог позвонить в Telegram: RuntimeError: WebRTC down"
    assert await ring_next(c, FakeDialer(), c.clock.now()) is None


@pytest.mark.db
async def test_calls_not_connected_are_explained(container: Any) -> None:
    c = container
    await BotService(c).handle_update(msg("позвони мне через 5 минут", 1))
    assert c.telegram.texts()[-1].startswith("📞 Звонки в Telegram ещё не подключены.")


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("позвони мне через 10 минут", "через 10 минут"),
        ("Набери меня в 7:30 и разбуди", "в 7:30 и разбуди"),
        ("позвони через полчаса и напомни про врача", "через полчаса и напомни про врача"),
        ("позвони мне", ""),
        ("Позвони мне, поболтаем", "поболтаем"),
        ("позвони, поговорим", "поговорим"),
        ("набери меня через 10 минут, хочу поболтать", "через 10 минут, хочу поболтать"),
    ],
)
def test_call_me_phrases(text: str, rest: str) -> None:
    routed = keyword_route(text)
    assert (routed.intent, routed.query) == (Intent.CALL_ME, rest)


@pytest.mark.parametrize("text", ["позвони маме завтра", "позвонить в банк", "напомни позвонить врачу в 15:00"])
def test_calling_someone_else_is_a_task(text: str) -> None:
    assert keyword_route(text).intent != Intent.CALL_ME


def test_call_times() -> None:
    now = local(19, 30)
    assert parse_call_me("в 7:30", now, TZ)[0] == datetime(2026, 9, 24, 7, 30, tzinfo=TZ)  # the evening → tomorrow
    assert parse_call_me("через 3 дня", now, TZ)[0] is None
    assert parse_call_me("завтра в 8 и напомни про врача", now, TZ)[1] == "Сэр, напоминаю про врача."
    assert parse_call_me("поболтаем", now, TZ) == (now, None)  # a call to talk: now, no message
    assert parse_call_me("через 10 минут, хочу поболтать", now, TZ) == (now + timedelta(minutes=10), None)
    assert parse_call_me("и скажи, что пора поговорить с мамой", now, TZ)[1] == (
        "Сэр, вы просили сказать: пора поговорить с мамой."
    )  # «поговорить» inside the message is the message


@pytest.mark.db
async def test_call_me_to_talk_rings_into_a_conversation(container: Any) -> None:
    c = container
    calls_on(c)
    await BotService(c).handle_update(msg("Позвони мне, поболтаем", 1))
    assert c.telegram.texts()[-1].splitlines()[0] == "📞 Звоню."
    [call] = await calls(c)
    assert call.spoken_text is None
    dialer = FakeDialer()
    result = await ring_next(c, dialer, c.clock.now())
    assert result is not None and result["status"] == "completed"
    assert (dialer.talks, dialer.calls) == (1, [(0, 0)])  # no message synthesized: the conversation speaks
    assert c.tts.calls == []
