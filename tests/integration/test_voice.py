"""Voice replies (TTS) and voice notes (STT): adapters over recorded HTTP shapes + bot behaviour with fakes."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from jarvis.bot.intents import Intent, keyword_route
from jarvis.bot.service import BotService
from jarvis.core.errors import PermanentError, TransientError
from jarvis.integrations.stt.client import OpenRouterSTT
from jarvis.integrations.telegram.client import HttpTelegramApi
from jarvis.integrations.tts.client import OpenRouterTTS
from jarvis.scheduler.queue import enqueue
from jarvis.services.container import build_container
from tests.conftest import make_settings

USER = 1001


# --- adapters ------------------------------------------------------------------------------------------------


@respx.mock
async def test_openrouter_tts_mp3_and_pcm() -> None:
    route = respx.post("https://openrouter.ai/api/v1/audio/speech").mock(
        side_effect=[
            httpx.Response(200, content=b"ID3mp3-bytes", headers={"content-type": "audio/mpeg"}),
            httpx.Response(200, content=b"\x00\x01" * 2400, headers={"content-type": "audio/pcm;rate=24000"}),
        ]
    )
    grok = OpenRouterTTS("key", "x-ai/grok-voice-tts-1.0", "Rex", style="[calm]")
    audio = await grok.synthesize("Добрый вечер.")
    assert (audio.data, audio.mime_type) == (b"ID3mp3-bytes", "audio/mpeg")
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "model": "x-ai/grok-voice-tts-1.0",
        "input": "Добрый вечер.",
        "voice": "Rex",
        "response_format": "mp3",
    }
    gemini = OpenRouterTTS("key", "google/gemini-3.1-flash-tts-preview", "Charon", style="[calm]")
    audio = await gemini.synthesize("Добрый вечер.", voice="Iapetus")
    body = json.loads(route.calls[1].request.content)
    assert body["response_format"] == "pcm"  # Gemini returns raw PCM only
    assert body["input"] == "Добрый вечер."  # the manner goes as `instructions`, never into the spoken text
    assert body["instructions"] == "[calm]"
    assert body["voice"] == "Iapetus"
    assert audio.mime_type == "audio/mpeg"
    assert audio.data[:2] in (b"\xff\xf3", b"\xff\xfb", b"ID")  # encoded to MP3 locally
    await grok.aclose()
    await gemini.aclose()


@respx.mock
async def test_gemini_speech_is_synthesized_in_pieces_in_parallel_and_stitched() -> None:
    """16.10 «Charon is great but slow»: three sentences took 6 s whole; pieces in parallel take the longest one."""
    route = respx.post("https://openrouter.ai/api/v1/audio/speech").mock(
        return_value=httpx.Response(200, content=b"\x01\x00" * 2400, headers={"content-type": "audio/pcm;rate=24000"})
    )
    used: list[int] = []

    async def budget() -> None:
        used.append(1)

    tts = OpenRouterTTS("key", "google/gemini-3.8-flash-tts", "Charon", style="manner", budget=budget)
    text = (
        "Добрый вечер, сэр. Восемь миллиардов умножить на ноль целых три десятых процента — двадцать четыре "
        "миллиона. Завтра встреча."
    )
    audio = await tts.synthesize(text)
    inputs = [json.loads(call.request.content)["input"] for call in route.calls]
    assert sorted(inputs) == sorted(
        [
            "Добрый вечер, сэр.",
            "Восемь миллиардов умножить на ноль целых три десятых процента",
            "— двадцать четыре миллиона.",
            "Завтра встреча.",
        ]
    )
    assert all(json.loads(call.request.content)["instructions"] == "manner" for call in route.calls)
    assert used == [1]  # one reply = one budget unit, however many pieces
    assert audio.mime_type == "audio/mpeg"
    await tts.aclose()


def test_speech_chunks_keep_short_phrases_whole() -> None:
    from jarvis.integrations.tts.client import speech_chunks, stitch

    assert speech_chunks("Да, сэр.") == [("Да, сэр.", 0.25)]
    assert [p for p, _ in speech_chunks("Завтра в десять утра встреча, выходить лучше в девять сорок.")] == [
        "Завтра в десять утра встреча, выходить лучше в девять сорок."
    ]  # one sentence under the target is never cut
    pcm, rate = stitch([(b"\x01\x00" * 10, 100), (b"\x02\x00" * 10, 100)], [0.25])
    assert (rate, len(pcm)) == (100, 20 + 2 * 25 + 20)


@respx.mock
async def test_openrouter_tts_errors_and_budget() -> None:
    respx.post("https://openrouter.ai/api/v1/audio/speech").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad voice", "code": 400}})
    )
    tts = OpenRouterTTS("key", "x-ai/grok-voice-tts-1.0", "Nope")
    with pytest.raises(PermanentError):
        await tts.synthesize("тест")

    async def exhausted() -> None:
        raise PermanentError("tts: daily budget exhausted")

    limited = OpenRouterTTS("key", "x-ai/grok-voice-tts-1.0", "Rex", budget=exhausted)
    with pytest.raises(PermanentError):
        await limited.synthesize("тест")
    assert len(respx.calls) == 1  # the budget check happens before any billable call
    await tts.aclose()
    await limited.aclose()


@respx.mock
async def test_openrouter_stt_multipart() -> None:
    route = respx.post("https://openrouter.ai/api/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": " Что у меня завтра? ", "usage": {"seconds": 1.2}})
    )
    stt = OpenRouterSTT("key", "openai/whisper-large-v3-turbo")
    transcript = await stt.transcribe(b"OggS-voice", "audio/ogg")
    assert transcript.text == "Что у меня завтра?"
    sent = route.calls[0].request.content
    assert b'name="model"' in sent and b"openai/whisper-large-v3-turbo" in sent
    assert b'name="language"' in sent and b"ru" in sent
    assert b'filename="voice.ogg"' in sent and b"OggS-voice" in sent
    await stt.aclose()


@respx.mock
async def test_telegram_send_voice_multipart() -> None:
    route = respx.post("https://api.telegram.org/bottok/sendVoice").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    )
    api = HttpTelegramApi("tok")
    markup = {"inline_keyboard": [[{"text": "ОК", "callback_data": "ap:a:x"}]]}
    result = await api.send_voice(1001, b"ID3audio", caption="Готово", reply_markup=markup)
    assert result["message_id"] == 7
    sent = route.calls[0].request.content
    assert b'name="chat_id"' in sent and b"1001" in sent
    assert "Готово".encode() in sent
    assert b'"callback_data": "ap:a:x"' in sent
    assert b'filename="voice.mp3"' in sent and b"ID3audio" in sent
    respx.post("https://api.telegram.org/bottok/sendVoice").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "Bad Request: VOICE_MESSAGES_FORBIDDEN"})
    )
    with pytest.raises(PermanentError):
        await api.send_voice(1001, b"ID3audio")
    await api.aclose()


def test_voice_mode_phrases() -> None:
    assert keyword_route("Отвечай текстом").voice_on is False
    assert keyword_route("отвечай мне голосом").voice_on is True
    assert keyword_route("выключи голос").intent == Intent.VOICE_MODE
    assert keyword_route("Отвечай текстом").intent == Intent.VOICE_MODE


# --- bot -----------------------------------------------------------------------------------------------------


@pytest.fixture
def voice_container(db: Any, clock: Any) -> Any:
    return build_container(make_settings(voice_replies_enabled=True), db=db, clock=clock)


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


HOW_ARE_YOU = "Всё в порядке, системы работают. У вас на сегодня 0 задач. Чем могу помочь?"


@pytest.mark.db
async def test_conversation_is_answered_by_voice_with_the_text_as_caption(voice_container: Any) -> None:
    c = voice_container
    await BotService(c).handle_update(msg("Как дела?", 1))
    [sent] = c.telegram.sent
    assert sent["voice"].startswith(b"ID3fake-Charon")  # the default voice since 14.10
    assert sent["text"] == HOW_ARE_YOU  # full text as the caption
    [(spoken, voice)] = c.tts.calls
    assert (spoken, voice) == (HOW_ARE_YOU, "Charon")


@pytest.mark.db
async def test_lists_commands_and_buttons_stay_text(voice_container: Any) -> None:
    """The user's choice (2026-09-24): voice only for conversation and ideas, never for buttons/lists/commands."""
    c = voice_container
    c.todoist.add(id="HAIR", content="Постричься", priority=4, due={"date": "2026-09-24"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    bot = BotService(c)
    for n, text in enumerate(["Что у меня завтра?", "📋 Задачи", "/leave", "добавь купить хлеб"], 2):
        await bot.handle_update(msg(text, n))
    assert c.tts.calls == []
    assert not [m for m in c.telegram.sent if "voice" in m or "audio" in m]


@pytest.mark.db
async def test_buttons_are_never_attached_to_a_voice_note(voice_container: Any) -> None:
    """Found live 2026-09-24: button labels were unreadable under a voice/audio message."""
    from jarvis.bot.service import SPEAK_REPLIES, VOICE_OK

    c = voice_container
    bot = BotService(c)
    markup = {"inline_keyboard": [[{"text": "✅ Готово", "callback_data": "sd:X"}]]}
    SPEAK_REPLIES.set("Rex")
    VOICE_OK.set(True)
    await bot.reply(USER, "Короткий ответ с кнопкой", markup)
    text, voice = c.telegram.sent
    assert text["text"] == "Короткий ответ с кнопкой" and text["reply_markup"] == markup
    assert "voice" in voice and voice["text"] == "" and voice["reply_markup"] is None
    assert (USER, "record_voice") in c.telegram.actions


@pytest.mark.db
async def test_voice_failures_fall_back_to_text(voice_container: Any) -> None:
    c = voice_container
    bot = BotService(c)
    c.tts.fail_with = TransientError("openrouter-tts: timeout")
    await bot.handle_update(msg("Как дела?", 3))
    assert c.telegram.sent[-1] == {**c.telegram.sent[-1], "text": HOW_ARE_YOU}  # the real answer
    assert "voice" not in c.telegram.sent[-1]
    c.tts.fail_with = None
    c.telegram.fail_next_voice = PermanentError("telegram sendVoice: Bad Request: wrong file identifier")
    await bot.handle_update(msg("Как дела?", 4))
    assert "voice" not in c.telegram.sent[-1]
    assert c.telegram.texts()[-1] == HOW_ARE_YOU


@pytest.mark.db
async def test_voice_toggle_and_voice_choice(voice_container: Any) -> None:
    c = voice_container
    bot = BotService(c)
    await bot.handle_update(msg("Отвечай текстом", 5))
    await bot.handle_update(msg("Как дела?", 6))
    assert "voice" not in c.telegram.sent[-1]  # the choice is remembered
    await bot.handle_update(msg("/voice Iapetus", 7))  # switching the voice turns replies back on
    await bot.handle_update(msg("Как дела?", 8))
    assert c.telegram.sent[-1]["voice"].startswith(b"ID3fake-Iapetus")
    await bot.handle_update(msg("/voice Бонд", 9))
    assert "Нет такого голоса" in c.telegram.texts()[-1]
    await bot.handle_update(msg("/voice", 10))
    status = c.telegram.texts()[-1]
    assert status.startswith("🔊 Голос Iapetus") and "• Telegram: голосом — разговор и идеи" in status
    assert "• Mac: все ответы голосом" in status


def voice_note(update_id: int, file_id: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
            "voice": {"file_id": file_id, "file_unique_id": f"u-{file_id}", "duration": 2, "mime_type": "audio/ogg"},
        },
    }


@pytest.mark.db
async def test_voice_notes_are_understood_answers_follow_the_voice_policy(voice_container: Any) -> None:
    c = voice_container
    c.telegram.files["v1"] = b"OggS-question"
    c.stt.transcripts[b"OggS-question"] = "Что у меня завтра?"
    c.telegram.files["v2"] = b"OggS-talk"
    c.stt.transcripts[b"OggS-talk"] = "Как дела?"
    bot = BotService(c)
    assert await bot.handle_update(voice_note(10, "v1")) == "processed"
    echo, answer = c.telegram.sent
    assert echo == {**echo, "text": "🎙 «Что у меня завтра?»"} and "voice" not in echo  # what was heard
    assert c.telegram.texts()[1].startswith("📅 Завтра") and "voice" not in answer  # a list: text
    await bot.handle_update(voice_note(11, "v2"))
    assert c.telegram.sent[-1]["text"] == HOW_ARE_YOU and "voice" in c.telegram.sent[-1]  # talk: voice


@pytest.mark.db
async def test_taps_and_scheduled_messages_stay_text(voice_container: Any, worker: Any) -> None:
    c = voice_container
    bot = BotService(c)
    await bot.handle_update(msg("велосипед", 11))  # no LLM responder → inbox buttons (as a voice note)
    choice = c.telegram.sent[-1]["reply_markup"]["inline_keyboard"][1][1]["callback_data"]
    await bot.handle_update(
        {
            "update_id": 12,
            "callback_query": {
                "id": "cb",
                "data": choice,
                "from": {"id": USER},
                "message": {"message_id": 11, "chat": {"id": USER, "type": "private"}},
            },
        }
    )
    assert c.telegram.texts()[-1] == "📝 Заметка сохранена.\nвелосипед" and "voice" not in c.telegram.sent[-1]
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.evening", run_at=c.clock.now())
    await worker.drain()
    assert "voice" not in c.telegram.sent[-1]


@pytest.mark.db
async def test_failed_transcription_keeps_the_audio_as_an_idea(voice_container: Any) -> None:
    from sqlalchemy import select

    from jarvis.db.models import Idea

    c = voice_container
    c.telegram.files["v2"] = b"OggS-noise"

    async def broken(audio: bytes, mime_type: str, language: str = "ru") -> Any:
        raise TransientError("openrouter-stt: timeout")

    c.stt.transcribe = broken
    update = {
        "update_id": 20,
        "message": {
            "message_id": 20,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
            "voice": {"file_id": "v2", "file_unique_id": "u-v2", "duration": 2, "mime_type": "audio/ogg"},
        },
    }
    assert await BotService(c).handle_update(update) == "processed"
    async with c.db.session() as s:
        idea = await s.scalar(select(Idea))
    assert idea is not None and idea.raw_media_id is not None
    assert "расшифровать не вышло" in c.telegram.texts()[-1]


@pytest.mark.db
async def test_several_requests_get_one_voice_note(voice_container: Any) -> None:
    c = voice_container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    await BotService(c).handle_update(msg("Как дела? И добавь купить хлеб", 40))
    [sent] = c.telegram.sent  # talk + a command in one message: one voice note for the whole answer
    assert "voice" in sent
    assert sent["text"].startswith("Всё в порядке, системы работают. У вас на сегодня 1 задача.")
    assert "\n\n🛒 Добавил в покупки: <b>Хлеб</b>" in sent["text"]  # the parts merged as HTML, not flattened
    assert sent["parse_mode"] == "HTML"  # the caption keeps its formatting
    assert "<b>" not in c.tts.calls[0][0]  # speech gets plain text
    assert len(c.tts.calls) == 1


@pytest.mark.db
@pytest.mark.parametrize(
    "telegram_error",
    [
        # The exact answer seen live on 2026-09-24 (the documented VOICE_MESSAGES_FORBIDDEN was never returned).
        'telegram: HTTP 400: {"ok":false,"error_code":400,'
        '"description":"Bad Request: user restricted receiving of voice note messages"}',
        "telegram sendVoice: Bad Request: VOICE_MESSAGES_FORBIDDEN",
    ],
)
async def test_voice_forbidden_by_privacy_falls_back_to_audio_file(voice_container: Any, telegram_error: str) -> None:
    """Found live 2026-09-24: «записывает голосовое…», then plain text. Telegram rejects voice notes from bots
    when the user's (Premium) privacy settings forbid voice messages; an audio file still gets through."""
    from sqlalchemy import select

    from jarvis.db.models import AuditEvent

    c = voice_container
    bot = BotService(c)
    forbidden = PermanentError(telegram_error)
    c.telegram.fail_next_voice = forbidden
    await bot.handle_update(msg("Как дела?", 50))
    audio, hint = c.telegram.sent[-2:]
    assert audio["audio"].startswith(b"ID3fake") and audio["text"] == HOW_ARE_YOU
    assert hint["text"].startswith("🔇 Telegram не пропускает мои голосовые")
    c.telegram.fail_next_voice = forbidden
    await bot.handle_update(msg("Как дела?", 51))
    assert "audio" in c.telegram.sent[-1]
    assert sum(m["text"].startswith("🔇") for m in c.telegram.sent) == 1  # the hint is sent once
    async with c.db.session() as s:
        failures = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "VOICE_REPLY_FAILED"))).all()
    assert len(failures) == 2
    assert telegram_error in failures[0].details["error"]  # the provider's exact answer is kept
