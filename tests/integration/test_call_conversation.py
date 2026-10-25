"""Incoming Telegram calls: the user calls Jarvis and talks one phrase at a time (fake line, fake STT/TTS)."""

from __future__ import annotations

import array
import asyncio
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from jarvis.calls.conversation import (
    GOODBYE,
    GREETING,
    NOT_HEARD,
    TOO_SLOW,
    Conversation,
    Pending,
    PhoneChannel,
    Segmenter,
    VadConfig,
    choose,
    spoken_reply,
)
from jarvis.calls.service import _on_call, converse
from jarvis.integrations.stt.client import FakeSTT

RATE = 24000
FAST = VadConfig(idle_s=0.6, end_ms=200, start_ms=60, preroll_ms=60)


def tone(seconds: float, *, volume: float = 0.3) -> bytes:
    n = int(RATE * seconds)
    return array.array("h", (int(32767 * volume * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n))).tobytes()


def quiet(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def chunks(pcm: bytes, ms: int = 10) -> list[bytes]:
    size = RATE * 2 * ms // 1000
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


@dataclass
class FakeLine:
    """Plays `phrases` (user audio) one after each thing Jarvis says; records what Jarvis said."""

    phrases: list[bytes]
    rate: int = RATE
    heard: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    hung_up: asyncio.Event = field(default_factory=asyncio.Event)
    said: list[int] = field(default_factory=list)
    hangups: int = 0

    async def say(self, pcm: bytes) -> None:
        self.said.append(len(pcm))
        if len(pcm) > RATE * 2 * 0.2 and self.phrases:  # not a beep: the user answers after Jarvis spoke
            loop = asyncio.get_running_loop()
            phrase = self.phrases.pop(0)
            loop.call_later(0.01, lambda: [self.heard.put_nowait(c) for c in chunks(phrase)])

    async def hang_up(self) -> None:
        self.hangups += 1
        self.hung_up.set()


@dataclass
class SpeakingTTS:
    texts: list[str] = field(default_factory=list)

    async def synthesize_pcm(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ) -> tuple[bytes, int]:
        self.texts.append(text)
        return quiet(0.5), RATE


def test_a_phrase_ends_after_a_pause_and_keeps_its_start() -> None:
    seg = Segmenter(RATE, VadConfig(end_ms=300))
    assert seg.feed(quiet(0.5)) is None
    assert seg.feed(tone(1.0)) is None and seg.in_phrase
    phrase = seg.feed(quiet(0.4))
    assert phrase is not None
    assert 1.0 <= len(phrase) / (RATE * 2) <= 1.8  # the speech + the pre-roll + the closing pause
    assert seg.feed(quiet(1.0)) is None  # silence alone is never a phrase


async def test_turns_heard_answered_and_goodbye() -> None:
    line = FakeLine([quiet(0.1) + tone(0.4) + quiet(0.3), quiet(0.1) + tone(0.3) + quiet(0.3)])
    stt = FakeSTT(default="что у меня завтра")
    tts = SpeakingTTS()
    asked: list[str] = []

    async def brain(text: str) -> str:
        asked.append(text)
        return "Завтра в 10 встреча с Ниной."

    turns = await Conversation(line, brain, stt, tts, FAST).run()
    assert asked == ["что у меня завтра", "что у меня завтра"]
    assert turns == [("что у меня завтра", "Завтра в 10 встреча с Ниной.")] * 2
    assert tts.texts[0] == GREETING and tts.texts[-1] == GOODBYE
    assert line.hangups == 1


async def test_nothing_recognised_is_asked_again() -> None:
    line = FakeLine([tone(0.3) + quiet(0.3)])

    class EmptySTT:
        async def transcribe(self, audio: bytes, mime_type: str, language: str = "ru") -> Any:
            raise RuntimeError("openrouter-stt: empty transcript")

    tts = SpeakingTTS()
    turns = await Conversation(line, lambda t: asyncio.sleep(0, "x"), EmptySTT(), tts, FAST).run()
    assert turns == [] and NOT_HEARD in tts.texts


async def test_a_stuck_answer_is_cut_off() -> None:
    # M6 (review 17.10): the bot's answer was awaited without a limit — the call could hang past its ten minutes.
    line = FakeLine([quiet(0.1) + tone(0.4) + quiet(0.3)])
    tts = SpeakingTTS()
    cfg = VadConfig(idle_s=0.6, end_ms=200, start_ms=60, preroll_ms=60, hold_after_s=0.05, max_think_s=0.3)

    async def stuck(text: str) -> str:
        await asyncio.sleep(3600)
        return "never"

    turns = await asyncio.wait_for(Conversation(line, stuck, FakeSTT(default="что нового"), tts, cfg).run(), 10)
    assert turns == [("что нового", TOO_SLOW)]
    assert "Секунду, сэр." in tts.texts and tts.texts[-1] == GOODBYE


async def test_hanging_up_while_it_thinks_ends_the_wait() -> None:
    line = FakeLine([quiet(0.1) + tone(0.4) + quiet(0.3)])
    tts = SpeakingTTS()
    cfg = VadConfig(idle_s=0.6, end_ms=200, start_ms=60, preroll_ms=60, hold_after_s=0.05)  # thinks up to 60 s

    async def slow(text: str) -> str:
        asyncio.get_running_loop().call_later(0.2, line.hung_up.set)
        await asyncio.sleep(3600)
        return "never"

    started = asyncio.get_running_loop().time()
    turns = await asyncio.wait_for(Conversation(line, slow, FakeSTT(default="что нового"), tts, cfg).run(), 10)
    assert turns == [("что нового", TOO_SLOW)] and asyncio.get_running_loop().time() - started < 5
    assert GOODBYE not in tts.texts and TOO_SLOW not in tts.texts  # nobody is on the line to hear it


async def test_hanging_up_ends_without_goodbye() -> None:
    line = FakeLine([])
    line.hung_up.set()
    tts = SpeakingTTS()
    assert await Conversation(line, lambda t: asyncio.sleep(0, ""), FakeSTT(), tts, FAST).run() == []
    assert line.hangups == 0 and GOODBYE not in tts.texts


@pytest.mark.db
async def test_on_the_call_replies_are_captured_short_and_summed_up_silently(container: Any) -> None:
    c = container
    phone = PhoneChannel(c)
    spoken = await phone.ask("мои анализы")
    assert "анализов пока нет" in spoken.lower()
    assert c.telegram.sent == []  # nothing pings the phone during the call
    await phone.summary(2)
    [summary] = c.telegram.sent
    assert summary.get("silent") and "Разговор с Джарвисом · 2 мин" in c.telegram.texts()[0]
    assert "«мои анализы»" in c.telegram.texts()[0]


@pytest.mark.db
async def test_a_draft_is_confirmed_by_voice(container: Any, worker: Any) -> None:
    c = container
    await _on_call(c, c.clock.now() + timedelta(minutes=10))  # set by `converse` for a real call
    phone = PhoneChannel(c)
    spoken = await phone.ask("спал 8 часов")
    assert spoken.endswith("Подтвердить?") and phone.pending is not None and phone.pending.yes
    answer = await phone.ask("да")
    assert answer and phone.pending is None
    await worker.drain()
    assert c.lmf_api.days_store["2026-09-23"]["sleep_minutes"] == 480
    [result] = c.telegram.sent  # the job's «Записал в TLT» arrives without a sound while on the call
    assert result.get("silent") and "Записал в TLT" in c.telegram.texts()[0]


@pytest.mark.db
async def test_no_cancels_and_another_phrase_is_a_new_request(container: Any) -> None:
    c = container
    phone = PhoneChannel(c)
    await phone.ask("спал 7 часов")
    assert "Отменено" in await phone.ask("нет")  # the draft's own «✖️ Отмена» button, pressed by voice
    await phone.ask("спал 7 часов")
    await phone.ask("мои анализы")  # not an answer to the draft: a new request, the draft stays unanswered
    assert phone.pending is None and c.lmf_api.writes == []


def test_voice_choices() -> None:
    pending = Pending(1, "yes", "no", [])
    assert (choose(pending, "Да, записывай"), choose(pending, "нет"), choose(pending, "что у меня завтра")) == (
        "yes", "no", None,
    )  # fmt: skip
    options = Pending(2, None, "no", [("Постричься", "a"), ("Постирать", "b")])
    assert (choose(options, "второй"), choose(options, "1"), choose(options, "да")) == ("b", "a", None)
    assert choose(options, "давай второй вариант") == "b"


def test_a_new_command_never_presses_the_pending_button() -> None:
    # R35: «Удалить задачу А?» waits; «удали задачу Б» used to match «удали» and confirm A.
    pending = Pending(1, "delete-a", "keep-a", [])
    for said in ("удали задачу Б", "Удали молоко из покупок", "отмени встречу в пятницу", "не забудь купить хлеб",
                 "запиши: позвонить маме", "да, и перенеси врача на завтра"):  # fmt: skip
        assert choose(pending, said) is None, said
    assert [choose(pending, s) for s in ("Да, удаляй, сэр", "удали", "ок давай", "Нет, не удаляй", "не надо")] == [
        "delete-a", "delete-a", "delete-a", "keep-a", "keep-a",
    ]  # fmt: skip
    assert choose(Pending(2, None, None, [("A", "a"), ("B", "b")]), "перенеси на два часа") is None


def test_spoken_reply_drops_hints_and_keeps_it_short() -> None:
    html = "📅 <b>Завтра</b> · чт 24.09\n• 10:00 Встреча с Ниной\n• 18:00 Спорт\n<i>Совет: скажите «перенеси …»</i>"
    said = spoken_reply([html])
    assert "Встреча с Ниной" in said and "Совет" not in said
    assert len(spoken_reply(["• " + "очень длинная строка " * 10 + "\n"] * 20)) <= 400


@pytest.mark.db
async def test_an_incoming_call_is_logged(container: Any) -> None:
    c = container
    c.tts = SpeakingTTS()
    line = FakeLine([])
    line.hung_up.set()
    assert await converse(c, line) == 0
    from sqlalchemy import select

    from jarvis.db.models import CallRequest

    async with c.db.session() as s:
        [row] = (await s.scalars(select(CallRequest))).all()
    assert (row.provider, row.status, row.ended_reason) == ("telegram", "completed", "incoming: 0 phrase(s)")


@pytest.mark.db
async def test_jarvis_calling_to_talk_greets_differently_and_leaves_the_ledger_to_the_call(container: Any) -> None:
    from sqlalchemy import func, select

    from jarvis.calls.conversation import CALLING
    from jarvis.db.models import CallRequest

    c = container
    c.tts = SpeakingTTS()
    line = FakeLine([])
    line.hung_up.set()
    assert await converse(c, line, outgoing=True) == 0
    assert c.tts.texts[0] == CALLING  # «Сэр, Джарвис на связи. Слушаю вас.», not «Слушаю, сэр.»
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(CallRequest)) == 0  # ring_next finishes its own row
