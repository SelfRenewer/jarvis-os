"""The real dialer of Telegram calls: a second Telegram account (Telethon) + py-tgcalls (ntgcalls, WebRTC).

Only used by the `caller` service. It rings one person — the user's own account (the first
`TELEGRAM_ALLOWED_USER_IDS`) — and never reads or sends messages: the session is a full-access key to that account,
so nothing else is done with it. Sound is pushed as raw 16-bit mono PCM frames (no ffmpeg). py-tgcalls' update
check against GitHub is switched off. Versions are pinned (telethon 1.45.0, py-tgcalls 3.0.0, ntgcalls 3.0.3):
ntgcalls follows Telegram's call protocol and its updates sometimes break the API.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jarvis.core.errors import AuthError

log = logging.getLogger(__name__)

RATES = (24000, 48000)  # what py-tgcalls takes for mono audio (AudioQuality LOW / HIGH)
LINE_RATE = 48000  # incoming calls: Telegram's full-band rate (24 kHz sounded muffled live, 14.10)
LEAD_IN = 1.5  # seconds of silence after the answer: the phone reaches the ear before the first word
GAP = 1.5  # the message is said twice (live 14.10: once was over before the user was listening)
TAIL = 2.0  # then a pause before hanging up


def fit_rate(pcm: bytes, rate: int) -> tuple[bytes, int]:
    """16-bit mono PCM at any rate → 24 or 48 kHz (linear interpolation; speech only)."""
    if rate in RATES:
        return pcm, rate
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    if not src:
        return b"", 48000
    target = 48000
    n = int(len(src) * target / rate)
    out = array.array("h", bytes(2 * n))
    step = rate / target
    for i in range(n):
        x = i * step
        j = int(x)
        a = src[j]
        b = src[j + 1] if j + 1 < len(src) else a
        out[i] = int(a + (b - a) * (x - j))
    return out.tobytes(), target


def silence(seconds: float, rate: int) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def spoken(pcm: bytes, rate: int) -> bytes:
    """What the user hears after answering: a pause, the message, a pause, the message again, a pause."""
    return silence(LEAD_IN, rate) + pcm + silence(GAP, rate) + pcm + silence(TAIL, rate)


class TelegramLine:
    """A connected call (incoming, or Jarvis's call to talk) as `jarvis.calls.conversation.Line`."""

    def __init__(self, dialer: TelegramDialer, rate: int) -> None:
        self.rate = rate
        self.heard: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3000)  # ~30 s of 10 ms frames
        self.hung_up = asyncio.Event()
        self._dialer = dialer

    async def say(self, pcm: bytes) -> None:
        from pytgcalls.exceptions import NotInCallError

        if self.hung_up.is_set():
            return
        try:
            await self._dialer._stream(pcm, self.rate)
        except NotInCallError:
            self.hung_up.set()

    async def hang_up(self) -> None:
        self.hung_up.set()
        with contextlib.suppress(Exception):
            await self._dialer._calls.leave_call(self._dialer._target)


