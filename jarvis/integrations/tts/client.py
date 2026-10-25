"""Text-to-speech adapters (vendor-neutral protocol).

OpenRouter's `/audio/speech` endpoint gives many TTS models behind the key Jarvis already uses for the LLM.
Telegram shows MP3 (and OGG/Opus, M4A) as a voice message, so MP3 is requested directly; models that only
return raw PCM (Gemini TTS) are encoded to MP3 locally (LAME via `lameenc`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from jarvis.core.errors import NotConfiguredError, PermanentError, TransientError
from jarvis.core.http import ResilientHttp

OPENROUTER_URL = "https://openrouter.ai/api/v1/"
MP3 = "audio/mpeg"

# Voices known to work in Russian (checked live 2026-09-23); other models accept their own names.
GEMINI_VOICES: tuple[str, ...] = (
    "Charon",
    "Iapetus",
    "Orus",
    "Algenib",
    "Fenrir",
    "Puck",
    "Kore",
    "Aoede",
    "Zephyr",
    "Sadaltager",
)
KNOWN_VOICES: dict[str, tuple[str, ...]] = {
    "x-ai/grok-voice-tts-1.0": ("Rex", "Leo", "Sal", "Eve", "Ara"),
    "google/gemini-3.1-flash-tts-preview": GEMINI_VOICES,
    "google/gemini-3.8-flash-tts": GEMINI_VOICES,  # the user's pick 14.10: Charon, «AI butler» style
    "google/gemini-3.8-flash-lite-tts": GEMINI_VOICES,  # the same voices, faster (calls)
}

BudgetHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class SpeechAudio:
    data: bytes
    mime_type: str
    filename: str


class TextToSpeech(Protocol):
    name: str
    model: str
    voice: str

    async def synthesize(self, text: str, *, voice: str | None = None) -> SpeechAudio: ...

    async def synthesize_pcm(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ) -> tuple[bytes, int]:
        """16-bit little-endian mono PCM and its sample rate (for Telegram calls); `model` overrides the default."""
        ...


def pcm_to_mp3(pcm: bytes, *, sample_rate: int = 24000, channels: int = 1, bitrate_kbps: int = 64) -> bytes:
    """16-bit little-endian PCM → MP3 (speech quality)."""
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate_kbps)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(channels)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm)) + bytes(encoder.flush())


CHUNK_TARGET = 60  # characters: a Gemini TTS request takes ~1.4 s + half the audio's length (measured 16.10)
CHUNK_MIN = 20  # shorter pieces join a neighbour — a phrase cut too finely loses its intonation
SENTENCE_PAUSE = 0.25  # seconds of silence between stitched pieces
CLAUSE_PAUSE = 0.12
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE = re.compile(r"(?<=[,;:])\s+|\s+(?=[—–]\s)")


def speech_chunks(text: str) -> list[tuple[str, float]]:
    """Text → (piece, pause after it) for parallel synthesis (16.10: «Charon is great but slow» — six seconds for
    three sentences; in parallel the wait is the longest piece). Sentences first; a long sentence is cut at
    commas / dashes / semicolons into pieces of about `CHUNK_TARGET` characters, never shorter than `CHUNK_MIN`."""
    out: list[tuple[str, float]] = []
    for sentence in (x.strip() for x in _SENTENCE_END.split(text.strip())):
        if not sentence:
            continue
        clauses = [c.strip() for c in _CLAUSE.split(sentence) if c.strip()] if len(sentence) > CHUNK_TARGET else []
        pieces: list[str] = []
        for clause in clauses or [sentence]:
            if pieces and (
                len(pieces[-1]) < CHUNK_MIN or len(clause) < CHUNK_MIN or len(pieces[-1]) + len(clause) < CHUNK_TARGET
            ):
                pieces[-1] = f"{pieces[-1]} {clause}"
            else:
                pieces.append(clause)
        out += [(piece, CLAUSE_PAUSE) for piece in pieces[:-1]] + [(pieces[-1], SENTENCE_PAUSE)]
    return out


def stitch(parts: list[tuple[bytes, int]], pauses: list[float]) -> tuple[bytes, int]:
    """PCM pieces of one rate → one PCM with short pauses between them."""
    rate = parts[0][1]
    if any(r != rate for _, r in parts):
        raise PermanentError("openrouter-tts: pieces came back at different sample rates")
    out = bytearray()
    for i, (pcm, _) in enumerate(parts):
        out += pcm
        if i < len(parts) - 1:
            out += b"\x00\x00" * int(rate * pauses[i])
    return bytes(out), rate


def _pcm_rate(content_type: str) -> int:
    match = re.search(r"rate=(\d+)", content_type)
    return int(match.group(1)) if match else 24000


def pcm_payload(data: bytes, content_type: str) -> tuple[bytes, int]:
    """A «pcm» answer → (16-bit mono PCM, sample rate): raw PCM (the rate from the content type, else 24 kHz) or a
    WAV container. MP3 is refused — the caller has no decoder."""
    if data[:4] == b"RIFF":
        import io
        import wave

        with wave.open(io.BytesIO(data)) as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                raise PermanentError("openrouter-tts: WAV is not 16-bit mono")
            return wav.readframes(wav.getnframes()), wav.getframerate()
    if content_type.startswith(("audio/mpeg", "audio/mp3")) or data[:3] == b"ID3":
        raise PermanentError("openrouter-tts: expected PCM, got MP3")
    return data, _pcm_rate(content_type)


def returns_pcm_only(model: str) -> bool:
    return model.startswith("google/gemini")


class OpenRouterTTS:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        *,
        style: str = "",
        budget: BudgetHook | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.voice = voice
        self.style = style.strip()
        self._budget = budget
        self._http = ResilientHttp(
            "openrouter-tts",
            base_url=OPENROUTER_URL,
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "JARVIS"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def synthesize(self, text: str, *, voice: str | None = None) -> SpeechAudio:
        if self._budget is not None:
            await self._budget()
        pcm = returns_pcm_only(self.model)
        chunks = speech_chunks(text) if pcm else []
        if len(chunks) > 1:  # Gemini: pieces in parallel, stitched — the wait is the longest piece, not the sum
            parts = await asyncio.gather(*(self._pcm(piece, voice, self.model) for piece, _ in chunks))
            audio, rate = stitch(list(parts), [pause for _, pause in chunks])
            return SpeechAudio(pcm_to_mp3(audio, sample_rate=rate), MP3, "jarvis.mp3")
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": voice or self.voice,
            "response_format": "pcm" if pcm else "mp3",
        }
        if self.style and pcm:  # Gemini takes the manner as `instructions` (written into the text it may be read aloud)
            body["instructions"] = self.style
        # Generation has no side effects (only cost), so a timeout may be retried.
        response = await self._http.post("audio/speech", json=body, retry_non_idempotent=True)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            raise PermanentError(f"openrouter-tts: {response.text[:200]}")
        if not response.content:
            raise TransientError("openrouter-tts: empty audio")
        data = response.content
        if pcm or content_type.startswith("audio/pcm"):
            data = pcm_to_mp3(data, sample_rate=_pcm_rate(content_type))
        return SpeechAudio(data, MP3, "jarvis.mp3")

    async def synthesize_pcm(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ) -> tuple[bytes, int]:
        if self._budget is not None:
            await self._budget()
        return await self._pcm(text, voice, model or self.model)

    async def _pcm(self, text: str, voice: str | None, used: str) -> tuple[bytes, int]:
        body: dict[str, Any] = {"model": used, "input": text, "voice": voice or self.voice, "response_format": "pcm"}
        if self.style and returns_pcm_only(used):
            body["instructions"] = self.style
        response = await self._http.post("audio/speech", json=body, retry_non_idempotent=True)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            raise PermanentError(f"openrouter-tts: {response.text[:200]}")
        if not response.content:
            raise TransientError("openrouter-tts: empty audio")
        return pcm_payload(response.content, content_type)

    async def probe(self) -> dict[str, Any]:
        """The configured model is listed as a speech model (no generation, no cost)."""
        listing = (await self._http.get("models", params={"output_modalities": "speech"})).json().get("data") or []
        if self.model not in {m.get("id") for m in listing}:
            raise PermanentError(f"TTS model {self.model!r} is not an OpenRouter speech model")
        return {"id": f"{self.model} ({self.voice})"}


@dataclass
class FakeTTS:
    name: str = "fake"
    model: str = "fake-tts"
    voice: str = "Rex"
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_with: Exception | None = None

    async def synthesize(self, text: str, *, voice: str | None = None) -> SpeechAudio:
        if self.fail_with is not None:
            raise self.fail_with
        used = voice or self.voice
        self.calls.append((text, used))
        return SpeechAudio(b"ID3fake-" + used.encode(), MP3, "jarvis.mp3")

    async def synthesize_pcm(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ) -> tuple[bytes, int]:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((text, voice or self.voice))
        return b"\x00\x00" * 2400, 24000  # 0.1 s of silence


class UnavailableTTS:
    name = "unavailable"
    model = "none"
    voice = ""

    async def synthesize(self, text: str, *, voice: str | None = None) -> SpeechAudio:
        raise NotConfiguredError("text-to-speech is not configured (set OPENROUTER_API_KEY)")

    async def synthesize_pcm(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ) -> tuple[bytes, int]:
        raise NotConfiguredError("text-to-speech is not configured (set OPENROUTER_API_KEY)")
