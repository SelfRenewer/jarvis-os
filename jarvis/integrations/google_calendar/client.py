"""Google Calendar v3 adapter with OAuth refresh-token handling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from jarvis.core.clock import local_datetime, parse_rfc3339
from jarvis.core.errors import AuthError, NotFoundError, PermanentError, TransientError
from jarvis.core.http import ResilientHttp

API = "https://www.googleapis.com/calendar/v3/"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES_READ = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
SCOPE_MIRROR = "https://www.googleapis.com/auth/calendar.events.owned"


@dataclass(frozen=True)
class EventSnapshot:
    calendar_id: str
    event_id: str
    etag: str | None
    status: str
    summary: str | None
    location: str | None
    start_at: datetime | None
    end_at: datetime | None
    all_day: bool
    start_date: date | None
    end_date: date | None
    transparent: bool
    self_declined: bool
    recurring_event_id: str | None
    jarvis_managed: bool
    todoist_task_id: str | None
    updated: datetime | None


def parse_event(calendar_id: str, raw: dict[str, Any], user_tz: ZoneInfo) -> EventSnapshot:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    all_day = "date" in start and "dateTime" not in start
    start_at = end_at = None
    start_date = end_date = None
    if all_day:
        start_date = date.fromisoformat(start["date"])
        end_date = date.fromisoformat(end.get("date", start["date"]))
        start_at = local_datetime(start_date, datetime.min.time(), user_tz).astimezone(UTC)
        end_at = local_datetime(end_date, datetime.min.time(), user_tz).astimezone(UTC)
    elif start.get("dateTime"):
        start_at = parse_rfc3339(start["dateTime"])
        end_at = parse_rfc3339(end["dateTime"]) if end.get("dateTime") else start_at
    private = ((raw.get("extendedProperties") or {}).get("private")) or {}
    declined = any(a.get("self") and a.get("responseStatus") == "declined" for a in raw.get("attendees") or [])
    updated = parse_rfc3339(raw["updated"]) if raw.get("updated") else None
    return EventSnapshot(
        calendar_id=calendar_id,
        event_id=str(raw["id"]),
        etag=raw.get("etag"),
        status=str(raw.get("status") or "confirmed"),
        summary=raw.get("summary"),
        location=(raw.get("location") or "").strip() or None,
        start_at=start_at,
        end_at=end_at,
        all_day=all_day,
        start_date=start_date,
        end_date=end_date,
        transparent=raw.get("transparency") == "transparent",
        self_declined=declined,
        recurring_event_id=raw.get("recurringEventId"),
        jarvis_managed=private.get("jarvis_managed") == "true",
        todoist_task_id=private.get("todoist_task_id"),
        updated=updated,
    )


class TokenStore(Protocol):
    async def load_refresh_token(self) -> str | None: ...

    async def mark_needs_reauth(self, reason: str) -> None: ...


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, scopes: list[str], state: str, challenge: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


async def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    http = ResilientHttp("google-oauth", timeout=20.0, transport=transport)
    try:
        response = await http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        return dict(response.json())
    finally:
        await http.aclose()


class CalendarApi(Protocol):
    async def list_calendars(self) -> list[dict[str, Any]]: ...

    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]: ...

    async def watch(
        self, calendar_id: str, channel_id: str, address: str, token: str, ttl_seconds: int
    ) -> dict[str, Any]: ...

    async def stop_channel(self, channel_id: str, resource_id: str) -> None: ...

    async def upsert_mirror_event(
        self, calendar_id: str, event_id: str | None, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def find_mirror_event(self, calendar_id: str, todoist_task_id: str) -> dict[str, Any] | None: ...


class HttpCalendarApi:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_store: TokenStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = token_store
        self._http = ResilientHttp("google-calendar", base_url=API, timeout=20.0, transport=transport)
        self._oauth = ResilientHttp("google-oauth", timeout=20.0, transport=transport)
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._now = now
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._oauth.aclose()

    async def _token(self, force: bool = False) -> str:
        async with self._lock:
            if (
                not force
                and self._access_token
                and self._expires_at
                and self._now() < self._expires_at - timedelta(seconds=60)
            ):
                return self._access_token
            refresh = await self._store.load_refresh_token()
            if not refresh:
                raise AuthError("google: not authorized (run `jarvis google-auth`)")
            try:
                response = await self._oauth.post(
                    TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": refresh,
                        "grant_type": "refresh_token",
                    },
                    retry_non_idempotent=True,
                )
            except PermanentError as exc:
                if "invalid_grant" in str(exc) or isinstance(exc, AuthError) or "HTTP 400" in str(exc):
                    await self._store.mark_needs_reauth("refresh token rejected (invalid_grant)")
                    raise AuthError("google: refresh token revoked or expired — re-run `jarvis google-auth`") from exc
                raise
            data = response.json()
            self._access_token = str(data["access_token"])
            self._expires_at = self._now() + timedelta(seconds=int(data.get("expires_in", 3600)))
            return self._access_token

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            return await self._http.request(method, url, headers=headers, **kwargs)
        except AuthError:
            token = await self._token(force=True)  # access token expired early / revoked
            headers = {"Authorization": f"Bearer {token}"}
            return await self._http.request(method, url, headers=headers, **kwargs)

    async def list_calendars(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"maxResults": 250}
            if page:
                params["pageToken"] = page
            data = (await self._request("GET", "users/me/calendarList", params=params)).json()
            items.extend(data.get("items") or [])
            page = data.get("nextPageToken")
            if not page:
                break
        return items

    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        path = f"calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        for _ in range(40):
            params: dict[str, Any] = {
                "timeMin": time_min.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "timeMax": time_max.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "singleEvents": "true",
                "showDeleted": "true",
                "maxResults": 250,
                "orderBy": "startTime",
            }
            if page:
                params["pageToken"] = page
            data = (await self._request("GET", path, params=params)).json()
            items.extend(data.get("items") or [])
            page = data.get("nextPageToken")
            if not page:
                return items
        raise PermanentError("google: event pagination did not terminate")

    async def watch(
        self, calendar_id: str, channel_id: str, address: str, token: str, ttl_seconds: int
    ) -> dict[str, Any]:
        if not address.startswith("https://"):
            raise PermanentError("google push requires an HTTPS address")
        path = f"calendars/{urllib.parse.quote(calendar_id, safe='')}/events/watch"
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": token,
            "params": {"ttl": str(ttl_seconds)},
        }
        return dict((await self._request("POST", path, json=body)).json())

    async def stop_channel(self, channel_id: str, resource_id: str) -> None:
        try:
            await self._request("POST", "channels/stop", json={"id": channel_id, "resourceId": resource_id})
        except NotFoundError:
            return

    async def find_mirror_event(self, calendar_id: str, todoist_task_id: str) -> dict[str, Any] | None:
        path = f"calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        params = {
            "privateExtendedProperty": [f"todoist_task_id={todoist_task_id}", "jarvis_managed=true"],
            "showDeleted": "false",
            "maxResults": 5,
        }
        items = (await self._request("GET", path, params=params)).json().get("items") or []
        return dict(items[0]) if items else None

    async def upsert_mirror_event(self, calendar_id: str, event_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
        private = (body.get("extendedProperties") or {}).get("private") or {}
        if private.get("jarvis_managed") != "true" or not private.get("todoist_task_id"):
            raise PermanentError("mirror events must carry jarvis_managed + todoist_task_id")
        path = f"calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        if event_id:
            response = await self._request("PATCH", f"{path}/{urllib.parse.quote(event_id, safe='')}", json=body)
        else:
            response = await self._request("POST", path, json=body)
        return dict(response.json())


@dataclass
class FakeCalendarApi:
    calendars: list[dict[str, Any]] = field(default_factory=lambda: [{"id": "primary", "summary": "Main"}])
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    watches: list[dict[str, Any]] = field(default_factory=list)
    mirror_writes: list[dict[str, Any]] = field(default_factory=list)
    unavailable: bool = False

    def add_event(self, calendar_id: str, **raw: Any) -> dict[str, Any]:
        event = {"id": f"ev{len(self.events.get(calendar_id, [])) + 1}", "status": "confirmed", "etag": '"1"', **raw}
        self.events.setdefault(calendar_id, []).append(event)
        return event

    async def list_calendars(self) -> list[dict[str, Any]]:
        return list(self.calendars)

    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
        if self.unavailable:
            raise TransientError("google: fake outage")
        result = []
        for event in self.events.get(calendar_id, []):
            start = event.get("start", {})
            if "dateTime" in start:
                start_at = parse_rfc3339(start["dateTime"])
                end_at = parse_rfc3339(event["end"]["dateTime"])
                if end_at <= time_min or start_at >= time_max:
                    continue
            result.append(dict(event))
        return result

    async def watch(
        self, calendar_id: str, channel_id: str, address: str, token: str, ttl_seconds: int
    ) -> dict[str, Any]:
        record = {
            "id": channel_id,
            "resourceId": f"res-{channel_id}",
            "calendar_id": calendar_id,
            "expiration": str(int((datetime.now(UTC) + timedelta(seconds=ttl_seconds)).timestamp() * 1000)),
        }
        self.watches.append(record)
        return record

    async def stop_channel(self, channel_id: str, resource_id: str) -> None:
        return None

    async def find_mirror_event(self, calendar_id: str, todoist_task_id: str) -> dict[str, Any] | None:
        for event in self.events.get(calendar_id, []):
            private = (event.get("extendedProperties") or {}).get("private") or {}
            if private.get("todoist_task_id") == todoist_task_id and private.get("jarvis_managed") == "true":
                return dict(event)
        return None

    async def upsert_mirror_event(self, calendar_id: str, event_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
        self.mirror_writes.append({"calendar_id": calendar_id, "event_id": event_id, "body": body})
        if event_id:
            for event in self.events.get(calendar_id, []):
                if event["id"] == event_id:
                    event.update(body)
                    return dict(event)
        return self.add_event(calendar_id, **body)


FetchRefresh = Callable[[], Awaitable[str | None]]
