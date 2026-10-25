"""Incoming Telegram calls (2026-10-14): the user calls the caller account and talks to Jarvis, one phrase at a time.

Jarvis answers «Слушаю, сэр», then repeats: listen until a pause (energy VAD on the incoming PCM) → a short beep
(«heard you») → Whisper → the same bot as a typed message (`ask_jarvis`) → the reply spoken. Text replies, buttons
and approvals still land in the Telegram chat, so «удали задачу» is confirmed there by a signed button, as always.
Half-duplex: what arrives while Jarvis thinks or speaks is dropped. 30 s of silence → «До связи, сэр» and hang up;
a call lasts at most 10 minutes. Only the user's own account is answered (`TelegramDialer`).
The same conversation runs when Jarvis rings the user to talk («позвони мне, поболтаем», 2026-10-15), with `CALLING`
instead of the greeting.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import re
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

GREETING = "Слушаю, сэр."
CALLING = "Сэр, Джарвис на связи. Слушаю вас."  # Jarvis rang the user to talk («позвони мне, поболтаем»)
GOODBYE = "До связи, сэр."
NOT_HEARD = "Не расслышал, сэр. Повторите, пожалуйста."
DONE = "Готово, сэр."
HOLD = "Секунду, сэр."
TOO_SLOW = "Не успел ответить, сэр. Спросите ещё раз или напишите в чат."


@dataclass
class VadConfig:
    chunk_ms: int = 20
    start_ms: int = 160  # this much speech in a row starts a phrase
    end_ms: int = 700  # this much silence after speech ends it (900 felt slow live, 14.10)
    preroll_ms: int = 300  # kept from before the start (the first syllable is quiet)
    max_phrase_s: float = 25.0
    idle_s: float = 30.0  # no phrase started → goodbye
    max_call_s: float = 600.0
    hold_after_s: float = 3.0  # no answer yet → «Секунду, сэр» (the silence felt like a dropped line)
    max_think_s: float = 60.0  # no answer at all → said so; the call goes on (and still ends at `max_call_s`)
    min_rms: float = 500.0
    noise_factor: float = 3.0


def rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


@dataclass
class Segmenter:
    """Energy VAD on 16-bit mono PCM: `feed` chunks of any size; returns a phrase when its trailing pause ends."""

    rate: int
    cfg: VadConfig = field(default_factory=VadConfig)
    noise: float = 0.0
    _pending: bytes = b""
    _phrase: bytearray = field(default_factory=bytearray)
    _preroll: deque[bytes] = field(default_factory=deque)
    _run: int = 0  # consecutive speech chunks before the start
    _silence: int = 0
    in_phrase: bool = False

    @property
    def _chunk(self) -> int:
        return self.rate * 2 * self.cfg.chunk_ms // 1000

    def _threshold(self) -> float:
        return max(self.cfg.min_rms, self.noise * self.cfg.noise_factor)

    def feed(self, pcm: bytes) -> bytes | None:
        self._pending += pcm
        done: bytes | None = None
        while len(self._pending) >= self._chunk and done is None:
            chunk, self._pending = self._pending[: self._chunk], self._pending[self._chunk :]
            done = self._step(chunk)
        return done

    def _step(self, chunk: bytes) -> bytes | None:
        loud = rms(chunk) > self._threshold()
        ms = self.cfg.chunk_ms
        if not self.in_phrase:
            self._preroll.append(chunk)
            while len(self._preroll) * ms > self.cfg.preroll_ms + self.cfg.start_ms:
                self._preroll.popleft()
            if loud:
                self._run += 1
                if self._run * ms >= self.cfg.start_ms:
                    self.in_phrase, self._silence = True, 0
                    self._phrase = bytearray(b"".join(self._preroll))
                    self._preroll.clear()
            else:
                self._run = 0
                level = rms(chunk)
                self.noise = level if self.noise == 0 else 0.95 * self.noise + 0.05 * level
            return None
        self._phrase += chunk
        self._silence = 0 if loud else self._silence + 1
        too_long = len(self._phrase) >= self.rate * 2 * self.cfg.max_phrase_s
        if self._silence * ms >= self.cfg.end_ms or too_long:
            phrase = bytes(self._phrase)
            self.reset()
            return phrase
        return None

    def reset(self) -> None:
        self.in_phrase, self._run, self._silence = False, 0, 0
        self._phrase = bytearray()
        self._preroll.clear()
        self._pending = b""


def wav(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def beep(rate: int, *, hz: float = 880.0, seconds: float = 0.12, volume: float = 0.25) -> bytes:
    """A short soft tone: «I heard you, thinking»."""
    n = int(rate * seconds)
    samples = array.array("h", (int(32767 * volume * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))
    return samples.tobytes()


class Line(Protocol):
    """One connected call: incoming PCM chunks in `heard`, `say` plays PCM at `rate`."""

    rate: int
    heard: asyncio.Queue[bytes]
    hung_up: asyncio.Event

    async def say(self, pcm: bytes) -> None: ...

    async def hang_up(self) -> None: ...


Brain = Callable[[str], Awaitable[str]]


@dataclass
class Conversation:
    line: Line
    brain: Brain  # the transcript → what to say back
    stt: Any
    tts: Any
    cfg: VadConfig = field(default_factory=VadConfig)
    tts_model: str | None = None  # calls: the same voice on a faster model (CALL_TTS_MODEL)
    greeting: str = GREETING
    turns: list[tuple[str, str]] = field(default_factory=list)
    _voice: dict[str, bytes] = field(default_factory=dict)

    async def _pcm(self, text: str, *, cache: bool = False) -> bytes:
        if cache and text in self._voice:
            return self._voice[text]
        pcm, rate = await self.tts.synthesize_pcm(text, model=self.tts_model)
        if rate != self.line.rate:
            from jarvis.integrations.telegram.caller import fit_rate

            pcm, rate = fit_rate(pcm, rate)
            if rate != self.line.rate:
                pcm = _resample(pcm, rate, self.line.rate)
        if cache:
            self._voice[text] = pcm
        return pcm

    async def _say(self, text: str, *, cache: bool = False) -> None:
        await self.line.say(await self._pcm(text, cache=cache))
        _drain(self.line.heard)  # half-duplex: our own voice / what was said meanwhile is not a phrase

    async def run(self) -> list[tuple[str, str]]:
        started = time.monotonic()
        await self._say(self.greeting, cache=True)
        while not self.line.hung_up.is_set() and time.monotonic() - started < self.cfg.max_call_s:
            phrase = await self._listen()
            if phrase is None:
                break
            await self.line.say(beep(self.line.rate))
            try:
                heard = (await self.stt.transcribe(wav(phrase, self.line.rate), "audio/wav")).text.strip()
            except Exception as exc:  # empty transcript, provider down
                log.info("call phrase not transcribed: %s", exc)
                heard = ""
            if not heard:
                await self._say(NOT_HEARD, cache=True)
                continue
            answer = await self._think(heard, started + self.cfg.max_call_s)
            self.turns.append((heard, answer))
            if self.line.hung_up.is_set():
                break
            await self._say_sentences(answer or DONE)
        if not self.line.hung_up.is_set():
            await self._say(GOODBYE, cache=True)
            await self.line.hang_up()
        return self.turns

    async def _think(self, heard: str, until: float) -> str:
        """The answer; after `hold_after_s` of waiting the caller hears «Секунду, сэр» (once). The wait is bounded
        (M6, review 17.10: a stuck bot kept the call open past its ten minutes) and ends when the caller hangs up."""
        task = asyncio.ensure_future(self.brain(heard))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.cfg.hold_after_s)
            if not done:
                await self._say(HOLD, cache=True)
                hung_up = asyncio.ensure_future(self.line.hung_up.wait())
                limit = max(0.1, min(self.cfg.max_think_s, until - time.monotonic()))
                try:
                    waits: set[asyncio.Future[Any]] = {task, hung_up}
                    finished, _ = await asyncio.wait(waits, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    hung_up.cancel()
                if task not in finished:
                    task.cancel()
                    log.warning("call: no answer to %r in %.0f s", heard, limit)
                    return TOO_SLOW
            return await task
        except Exception:
            log.exception("call: the bot failed on %r", heard)
            return "Не получилось, сэр. Подробности в чате."

    async def _say_sentences(self, text: str) -> None:
        """The first sentence is synthesized alone and starts playing while the rest is being synthesized."""
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
        if len(parts) < 2 or len(parts[0]) > 160:
            await self._say(text)
            return
        rest = asyncio.create_task(self._pcm(" ".join(parts[1:])))
        try:
            await self.line.say(await self._pcm(parts[0]))
            await self.line.say(await rest)
        finally:
            if not rest.done():
                rest.cancel()
        _drain(self.line.heard)

    async def _listen(self) -> bytes | None:
        """The next phrase, or None after `idle_s` without one (or when the user hangs up)."""
        segmenter = Segmenter(self.line.rate, self.cfg)
        deadline = time.monotonic() + self.cfg.idle_s
        while not self.line.hung_up.is_set():
            if not segmenter.in_phrase and time.monotonic() > deadline:
                return None
            try:
                chunk = await asyncio.wait_for(self.line.heard.get(), timeout=0.5)
            except TimeoutError:
                continue
            if (phrase := segmenter.feed(chunk)) is not None:
                return phrase
        return None


def _drain(queue: asyncio.Queue[bytes]) -> None:
    while not queue.empty():
        queue.get_nowait()


def _resample(pcm: bytes, src: int, dst: int) -> bytes:
    """16-bit mono PCM src → dst Hz by linear interpolation (24 → 48 kHz for the call line)."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples or src == dst:
        return pcm
    n = int(len(samples) * dst / src)
    out = array.array("h", bytes(2 * n))
    last = len(samples) - 1
    step = src / dst
    for i in range(n):
        x = i * step
        j = int(x)
        a = samples[j]
        b = samples[j + 1] if j < last else a
        out[i] = int(a + (b - a) * (x - j))
    return out.tobytes()


