"""Speech-to-text adapters (vendor-neutral protocol)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jarvis.core.errors import NotConfiguredError, PermanentError
from jarvis.core.http import ResilientHttp


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    provider: str
    confidence: float | None = None


class SpeechToText(Protocol):
    name: str

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Transcript: ...


class DeepgramSTT:
    name = "deepgram"

    def __init__(self, api_key: str, model: str = "nova-3", *, transport: httpx.AsyncBaseTransport | None = None):
        self._model = model
        self._http = ResilientHttp(
            "deepgram",
            base_url="https://api.deepgram.com/v1/",
            timeout=60.0,
            headers={"Authorization": f"Token {api_key}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Transcript:
        params = {"model": self._model, "language": language, "smart_format": "true", "punctuate": "true"}
        response = await self._http.post(
            "listen",
            params=params,
            content=audio,
            headers={"Content-Type": mime_type or "audio/ogg"},
            retry_non_idempotent=True,
        )  # transcription has no side effects
        data = response.json()
        try:
            alt = data["results"]["channels"][0]["alternatives"][0]
        except (KeyError, IndexError) as exc:
            raise PermanentError("deepgram: unexpected response shape") from exc
        text = str(alt.get("transcript") or "").strip()
        if not text:
            raise PermanentError("deepgram: empty transcript")
        return Transcript(text=text, language=language, provider=self.name, confidence=alt.get("confidence"))


class OpenRouterSTT:
    """Whisper (or another transcription model) behind the OpenRouter key; accepts Telegram's OGG/Opus notes."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = "openai/whisper-large-v3-turbo",
        *,
        budget: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._budget = budget
        self._http = ResilientHttp(
            "openrouter-stt",
            base_url="https://openrouter.ai/api/v1/",
            timeout=90.0,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "JARVIS"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Transcript:
        if self._budget is not None:
            await self._budget()
        mime = mime_type or "audio/ogg"
        extension = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/mp4": "m4a"}.get(mime, "ogg")
        response = await self._http.post(
            "audio/transcriptions",
            data={"model": self.model, "language": language},
            files={"file": (f"voice.{extension}", audio, mime)},
            retry_non_idempotent=True,  # transcription has no side effects
        )
        text = str(response.json().get("text") or "").strip()
        if not text:
            raise PermanentError("openrouter-stt: empty transcript")
        return Transcript(text=text, language=language, provider=self.name)


@dataclass
class FakeSTT:
    name: str = "fake"
    transcripts: dict[bytes, str] = field(default_factory=dict)
    default: str = "тестовая расшифровка"

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Transcript:
        return Transcript(self.transcripts.get(audio, self.default), language, self.name, 1.0)


class UnavailableSTT:
    name = "unavailable"

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Transcript:
        raise NotConfiguredError("speech-to-text is not configured (set OPENROUTER_API_KEY or DEEPGRAM_API_KEY)")