class TelegramDialer:
    def __init__(self, api_id: int, api_hash: str, session: str, target_user_id: int) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._target = target_user_id
        self._client: Any = None
        self._calls: Any = None
        self.busy = False  # one call at a time: an outgoing call or a conversation
        self.on_incoming: Callable[[TelegramLine], Awaitable[None]] | None = None
        self.on_talk: Callable[[TelegramLine], Awaitable[None]] | None = None  # Jarvis rang the user to talk
        self._line: TelegramLine | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        from pytgcalls import PyTgCalls
        from pytgcalls.pytgcalls_session import PyTgCallsSession
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        PyTgCallsSession.notice_displayed = True  # no "update available" request to GitHub on start
        self._client = TelegramClient(
            StringSession(self._session),
            self._api_id,
            self._api_hash,
            device_model="Jarvis caller",
            system_version="Docker",
            app_version="jarvis",
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise AuthError("telegram caller: the session is not authorized — run `jarvis caller-login`")
        # The user must be among this account's chats or contacts: ids resolve only from the entity cache.
        await self._client.get_dialogs(limit=200)
        try:
            await self._client.get_input_entity(self._target)
        except ValueError as exc:
            raise AuthError(
                "telegram caller: your account is not in the caller's chats — write it one message"
            ) from exc
        self._log_phone_calls()
        self._calls = PyTgCalls(self._client)
        self._tolerate_accept_resend()
        self._handlers()
        await self._calls.start()

    def _log_phone_calls(self) -> None:
        """Every call state change with its discard reason (R32: calls ended a second after ringing, and py-tgcalls
        only says «declined» — Hangup = a device of the user's account hung up, Busy, Missed, Disconnect)."""
        from telethon import events
        from telethon.tl.types import UpdatePhoneCall

        @self._client.on(events.Raw(UpdatePhoneCall))
        async def _phone_call(update: Any) -> None:
            call = update.phone_call
            reason = getattr(call, "reason", None)
            log.info(
                "phone call %s: %s%s",
                getattr(call, "id", "?"),
                type(call).__name__,
                f" ({type(reason).__name__})" if reason is not None else "",
            )

    def _tolerate_accept_resend(self) -> None:
        """R32 (live 15.10): an incoming call dropped at once — `phone.acceptCall` answered CALL_ALREADY_ACCEPTED, the
        answer failed and Jarvis hung up a call the server had accepted. Telethon re-sends a request after a reconnect
        or a clock correction (the Docker VM clock jumps after the Mac sleeps), so the second copy meets the first's
        success. Accepted is what we asked for: py-tgcalls goes on to wait for the confirmation (≤ 30 s)."""
        from telethon.errors.rpcerrorlist import CallAlreadyAcceptedError

        app = self._calls._app
        accept = app.accept_call

        async def accept_once(*args: Any, **kwargs: Any) -> Any:
            try:
                return await accept(*args, **kwargs)
            except CallAlreadyAcceptedError:
                log.warning("incoming call: accept answered «already accepted» — waiting for the confirmation")
                return None

        app.accept_call = accept_once

    def _handlers(self) -> None:
        """Incoming calls: only the user's own account is answered; frames of the user's voice feed the line."""
        from pytgcalls import filters
        from pytgcalls.types import ChatUpdate, Device, Direction

        @self._calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _incoming(_: Any, update: Any) -> None:
            await self._on_incoming(int(update.chat_id))

        @self._calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _ended(_: Any, update: Any) -> None:
            if self._line is not None and int(update.chat_id) == self._target:
                self._line.hung_up.set()

        @self._calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def _heard(_: Any, update: Any) -> None:
            line = self._line
            if line is None or int(update.chat_id) != self._target:
                return
            for frame in update.frames:
                with contextlib.suppress(asyncio.QueueFull):  # a stalled consumer drops audio, never memory
                    line.heard.put_nowait(frame.frame)

    async def _on_incoming(self, chat_id: int) -> None:
        if chat_id == self._target and self.busy:
            # Never leave_call here: it is the same chat id as the call being answered or talked on, so «declining»
            # a repeated update hung up the live call (R32).
            log.info("incoming call: a repeated update while busy — ignored")
            return
        if chat_id != self._target or self.on_incoming is None:
            log.info("incoming call declined (not the owner)")
            with contextlib.suppress(Exception):
                await self._calls.leave_call(chat_id)
            return
        log.info("incoming call from the owner: answering")
        self.busy = True
        task = asyncio.create_task(self._answer())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self) -> None:
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream
        from pytgcalls.types.raw import AudioParameters

        params = AudioParameters(bitrate=LINE_RATE, channels=1)
        try:
            await self._calls.play(self._target, MediaStream(ExternalMedia.AUDIO, params), CallConfig(timeout=30))
            log.info("incoming call connected")
            assert self.on_incoming is not None
            await self._converse(self.on_incoming)
        except Exception:
            log.exception("incoming call failed")
        finally:
            self._line = None
            with contextlib.suppress(Exception):
                await self._calls.leave_call(self._target)
            log.info("incoming call ended")
            self.busy = False

    async def _converse(self, talk: Callable[[TelegramLine], Awaitable[None]]) -> None:
        """A connected call: hear the user (frames → `_heard`) and let the conversation run until it ends."""
        from pytgcalls.types import RecordStream
        from pytgcalls.types.raw import AudioParameters

        await self._calls.record(self._target, RecordStream(True, AudioParameters(bitrate=LINE_RATE, channels=1)))
        self._line = TelegramLine(self, LINE_RATE)
        try:
            await talk(self._line)
        finally:
            self._line = None

    async def dial(self, pcm: bytes, sample_rate: int, *, ring_seconds: int, talk: bool = False) -> str:
        """Ring the user; once answered, play the message twice — or, with `talk`, start a conversation."""
        from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, NotInCallError, TimedOutAnswer
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream
        from pytgcalls.types.raw import AudioParameters

        if talk and self.on_talk is None:
            raise RuntimeError("telegram caller: no conversation for a call to talk")
        pcm, rate = (b"", LINE_RATE) if talk else fit_rate(pcm, sample_rate)
        stream = MediaStream(ExternalMedia.AUDIO, AudioParameters(bitrate=rate, channels=1))
        if self.busy:
            return "busy"  # the user is talking to Jarvis right now
        self.busy = True
        try:
            try:  # returns once the user answered and the call connected
                await self._calls.play(self._target, stream, CallConfig(timeout=ring_seconds))
            except TimedOutAnswer:
                log.info("outgoing call: no answer")
                return "no_answer"
            except CallBusy:
                log.info("outgoing call: busy")
                return "busy"
            except (CallDeclined, CallDiscarded) as exc:
                log.info("outgoing call: %s", type(exc).__name__)
                return "declined"
            log.info("outgoing call answered (%s)", "talk" if talk else "message")
            try:
                if talk:
                    assert self.on_talk is not None
                    await self._converse(self.on_talk)
                else:
                    await self._stream(spoken(pcm, rate), rate)
            except NotInCallError:
                pass  # hung up while listening: still answered
            finally:
                with contextlib.suppress(Exception):
                    await self._calls.leave_call(self._target)
            return "completed"
        finally:
            self.busy = False

    async def _stream(self, pcm: bytes, rate: int) -> None:
        """10 ms frames in real time (a monotonic schedule, so delays do not add up)."""
        from pytgcalls.types import Device

        frame = rate * 2 // 100
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i, offset in enumerate(range(0, len(pcm), frame)):
            chunk = pcm[offset : offset + frame]
            if len(chunk) < frame:
                chunk += b"\x00" * (frame - len(chunk))
            await self._calls.send_frame(self._target, Device.MICROPHONE, chunk)
            delay = start + (i + 1) * 0.01 - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()


async def login(api_id: int, api_hash: str) -> str:
    """Interactive first login (phone, code from Telegram, cloud password) in the user's terminal → session string."""
    import getpass

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash, device_model="Jarvis caller", app_version="jarvis")
    await client.start(
        phone=lambda: input("Номер аккаунта-звонаря (+…): ").strip(),
        code_callback=lambda: input("Код из Telegram: ").strip(),
        password=lambda: getpass.getpass("Облачный пароль (не отображается): "),
    )
    me = await client.get_me()
    session = str(client.session.save())
    await client.disconnect()
    log.info("caller login ok: user id %s", getattr(me, "id", "?"))
    return session
