"""HTTP-level adapter tests against recorded-shape responses (respx). No real network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from jarvis.core.errors import AuthError, LiveActionBlockedError, NotFoundError, PermanentError
from jarvis.integrations.google_calendar.client import HttpCalendarApi, parse_event
from jarvis.integrations.maps.routes import HttpRoutesApi, RouteStatus, billing_sku, parse_coordinates, waypoint
from jarvis.integrations.stt.client import DeepgramSTT
from jarvis.integrations.telegram.client import HttpTelegramApi, InlineButton
from jarvis.integrations.todoist.client import HttpTodoistApi, parse_task
from jarvis.integrations.voice.providers import CallSpec, TwilioCallProvider, VapiCallProvider, mask_number

TZ = ZoneInfo("Asia/Tbilisi")


# --- Todoist ---------------------------------------------------------------------------------------


@respx.mock
async def test_todoist_pagination_and_schedule() -> None:
    api = HttpTodoistApi("tok")
    # More specific route first: respx matches params as a subset.
    respx.get("https://api.todoist.com/api/v1/tasks", params={"limit": "200", "cursor": "c2"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "2", "content": "b"}], "next_cursor": None})
    )
    respx.get("https://api.todoist.com/api/v1/tasks", params={"limit": "200"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "1", "content": "a"}], "next_cursor": "c2"})
    )
    tasks = await api.list_active_tasks()
    assert [t["id"] for t in tasks] == ["1", "2"]
    route = respx.post("https://api.todoist.com/api/v1/tasks/1").mock(return_value=httpx.Response(200, json={}))
    await api.schedule_task("1", datetime(2026, 9, 23, 14, 15, tzinfo=UTC), 60, request_id="r1")
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"due_datetime": "2026-09-23T14:15:00Z", "duration": 60, "duration_unit": "minute"}
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert route.calls[0].request.headers["x-request-id"] == "r1"
    await api.aclose()


@respx.mock
async def test_todoist_404_and_invalid_id() -> None:
    api = HttpTodoistApi("tok")
    respx.get("https://api.todoist.com/api/v1/tasks/9").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError):
        await api.get_task("9")
    with pytest.raises(PermanentError):
        await api.get_task("../../etc")
    with pytest.raises(PermanentError):
        await api.schedule_task("1", datetime(2026, 1, 1, tzinfo=UTC), 24 * 60, request_id="x")  # ≥ 24h rejected
    await api.aclose()


@respx.mock
async def test_todoist_task_management_requests() -> None:
    from datetime import date

    api = HttpTodoistApi("tok")
    update = respx.post("https://api.todoist.com/api/v1/tasks/7").mock(return_value=httpx.Response(200, json={}))
    close = respx.post("https://api.todoist.com/api/v1/tasks/7/close").mock(return_value=httpx.Response(204))
    reopen = respx.post("https://api.todoist.com/api/v1/tasks/7/reopen").mock(return_value=httpx.Response(204))
    remove = respx.delete("https://api.todoist.com/api/v1/tasks/7").mock(return_value=httpx.Response(204))
    await api.update_task("7", request_id="p", priority=4)
    await api.update_task("7", request_id="d", due_date=date(2026, 9, 24))
    await api.update_task(
        "7", request_id="t", due_datetime=datetime(2026, 9, 24, 6, 0, tzinfo=UTC), duration_minutes=30
    )
    await api.close_task("7", request_id="c")
    await api.reopen_task("7", request_id="o")
    await api.delete_task("7", request_id="x")
    bodies = [json.loads(call.request.content) for call in update.calls]
    assert bodies == [
        {"priority": 4},
        {"due_date": "2026-09-24"},
        {"due_datetime": "2026-09-24T06:00:00Z", "duration": 30, "duration_unit": "minute"},
    ]
    assert [c.request.headers["x-request-id"] for c in update.calls] == ["p", "d", "t"]
    assert close.called and reopen.called and remove.called
    assert remove.calls[0].request.headers["x-request-id"] == "x"
    with pytest.raises(PermanentError):
        await api.update_task("7", request_id="bad", priority=5)
    with pytest.raises(PermanentError):
        await api.update_task(
            "7", request_id="bad", due_date=date(2026, 9, 24), due_datetime=datetime(2026, 9, 24, tzinfo=UTC)
        )
    with pytest.raises(PermanentError):
        await api.delete_task("../7", request_id="bad")
    await api.aclose()


def test_parse_task_variants() -> None:
    floating = parse_task(
        {"id": "1", "content": "x", "due": {"date": "2026-09-23T15:00:00", "is_recurring": False, "timezone": None}}, TZ
    )
    assert floating.due_datetime == datetime(2026, 9, 23, 11, 0, tzinfo=UTC)  # 15:00 Tbilisi
    fixed = parse_task(
        {
            "id": "2",
            "content": "x",
            "due": {"date": "2026-09-23", "datetime": "2026-09-23T15:00:00Z", "timezone": "UTC"},
        },
        TZ,
    )
    assert fixed.due_datetime == datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
    all_day = parse_task(
        {
            "id": "3",
            "content": "x",
            "due": {"date": "2026-09-24", "is_recurring": True},
            "duration": {"amount": 1, "unit": "day"},
            "deadline": {"date": "2026-09-30"},
        },
        TZ,
    )
    assert all_day.due_datetime is None
    assert all_day.due_is_recurring
    assert all_day.duration_minutes == 1440
    assert str(all_day.deadline_date) == "2026-09-30"


# --- Google Calendar -------------------------------------------------------------------------------


class _Store:
    def __init__(self, token: str | None = "refresh") -> None:
        self.token = token
        self.reauth: list[str] = []

    async def load_refresh_token(self) -> str | None:
        return self.token

    async def mark_needs_reauth(self, reason: str) -> None:
        self.reauth.append(reason)


@respx.mock
async def test_google_token_refresh_and_events() -> None:
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
    )
    events = respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "status": "confirmed",
                        "summary": "Meet",
                        "start": {"dateTime": "2026-09-23T15:00:00+04:00"},
                        "end": {"dateTime": "2026-09-23T16:00:00+04:00"},
                    }
                ]
            },
        )
    )
    api = HttpCalendarApi("cid", "csecret", _Store())
    items = await api.list_events("primary", datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 24, tzinfo=UTC))
    assert items[0]["id"] == "e1"
    request = events.calls[0].request
    assert request.headers["authorization"] == "Bearer at-1"
    assert request.url.params["singleEvents"] == "true"
    assert request.url.params["showDeleted"] == "true"
    await api.aclose()


@respx.mock
async def test_google_invalid_grant_marks_reauth() -> None:
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    store = _Store()
    api = HttpCalendarApi("cid", "csecret", store)
    with pytest.raises(AuthError, match="re-run"):
        await api.list_calendars()
    assert store.reauth
    await api.aclose()


async def test_google_not_authorized() -> None:
    api = HttpCalendarApi("cid", "csecret", _Store(token=None))
    with pytest.raises(AuthError, match="google-auth"):
        await api.list_calendars()
    await api.aclose()


async def test_google_mirror_requires_markers() -> None:
    api = HttpCalendarApi("cid", "csecret", _Store())
    with pytest.raises(PermanentError, match="jarvis_managed"):
        await api.upsert_mirror_event("mirror", None, {"summary": "x"})
    await api.aclose()


def test_parse_event_all_day_and_declined() -> None:
    event = parse_event(
        "primary",
        {
            "id": "e",
            "start": {"date": "2026-09-23"},
            "end": {"date": "2026-09-24"},
            "attendees": [{"self": True, "responseStatus": "declined"}],
            "extendedProperties": {"private": {"jarvis_managed": "true", "todoist_task_id": "T1"}},
        },
        TZ,
    )
    assert event.all_day
    assert event.self_declined
    assert event.jarvis_managed
    assert event.todoist_task_id == "T1"
    assert event.start_at == datetime(2026, 9, 22, 20, 0, tzinfo=UTC)  # local midnight in Tbilisi


# --- Maps ------------------------------------------------------------------------------------------


@respx.mock
async def test_routes_traffic_aware_request() -> None:
    route = respx.post("https://routes.googleapis.com/directions/v2:computeRoutes").mock(
        return_value=httpx.Response(
            200, json={"routes": [{"duration": "1320s", "staticDuration": "1100s", "distanceMeters": 5400}]}
        )
    )
    api = HttpRoutesApi("mapkey")
    result = await api.compute("A", "B", datetime.now(UTC) + timedelta(hours=1), "DRIVE")
    assert result.status == RouteStatus.OK
    assert result.duration_seconds == 1320
    assert result.traffic_aware
    body = json.loads(route.calls[0].request.content)
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    assert "departureTime" in body
    assert route.calls[0].request.headers["x-goog-fieldmask"].startswith("routes.duration")
    await api.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(200, json={}), RouteStatus.NOT_FOUND),
        (httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}), RouteStatus.NOT_FOUND),
        (httpx.Response(429), RouteStatus.QUOTA),
        (httpx.Response(503), RouteStatus.UNAVAILABLE),
    ],
)
async def test_routes_failures_never_invent_duration(response: httpx.Response, status: RouteStatus) -> None:
    respx.post("https://routes.googleapis.com/directions/v2:computeRoutes").mock(return_value=response)
    api = HttpRoutesApi("mapkey")
    api._http._sleep = _no_sleep  # type: ignore[attr-defined]
    result = await api.compute("A", "B", datetime.now(UTC), "WALK")
    assert result.status == status
    assert result.duration_seconds is None
    await api.aclose()


ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ONE_ROUTE = {"routes": [{"duration": "600s", "staticDuration": "600s", "distanceMeters": 3000}]}


@respx.mock
async def test_routes_coordinates_become_lat_lng_waypoints() -> None:
    route = respx.post(ROUTES_URL).mock(return_value=httpx.Response(200, json=ONE_ROUTE))
    api = HttpRoutesApi("mapkey")
    result = await api.compute("41.73953,44.82496", "Rustaveli Ave 1, Tbilisi", datetime.now(UTC), "DRIVE")
    assert result.status == RouteStatus.OK
    body = json.loads(route.calls[0].request.content)
    assert body["origin"] == {"location": {"latLng": {"latitude": 41.73953, "longitude": 44.82496}}}
    assert body["destination"] == {"address": "Rustaveli Ave 1, Tbilisi"}
    assert route.calls[0].request.headers["x-goog-api-key"] == "mapkey"
    await api.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("mode", "traffic_aware", "preference", "departure"),
    [
        ("DRIVE", True, "TRAFFIC_AWARE", True),  # Pro SKU, live traffic for the planned departure
        ("DRIVE", False, None, False),  # Essentials SKU: no traffic, departureTime "not recommended"
        ("WALK", True, None, False),  # routingPreference fails for WALK; departure is meaningless
        ("BICYCLE", True, None, False),
        ("TRANSIT", True, None, True),  # schedules depend on the departure
        ("TWO_WHEELER", True, "TRAFFIC_AWARE", True),
    ],
)
async def test_routes_request_matches_mode(
    mode: str, traffic_aware: bool, preference: str | None, departure: bool
) -> None:
    route = respx.post(ROUTES_URL).mock(return_value=httpx.Response(200, json=ONE_ROUTE))
    api = HttpRoutesApi("mapkey", traffic_aware=traffic_aware)
    result = await api.compute("A", "B", datetime.now(UTC) + timedelta(hours=2), mode)
    body = json.loads(route.calls[0].request.content)
    assert body["travelMode"] == mode
    assert body.get("routingPreference") == preference
    assert ("departureTime" in body) is departure
    assert result.traffic_aware is (preference is not None)
    await api.aclose()


@respx.mock
async def test_routes_departure_too_close_is_sent_as_now() -> None:
    """A departure seconds away could reach Google in the past → 400 → wrongly "address not found"."""
    route = respx.post(ROUTES_URL).mock(return_value=httpx.Response(200, json=ONE_ROUTE))
    api = HttpRoutesApi("mapkey")
    await api.compute("A", "B", datetime.now(UTC) + timedelta(seconds=20), "DRIVE")
    body = json.loads(route.calls[0].request.content)
    assert "departureTime" not in body
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    await api.aclose()


@respx.mock
async def test_routes_invalid_key_is_unavailable_not_unknown_address() -> None:
    """Google answers a bad key with HTTP 400 INVALID_ARGUMENT — that is not "address not found"."""
    respx.post(ROUTES_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
    )
    api = HttpRoutesApi("bad-key")
    result = await api.compute("A", "B", datetime.now(UTC), "DRIVE")
    assert result.status == RouteStatus.UNAVAILABLE
    assert result.duration_seconds is None
    assert "API key" in (result.error or "")
    await api.aclose()


def test_routes_billing_sku_and_coordinates() -> None:
    assert billing_sku("DRIVE", True) == "Pro"
    assert billing_sku("drive", False) == "Essentials"
    assert billing_sku("WALK", True) == "Essentials"
    assert billing_sku("TWO_WHEELER", False) == "Enterprise"
    assert parse_coordinates("41.7151, 44.8271") == (41.7151, 44.8271)
    assert parse_coordinates("-33.9,151.2") == (-33.9, 151.2)
    assert parse_coordinates("Blue Door Cafe, 12 Rustaveli Ave") is None
    assert parse_coordinates("Rustaveli Ave 1, Tbilisi") is None
    assert parse_coordinates("95.0,41.0") is None  # not a latitude
    assert waypoint("Blue Door Cafe, Tbilisi") == {"address": "Blue Door Cafe, Tbilisi"}


async def _no_sleep(_: float) -> None:
    return None


# --- Telegram --------------------------------------------------------------------------------------


@respx.mock
async def test_telegram_send_and_errors() -> None:
    api = HttpTelegramApi("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef")
    route = respx.post("https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
    )
    result = await api.send_message(1001, "x" * 5000)  # split into two messages
    assert result["message_id"] == 5
    assert route.call_count == 2
    respx.post("https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef/getMe").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
    )
    with pytest.raises(PermanentError, match="Unauthorized"):
        await api.get_me()
    await api.aclose()


@respx.mock
async def test_telegram_polls_live_location_updates() -> None:
    """Live locations arrive as `edited_message`: without it in allowed_updates Telegram never sends them."""
    api = HttpTelegramApi("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef")
    route = respx.post("https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    await api.get_updates(5, timeout=1)
    body = json.loads(route.calls[0].request.content)
    assert body["allowed_updates"] == ["message", "edited_message", "callback_query"]
    assert body["offset"] == 5
    await api.aclose()


def test_inline_button_callback_limit() -> None:
    InlineButton("ok", "ap:a:" + "x" * 12)
    with pytest.raises(ValueError, match="64"):
        InlineButton("bad", "x" * 65)


# --- Voice -----------------------------------------------------------------------------------------


@respx.mock
async def test_vapi_call_request_shape() -> None:
    route = respx.post("https://api.vapi.ai/call").mock(
        return_value=httpx.Response(201, json={"id": "call-1", "status": "queued", "secret": "x"})
    )
    provider = VapiCallProvider("vapikey", "asst", "phone", live_enabled=True)
    placed = await provider.place_call(CallSpec("k1", "+15555550111", {"event_title": "Meet"}, "text"))
    assert placed.external_call_id == "call-1"
    assert "secret" not in placed.response
    body = json.loads(route.calls[0].request.content)
    assert body["customer"] == {"number": "+15555550111"}
    assert body["assistantOverrides"]["variableValues"] == {"event_title": "Meet"}
    assert body["metadata"]["jarvis_idempotency_key"] == "k1"
    await provider.aclose()


@respx.mock
async def test_vapi_post_not_retried_on_5xx() -> None:
    route = respx.post("https://api.vapi.ai/call").mock(return_value=httpx.Response(502))
    provider = VapiCallProvider("vapikey", "asst", "phone", live_enabled=True)
    with pytest.raises(Exception, match="502"):
        await provider.place_call(CallSpec("k1", "+15555550111", {}, "text"))
    assert route.call_count == 1  # a possibly-placed paid call is never blindly re-sent
    await provider.aclose()


async def test_live_calls_blocked_unless_enabled() -> None:
    vapi = VapiCallProvider("k", "a", "p", live_enabled=False)
    twilio = TwilioCallProvider("AC1", "tok", "+10000000000", live_enabled=False)
    for provider in (vapi, twilio):
        with pytest.raises(LiveActionBlockedError):
            await provider.place_call(CallSpec("k", "+15555550111", {}, "t"))
        await provider.aclose()


@respx.mock
async def test_twilio_twiml_escaped() -> None:
    route = respx.post("https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json").mock(
        return_value=httpx.Response(201, json={"sid": "CA1", "status": "queued"})
    )
    provider = TwilioCallProvider("AC1", "tok", "+10000000000", live_enabled=True)
    await provider.place_call(CallSpec("k", "+15555550111", {}, "Пора выходить <script>"))
    form = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
    assert "%26lt%3Bscript%26gt%3B" in form["Twiml"]
    await provider.aclose()


def test_mask_number() -> None:
    assert mask_number("+15555550111") == "+*******0111"


# --- STT -------------------------------------------------------------------------------------------


@respx.mock
async def test_deepgram_transcript() -> None:
    route = respx.post("https://api.deepgram.com/v1/listen").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {
                    "channels": [{"alternatives": [{"transcript": "Запиши идею про inbox", "confidence": 0.93}]}]
                }
            },
        )
    )
    stt = DeepgramSTT("dgkey")
    transcript = await stt.transcribe(b"OggS...", "audio/ogg")
    assert transcript.text == "Запиши идею про inbox"
    assert route.calls[0].request.headers["authorization"] == "Token dgkey"
    assert route.calls[0].request.url.params["language"] == "ru"
    await stt.aclose()


def _unused(*_: Any) -> None:
    return None
