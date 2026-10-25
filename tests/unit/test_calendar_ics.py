"""Google Calendar through the secret iCal address: recurrences, exceptions, moved instances, all-day events, a
declined invitation, a stable etag across downloads (DTSTAMP changes every time), a reset address."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis.core.errors import AuthError, CapabilityUnavailableError
from jarvis.integrations.google_calendar.client import parse_event
from jarvis.integrations.google_calendar.ics import IcsCalendarApi, owner_of

TZ = ZoneInfo("Asia/Tbilisi")
URL = "https://calendar.google.com/calendar/ical/me%40example.com/private-abc123/basic.ics"


def feed(stamp: str = "20260923T050000Z") -> bytes:
    return f"""BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
X-WR-CALNAME:Alex Example
X-WR-TIMEZONE:Asia/Tbilisi
BEGIN:VEVENT
DTSTART;TZID=Asia/Tbilisi:20260924T150000
DTEND;TZID=Asia/Tbilisi:20260924T160000
DTSTAMP:{stamp}
UID:single1@google.com
SUMMARY:Стоматолог
LOCATION:Тбилиси\\, Руставели 1
STATUS:CONFIRMED
LAST-MODIFIED:20260920T100000Z
SEQUENCE:0
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Tbilisi:20260928T100000
DTEND;TZID=Asia/Tbilisi:20260928T110000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4
EXDATE;TZID=Asia/Tbilisi:20261005T100000
DTSTAMP:{stamp}
UID:weekly1@google.com
SUMMARY:Планёрка
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Tbilisi:20261012T120000
DTEND;TZID=Asia/Tbilisi:20261012T130000
RECURRENCE-ID;TZID=Asia/Tbilisi:20261012T100000
DTSTAMP:{stamp}
UID:weekly1@google.com
SUMMARY:Планёрка (перенос)
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260926
DTEND;VALUE=DATE:20260927
DTSTAMP:{stamp}
UID:allday1@google.com
SUMMARY:День рождения Ани
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
DTSTART:20260925T080000Z
DTEND:20260925T090000Z
DTSTAMP:{stamp}
UID:invite1@google.com
SUMMARY:Чужая встреча
ATTENDEE;PARTSTAT=DECLINED;CN=me@example.com:mailto:me@example.com
ATTENDEE;PARTSTAT=ACCEPTED;CN=boss@example.com:mailto:boss@example.com
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").encode()


def api_with(*responses: httpx.Response) -> tuple[IcsCalendarApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return IcsCalendarApi(URL, TZ, transport=httpx.MockTransport(handler)), seen


WINDOW = (datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC))


def test_owner_from_the_address() -> None:
    assert owner_of(URL) == "me@example.com"


async def test_events_in_the_google_shape() -> None:
    api, _ = api_with(httpx.Response(200, content=feed()))
    assert await api.list_calendars() == [
        {"id": "primary", "summary": "Alex Example", "primary": True, "accessRole": "reader"}
    ]
    events = {e["id"]: e for e in await api.list_events("primary", *WINDOW)}
    snaps = {k: parse_event("primary", v, TZ) for k, v in events.items()}
    single = snaps["single1"]
    assert (single.summary, single.location) == ("Стоматолог", "Тбилиси, Руставели 1")
    assert single.start_at == datetime(2026, 9, 24, 11, 0, tzinfo=UTC)  # 15:00 Tbilisi
    assert single.updated == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    weekly = sorted(k for k in snaps if k.startswith("weekly1_"))  # 05.10 is excluded (EXDATE)
    assert weekly == ["weekly1_20260928T060000Z", "weekly1_20261012T060000Z", "weekly1_20261019T060000Z"]
    assert sorted(snaps) == ["allday1", "invite1", "single1", *weekly]  # single events keep their own id
    moved = snaps["weekly1_20261012T060000Z"]  # the instance keeps its id, but moved to 12:00 and renamed
    assert (moved.summary, moved.start_at) == ("Планёрка (перенос)", datetime(2026, 10, 12, 8, 0, tzinfo=UTC))
    assert moved.recurring_event_id == "weekly1"
    allday = snaps["allday1"]
    assert (allday.all_day, allday.start_date, allday.transparent) == (True, datetime(2026, 9, 26).date(), True)
    assert snaps["invite1"].self_declined is True


async def test_etag_ignores_the_export_time_and_the_feed_is_cached() -> None:
    api, seen = api_with(httpx.Response(200, content=feed("20260923T050000Z")))
    first = {e["id"]: e["etag"] for e in await api.list_events("primary", *WINDOW)}
    await api.list_calendars()
    assert len(seen) == 1  # one download serves one reconcile
    api2, _ = api_with(httpx.Response(200, content=feed("20260923T051500Z")))
    second = {e["id"]: e["etag"] for e in await api2.list_events("primary", *WINDOW)}
    assert first == second


async def test_reset_address_is_an_auth_error_and_writes_are_unavailable() -> None:
    api, _ = api_with(httpx.Response(404, text="Not Found"))
    with pytest.raises(AuthError, match="секретный адрес больше не работает"):
        await api.list_events("primary", *WINDOW)
    with pytest.raises(CapabilityUnavailableError):
        await api.upsert_mirror_event("primary", None, {})
    assert await api.list_events("other@example.com", *WINDOW) == []  # one calendar per address


@pytest.mark.db
async def test_calendar_sync_over_the_ics_feed(db: Any, clock: Any) -> None:
    from sqlalchemy import select

    from jarvis.db.models import CalendarEvent
    from jarvis.sync.calendar_sync import CalendarSync
    from tests.conftest import make_settings

    stamps = iter(["20260923T050000Z", "20260923T051000Z"])
    api = IcsCalendarApi(
        URL, TZ, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=feed(next(stamps))))
    )
    sync = CalendarSync(api, make_settings())
    async with db.session() as s:
        first = await sync.reconcile(s, clock.now())
    async with db.session() as s:
        stored = (await s.scalars(select(CalendarEvent).order_by(CalendarEvent.start_at))).all()
    assert first["changed"] == len(stored) >= 4
    assert stored[0].summary == "Стоматолог"
    api._cache = None  # the next reconcile downloads again (DTSTAMP differs)
    async with db.session() as s:
        second = await sync.reconcile(s, clock.now())
    assert (second["changed"], second["removed"]) == (0, 0)
