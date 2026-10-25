"""Telegram call audio: PCM resampled to a rate py-tgcalls takes; the message is said twice with pauses."""

from __future__ import annotations

import array

from jarvis.integrations.telegram.caller import GAP, LEAD_IN, TAIL, fit_rate, silence, spoken


def test_supported_rates_pass_and_others_become_48k() -> None:
    pcm = array.array("h", [0, 1000, -1000, 500] * 2205).tobytes()  # 0.4 s at 22.05 kHz
    assert fit_rate(pcm, 24000) == (pcm, 24000)
    out, rate = fit_rate(pcm, 22050)
    assert rate == 48000 and abs(len(out) // 2 - 19200) <= 1  # the same 0.4 s


def test_the_message_is_said_twice_between_pauses() -> None:
    pcm = b"\x01\x00" * 2400  # 0.1 s at 24 kHz
    audio = spoken(pcm, 24000)
    assert audio.count(pcm) == 2
    assert len(audio) == len(silence(LEAD_IN + GAP + TAIL, 24000)) + 2 * len(pcm)


async def test_a_repeated_incoming_update_never_hangs_up_the_live_call() -> None:
    """R32 (15.10): «declining» a repeated update while busy called leave_call on the owner's chat — the live call."""
    from types import SimpleNamespace

    from jarvis.integrations.telegram.caller import TelegramDialer

    left: list[int] = []

    async def leave_call(chat_id: int) -> None:
        left.append(chat_id)

    dialer = TelegramDialer(1, "hash", "session", 42)
    dialer._calls = SimpleNamespace(leave_call=leave_call)
    dialer.busy = True
    await dialer._on_incoming(42)
    assert left == []  # ignored, the call goes on
    await dialer._on_incoming(7)
    assert left == [7]  # a stranger is still declined


async def test_already_accepted_counts_as_accepted() -> None:
    """R32: Telethon re-sends acceptCall after a reconnect / clock fix; the copy answers CALL_ALREADY_ACCEPTED."""
    from types import SimpleNamespace

    from telethon.errors.rpcerrorlist import CallAlreadyAcceptedError

    from jarvis.integrations.telegram.caller import TelegramDialer

    async def accept_call(*_: object) -> None:
        raise CallAlreadyAcceptedError(request=None)

    dialer = TelegramDialer(1, "hash", "session", 42)
    dialer._calls = SimpleNamespace(_app=SimpleNamespace(accept_call=accept_call))
    dialer._tolerate_accept_resend()
    assert await dialer._calls._app.accept_call(42, b"g_b", None) is None