# A pending button is pressed only by a short answer made of these words alone (R35): «удали задачу Б» while
# «Удалить задачу А?» waits is a new request, not a «да» — a first-word match once confirmed A.
_YES_WORDS = frozenset((
    "да", "ага", "угу", "давай", "конечно", "подтверждаю", "подтверди", "верно", "точно", "ок", "окей", "хорошо",
    "записывай", "запиши", "удаляй", "удали", "запускай", "запусти", "делай", "сделай", "применяй", "примени",
    "согласен", "можно",
))  # fmt: skip
_NO_WORDS = frozenset(("нет", "не", "надо", "нужно", "отмена", "отмени", "отменяй", "стоп", "оставь", "оставляй"))
_FILLER = frozenset(
    ("сэр", "джарвис", "пожалуйста", "так", "это", "ее", "его", "их", "все", "всё", "уж", "ну", "же", "вот")
)
_PICK_WORDS = frozenset(("давай", "вариант", "пункт", "номер", "задачу", "задача"))
MAX_ANSWER_WORDS = 5
_NEGATIVE_BUTTON = re.compile(r"✖|❌|отмен|нет\b|оставить|не надо|позже", re.I)
_ORDINALS = {
    "первый": 0, "первую": 0, "первое": 0, "один": 0, "1": 0,
    "второй": 1, "вторую": 1, "второе": 1, "два": 1, "2": 1,
    "третий": 2, "третью": 2, "третье": 2, "три": 2, "3": 2,
    "четвёртый": 3, "четвертый": 3, "четвёртую": 3, "четвертую": 3, "четыре": 3, "4": 3,
    "пятый": 4, "пятую": 4, "пять": 4, "5": 4,
}  # fmt: skip
_HINTS = re.compile(r"<i>.*?</i>|<blockquote[^>]*>.*?</blockquote>", re.S)
MAX_SPOKEN = 320  # characters read aloud per answer; the rest is in the chat summary after the call


