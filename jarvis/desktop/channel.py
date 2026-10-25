"""The Mac app as another chat with Jarvis: the same bot logic, replies captured instead of sent to Telegram.

Voice on the Mac is its own setting (2026-10-14): every reply is spoken (one audio for the whole answer, links,
keys and paths skipped by `to_speech`), independent of the Telegram /voice toggle.

The conversation is the user's own chat (same `chat_id`), so context is shared: «На когда перенести …?» asked on the
Mac can be answered in Telegram and the other way round. Buttons are signed exactly like in Telegram and come back
through `handle_callback`, which verifies them. Notifications that happen later (reminders, results of approved
jobs) still go to Telegram.
"""

from __future__ import annotations

import base64
import itertools
import logging
import time
from typing import Any, cast

from jarvis.bot.fmt import strip_tags
from jarvis.bot.service import SPEECH_COLLECT, BotService
from jarvis.bot.speech import to_speech
from jarvis.core.errors import JarvisError
from jarvis.desktop.service import CHANNEL
from jarvis.integrations.telegram.client import FakeTelegramApi
from jarvis.services.container import Container

log = logging.getLogger(__name__)

# Message ids unique across requests (the app replaces a message by id when a button edits it).
_ID_BLOCKS = itertools.count(int(time.time()) % 1_000_000_000)


class CaptureTelegram(FakeTelegramApi):
    """Records what the bot would send; markups are signed with the real key so the app's taps verify."""

    desktop = True  # the conversation is on the Mac: «открой сайт …» opens it right there


class _ChannelContainer:
    """The real container with a capturing Telegram adapter (everything else is shared)."""

    def __init__(self, base: Container, telegram: CaptureTelegram) -> None:
        self._base = base
        self.telegram = telegram

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _buttons(markup: dict[str, Any] | None) -> list[list[dict[str, str]]]:
    """Callback buttons only: URL buttons (the 2GIS route, for the phone) are dropped, and so are rows left empty."""
    rows = (markup or {}).get("inline_keyboard") or []
    kept = [[{"text": b["text"], "data": b["callback_data"]} for b in row if "callback_data" in b] for row in rows]
    return [row for row in kept if row]


def _message(entry: dict[str, Any]) -> dict[str, Any]:
    audio = entry.get("voice") or entry.get("audio")
    return {
        "id": entry["message_id"],
        "text": entry.get("text") or "",
        "format": "html" if entry.get("parse_mode") == "HTML" else "plain",
        "buttons": _buttons(entry.get("reply_markup")),
        "audio": base64.b64encode(audio).decode() if isinstance(audio, bytes | bytearray) else None,
    }


def plain_text(text: str) -> str:
    """HTML parts of a reply as plain text (for a quick notification line)."""
    return strip_tags(text)


class DesktopChannel:
    def __init__(self, container: Container) -> None:
        self.container = container

    def _bot(self) -> tuple[BotService, CaptureTelegram, int]:
        capture = CaptureTelegram(signer=self.container.callback_signer)
        capture._message_id = next(_ID_BLOCKS) * 1000
        bot = BotService(cast(Container, _ChannelContainer(self.container, capture)))
        allowed = self.container.settings.telegram_allowed_user_ids
        if not allowed:
            raise JarvisError("TELEGRAM_ALLOWED_USER_IDS is empty: there is no user to talk for")
        return bot, capture, int(allowed[0])

    @staticmethod
    def _result(capture: CaptureTelegram) -> dict[str, Any]:
        return {
            "messages": [_message(entry) for entry in capture.sent],
            "edits": [
                {
                    "id": edit["message_id"],
                    "text": edit["text"],
                    "format": "html" if edit.get("parse_mode") == "HTML" else "plain",
                    "buttons": _buttons(edit.get("reply_markup")),
                }
                for edit in capture.edits
            ],
            "toast": capture.answered[-1][1] if capture.answered else None,
        }

    async def _speak(self, bot: BotService, result: dict[str, Any], said: list[str]) -> dict[str, Any]:
        """One audio for the whole answer, attached to its first message (the app plays it); text-only on failure."""
        result["voice_mac"] = await bot.mac_voice_enabled()
        if not (result["voice_mac"] and said and result["messages"] and bot._tts_available()):
            return result
        speech = to_speech("\n".join(said), self.container.settings.tts_max_chars)
        if not speech:
            return result
        try:
            audio = await self.container.tts.synthesize(speech, voice=(await bot.voice_settings())[1])
        except Exception as exc:  # budget, provider outage: the text is there anyway
            log.warning("mac voice failed: %s", exc)
            return result
        result["messages"][0]["audio"] = base64.b64encode(audio.data).decode()
        return result

    async def message(self, text: str, *, spoken: bool = False) -> dict[str, Any]:
        """A message typed or said on the Mac (`spoken` is informational: the Mac speaks every reply when its voice
        is on)."""
        bot, capture, chat_id = self._bot()
        message: dict[str, Any] = {
            "message_id": None,
            "text": text[:4000],
            "from": {"id": chat_id},
            "chat": {"id": chat_id, "type": "private"},
        }
        channel_token = CHANNEL.set("desktop")
        said: list[str] = []
        speech_token = SPEECH_COLLECT.set(said)
        try:
            await bot._handle_message(message)
        except JarvisError as exc:
            await bot.reply(chat_id, bot.persona.sorry(f"⚠️ {bot._friendly(exc)}"))
        except Exception:
            log.exception("desktop message failed")
            await bot.reply(chat_id, "⚠️ Внутренняя ошибка. Подробности в логах сервера.")
        finally:
            SPEECH_COLLECT.reset(speech_token)
            CHANNEL.reset(channel_token)
        return await self._speak(bot, self._result(capture), said)

    async def set_voice(self, enabled: bool) -> dict[str, Any]:
        """The app's menu item «Отвечать голосом» (the Mac setting only; Telegram keeps its own)."""
        bot, _capture, _chat_id = self._bot()
        await bot._save_voice_settings(mac=enabled)
        return {"voice_mac": await bot.mac_voice_enabled()}

    async def voice_state(self) -> bool:
        bot, _capture, _chat_id = self._bot()
        return await bot.mac_voice_enabled()

    async def callback(self, data: str, message_id: int | None) -> dict[str, Any]:
        """A button tapped in the Mac app: the same signed data as in Telegram, verified by the bot."""
        bot, capture, chat_id = self._bot()
        callback = {
            "id": "desktop",
            "data": data[:200],
            "from": {"id": chat_id},
            "message": {"message_id": message_id or 0, "chat": {"id": chat_id, "type": "private"}},
        }
        channel_token = CHANNEL.set("desktop")
        said: list[str] = []
        speech_token = SPEECH_COLLECT.set(said)
        try:
            await bot.handle_callback(callback)
        except JarvisError as exc:
            await bot.reply(chat_id, bot.persona.sorry(f"⚠️ {bot._friendly(exc)}"))
        finally:
            SPEECH_COLLECT.reset(speech_token)
            CHANNEL.reset(channel_token)
        return await self._speak(bot, self._result(capture), said)
