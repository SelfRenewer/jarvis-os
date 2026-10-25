"""Walk through a simulated day with the REAL application code and FAKE external services.

Nothing leaves the machine: Telegram/Todoist/Calendar/Maps/calls are in-memory fakes (JARVIS_ENV=test
wiring), the database is a scratch `jarvis_demo` created and dropped by scripts/demo.sh.
Prints a JSON timeline of what the user would see.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from jarvis.bot.service import BotService
from jarvis.core.clock import FrozenClock
from jarvis.core.config import Settings
from jarvis.core.retry import RetryPolicy
from jarvis.db.models import ScheduledJob
from jarvis.db.session import Database
from jarvis.jobs.handlers import build_registry
from jarvis.scheduler.queue import JobQueue, enqueue
from jarvis.scheduler.runner import Worker
from jarvis.services.container import build_container

TZ = ZoneInfo("Asia/Tbilisi")
USER = 1001
DAY = datetime(2026, 9, 24, tzinfo=TZ).date()


def at(h: int, m: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def main() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        jarvis_env="test",
        database_url=os.environ["DATABASE_URL"],
        telegram_allowed_user_ids=str(USER),
        default_origin="Blue Door Cafe, 12 Rustaveli Ave, Tbilisi",
        user_phone_number="+15555550111",
        voice_calls_enabled=True,
        dev_repo_ids="golden-app",
    )
    clock = FrozenClock(at(7, 55))
    db = Database.from_url(settings.database_url)
    c = build_container(settings, db=db, clock=clock)
    worker = Worker(JobQueue(db, clock, worker_id="demo", retry=RetryPolicy(10, 60, 3, 0)), build_registry(), c)
    bot = BotService(c)
    timeline: list[dict[str, Any]] = []
    seen = 0
    update_id = 0

    def collect(source: str) -> None:
        nonlocal seen
        for msg in c.telegram.sent[seen:]:  # type: ignore[attr-defined]
            buttons = [b["text"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]
            timeline.append(
                {
                    "time": clock.now().astimezone(TZ).strftime("%H:%M"),
                    "from": "jarvis",
                    "via": source,
                    "text": msg["text"],
                    "buttons": buttons,
                }
            )
        seen = len(c.telegram.sent)  # type: ignore[attr-defined]

    async def say(text: str) -> None:
        nonlocal update_id
        update_id += 1
        timeline.append({"time": clock.now().astimezone(TZ).strftime("%H:%M"), "from": "user", "text": text})
        await bot.handle_update(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "text": text,
                    "from": {"id": USER},
                    "chat": {"id": USER, "type": "private"},
                },
            }
        )
        await worker.drain()
        collect("reply")

    async def run(job_type: str) -> None:
        async with db.session() as s:
            await enqueue(s, job_type=job_type, run_at=clock.now())
        await worker.drain()
        collect("job")

    # --- external world (fakes) ---
    c.todoist.add(
        id="T1",
        content="Сходить в барбершоп",
        priority=3,
        due={"date": DAY.isoformat()},  # type: ignore[attr-defined]
        duration={"amount": 60, "unit": "minute"},
    )
    c.todoist.add(id="T2", content="Оплатить интернет", priority=2, due={"date": DAY.isoformat()})  # type: ignore[attr-defined]
    for start, end, title, loc in [
        ((9, 0), (12, 30), "Работа над проектом", None),
        ((13, 0), (14, 0), "Обед с Ниной", None),
        ((15, 0), (16, 0), "Встреча с клиентом", "Tbilisi Boulevard, Tbilisi"),
    ]:
        extra = {"location": loc} if loc else {}
        c.calendar.add_event(
            "primary",
            summary=title,
            start={"dateTime": iso(at(*start))},  # type: ignore[attr-defined]
            end={"dateTime": iso(at(*end))},
            **extra,
        )
    c.routes.durations[(settings.default_origin, "Tbilisi Boulevard, Tbilisi")] = 22 * 60  # type: ignore[attr-defined]

    await run("todoist.reconcile")
    await run("calendar.reconcile")  # also plans reminders

    clock.set(at(8, 0))
    await run("briefing.morning")  # the day + the plan proposal, one message

    await say("Что у меня завтра?")
    await say("Готово: интернет")
    await say("Добавь купить молоко")
    await say("Напомни мне позвонить в банк завтра в 10")
    await say("Запиши идею: AI Inbox, который понимает, задача это, идея или проект")
    await say("Добавь яйца, молоко и 2 кг куриного филе")
    offers = {
        "stores": [
            {"store_key": "a", "name": "Магазин A", "delivery_fee": "3.99", "service_fee": "1.00"},
            {"store_key": "b", "name": "Магазин B", "delivery_fee": None, "service_fee": "0.50"},
        ],
        "offers": [
            {
                "store_key": "a",
                "product_name": "Milk 1L",
                "package_quantity": "1",
                "package_unit": "l",
                "price": "4.50",
            },
            {
                "store_key": "a",
                "product_name": "Chicken fillet 1kg",
                "package_quantity": "1",
                "package_unit": "kg",
                "price": "14.00",
            },
            {
                "store_key": "a",
                "product_name": "Eggs 10",
                "package_quantity": "10",
                "package_unit": "pcs",
                "price": "5.50",
            },
            {
                "store_key": "b",
                "product_name": "Milk 900ml",
                "package_quantity": "900",
                "package_unit": "ml",
                "price": "4.20",
            },
            {
                "store_key": "b",
                "product_name": "Chicken fillet 500g",
                "package_quantity": "500",
                "package_unit": "g",
                "price": "6.40",
            },
        ],
    }
    await say("/shop import " + json.dumps(offers, ensure_ascii=False))
    await say("/shop quote")
    await say("Преврати эту идею в ТЗ")

    # Let the day run: jump to each scheduled job (as the always-on worker would) until 17:00.
    calls: list[dict[str, Any]] = []
    while True:
        async with db.session() as s:
            next_run = await s.scalar(select(func.min(ScheduledJob.run_at)).where(ScheduledJob.status == "pending"))
        if next_run is None or next_run > at(17, 0):
            break
        clock.set(max(next_run, clock.now()))
        placed_before = len(c.call_provider.placed)  # type: ignore[attr-defined]
        await worker.drain()
        collect("job")
        for spec in c.call_provider.placed[placed_before:]:  # type: ignore[attr-defined]
            calls.append({"time": clock.now().astimezone(TZ).strftime("%H:%M"), "variables": spec.variables})

    clock.set(at(21, 0))
    await run("briefing.evening")  # what is not done + buttons to move it

    print(json.dumps({"timeline": timeline, "calls": calls}, ensure_ascii=False))
    await c.aclose()


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL", "").endswith("/jarvis_demo"):
        sys.exit("refusing to run: DATABASE_URL must point to the scratch database jarvis_demo")
    asyncio.run(main())
