"""Outbound call providers: Vapi (primary), Twilio TwiML (minimal fallback), Fake (tests).

Providers never decide *when* to call — the durable Jarvis scheduler does. Live providers refuse to
dial unless the runtime explicitly enables live calls.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from jarvis.core.errors import LiveActionBlockedError, PermanentError
from jarvis.core.http import ResilientHttp


@dataclass(frozen=True)
class CallSpec:
    idempotency_key: str
    to_number: str
    variables: dict[str, str]
    spoken_text: str  # used by simple TTS providers (Twilio fallback)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CallPlaced:
    provider: str
    external_call_id: str
    status: str
    response: dict[str, Any]


class CallProvider(Protocol):
    name: str

    async def place_call(self, spec: CallSpec) -> CallPlaced: ...


def mask_number(number: str) -> str:
    digits = [c for c in number if c.isdigit()]
    if len(digits) < 4:
        return "***"
    return "+" + "*" * (len(digits) - 4) + "".join(digits[-4:])


def _validate_e164(number: str) -> None:
    if not number.startswith("+") or not number[1:].isdigit() or not 8 <= len(number) <= 16:
        raise PermanentError("phone number must be E.164, e.g. +15555550123")


class VapiCallProvider:
    name = "vapi"

    def __init__(
        self,
        api_key: str,
        assistant_id: str,
        phone_number_id: str,
        *,
        live_enabled: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._assistant_id = assistant_id
        self._phone_number_id = phone_number_id
        self._live = live_enabled
        self._http = ResilientHttp(
            "vapi",
            base_url="https://api.vapi.ai/",
            timeout=20.0,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def place_call(self, spec: CallSpec) -> CallPlaced:
        if not self._live:
            raise LiveActionBlockedError("live phone calls are disabled (VOICE_CALLS_ENABLED=false or test mode)")
        _validate_e164(spec.to_number)
        body = {
            "assistantId": self._assistant_id,
            "phoneNumberId": self._phone_number_id,
            "customer": {"number": spec.to_number},
            "assistantOverrides": {"variableValues": spec.variables},
            "metadata": {**spec.metadata, "jarvis_idempotency_key": spec.idempotency_key},
        }
        # POST is NOT retried after the request may have been sent (non-idempotent, billable).
        data = (await self._http.post("call", json=body)).json()
        return CallPlaced(
            "vapi",
            str(data["id"]),
            str(data.get("status", "queued")),
            {"id": data.get("id"), "status": data.get("status")},
        )

    async def get_assistant(self) -> dict[str, Any]:
        return dict((await self._http.get(f"assistant/{self._assistant_id}")).json())


class TwilioCallProvider:
    """Minimal fallback: plays a spoken sentence (no dialogue)."""

    name = "twilio"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        live_enabled: bool,
        status_callback_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sid = account_sid
        self._from = from_number
        self._live = live_enabled
        self._callback = status_callback_url
        self._http = ResilientHttp(
            "twilio",
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/",
            timeout=20.0,
            transport=transport,
        )
        self._auth = (account_sid, auth_token)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def place_call(self, spec: CallSpec) -> CallPlaced:
        if not self._live:
            raise LiveActionBlockedError("live phone calls are disabled (VOICE_CALLS_ENABLED=false or test mode)")
        _validate_e164(spec.to_number)
        twiml = f'<Response><Say language="ru-RU">{html.escape(spec.spoken_text[:500])}</Say></Response>'
        form = {"To": spec.to_number, "From": self._from, "Twiml": twiml}
        if self._callback:
            form["StatusCallback"] = self._callback
        data = (await self._http.post("Calls.json", data=form, auth=self._auth)).json()
        return CallPlaced(
            "twilio",
            str(data["sid"]),
            str(data.get("status", "queued")),
            {"sid": data.get("sid"), "status": data.get("status")},
        )

    async def get_account(self) -> dict[str, Any]:
        response = await self._http.get(f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}.json", auth=self._auth)
        return {"status": response.json().get("status")}


@dataclass
class FakeCallProvider:
    name: str = "fake"
    placed: list[CallSpec] = field(default_factory=list)
    fail_with: Exception | None = None

    async def place_call(self, spec: CallSpec) -> CallPlaced:
        if self.fail_with is not None:
            raise self.fail_with
        self.placed.append(spec)
        return CallPlaced(self.name, f"fake-call-{len(self.placed)}", "queued", {"status": "queued"})


class DisabledCallProvider:
    name = "disabled"

    async def place_call(self, spec: CallSpec) -> CallPlaced:
        raise LiveActionBlockedError("no voice provider configured")
