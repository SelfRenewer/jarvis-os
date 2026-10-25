"""Google Calendar through its secret iCal address — read-only, no OAuth, no Cloud project, no 7-day expiry.

`GOOGLE_CALENDAR_ICS_URL` (Google Calendar → Settings → the calendar → «Integrate calendar» → «Secret address in
iCal format») is a key to the whole calendar: it lives only in `.env` and is never logged. The feed is expanded
(recurrences, exceptions, moved instances, time zones) by `recurring-ical-events` and returned in the shape of the
Google Calendar API, so `CalendarSync` works unchanged. Push channels and writes (the mirror strategy) need OAuth.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import icalendar
import recurring_ical_events

from jarvis.core.errors import AuthError, CapabilityUnavailableError, NotFoundError, PermanentError
from jarvis.core.http import ResilientHttp
from jarvis.core.retry import RetryPolicy

FEED_TTL = 60.0  # one download serves list_calendars + list_events of the same reconcile
_STATUS = {"CANCELLED": "cancelled", "TENTATIVE": "tentative"}


def owner_of(url: str) -> str | None:
    """`…/calendar/ical/<calendar id = owner e-mail>/private-…/basic.ics` → the owner (to spot «declined by me»)."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if "ical" in parts and parts.index("ical") + 1 < len(parts):
        return unquote(parts[parts.index("ical") + 1]).lower()
    return None


def _value(component: Any, name: str) -> Any:
    prop = component.get(name)
    return getattr(prop, "dt", prop)


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    return str(value).strip() or None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(value: datetime, tz: ZoneInfo) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


def clean_uid(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "", str(value or "")).removesuffix("@google.com")[:200] or "noid"


def recurring_uids(calendar: Any) -> set[str]:
    """UIDs of series in the feed itself (the expander adds RECURRENCE-ID to every occurrence, even single ones)."""
    return {
        clean_uid(c.get("UID"))
        for c in calendar.walk("VEVENT")
        if any(c.get(k) is not None for k in ("RRULE", "RDATE", "RECURRENCE-ID"))
    }


def to_google_event(event: Any, tz: ZoneInfo, owner: str | None, series: set[str] | None = None) -> dict[str, Any]:
    """One VEVENT occurrence → a Google Calendar API event resource (the fields `parse_event` reads)."""
    uid = clean_uid(event.get("UID"))
    start = _value(event, "DTSTART")
    end = _value(event, "DTEND")
    if end is None and event.get("DURATION") is not None:
        end = start + _value(event, "DURATION")
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    if all_day:
        end = end if isinstance(end, date) and not isinstance(end, datetime) else start + timedelta(days=1)
        start_field, end_field = {"date": start.isoformat()}, {"date": end.isoformat()}
        stamp = start.strftime("%Y%m%d")
    else:
        start = _aware(start, tz)
        end = _aware(end, tz) if isinstance(end, datetime) else start
        start_field, end_field = {"dateTime": _rfc3339(start)}, {"dateTime": _rfc3339(end)}
        stamp = start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    recurring = uid in series if series is not None else event.get("RRULE") is not None
    if recurring and (original := _value(event, "RECURRENCE-ID")) is not None:
        # An instance keeps the id of its ORIGINAL start (as in Google): a moved meeting is the same event.
        if isinstance(original, datetime):
            stamp = _aware(original, tz).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        else:
            stamp = original.strftime("%Y%m%d")
    raw: dict[str, Any] = {
        "id": f"{uid}_{stamp}" if recurring else uid,
        "status": _STATUS.get(str(event.get("STATUS") or "").upper(), "confirmed"),
        "summary": _text(event, "SUMMARY"),
        "location": _text(event, "LOCATION"),
        "start": start_field,
        "end": end_field,
        "transparency": "transparent" if str(event.get("TRANSP") or "").upper() == "TRANSPARENT" else "opaque",
    }
    if recurring:
        raw["recurringEventId"] = uid
    modified = _value(event, "LAST-MODIFIED")
    if isinstance(modified, datetime):
        raw["updated"] = _rfc3339(_aware(modified, ZoneInfo("UTC")))
    attendees = event.get("ATTENDEE")
    if attendees is not None and owner:
        for attendee in attendees if isinstance(attendees, list) else [attendees]:
            if str(attendee).lower().removeprefix("mailto:") == owner:
                partstat = str(attendee.params.get("PARTSTAT") or "").upper()
                raw["attendees"] = [
                    {"self": True, "responseStatus": "declined" if partstat == "DECLINED" else "accepted"}
                ]
    # DTSTAMP is the export time (it changes on every download): the etag covers only what the user can change.
    material = "|".join(
        str(x) for x in (raw["id"], start_field, end_field, raw["summary"], raw["location"], raw["status"],
                         raw["transparency"], raw.get("updated"), event.get("SEQUENCE"), raw.get("attendees"))
    )  # fmt: skip
    raw["etag"] = '"' + hashlib.sha256(material.encode()).hexdigest()[:24] + '"'
    return raw


class IcsCalendarApi:
    """The CalendarApi protocol over a secret iCal feed (one calendar, read-only)."""

    def __init__(self, url: str, tz: ZoneInfo, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url
        self._tz = tz
        self._owner = owner_of(url)
        self._http = ResilientHttp(
            "google-ics",
            timeout=20.0,
            retry=RetryPolicy(base_seconds=1.0, cap_seconds=5.0, max_attempts=3, jitter_ratio=0),
            transport=transport,
        )
        self._cache: tuple[float, icalendar.Calendar] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _feed(self) -> icalendar.Calendar:
        if self._cache and time.monotonic() - self._cache[0] < FEED_TTL:
            return self._cache[1]
        try:
            response = await self._http.get(self._url)
        except NotFoundError as exc:  # the address was reset in Google Calendar
            raise AuthError("google-ics: секретный адрес больше не работает (сброшен?)") from exc
        try:
            calendar = icalendar.Calendar.from_ical(response.content)
        except ValueError as exc:
            raise PermanentError("google-ics: the address did not return an iCal calendar") from exc
        self._cache = (time.monotonic(), calendar)
        return calendar

    def _calendar_tz(self, calendar: icalendar.Calendar) -> ZoneInfo:
        name = str(calendar.get("X-WR-TIMEZONE") or "")
        try:
            return ZoneInfo(name) if name else self._tz
        except (ZoneInfoNotFoundError, ValueError):
            return self._tz

    async def list_calendars(self) -> list[dict[str, Any]]:
        calendar = await self._feed()
        name = str(calendar.get("X-WR-CALNAME") or self._owner or "primary")
        return [{"id": "primary", "summary": name, "primary": True, "accessRole": "reader"}]

    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
        if calendar_id not in ("primary", self._owner):
            return []  # the secret address carries one calendar
        calendar = await self._feed()
        tz = self._calendar_tz(calendar)
        occurrences = recurring_ical_events.of(calendar).between(time_min, time_max)
        series = recurring_uids(calendar)
        return [to_google_event(event, tz, self._owner, series) for event in occurrences]

    async def watch(
        self, calendar_id: str, channel_id: str, address: str, token: str, ttl_seconds: int
    ) -> dict[str, Any]:
        raise CapabilityUnavailableError("google-ics", "push notifications need OAuth (polling keeps working)")

    async def stop_channel(self, channel_id: str, resource_id: str) -> None:
        return None

    async def upsert_mirror_event(self, calendar_id: str, event_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
        raise CapabilityUnavailableError("google-ics", "the address is read-only: the mirror needs OAuth")

    async def find_mirror_event(self, calendar_id: str, todoist_task_id: str) -> dict[str, Any] | None:
        return None
