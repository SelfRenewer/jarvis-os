"""Jarvis for Mac, more actions (2026-10-14): volume, the clipboard, notices on the Mac («напомни на маке …» and
reminders mirrored while the app is online). Only a closed set of payloads Jarvis built itself. Fakes only.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi (05:00 UTC)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from jarvis.bot.service import BotService
from jarvis.db.models import MacAction, Reminder, ScheduledJob
from jarvis.desktop.service import mac_reminder, parse_volume
from jarvis.scheduler.queue import enqueue

USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


def test_volume_phrases() -> None:
    assert parse_volume("громкость 30") == {"op": "set", "level": "30"}
    assert parse_volume("сделай звук на 50%") == {"op": "set", "level": "50"}
    assert parse_volume("погромче") == {"op": "up", "step": "10"}
    assert parse_volume("потише на 20") == {"op": "down", "step": "20"}
    assert parse_volume("выключи звук") == {"op": "mute"}
    assert parse_volume("включи звук") == {"op": "unmute"}
    assert parse_volume("громкость на 150") is None
    assert parse_volume("сделай погромче музыку в спотифай") is None
    assert parse_volume("включи голос") is None


def test_mac_reminder_times() -> None:
    now = datetime(2026, 9, 23, 11, 0, tzinfo=UTC)  # 15:00 in Tbilisi
    assert mac_reminder("через 10 минут выключить чайник", now, TZ) == ("выключить чайник", now + timedelta(minutes=10))
    title, at = mac_reminder("позвонить маме в 16:30", now, TZ)  # type: ignore[misc]
    assert (title, at.astimezone(TZ).strftime("%d %H:%M")) == ("позвонить маме", "23 16:30")
    title, at = mac_reminder("в 9 созвон", now, TZ)  # type: ignore[misc]
    assert at.astimezone(TZ).strftime("%d %H:%M") == "24 09:00"  # «в 9» said at 15:00 is tomorrow
    assert mac_reminder("просто текст", now, TZ) is None


async def online(c: Any) -> None:
    async with c.db.session() as s:
        await c.desktop.hello(s, c.clock.now(), apps=["Safari"], projects=[], version="1.0")


async def actions(c: Any) -> list[MacAction]:
    async with c.db.session() as s:
        return list((await s.scalars(select(MacAction).order_by(MacAction.created_at))).all())


@pytest.mark.db
async def test_volume_and_clipboard_become_mac_actions(container: Any) -> None:
    c = container
    await online(c)
    bot = BotService(c)
    await bot.handle_update(msg("громкость 30", 1))
    assert c.telegram.texts()[-1] == "🔊 На Mac: громкость 30%."
    await bot.handle_update(msg("скопируй на мак: Руставели 12, кв. 5.", 2))
    assert c.telegram.texts()[-1] == "📋 Скопирую на Mac в буфер обмена (20 симв.) — вставьте ⌘V."
    rows = await actions(c)
    assert [(a.kind, a.payload, a.status) for a in rows] == [
        ("volume", {"op": "set", "level": "30", "label": "громкость 30%"}, "pending"),
        ("copy_text", {"text": "Руставели 12, кв. 5.", "label": "текст в буфер обмена"}, "pending"),
    ]


@pytest.mark.db
async def test_mac_reminder_pops_up_on_the_mac_or_falls_back_to_telegram(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await online(c)
    await bot.handle_update(msg("напомни на маке через 10 минут выключить чайник", 10))
    assert c.telegram.texts()[-1] == "🔔 Напомню на Mac в 09:10: выключить чайник."
    sent = len(c.telegram.sent)
    c.clock.advance(timedelta(minutes=10))
    await online(c)  # the app keeps polling
    await worker.drain()
    rows = await actions(c)
    assert [(a.kind, a.payload["body"]) for a in rows] == [("notify", "🔔 выключить чайник")]
    assert len(c.telegram.sent) == sent  # shown on the Mac, not duplicated in Telegram

    await bot.handle_update(msg("напомни на маке через 5 минут проверить духовку", 11))
    c.clock.advance(timedelta(minutes=30))  # the Mac went to sleep: not seen for 25 minutes
    await worker.drain()
    assert c.telegram.texts()[-1] == "🔔 проверить духовку (Mac не на связи — напоминаю здесь)"


@pytest.mark.db
async def test_reminders_are_mirrored_to_an_online_mac(container: Any, worker: Any) -> None:
    c = container
    start = datetime(2026, 9, 23, 10, 0, tzinfo=TZ)
    c.calendar.add_event(
        "primary",
        summary="Встреча с Ниной",
        start={"dateTime": start.astimezone(UTC).isoformat()},
        end={"dateTime": (start + timedelta(hours=1)).astimezone(UTC).isoformat()},
    )
    async with c.db.session() as s:
        await enqueue(s, job_type="calendar.reconcile", run_at=c.clock.now())
    await worker.drain()
    while True:  # run every job up to the meeting, keeping the Mac online
        async with c.db.session() as s:
            next_run = await s.scalar(select(func.min(ScheduledJob.run_at)).where(ScheduledJob.status == "pending"))
        if next_run is None or next_run > start:
            break
        c.clock.set(max(next_run, c.clock.now()))
        await online(c)
        await worker.drain()
    async with c.db.session() as s:
        outcomes = [r.outcome for r in (await s.scalars(select(Reminder))).all()]
    assert "telegram_sent+mac" in outcomes
    notices = [a for a in await actions(c) if a.kind == "notify"]
    assert notices and "Встреча с Ниной" in notices[0].payload["body"]
    assert any("Встреча с Ниной" in t for t in c.telegram.texts())  # Telegram still gets it


@pytest.mark.db
async def test_no_mirroring_when_the_mac_is_offline_or_disabled(container: Any, worker: Any) -> None:
    from jarvis.jobs.handlers import _mirror_to_mac

    c = container
    assert await _mirror_to_mac(c, "текст", c.clock.now(), "k1") is False  # never connected
    await online(c)
    c.settings.mac_notify_reminders = False
    assert await _mirror_to_mac(c, "текст", c.clock.now(), "k1") is False
    c.settings.mac_notify_reminders = True
    assert await _mirror_to_mac(c, "текст", c.clock.now(), "k1") is True
    assert await _mirror_to_mac(c, "текст", c.clock.now() + timedelta(minutes=5), "k2") is False  # not seen for 5 min


@pytest.mark.db
async def test_a_retried_reminder_is_not_shown_twice_on_the_mac(container: Any) -> None:
    # L3 (review 17.10): a job retried after the notice was queued used to queue a second one.
    from jarvis.jobs.handlers import _mirror_to_mac

    c = container
    await online(c)
    assert await _mirror_to_mac(c, "Встреча", c.clock.now(), "reminder:1") is True
    assert await _mirror_to_mac(c, "Встреча", c.clock.now(), "reminder:1") is True
    assert await _mirror_to_mac(c, "Врач", c.clock.now(), "reminder:2") is True
    assert [a.payload["body"] for a in await actions(c)] == ["Встреча", "Врач"]


@pytest.mark.db
async def test_a_mac_notice_with_nowhere_to_go_is_not_a_success(container: Any, worker: Any) -> None:
    from jarvis.core.errors import NotConfiguredError

    c = container

    async def nowhere(*args: Any, **kwargs: Any) -> Any:
        raise NotConfiguredError("telegram: no chat")

    c.notifier.send = nowhere
    async with c.db.session() as s:
        await enqueue(s, job_type="desktop.notify", run_at=c.clock.now(), payload={"text": "чайник"})
    await worker.drain()
    async with c.db.session() as s:
        job = await s.scalar(select(ScheduledJob).where(ScheduledJob.job_type == "desktop.notify"))
    assert job is not None and job.status == "skipped" and "undelivered" in (job.last_error or "")