@dataclass
class Pending:
    """Buttons of the last answer, to be pressed by voice: «да» / «нет» or a number."""

    message_id: int
    yes: str | None
    no: str | None
    options: list[tuple[str, str]]  # (label, callback data)


def spoken_reply(html_texts: list[str], max_chars: int = MAX_SPOKEN) -> str:
    """The answer as it is said on a call: hints, examples and folded details dropped, the first lines only."""
    from jarvis.bot.fmt import strip_tags
    from jarvis.bot.speech import to_speech

    lines: list[str] = []
    for html in html_texts:
        text = strip_tags(_HINTS.sub("", html))
        lines += [line.strip() for line in text.splitlines() if line.strip()]
    said = ""
    for line in lines:
        if said and len(said) + len(line) > max_chars:
            break
        said = f"{said}. {line}" if said and not said.endswith((".", "!", "?", ":")) else f"{said} {line}".strip()
    return to_speech(said, max_chars + 80) or ""


def pending_from(message: dict[str, Any]) -> Pending | None:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    buttons = [(b["text"], b["callback_data"]) for row in rows for b in row if "callback_data" in b]
    if not buttons:
        return None
    negative = [b for b in buttons if _NEGATIVE_BUTTON.search(b[0])]
    positive = [b for b in buttons if b not in negative]
    if len(positive) == 1:
        return Pending(int(message["message_id"]), positive[0][1], negative[0][1] if negative else None, [])
    return Pending(int(message["message_id"]), None, negative[0][1] if negative else None, positive[:5])


def choose(pending: Pending, said: str) -> str | None:
    """The callback data the user picked by voice, or None (then the phrase is a new request)."""
    words = re.findall(r"[а-яa-z0-9]+", said.lower().replace("ё", "е"))
    if not words or len(words) > MAX_ANSWER_WORDS:
        return None
    rest = [w for w in words if w not in _FILLER]
    if pending.yes and rest and all(w in _YES_WORDS for w in rest):
        return pending.yes
    if rest and rest[0] in _NO_WORDS and all(w in _NO_WORDS or w in _YES_WORDS for w in rest):
        return pending.no or ""  # «нет», «не надо», «нет, не удаляй»
    picks = [w for w in rest if w not in _PICK_WORDS]
    if len(picks) == 1:
        index = _ORDINALS.get(picks[0], _ORDINALS.get(picks[0].replace("е", "ё"), -1))
        if 0 <= index < len(pending.options):
            return pending.options[index][1]
    return None


class PhoneChannel:
    """The call as another chat with Jarvis, like the Mac app: the bot's replies are captured (nothing pings the
    phone during the call), buttons are pressed by voice (the same signed callbacks, verified by the bot), and the
    spoken answer is short. After the call one silent summary goes to the chat, with any unanswered buttons."""

    def __init__(self, c: Any) -> None:
        self.c = c
        self.pending: Pending | None = None
        self.unanswered: dict[str, Any] | None = None  # the last message with buttons nobody pressed
        self.log: list[tuple[str, str]] = []

    def _bot(self) -> tuple[Any, Any, int]:
        from typing import cast

        from jarvis.bot.service import BotService
        from jarvis.desktop.channel import _ID_BLOCKS, CaptureTelegram, _ChannelContainer
        from jarvis.services.container import Container

        capture = CaptureTelegram(signer=self.c.callback_signer)
        capture.desktop = False  # «открой сайт …» from the phone is not opened on the Mac
        capture._message_id = next(_ID_BLOCKS) * 1000
        bot = BotService(cast(Container, _ChannelContainer(self.c, capture)))
        return bot, capture, int(self.c.settings.telegram_allowed_user_ids[0])

    async def ask(self, text: str) -> str:
        from jarvis.bot.intents import VOICE_CALL
        from jarvis.core.errors import JarvisError

        if self.pending is not None and (data := choose(self.pending, text)) is not None:
            pending, self.pending, self.unanswered = self.pending, None, None
            if not data:
                answer = "Отменил, сэр."
                self.log.append((text, answer))
                return answer
            return await self._tap(text, data, pending.message_id)
        self.pending = None
        bot, capture, chat_id = self._bot()
        chat = {"id": chat_id, "type": "private"}
        message = {"message_id": None, "text": text[:4000], "from": {"id": chat_id}, "chat": chat}
        token = VOICE_CALL.set(True)
        try:
            await bot._handle_message(message)
        except JarvisError as exc:
            await bot.reply(chat_id, bot.persona.sorry(f"⚠️ {bot._friendly(exc)}"))
        finally:
            VOICE_CALL.reset(token)
        return self._answer(text, capture)

    async def _tap(self, said: str, data: str, message_id: int) -> str:
        from jarvis.core.errors import JarvisError

        bot, capture, chat_id = self._bot()
        callback = {
            "id": "phone",
            "data": data[:200],
            "from": {"id": chat_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}},
        }
        try:
            await bot.handle_callback(callback)
        except JarvisError as exc:
            await bot.reply(chat_id, bot.persona.sorry(f"⚠️ {bot._friendly(exc)}"))
        toast = capture.answered[-1][1] if capture.answered else ""
        answer = self._answer(said, capture, edits=True) or toast or "Готово, сэр."
        return answer

    def _answer(self, said: str, capture: Any, *, edits: bool = False) -> str:
        texts = [str(m.get("text") or "") for m in capture.sent]
        if edits:
            texts = [str(e.get("text") or "") for e in capture.edits] + texts
        answer = spoken_reply(texts)
        keyboards = [m for m in capture.sent if (m.get("reply_markup") or {}).get("inline_keyboard")]
        with_buttons = keyboards[-1] if keyboards else None
        if with_buttons is not None and (pending := pending_from(with_buttons)) is not None:
            self.pending, self.unanswered = pending, with_buttons
            if pending.yes:
                answer = f"{answer} Подтвердить?".strip()
            elif pending.options:
                spoken = ", ".join(f"{i + 1} — {label}" for i, (label, _) in enumerate(pending.options))
                answer = f"{answer} Варианты: {spoken}. Скажите номер.".strip()
        self.log.append((said, answer))
        return answer

    async def summary(self, minutes: int) -> None:
        """One silent message after the call: what was said and answered; buttons nobody pressed come back."""
        from jarvis.bot import fmt

        if not self.log:
            return
        chat_id = int(self.c.settings.telegram_allowed_user_ids[0])
        items = [fmt.bullet(f"«{heard}»", answer[:200] or "—") for heard, answer in self.log[-12:]]
        text = fmt.lines(fmt.title("📞", "Разговор с Джарвисом", f"{minutes} мин"), items)
        await self.c.telegram.send_message(chat_id, text, parse_mode="HTML", silent=True)
        if self.unanswered is not None:  # its buttons were signed when captured: unsigned here, the adapter signs again
            await self.c.telegram.send_message(
                chat_id,
                str(self.unanswered.get("text") or ""),
                reply_markup=self._unsigned(self.unanswered.get("reply_markup")),
                parse_mode=self.unanswered.get("parse_mode"),
                silent=True,
            )

    def _unsigned(self, markup: dict[str, Any] | None) -> dict[str, Any] | None:
        if not markup:
            return None
        rows = []
        for row in markup.get("inline_keyboard") or []:
            kept = []
            for button in row:
                data = button.get("callback_data")
                original = self.c.callback_signer.verify(data) if data else None
                if data and original is None:
                    continue
                kept.append({**button, "callback_data": original} if data else dict(button))
            if kept:
                rows.append(kept)
        return {"inline_keyboard": rows} if rows else None
