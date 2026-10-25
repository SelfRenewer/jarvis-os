"""Telegram interface: authorization, commands, natural language, voice notes, approval callbacks.

Security: only private chats of allowlisted user ids are served; everyone else is ignored without a
reply (no information leak) and audited. Each update id is processed at most once.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import html
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from jarvis.agenda.service import attention_block, calendar_connected, human_minutes, plural
from jarvis.approvals.service import APPROVE, REJECT, decide, request_approval
from jarvis.audit.service import A, audit
from jarvis.bot import fmt
from jarvis.bot.intents import (
    INBOX_INTENTS,
    VOICE_CALL,
    Intent,
    RoutedIntent,
    normalize_command,
    route_message,
    split_due,
    strip_address,
)
from jarvis.bot.location import FORWARDED_LOCATION, HOME_CALLBACK, TravelDesk
from jarvis.bot.menu import (
    KEYBOARD_PREF_KEY,
    KEYBOARD_VERSION,
    MAIN_MENU,
    SETTINGS_BUTTON,
    main_keyboard,
    menu_intent,
    more_intent,
    more_keyboard,
    settings_keyboard,
)
from jarvis.bot.persona import Persona
from jarvis.bot.speech import to_speech
from jarvis.calc.currency import parse_conversion
from jarvis.calc.expression import CalcError, calculate
from jarvis.core.clock import format_local, to_local
from jarvis.core.dates import Period, human_day, parse_period, parse_when
from jarvis.core.errors import (
    AuthError,
    JarvisError,
    LLMOutputError,
    NotConfiguredError,
    PermanentError,
    TransientError,
)
from jarvis.core.text import is_pronoun, matches_stems, normalize_due_phrase
from jarvis.db.models import (
    CalendarEvent,
    ChatContext,
    DevSpec,
    Idea,
    IntegrationSyncState,
    Note,
    Notification,
    PendingChoice,
    RawMedia,
    Reminder,
    ScheduledJob,
    TelegramUpdate,
    TodoistTask,
    UserPreference,
)
from jarvis.desktop.service import CHANNEL
from jarvis.inbox.service import INBOX_OPTIONS, choice_button, offer_choice, take_choice
from jarvis.integrations.lmf.client import FoodNotSupportedError
from jarvis.integrations.telegram.client import MAX_CAPTION, InlineButton, inline_keyboard
from jarvis.integrations.tts.client import KNOWN_VOICES
from jarvis.jobs.handlers import approval_keyboard
from jarvis.photos.reader import MAX_PHOTO_BYTES, PhotoKind, PhotoReading, read_photo
from jarvis.scheduler.queue import enqueue, enqueue_coalesced
from jarvis.services.container import Container
from jarvis.shopping.providers import ImportDocument
from jarvis.tasks.service import (
    PRIORITY_EMOJI,
    SCOPE_RU,
    DueTarget,
    TargetInPastError,
    descendants,
    match_tasks,
)
from jarvis.tracker.card import STEP_FIELD

log = logging.getLogger(__name__)

# Set while handling one of the user's messages when voice replies are on (per asyncio task, so concurrent
# updates never mix). Callback taps and scheduled notifications stay text.
SPEAK_REPLIES: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_speak_replies", default=None)
VOICE_PREF_KEY = "voice_replies"
# Texts the bot «said» in this request (the Mac channel speaks them all; Telegram has its own voice policy).
SPEECH_COLLECT: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("jarvis_speech", default=None)
PORTIONS_KEY = "supplement_portions"  # «энергетик» → {"supplement": "Caffeine", "dose": 160}
PORTION_OPTIONS = {"a": 80, "b": 160, "c": 200}  # caffeine per drink, mg — the user picks, Jarvis never guesses
# Voice replies only where talking makes sense: conversation and ideas (not buttons, lists, commands).
VOICE_OK: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_voice_ok", default=False)
VOICE_INTENTS = frozenset({Intent.SMALL_TALK, Intent.ASK, Intent.CAPTURE_IDEA, Intent.SEARCH_IDEAS})
FORWARD_KEYS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
# The persona's short first line («Да, сэр.») for the next reply of the current request.
PENDING_ACK: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_pending_ack", default=None)
ACK_INTENTS = frozenset(
    {
        Intent.ADD_TASK,
        Intent.REMIND,
        Intent.SCHEDULE_REFERENCED_TASK,
        Intent.COMPLETE_TASK,
        Intent.DELETE_TASK,
        Intent.SET_PRIORITY,
        Intent.BULK_TASKS,
        Intent.CAPTURE_IDEA,
        Intent.CAPTURE_NOTE,
        Intent.SHOPPING_ADD,
        Intent.VOICE_MODE,
        Intent.PLAN,
        Intent.IDEA_TO_SPEC,
        Intent.MED_ADD,
        Intent.MED_REMOVE,
        Intent.MED_EXTEND,
        Intent.MED_TAKEN,
        Intent.MED_PACK,
        Intent.MED_PORTION,
        Intent.FOOD_PRODUCT,
        Intent.FOOD_TARGETS,
        Intent.SITE_OPEN,
        Intent.SITE_STOP,
        Intent.TUNNEL_CLOSE,
        Intent.SHOPPING_FROM_TASKS,
        Intent.MAC_OPEN,
        Intent.CLAUDE_CODE,
    }
)
QUERY_INTENTS = frozenset(
    {
        Intent.SHOW_TODAY,
        Intent.SHOW_AGENDA,
        Intent.SHOW_TASKS,
        Intent.SHOW_SUBTASKS,
        Intent.SHOW_NOTES,
        Intent.SEARCH_IDEAS,
        Intent.SEARCH_ALL,
        Intent.SHOPPING_SHOW,
        Intent.LEAVE_TIME,
        Intent.WEATHER,
        Intent.MED_LIST,
        Intent.TRACKER_SHOW,
        Intent.DIARY,
        Intent.TPL_SHOW,
        Intent.FOOD_SHOW,
        Intent.SITES_LIST,
        Intent.HEALTH_SHOW,
        Intent.LABS_SHOW,
    }
)
# Set while one message with several requests is handled: replies are collected and sent as one message.
REPLY_BUFFER: contextvars.ContextVar[list[tuple[fmt.Html, dict[str, Any] | None, bool]] | None] = (
    contextvars.ContextVar("jarvis_reply_buffer", default=None)
)

# /help: short sections; the long example lists fold into expandable quotes (Telegram shows the first lines).
HELP_SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "📋",
        "Задачи и календарь",
        (
            "«Что у меня завтра?», «Что на неделе?», «Что главное?»",
            "«Готово: стрижка», «Перенеси стрижку на пятницу в 18:00»",
            "«Приоритет 1: стрижка», «Удали задачу …»",
            "«Перенеси все просроченные на завтра» — с подтверждением",
            "«Добавь в обустройство квартиры купить тарелки»",
            "«Напомни позвонить в банк завтра в 10»",
        ),
    ),
    (
        "📓",
        "Дневник (TLT) и таблетки",
        (
            "«Заполни день» — спрошу по шагам: сон, оценка, настроение, что было",
            "«Спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT»",
            "«Вчера была тренировка и холодный душ»",
            "«TPL: отметь утреннюю рутину и чтение»",
            "«Утренние таблетки: D3 6000, цинк 25 в 9:00», «Выпил утренние»",
            "«✅ Выпил всё» в напоминании записывает таблетки в TLT само",
            "«Мои анализы», «Какой ферритин в анализах?» — результаты из LMF",
        ),
    ),
    (
        "🍽",
        "Еда и расходы",
        (
            "«Съел 200 г гречки и 2 яйца», «На обед борщ», фото тарелки или этикетки",
            "«Сколько я сегодня съел?», «Норма 2200 ккал, белок 140»",
            "«Потратил 45 лари на такси», фото чека",
            "«Сколько я потратил в октябре?», «Удали последний расход»",
        ),
    ),
    (
        "💡",
        "Идеи, заметки, поиск",
        (
            "«Запиши идею …», «Заметка: код домофона 4521»",
            "«Найди барбера», «Где у меня код домофона»",
            "«Изучи ВНЖ Грузии» — сначала покажу цену (≈ $0.01–0.03), ищу после кнопки",
            "«10 000 000 ÷ 1500», «15% от 2000», «Курс лари» — считаю сразу, без ИИ",
        ),
    ),
    (
        "🛒",
        "Покупки, Mac, сайты",
        (
            "«Добавь яйца, молоко и 2 кг филе», «Иду в магазин»",
            "«Открой телегу», «Громкость 30», «Скопируй на мак: …»",
            "«Напомни на маке через 10 минут …»",
            "«Открой сайт TLT», «Какие сайты запущены?», «Закрой туннель»",
        ),
    ),
]
HELP_COMMANDS = "/today /agenda /tasks /tlt /tpl /food /health /labs /money /plan /notes /ideas /status /voice /menu"

DEFAULT_PERIOD = "d30"  # 📊 opens on the last 30 days
MAC_NOT_CONNECTED = fmt.lines(
    fmt.line("🖥", fmt.b("Приложение Jarvis для Mac ещё не подключено.")),
    fmt.line("Установка: в папке проекта", fmt.Html(f"{fmt.code('./desktop/install.sh')},")),
    "затем откройте Jarvis из папки «Программы» — и я смогу открывать приложения, сайты и запускать Claude Code.",
)
# LMF answered 401: its API needs Jarvis's token (LMF_API_TOKEN), which is missing or stale.
LMF_ACCESS_DENIED = fmt.lines(
    fmt.line("📓", fmt.b("TLT не пускает Джарвиса: нужен токен LMF.")),
    fmt.line("На Mac в папке проекта:", fmt.Html(f"{fmt.code('uv run jarvis lmf-setup')},"), "затем make restart."),
    "Ничего не записал.",
)
# What is confirmed must be what runs: a Claude Code task is shown in full before «▶️ Запустить», so it is capped.
CLAUDE_PROMPT_MAX = 3000
MOVE_HINT = "➡️ Перенести — скажите, например: «стрижку на пятницу в 18» или «на 2 часа позже»."
# «На когда перенести …?» is answered by the next message (not by buttons).
AWAIT_DUE = "await_due"
AWAIT_DUE_TTL = timedelta(minutes=15)
# «В каком проекте запустить Claude Code?» is answered by the next message («в джарвисе»).
AWAIT_PROJECT = "await_project"
CLAUDE_PROJECT_KEY = "claude_last_project"
# «Заполни день»: one question at a time; each step is a pending choice (buttons) that words can answer too.
FILL_KIND = "tlt_fill"
FILL_TTL = timedelta(minutes=30)
FILL_CONTROL = {
    "done": r"(?:готово|вс[её]|хватит|достаточно|записывай|запиши|больше\s+ничего|ничего)",
    "skip": r"(?:пропусти\w*|пропуск|дальше|следующ\w*|не\s+знаю|не\s+помню|без\s+(?:оценки|этого))",
    "stop": r"(?:стоп|отмена|отмени\w*|не\s+надо|потом|позже|прекрати)",
}

VOICE_FORBIDDEN_HINT = (
    "🔇 Telegram не пропускает мои голосовые: в ваших настройках приватности голосовые сообщения запрещены "
    "(функция Telegram Premium). Пока присылаю голос аудиофайлом.\n\n"
    "Чтобы приходили настоящие голосовые: Настройки → Конфиденциальность → Голосовые сообщения → "
    "«Всегда разрешать» → добавить своего бота (или выбрать «Все»). Потом напишите /voice on."
)

# Telegram's answers when the user's privacy settings block voice notes (seen live: the second one).
VOICE_FORBIDDEN_MARKERS = ("voice_messages_forbidden", "restricted receiving of voice")


def voice_forbidden(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in VOICE_FORBIDDEN_MARKERS)


SMALL_TALK = {"привет", "спасибо", "ок", "окей", "ok", "хорошо", "понял", "ясно", "пока", "да", "нет", "👍"}
INBOX_KIND_RU = {
    "add_task": "задача",
    "remind": "задача",
    "capture_idea": "идея",
    "shopping_add": "покупка",
    "capture_note": "заметка",
}


JOB_NAMES_RU = {
    "planner.daily": "ежедневный план",
    "planner.apply": "применение плана",
    "reminder.fire": "напоминание",
    "reminders.plan": "планирование напоминаний",
    "todoist.reconcile": "синхронизация Todoist",
    "todoist.refresh_task": "обновление задачи",
    "calendar.reconcile": "синхронизация календаря",
    "idea.analyze": "разбор идеи",
    "notify.telegram": "сообщение в Telegram",
    "briefing.morning": "утренний брифинг",
    "briefing.evening": "вечерний обзор",
    "tasks.apply_op": "изменение задач",
    "usage.snapshot": "учёт расходов",
}


def explain_error(error: str | None) -> str:
    text_err = error or ""
    known = [
        ("chat not found", "бот ещё не мог вам писать (не был нажат Start)"),
        ("bot was blocked", "бот заблокирован в Telegram"),
        ("authorization failed", "доступ отозван — нужно заново авторизоваться"),
        ("invalid_grant", "доступ Google истёк — нужно заново авторизоваться"),
        ("timeout", "сервис не ответил вовремя"),
        ("rate limited", "сервис временно ограничил запросы"),
    ]
    for needle, human in known:
        if needle in text_err.lower():
            return human
    return "техническая ошибка (подробности: make status)"


def shopping_view(items: list[Any], heading: str, note: str | None = None) -> tuple[fmt.Html, dict[str, Any]]:
    """The list + a «✅ <item>» button per item (first 10, two per row) and «🧹 Купил всё». Ids are Jarvis's own."""
    from jarvis.shopping.service import display_name

    lines = []
    for item in items:
        qty = f"{item.quantity.normalize():f} {item.quantity_unit}" if item.quantity is not None else None
        lines.append(fmt.bullet(display_name(item), qty))
    buttons = [InlineButton(f"✅ {display_name(i)[:24]}", f"sb:{i.id.hex}") for i in items[:10]]
    rows = [buttons[n : n + 2] for n in range(0, len(buttons), 2)]
    rows.append([InlineButton("🧹 Купил всё", "sb:all")])
    text_msg = fmt.blocks(fmt.title("🛒", heading, str(len(items))), lines, fmt.hint(note) if note else None)
    return text_msg, inline_keyboard(rows)


def compact_number(value: int) -> str:
    """12 345 → 12.3k, 1 234 567 → 1.2M (fits a narrow table column)."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def usage_button(period: Any, current: str) -> InlineButton:
    label = period.label[:1].upper() + period.label[1:]
    return InlineButton(f"• {label}" if period.key == current else label, f"us:{period.key}")


class BotService:
    LMF_START_WAIT = 2.0  # seconds for LMF to come up after Jarvis OS (or LocalSites) starts TLT

    def __init__(self, container: Container) -> None:
        self.c = container
        self.allowed = set(container.settings.telegram_allowed_user_ids)
        self.persona = Persona(container.settings.persona_enabled, container.settings.persona_address)
        self.travel_desk = TravelDesk(container)

    # --- plumbing ----------------------------------------------------------------------------------

    async def reply(
        self,
        chat_id: int,
        text_msg: str,
        markup: dict[str, Any] | None = None,
        *,
        speak: bool = True,
        parse_mode: str | None = None,
    ) -> None:
        """Every reply is Telegram HTML: `Html` (built with `jarvis.bot.fmt`) is sent as is, a plain `str` is
        escaped (`parse_mode="HTML"` marks an unmarked string that is already markup)."""
        if self.c.telegram is None:
            raise NotConfiguredError("telegram not configured")
        body = fmt.Html(text_msg) if parse_mode == "HTML" else fmt.to_html(text_msg)
        if ack := PENDING_ACK.get():
            PENDING_ACK.set(None)
            if not self.persona.is_apology(fmt.plain(body)):  # «Извольте, сэр. Прошу прощения…» would be odd
                body = fmt.lines(fmt.i(ack), body)
        buffer = REPLY_BUFFER.get()
        if buffer is not None:  # several requests in one message: merged into one HTML reply later
            buffer.append((body, markup, speak and VOICE_OK.get()))
            return
        if speak and (spoken := SPEECH_COLLECT.get()) is not None:
            spoken.append(fmt.plain(body))
        voice = SPEAK_REPLIES.get()
        if speak and voice and VOICE_OK.get() and await self._reply_with_voice(chat_id, body, markup, voice):
            return
        await self.c.telegram.send_message(chat_id, body, reply_markup=markup, parse_mode="HTML")

    async def _reply_with_voice(
        self, chat_id: int, text_msg: fmt.Html, markup: dict[str, Any] | None, voice: str
    ) -> bool:
        """Voice note + the same text (as the caption when it fits). Any failure → False → plain text reply."""
        assert self.c.telegram is not None
        speech = to_speech(fmt.plain(text_msg), self.c.settings.tts_max_chars)
        if not speech:
            return False
        # Buttons are never attached to a voice/audio message (their labels are unreadable there): a reply with
        # buttons, or one too long for a caption, goes out as text at once and the voice note follows.
        fits = len(text_msg) <= MAX_CAPTION - 24 and not markup
        if not fits:
            await self.c.telegram.send_message(chat_id, text_msg, reply_markup=markup, parse_mode="HTML")
        await self.c.telegram.send_chat_action(chat_id, "record_voice")
        caption, buttons = (text_msg, markup) if fits else (None, None)
        mode = "HTML" if caption else None
        # Any failure → False: a short text is then sent as plain text by the caller (a long one already went out).
        try:
            audio = await self.c.tts.synthesize(speech, voice=voice)
        except Exception as exc:  # budget, provider outage, unknown voice
            await self._voice_failed("tts", exc)
            return not fits
        try:
            await self.c.telegram.send_voice(
                chat_id,
                audio.data,
                filename=audio.filename,
                mime_type=audio.mime_type,
                caption=caption,
                reply_markup=buttons,
                parse_mode=mode,
            )
            return True
        except Exception as exc:
            await self._voice_failed("send_voice", exc)
            if not voice_forbidden(exc):
                return not fits
        # The user's Telegram privacy settings block voice messages from the bot; an audio file is not blocked.
        try:
            await self.c.telegram.send_audio(
                chat_id,
                audio.data,
                filename=audio.filename,
                mime_type=audio.mime_type,
                caption=caption,
                reply_markup=buttons,
                parse_mode=mode,
            )
        except Exception as exc:
            await self._voice_failed("send_audio", exc)
            return not fits
        await self._voice_forbidden_hint(chat_id)
        return True

    async def _voice_failed(self, step: str, exc: Exception) -> None:
        """Logged and kept in the audit log (survives restarts; shown by /voice)."""
        log.warning("voice reply failed at %s: %s: %s", step, type(exc).__name__, exc)
        with contextlib.suppress(Exception):
            async with self.c.db.session() as s:
                await audit(
                    s,
                    now=self.c.clock.now(),
                    actor="jarvis",
                    action=A.VOICE_REPLY_FAILED,
                    result="error",
                    details={"step": step, "error": f"{type(exc).__name__}: {exc}"[:300]},
                )

    async def _voice_forbidden_hint(self, chat_id: int) -> None:
        """Once: explain how to let the bot's voice notes through the Telegram privacy settings."""
        async with self.c.db.session() as s:
            pref = await s.get(UserPreference, VOICE_PREF_KEY)
            if pref is not None and pref.value.get("forbidden_hint_sent"):
                return
        await self._save_voice_settings(forbidden_hint_sent=self.c.clock.now().date().isoformat())
        assert self.c.telegram is not None
        await self.c.telegram.send_message(chat_id, VOICE_FORBIDDEN_HINT)

    async def voice_settings(self) -> tuple[bool, str]:
        """(voice replies on?, voice name): the chat toggle overrides VOICE_REPLIES_ENABLED / TTS_VOICE."""
        s = self.c.settings
        async with self.c.db.session() as session:
            pref = await session.get(UserPreference, VOICE_PREF_KEY)
        value = pref.value if pref is not None else {}
        return bool(value.get("enabled", s.voice_replies_enabled)), str(value.get("voice") or s.tts_voice)

    async def mac_voice_enabled(self) -> bool:
        """Jarvis for Mac speaks every reply unless switched off there (independent of the Telegram toggle)."""
        async with self.c.db.session() as session:
            pref = await session.get(UserPreference, VOICE_PREF_KEY)
        value = pref.value if pref is not None else {}
        return bool(value.get("mac", self.c.settings.mac_voice_replies_enabled))

    async def _save_voice_settings(self, **changes: Any) -> None:
        async with self.c.db.session() as session:
            pref = await session.get(UserPreference, VOICE_PREF_KEY)
            if pref is None:
                session.add(UserPreference(key=VOICE_PREF_KEY, value=dict(changes)))
            else:
                pref.value = {**pref.value, **changes}

    def _tts_available(self) -> bool:
        return self.c.status.get("tts", "").startswith(("CONFIGURED", "IMPLEMENTED_MOCK_TESTED"))

    def authorized(self, user_id: int | None, chat: dict[str, Any] | None) -> bool:
        if user_id is None or user_id not in self.allowed:
            return False
        if chat is None:
            return True  # callback queries without message context are still bound to the user id
        return chat.get("type") == "private" and chat.get("id") == user_id

    async def _set_context(self, chat_id: int, **values: Any) -> None:
        async with self.c.db.session() as s:
            stmt = insert(ChatContext).values(chat_id=chat_id, **values)
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ChatContext.chat_id], set_={k: stmt.excluded[k] for k in values}
                )
            )

    async def _context(self, chat_id: int) -> ChatContext | None:
        async with self.c.db.session() as s:
            return await s.get(ChatContext, chat_id)

    async def handle_update(self, update: dict[str, Any]) -> str:
        now = self.c.clock.now()
        update_id = int(update.get("update_id", 0))
        async with self.c.db.session() as s:
            inserted = await s.execute(
                insert(TelegramUpdate)
                .values(update_id=update_id, received_at=now, status="received")
                .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
                .returning(TelegramUpdate.update_id)
            )
            if inserted.first() is None:
                return "duplicate"
        message = update.get("message")
        callback = update.get("callback_query")
        # Edited messages are only live-location updates for Jarvis: an edited text is never run again.
        edited = update.get("edited_message") if message is None and callback is None else None
        sender = (message or callback or edited or {}).get("from") or {}
        source = message or edited
        chat = (source or {}).get("chat") if source else ((callback or {}).get("message") or {}).get("chat")
        if not self.authorized(sender.get("id"), chat):
            async with self.c.db.session() as s:
                await audit(
                    s,
                    now=now,
                    actor=f"telegram:{sender.get('id')}",
                    action=A.TELEGRAM_UNAUTHORIZED,
                    result="denied",
                    details={"chat_type": (chat or {}).get("type")},
                )
            return "unauthorized"
        try:
            if callback is not None:
                await self.handle_callback(callback)
            elif message is not None:
                await self.handle_message(message)
            elif edited is not None and edited.get("location"):
                await self.handle_location(edited, edited=True)
            status = "processed"
        except JarvisError as exc:
            log.warning("bot error: %s", exc)
            chat_id = (chat or {}).get("id")
            if chat_id:
                await self.reply(chat_id, self.persona.sorry(f"⚠️ {self._friendly(exc)}"))
            status = "error"
        except Exception:
            log.exception("unexpected bot error")
            chat_id = (chat or {}).get("id")
            if chat_id:
                with contextlib.suppress(Exception):
                    await self.reply(chat_id, "⚠️ Внутренняя ошибка. Подробности в логах сервера.")
            status = "error"
        async with self.c.db.session() as s:
            row = await s.get(TelegramUpdate, update_id)
            if row is not None:
                row.status = status
        return status

    def _listening(self) -> str:
        """The message was only «Джарвис» / «Эй, Джарвис»."""
        return f"Слушаю, {self.persona.address}." if self.persona.enabled else "Слушаю."

    @staticmethod
    def _friendly(exc: Exception) -> str:
        if isinstance(exc, NotConfiguredError):
            return f"Интеграция не настроена: {exc}"
        return f"Не получилось: {exc}"

    # --- messages ----------------------------------------------------------------------------------

    async def handle_message(self, message: dict[str, Any]) -> None:
        enabled, voice = await self.voice_settings()
        token = SPEAK_REPLIES.set(voice if enabled and self._tts_available() else None)
        try:
            await self._handle_message(message)
        finally:
            SPEAK_REPLIES.reset(token)
        await self._ensure_keyboard(int(message["chat"]["id"]))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        forwarded = any(message.get(key) for key in FORWARD_KEYS)
        if message.get("voice"):
            await self.handle_voice(message, forwarded=forwarded)
            return
        if message.get("photo") or str((message.get("document") or {}).get("mime_type") or "").startswith("image/"):
            await self.handle_photo(message)
            return
        if message.get("location"):
            await self.handle_location(message)
            return
        said = (message.get("text") or "").strip()
        if not said:
            await self.reply(chat_id, "Я понимаю текст, голосовые и фото (еда, этикетки, чеки).")
            return
        text_in = strip_address(said)  # «Джарвис, …»: the address is not part of the request (nor of a note)
        if not text_in:
            await self.reply(chat_id, self._listening())
            return
        if forwarded:
            # Someone else's words must not run commands (a forwarded «удали все задачи» or «/done …», R31): offer to
            # save them. Checked before /commands and voice notes too (photos only ever make drafts with buttons).
            await self.cmd_inbox_ask(
                chat_id,
                RoutedIntent(intent=Intent.UNKNOWN),
                text_in,
                message,
                note="↪️ Пересланное сообщение — как команду я его не выполняю.",
            )
            return
        if text_in.startswith("/"):
            await self.handle_command(chat_id, text_in, message)
            return
        if text_in == SETTINGS_BUTTON:
            await self.cmd_settings(chat_id)
            return
        if (pressed := menu_intent(text_in)) is not None:  # a reply-keyboard button: exact, no LLM
            await self.dispatch(chat_id, pressed, text_in, message)
            return
        if (
            await self._answer_pending_due(chat_id, text_in)
            or await self._answer_pending_project(chat_id, text_in)
            or await self._answer_pending_fill(chat_id, text_in)
        ):
            return
        actions = await self._route(text_in)
        await self.dispatch_all(chat_id, actions, text_in, message)

    async def handle_location(self, message: dict[str, Any], *, edited: bool = False) -> None:
        """A shared location (📎 or a request_location button) or a live-location update → the travel origin."""
        if any(message.get(key) for key in FORWARD_KEYS):  # someone else's point is not where the user is
            if not edited:
                await self.reply(int(message["chat"]["id"]), FORWARDED_LOCATION, speak=False)
            return
        if answer := await self.travel_desk.on_location(message, edited=edited):
            text_msg, rows = answer
            await self.reply(int(message["chat"]["id"]), text_msg, inline_keyboard(rows) if rows else None, speak=False)

    async def handle_voice(self, message: dict[str, Any], *, forwarded: bool = False) -> None:
        chat_id = int(message["chat"]["id"])
        voice = message["voice"]
        if self.c.telegram is None:
            raise NotConfiguredError("telegram not configured")
        audio = await self.c.telegram.download_file(voice["file_id"])
        digest = hashlib.sha256(audio).hexdigest()
        async with self.c.db.session() as s:
            stmt = insert(RawMedia).values(
                kind="voice",
                mime_type=voice.get("mime_type") or "audio/ogg",
                size_bytes=len(audio),
                sha256=digest,
                data=audio,
                telegram_file_unique_id=voice.get("file_unique_id"),
            )
            result = await s.execute(
                stmt.on_conflict_do_nothing(index_elements=[RawMedia.telegram_file_unique_id]).returning(RawMedia.id)
            )
            row = result.first()
            media_id = (
                row[0]
                if row
                else await s.scalar(
                    select(RawMedia.id).where(RawMedia.telegram_file_unique_id == voice.get("file_unique_id"))
                )
            )
        try:
            transcript = await self.c.stt.transcribe(audio, voice.get("mime_type") or "audio/ogg")
        except (NotConfiguredError, PermanentError, TransientError) as exc:
            # Keep the original audio as an idea so nothing is lost (not configured, budget, provider outage).
            log.warning("voice note not transcribed: %s", exc)
            async with self.c.db.session() as s:
                idea, _ = await self.c.ideas.capture(
                    s,
                    raw_input="[голосовое сообщение без расшифровки]",
                    source="telegram_voice",
                    now=self.c.clock.now(),
                    chat_id=chat_id,
                    message_id=message.get("message_id"),
                    raw_media_id=media_id,
                )
            reason = (
                "распознавание речи не настроено" if isinstance(exc, NotConfiguredError) else "расшифровать не вышло"
            )
            await self.reply(chat_id, f"🎙 Аудио сохранено как идея, но {reason}. Попробуй ещё раз или напиши текстом.")
            return
        heard = strip_address(transcript.text)
        if not heard:
            await self.reply(chat_id, self._listening())
            return
        if forwarded:  # someone else's voice note: transcribed, offered to be saved, never run as a command (R31)
            await self.cmd_inbox_ask(
                chat_id,
                RoutedIntent(intent=Intent.UNKNOWN),
                heard,
                message,
                note="↪️ Пересланное голосовое — как команду я его не выполняю.",
            )
            return
        if (
            await self._answer_pending_due(chat_id, heard)
            or await self._answer_pending_project(chat_id, heard)
            or await self._answer_pending_fill(chat_id, heard)
        ):
            return
        actions = await self._route(heard)
        routed = actions[0]
        if len(actions) == 1 and routed.intent in (Intent.UNKNOWN, Intent.HELP):
            routed = RoutedIntent(intent=Intent.CAPTURE_IDEA, idea_text=heard)
        if len(actions) == 1 and routed.intent == Intent.CAPTURE_IDEA:
            async with self.c.db.session() as s:
                idea, _created = await self.c.ideas.capture(
                    s,
                    raw_input=strip_address(routed.idea_text or heard),
                    transcript=transcript.text,
                    source="telegram_voice",
                    now=self.c.clock.now(),
                    chat_id=chat_id,
                    message_id=message.get("message_id"),
                    raw_media_id=media_id,
                )
            await self._set_context(chat_id, last_idea_id=idea.id)
            await self.reply(chat_id, f"🎙 «{transcript.text.strip(' «»"')[:500]}»", speak=False)
            voice_token = VOICE_OK.set(True)
            try:
                await self.reply(chat_id, "💡 Идея записана, анализирую…")
            finally:
                VOICE_OK.reset(voice_token)
            return
        await self.reply(chat_id, f"🎙 «{transcript.text.strip(' «»"')[:300]}»", speak=False)
        await self.dispatch_all(chat_id, actions if len(actions) > 1 else [routed], heard, message)

    async def handle_photo(self, message: dict[str, Any]) -> None:
        """A photo (or an image sent as a file): a food label / a dish → a КБЖУ draft, a receipt → an expense draft.
        One LLM call reads it; nothing is written before the confirmation button."""
        chat_id = int(message["chat"]["id"])
        if message.get("photo"):
            photo, mime = message["photo"][-1], "image/jpeg"  # the largest size Telegram made
        else:
            photo = message["document"]
            mime = str(photo.get("mime_type") or "image/jpeg")
        if int(photo.get("file_size") or 0) > MAX_PHOTO_BYTES:
            await self.reply(chat_id, "📷 Фото слишком большое (больше 8 МБ) — пришлите обычным фото, не файлом.")
            return
        if self.c.telegram is None:
            raise NotConfiguredError("telegram not configured")
        if self.c.llm is None:
            await self.reply(chat_id, "📷 Фото я понимаю только с ИИ, а он не настроен (OPENROUTER_API_KEY).")
            return
        caption = (message.get("caption") or "").strip()
        with contextlib.suppress(Exception):
            await self.c.telegram.send_chat_action(chat_id, "typing")
        image = await self.c.telegram.download_file(photo["file_id"])
        await self._store_media("photo", mime, image, photo.get("file_unique_id"))
        try:
            reading = await read_photo(self.c.llm, image, mime, caption)
        except (LLMOutputError, NotConfiguredError, TransientError, PermanentError) as exc:
            log.warning("photo not read: %s", exc)
            reason = "ИИ не настроен" if isinstance(exc, NotConfiguredError) else "ИИ не ответил как надо"
            await self.reply(chat_id, self.persona.sorry(f"Не смог разобрать фото ({reason}). Напишите словами."))
            return
        if reading.kind in (PhotoKind.FOOD_LABEL, PhotoKind.DISH):
            await self.cmd_food_photo(chat_id, reading, caption)
            return
        if reading.kind == PhotoKind.RECEIPT:
            await self.cmd_receipt_photo(chat_id, reading, caption, photo.get("file_unique_id"))
            return
        seen = f"Вижу: {reading.note.strip()[:200]}. " if reading.note else ""
        await self.reply(chat_id, f"📷 {seen}Я понимаю фото еды, этикеток и чеков.")

    async def _store_media(self, kind: str, mime: str, data: bytes, unique_id: str | None) -> None:
        async with self.c.db.session() as s:
            await s.execute(
                insert(RawMedia)
                .values(
                    kind=kind,
                    mime_type=mime[:60],
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    data=data,
                    telegram_file_unique_id=unique_id,
                )
                .on_conflict_do_nothing(index_elements=[RawMedia.telegram_file_unique_id])
            )

    async def cmd_food_photo(self, chat_id: int, reading: PhotoReading, caption: str) -> None:
        prepared = await self._tracker_call(
            chat_id,
            self.c.tracker.prepare_food_photo(reading, caption, self.c.clock.now()),
            lambda: self.c.tracker.prepare_food_photo(reading, caption, self.c.clock.now()),
        )
        if prepared is not None:
            await self._tracker_next(chat_id, prepared)

    async def cmd_receipt_photo(
        self, chat_id: int, reading: PhotoReading, caption: str, media_ref: str | None = None
    ) -> None:
        """A receipt read from a photo → an expense draft; written only after «✅ Записать» (the LLM read it)."""
        if reading.receipt is None:
            await self.reply(chat_id, "🧾 Похоже на чек, но я не разобрал его. Скажите словами: «потратил 45 лари …».")
            return
        now = self.c.clock.now()
        draft = self.c.finance.receipt_draft(reading.receipt, caption, now)
        if isinstance(draft, str):
            await self.reply(chat_id, draft)
            return
        async with self.c.db.session() as s:
            approval = await request_approval(
                s,
                kind="expense_add",
                target_type="expense",
                target_id=draft["day"],
                now=now,
                ttl=timedelta(days=2),
                payload={"draft": draft, "chat_id": chat_id, "media_ref": media_ref},
            )
        keyboard = approval_keyboard(approval.nonce, "✅ Записать", "✖️ Отмена")
        await self.reply(chat_id, self.c.finance.receipt_text(draft, now), keyboard)

    # --- researcher ----------------------------------------------------------------------------------------

    async def cmd_research(self, chat_id: int, question: str) -> None:
        """«изучи ВНЖ Грузии» → the price first; the paid web search runs only after «🔎 Искать»."""
        question = question.strip(" ?.!")[:300]
        if not question:
            await self.reply(chat_id, "🔎 Что изучить? Например: «изучи ВНЖ Грузии».")
            return
        if not self.c.research.available:
            await self.reply(chat_id, "🔎 Поиск в интернете не подключён: нужен ключ OpenRouter (RESEARCH_ENABLED).")
            return
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            approval = await request_approval(
                s,
                kind="research",
                target_type="research",
                target_id=hashlib.sha256(question.encode()).hexdigest()[:16],
                now=now,
                ttl=timedelta(days=1),
                payload={"question": question, "chat_id": chat_id},
            )
        keyboard = approval_keyboard(approval.nonce, "🔎 Искать", "✖️ Отмена")
        await self.reply(chat_id, self.c.research.offer_text(question), keyboard)

    # --- finance -------------------------------------------------------------------------------------------

    async def cmd_expense_add(self, chat_id: int, text: str, message: dict[str, Any]) -> None:
        """«потратил 45 лари на такси» — parsed by code, recorded at once, «↩️ Отменить» under the answer."""
        from jarvis.finance.parse import parse_expense, spent_day

        parsed = parse_expense(text, self.c.settings.finance_default_currency)
        if parsed is None or parsed.error or not parsed.items:
            hint = parsed.error if parsed is not None and parsed.error else "Не понял сумму."
            await self.reply(chat_id, f"💸 {hint} Например: «потратил 45 лари на такси».")
            return
        now = self.c.clock.now()
        day = spent_day(parsed.day_word, self.c.finance.today(now))
        if day is None:
            await self.reply(chat_id, f"💸 Не понял дату «{parsed.day_word}».")
            return
        async with self.c.db.session() as s:
            rows = await self.c.finance.add(
                s,
                parsed.items,
                day,
                source="voice" if message.get("voice") else "text",
                source_ref=self._request_id(chat_id, message),
            )
            answer = await self.c.finance.added_text(s, rows, now)
        undo = inline_keyboard([[InlineButton("↩️ Отменить", f"ex:{rows[0].id.hex}")]])
        await self.reply(chat_id, answer, undo)

    async def cmd_expense_show(self, chat_id: int, text: str) -> None:
        from jarvis.finance.service import parse_spend_query

        now = self.c.clock.now()
        query = parse_spend_query(text, self.c.finance.today(now))
        async with self.c.db.session() as s:
            answer = await self.c.finance.period_text(s, query, now)
        await self.reply(chat_id, answer)

    async def cmd_expense_remove(self, chat_id: int, text: str) -> None:
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            expense = await self.c.finance.find_for_removal(s, text, now)
            if expense is None:
                await self.reply(chat_id, "💸 Такого расхода за последние 60 дней не нашёл.")
                return
            approval = await request_approval(
                s,
                kind="expense_delete",
                target_type="expense",
                target_id=str(expense.id),
                now=now,
                ttl=timedelta(days=1),
                payload={"expense_id": str(expense.id), "chat_id": chat_id},
            )
            line = self.c.finance.line(expense, with_day=True, today=self.c.finance.today(now))
        keyboard = approval_keyboard(approval.nonce, "🗑 Удалить", "✖️ Отмена")
        await self.reply(chat_id, f"🗑 Удалить расход?\n{line}", keyboard)

    async def _reminder_answer(
        self, chat_id: int, reminder_id: uuid.UUID, snooze: bool, callback: dict[str, Any]
    ) -> str:
        """«👌 Понял» stops the repeats; «⏰ 10 мин» stops them and reminds once more in 10 minutes."""
        from jarvis.reminders.escalation import acknowledge

        async with self.c.db.session() as s:
            reminder = await acknowledge(s, reminder_id, self.c.clock.now(), snooze=snooze)
        if reminder is None:
            return "Напоминание уже неактуально"
        message_id = (callback.get("message") or {}).get("message_id")
        if message_id and self.c.telegram is not None:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
        return "⏰ Напомню через 10 минут" if snooze else "👌 Принято, больше не напоминаю"

    async def _undo_expense(self, chat_id: int, expense_id: uuid.UUID) -> str:
        async with self.c.db.session() as s:
            rows = await self.c.finance.undo(s, expense_id, self.c.clock.now())
        if not rows:
            return "Уже отменено"
        await self.reply(chat_id, "↩️ Отменил: " + ", ".join(r.description for r in rows))
        return "Отменено"

    async def handle_command(self, chat_id: int, text_in: str, message: dict[str, Any]) -> None:
        command, _, arg = text_in.partition(" ")
        command = command.split("@")[0].lower()
        arg = arg.strip()
        mapping: dict[str, RoutedIntent] = {
            "/start": RoutedIntent(intent=Intent.HELP),
            "/help": RoutedIntent(intent=Intent.HELP),
            "/today": RoutedIntent(intent=Intent.SHOW_TODAY),
            "/agenda": RoutedIntent(intent=Intent.SHOW_AGENDA, date_text=arg or None),
            "/done": RoutedIntent(intent=Intent.COMPLETE_TASK, task_text=arg or None),
            "/note": RoutedIntent(intent=Intent.CAPTURE_NOTE, note_text=arg or None),
            "/notes": RoutedIntent(intent=Intent.SHOW_NOTES, query=arg or None),
            "/tasks": RoutedIntent(intent=Intent.SHOW_TASKS),
            "/plan": RoutedIntent(intent=Intent.PLAN),
            "/leave": RoutedIntent(intent=Intent.LEAVE_TIME),
            "/home": RoutedIntent(intent=Intent.ORIGIN_RESET),
            "/status": RoutedIntent(intent=Intent.STATUS),
            "/spec": RoutedIntent(intent=Intent.IDEA_TO_SPEC),
            "/dev": RoutedIntent(intent=Intent.START_DEVELOPMENT, repo_id=arg or None),
            "/idea": RoutedIntent(intent=Intent.CAPTURE_IDEA, idea_text=arg or None),
            "/ideas": RoutedIntent(intent=Intent.SEARCH_IDEAS, query=arg or None),
            "/tlt": RoutedIntent(intent=Intent.TRACKER_SHOW, query=f"tlt {arg}".strip()),
            "/tpl": RoutedIntent(intent=Intent.TPL_SHOW, query=f"tpl {arg}".strip()),
            "/food": RoutedIntent(intent=Intent.FOOD_SHOW, query=f"еда {arg}".strip()),
            "/money": RoutedIntent(intent=Intent.EXPENSE_SHOW, query=f"расходы {arg}".strip()),
            "/health": RoutedIntent(intent=Intent.HEALTH_SHOW, query=arg or None),
            "/labs": RoutedIntent(intent=Intent.LABS_SHOW, query=arg or None),
        }
        if command == "/start":
            await self.cmd_start(chat_id)
            return
        if command == "/tlt" and not arg:
            await self.dispatch(chat_id, RoutedIntent(intent=Intent.DIARY), arg, message)
            return
        if command == "/menu":
            await self.cmd_menu(chat_id)
            return
        if command == "/shop":
            await self.cmd_shop(chat_id, arg)
            return
        if command == "/sync":
            await self.cmd_sync(chat_id)
            return
        if command == "/voice":
            await self.cmd_voice(chat_id, arg)
            return
        routed = mapping.get(command)
        if routed is None:
            await self.reply(chat_id, "Неизвестная команда. /help")
            return
        await self.dispatch(chat_id, routed, arg, message)

    async def help_text(self) -> fmt.Html:
        s = self.c.settings
        enabled, voice = await self.voice_settings()
        state = f"вкл., голос {voice}" if enabled and self._tts_available() else "выкл."
        sections: list[object] = [
            fmt.lines(
                fmt.title("🤖", "JARVIS — что я умею"),
                fmt.hint(
                    "Пишите или говорите как человеку — можно несколько просьб сразу. Непонятное разложу сам на "
                    "задачу, идею, покупку или заметку, а если не уверен — спрошу кнопками."
                ),
            ),
            fmt.lines(fmt.head("Кнопки внизу", "⌨️"), " · ".join(label for row in MAIN_MENU for label in row)),
        ]
        sections += [
            fmt.lines(fmt.head(name, emoji), fmt.quote(fmt.lines(*examples), expandable=True))
            for emoji, name, examples in HELP_SECTIONS
        ]
        sections.append(
            fmt.lines(
                fmt.head("Каждый день", "⏰"),
                fmt.bullet(f"{s.briefing_morning_at} — брифинг, погода и план дня"),
                fmt.bullet(f"{s.briefing_evening_at} — итоги дня"),
                fmt.bullet("о смене погоды предупрежу заранее"),
                fmt.line("Голосовые ответы:", fmt.b(state)),
            )
        )
        sections.append(fmt.hint(f"Команды: {HELP_COMMANDS}"))
        return fmt.blocks(*sections)

    def _inbox_doubt(self, routed: RoutedIntent, raw: str) -> bool:
        """AI Inbox: ask with buttons instead of guessing."""
        if routed.intent == Intent.UNKNOWN:
            # The LLM said "not something to save" → trust it; without a (working) LLM we cannot tell → ask.
            return routed.source in ("no_llm", "llm_failed", "llm_timeout") and not raw.rstrip().endswith("?")
        if routed.intent == Intent.CAPTURE_NOTE and routed.confidence == "low":
            return True  # «запомни код домофона 1234»: a note only after the «📝 Заметка» button
        if routed.intent.value not in INBOX_INTENTS or routed.source != "llm":
            return False
        if routed.confidence == "low":
            return True
        return routed.intent in (Intent.ADD_TASK, Intent.REMIND) and not (routed.task_text or "").strip()

    async def _route(self, text: str) -> list[RoutedIntent]:
        return await route_message(
            text,
            self.c.llm,
            await self._open_task_titles(),
            self._llm_context,
            timeout=(
                self.c.settings.call_router_timeout_seconds
                if VOICE_CALL.get()
                else self.c.settings.llm_router_timeout_seconds
            ),
        )

    async def _open_task_titles(self) -> list[str]:
        """Titles only (no ids) — context for the LLM router; the task itself is always resolved in SQL."""
        async with self.c.db.session() as s:
            rows = await s.scalars(
                select(TodoistTask.content)
                .where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
                .order_by(TodoistTask.due_date.nulls_last(), TodoistTask.content)
                .limit(60)
            )
            return list(rows.all())

    async def dispatch_all(self, chat_id: int, actions: list[RoutedIntent], raw: str, message: dict[str, Any]) -> None:
        """Several requests in one message: run each in order, answer with one message (texts + all buttons)."""
        if len(actions) == 1:
            await self.dispatch(chat_id, actions[0], raw, message)
            return
        token = REPLY_BUFFER.set([])
        try:
            for index, action in enumerate(actions):
                # Each part gets its own idempotency key (two tasks from one message are two tasks).
                part = message if index == 0 else {**message, "message_id": None, "_part": index}
                part.setdefault("_origin_id", message.get("message_id"))
                try:
                    await self.dispatch(chat_id, action, action.text or raw, part, ack=index == 0)
                except JarvisError as exc:
                    log.warning("bot error in part %s: %s", index, exc)
                    await self.reply(chat_id, self.persona.sorry(f"⚠️ {self._friendly(exc)}"))
            collected = REPLY_BUFFER.get() or []
        finally:
            REPLY_BUFFER.reset(token)
        rows = [row for _, markup, _ in collected if markup for row in markup.get("inline_keyboard", [])]
        voice_token = VOICE_OK.set(any(speak for _, _, speak in collected))
        try:
            await self.reply(
                chat_id,
                fmt.blocks(*(text_part for text_part, _, _ in collected)),  # every part is HTML already
                {"inline_keyboard": rows} if rows else None,
            )
        finally:
            VOICE_OK.reset(voice_token)

    @staticmethod
    def _request_id(chat_id: int, message: dict[str, Any]) -> str:
        origin = message.get("message_id") or message.get("_origin_id") or uuid.uuid4().hex
        part = message.get("_part")
        return f"tg-{chat_id}-{origin}" + (f"-{part}" if part else "")

    async def dispatch(
        self, chat_id: int, routed: RoutedIntent, raw: str, message: dict[str, Any], *, ack: bool = True
    ) -> None:
        intent = routed.intent
        if raw.strip().lower().strip(" .!") in SMALL_TALK and intent == Intent.UNKNOWN and not routed.reply:
            await self.reply(chat_id, "👌 Если нужно — /help")
            return
        if raw.strip() and self._inbox_doubt(routed, raw):
            said = routed.note_text if intent == Intent.CAPTURE_NOTE and routed.note_text else raw  # «запомни X» → X
            await self.cmd_inbox_ask(chat_id, routed, said, message)
            return
        first_line = None
        if ack and intent in ACK_INTENTS:
            first_line = self.persona.ack()
        elif ack and intent in QUERY_INTENTS:
            first_line = self.persona.query()
        token = PENDING_ACK.set(first_line)
        talk = intent in VOICE_INTENTS or (intent == Intent.UNKNOWN and bool(routed.reply))
        voice_token = VOICE_OK.set(talk)
        try:
            await self._dispatch(chat_id, routed, raw, message)
        finally:
            PENDING_ACK.reset(token)
            VOICE_OK.reset(voice_token)

    async def _dispatch(self, chat_id: int, routed: RoutedIntent, raw: str, message: dict[str, Any]) -> None:
        intent = routed.intent
        handlers = {
            Intent.SHOW_FOCUS: lambda: self.cmd_focus(chat_id),
            Intent.SUGGEST_NEXT: lambda: self.cmd_suggest_next(chat_id),
            Intent.SEARCH_ALL: lambda: self.cmd_search(chat_id, routed.query or raw),
            Intent.SMALL_TALK: lambda: self.cmd_small_talk(chat_id, routed, raw),
            Intent.ASK: lambda: self.cmd_ask(chat_id, routed, raw),
            Intent.WEATHER: lambda: self.cmd_weather(chat_id),
            Intent.CALCULATE: lambda: self.cmd_calculate(chat_id, routed.query or raw),
            Intent.CONVERT_CURRENCY: lambda: self.cmd_currency(chat_id, routed.query or raw),
            Intent.TRACKER_LOG: lambda: self.cmd_tracker_log(chat_id, raw or routed.query or ""),
            Intent.DIARY: lambda: self.cmd_diary(chat_id),
            Intent.DIARY_FILL: lambda: self.cmd_diary_fill(chat_id, routed.query or raw),
            Intent.MENU: lambda: self.cmd_more(chat_id),
            Intent.TRACKER_SHOW: lambda: self.cmd_tracker_show(chat_id, routed.query or raw),
            Intent.HEALTH_SHOW: lambda: self.cmd_health(chat_id, routed.query or raw),
            Intent.LABS_SHOW: lambda: self.cmd_labs(chat_id, routed.query),
            Intent.CALL_ME: lambda: self.cmd_call_me(chat_id, routed.query or ""),
            Intent.TPL_MARK: lambda: self.cmd_tpl_mark(chat_id, routed.query or raw),
            Intent.TPL_SHOW: lambda: self.cmd_tpl_show(chat_id, routed.query or raw),
            Intent.MED_ADD: lambda: self.cmd_med_add(chat_id, routed),
            Intent.MED_LIST: lambda: self.cmd_meds(chat_id),
            Intent.MED_REMOVE: lambda: self.cmd_med_remove(chat_id, routed.task_text),
            Intent.MED_EXTEND: lambda: self.cmd_med_extend(chat_id, routed.task_text, routed.query),
            Intent.MED_TAKEN: lambda: self.cmd_med_taken(chat_id, routed.task_text or routed.query, routed.date_text),
            Intent.MED_PACK: lambda: self.cmd_med_pack(chat_id, routed.query or raw),
            Intent.MED_PORTION: lambda: self.cmd_med_portion(chat_id, routed.query or raw),
            Intent.SITE_OPEN: lambda: self.cmd_site_open(chat_id, routed.app_text or routed.query or raw),
            Intent.SITE_STOP: lambda: self.cmd_site_stop(chat_id, routed.app_text or routed.query or raw),
            Intent.SITES_LIST: lambda: self.cmd_sites_list(chat_id),
            Intent.TUNNEL_CLOSE: lambda: self.cmd_tunnel_close(chat_id, routed.app_text),
            Intent.FOOD_LOG: lambda: self.cmd_food_log(chat_id, raw or routed.query or ""),
            Intent.FOOD_SHOW: lambda: self.cmd_food_show(chat_id, routed.query or raw),
            Intent.FOOD_REMOVE: lambda: self.cmd_food_remove(chat_id, routed.query or raw),
            Intent.EXPENSE_ADD: lambda: self.cmd_expense_add(chat_id, routed.query or raw, message),
            Intent.RESEARCH: lambda: self.cmd_research(chat_id, routed.query or raw),
            Intent.MAC_VOLUME: lambda: self.cmd_mac_volume(chat_id, routed.query or raw),
            Intent.MAC_COPY: lambda: self.cmd_mac_copy(chat_id, routed.query or raw),
            Intent.MAC_REMIND: lambda: self.cmd_mac_remind(chat_id, routed.query or raw, message),
            Intent.EXPENSE_SHOW: lambda: self.cmd_expense_show(chat_id, routed.query or raw),
            Intent.EXPENSE_REMOVE: lambda: self.cmd_expense_remove(chat_id, routed.query or raw),
            Intent.FOOD_PRODUCT: lambda: self._tracker_reply(
                chat_id, self.c.tracker.save_product_text(routed.query or raw)
            ),
            Intent.FOOD_TARGETS: lambda: self._tracker_reply(
                chat_id, self.c.tracker.set_targets_text(routed.query or raw)
            ),
            Intent.SHOW_USAGE: lambda: self.cmd_usage(chat_id),
            Intent.HELP: lambda: self.cmd_help(chat_id),
            Intent.VOICE_MODE: lambda: self.cmd_voice(chat_id, "on" if routed.voice_on else "off"),
            Intent.SHOW_TODAY: lambda: self.cmd_today(chat_id),
            Intent.SHOW_AGENDA: lambda: self.cmd_agenda(chat_id, routed.date_text),
            Intent.COMPLETE_TASK: lambda: self.cmd_task_op(chat_id, "complete", routed, message),
            Intent.DELETE_TASK: lambda: self.cmd_task_op(chat_id, "delete", routed, message),
            Intent.SET_PRIORITY: lambda: self.cmd_task_op(chat_id, "priority", routed, message),
            Intent.BULK_TASKS: lambda: self.cmd_bulk(chat_id, routed),
            Intent.CAPTURE_NOTE: lambda: self.cmd_note(chat_id, routed.note_text or raw, message),
            Intent.SHOW_NOTES: lambda: self.cmd_notes(chat_id, routed.query),
            Intent.SHOW_TASKS: lambda: self.cmd_tasks(chat_id),
            Intent.SHOW_SUBTASKS: lambda: self.cmd_subtasks(chat_id, routed.task_text),
            Intent.PLAN: lambda: self.cmd_plan(chat_id),
            Intent.LEAVE_TIME: lambda: self.cmd_leave(chat_id),
            Intent.ORIGIN_RESET: lambda: self.cmd_origin_reset(chat_id),
            Intent.STATUS: lambda: self.cmd_status(chat_id),
            Intent.ADD_TASK: lambda: self.cmd_add_task(chat_id, routed, message),
            Intent.REMIND: lambda: self.cmd_add_task(chat_id, routed, message),
            Intent.SCHEDULE_REFERENCED_TASK: lambda: self.cmd_schedule_referenced(chat_id, routed, message),
            Intent.CAPTURE_IDEA: lambda: self.cmd_idea(chat_id, routed.idea_text or raw, message),
            Intent.IDEA_TO_SPEC: lambda: self.cmd_spec(chat_id),
            Intent.START_DEVELOPMENT: lambda: self.cmd_dev(chat_id, routed.repo_id),
            Intent.SEARCH_IDEAS: lambda: self.cmd_ideas(chat_id, routed.query),
            Intent.SHOPPING_ADD: lambda: self.cmd_shop(chat_id, "add " + (routed.shopping_text or raw)),
            Intent.SHOPPING_SHOW: lambda: self.cmd_shop(chat_id, ""),
            Intent.SHOPPING_QUOTE: lambda: self.cmd_shop(chat_id, "quote"),
            Intent.SHOPPING_TRIP: lambda: self.cmd_shopping_trip(chat_id),
            Intent.MAC_OPEN: lambda: self.cmd_mac_open(chat_id, routed.app_text),
            Intent.LAB_OPEN: lambda: self.cmd_lab_open(chat_id, routed.query or ""),
            Intent.OS_OPEN: lambda: self.cmd_os_open(chat_id, routed.query or "/"),
            Intent.PROFILE_REMEMBER: lambda: self.cmd_profile_remember(chat_id, routed.note_text or ""),
            Intent.PROFILE_SHOW: lambda: self.cmd_profile_show(chat_id),
            Intent.PROFILE_FORGET: lambda: self.cmd_profile_forget(chat_id, routed.query or ""),
            Intent.CLAUDE_CODE: lambda: self.cmd_claude_code(chat_id, routed),
            Intent.SHOPPING_FROM_TASKS: lambda: self.cmd_shopping_from_tasks(chat_id),
        }
        handler = handlers.get(intent)
        if handler is None:
            if routed.reply:  # the LLM answered a question / small talk (facts only from the given context)
                await self.reply(chat_id, routed.reply.strip()[:1500])
                return
            if routed.source == "llm_timeout":  # honest: the question needed the LLM and it was too slow (R27)
                await self.reply(
                    chat_id,
                    self.persona.sorry(
                        "ИИ сейчас отвечает слишком долго, а без него я этот вопрос не понял. "
                        "Спросите ещё раз чуть позже или короче — команды в /help."
                    ),
                )
                return
            await self.reply(chat_id, self.persona.sorry("Не понял запрос. Попробуй /help или «Запиши идею …»."))
            return
        await handler()

    # --- tasks & calendar --------------------------------------------------------------------------

    async def _fresh_tasks(self) -> None:
        """Before answering about tasks: pull Todoist when the cache is older than a minute, so a task completed in
        the Todoist app or via ChatGPT a moment ago is not shown (there is no webhook; polling runs every 2 min)."""
        if self.c.todoist_sync is None:
            return
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            last = await s.scalar(
                select(IntegrationSyncState.last_success_at).where(
                    IntegrationSyncState.provider == "todoist", IntegrationSyncState.resource == "tasks"
                )
            )
        if last is not None and now - last < timedelta(seconds=60):
            return
        try:
            async with self.c.db.session() as s:
                await self.c.todoist_sync.reconcile(s, now)
        except Exception as exc:  # answer from the cache rather than fail
            log.warning("on-demand Todoist sync failed: %s", exc)

    async def cmd_today(self, chat_id: int) -> None:
        await self._fresh_tasks()
        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        async with self.c.db.session() as s:
            text_msg = await self.c.agenda.agenda_text(
                s, Period(today, today), now, calendar_connected=calendar_connected(self.c.status)
            )
            leave = await self._next_leave(s, now)
        head, _, rest = text_msg.partition("\n")  # the agenda's lines never share a tag: safe to cut
        weather = None
        if forecast := await self.c.weather.forecast():  # the weather comes with today's tasks, under the title
            weather = self.c.weather.today_text(forecast, now)
        attention, rows = await self._attention()
        text_out = fmt.blocks(fmt.lines(fmt.Html(head), weather), fmt.Html(rest.strip("\n")), leave, attention or None)
        await self.reply(chat_id, text_out, inline_keyboard(rows) if rows else None)

    async def cmd_agenda(self, chat_id: int, date_text: str | None) -> None:
        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        if date_text and date_text.strip():
            period = parse_period(date_text, today)
            if period is None:
                await self.reply(
                    chat_id,
                    f"Не понял дату «{date_text}». Примеры: завтра, в пятницу, 25.09, на неделе, на выходных, "
                    "с 25.09 по 30.09.",
                )
                return
        else:
            period = Period(today, today + timedelta(days=6))
        if period.single and period.start == today:
            await self.cmd_today(chat_id)
            return
        await self._fresh_tasks()
        async with self.c.db.session() as s:
            text_msg = await self.c.agenda.agenda_text(
                s, period, now, calendar_connected=calendar_connected(self.c.status)
            )
        await self.reply(chat_id, text_msg)

    async def _next_leave(self, s: Any, now: datetime) -> str | None:
        return await self.c.agenda.next_leave_text(s, now)

    async def cmd_leave(self, chat_id: int) -> None:
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            # From the current starting point (a fresh Telegram location wins over DEFAULT_ORIGIN).
            text_msg = await self.travel_desk.leave_text(s, now) or await self._next_leave(s, now)
            route = await self.travel_desk.route_markup(s, now) if text_msg else None  # «🧭 Маршрут в 2ГИС»
            if text_msg is None:
                upcoming = await s.scalar(
                    select(CalendarEvent)
                    .where(
                        CalendarEvent.start_at > now,
                        CalendarEvent.status != "cancelled",
                        CalendarEvent.all_day.is_(False),
                        CalendarEvent.jarvis_managed.is_(False),
                    )
                    .order_by(CalendarEvent.start_at)
                    .limit(1)
                )
                if upcoming is None:
                    text_msg = "Ближайших встреч нет."
                elif not upcoming.location:
                    start_local = format_local(upcoming.start_at, self.c.settings.tz)  # type: ignore[arg-type]
                    text_msg = f"Ближайшее: «{upcoming.summary}» в {start_local} — без адреса, время в пути не считаю."
                else:
                    text_msg = (
                        "Время в пути пока не рассчитано (нет ключа Maps или адрес не найден). "
                        "Я не буду угадывать — проверь дорогу сам."
                    )
        await self.reply(chat_id, text_msg, route)

    async def cmd_origin_reset(self, chat_id: int) -> None:
        await self.reply(chat_id, await self.travel_desk.forget())

    async def cmd_tasks(self, chat_id: int) -> None:
        await self._fresh_tasks()
        async with self.c.db.session() as s:
            tasks = (
                await s.scalars(
                    select(TodoistTask)
                    .where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
                    .order_by(TodoistTask.due_date.nulls_last(), TodoistTask.priority.desc(), TodoistTask.content)
                )
            ).all()
        if not tasks:
            await self.reply(chat_id, "Задач в кэше нет (Todoist не подключён или всё сделано).")
            return
        attention, rows = await self._attention()
        text_msg = fmt.blocks(
            fmt.title("📋", "Задачи", plural(len(tasks), "открыта", "открыты", "открыто")),
            self._task_tree(list(tasks)),
            attention or None,
        )
        await self.reply(chat_id, text_msg, inline_keyboard(rows) if rows else None)

    def _task_line(self, task: TodoistTask, *, bold: bool = False) -> fmt.Html:
        """«Купить сигару — <i>22.09</i> 🔁»."""
        due = ""
        if task.due_datetime:
            due = format_local(task.due_datetime, self.c.settings.tz, "%d.%m %H:%M")
        elif task.due_date:
            due = task.due_date.strftime("%d.%m")
        name = fmt.b(task.content) if bold else fmt.esc(task.content)
        return fmt.line(name, fmt.Html(f"— {fmt.i(due)}") if due else None, "🔁" if task.due_is_recurring else None)

    def _task_tree(self, tasks: list[TodoistTask], max_roots: int = 40, max_children: int = 15) -> list[fmt.Html]:
        """Parents with their subtasks indented under them (Todoist hierarchy), in the given order."""
        by_id = {t.id: t for t in tasks}
        children: dict[str, list[TodoistTask]] = {}
        for task in tasks:
            if task.parent_id and task.parent_id in by_id:
                children.setdefault(task.parent_id, []).append(task)
        roots = [t for t in tasks if not (t.parent_id and t.parent_id in by_id)]
        lines: list[fmt.Html] = []

        def render(task: TodoistTask, depth: int) -> None:
            kids = children.get(task.id, [])
            if depth == 0 and kids:
                left = len(descendants(task.id, tasks))
                lines.append(fmt.line("📂", self._task_line(task, bold=True), fmt.i(f"· осталось {left}")))
            else:
                lines.append(fmt.line("   " * depth + "◦" if depth else "•", self._task_line(task)))
            for kid in kids[:max_children]:
                render(kid, depth + 1)
            if len(kids) > max_children:
                lines.append(fmt.hint("   " * (depth + 1) + f"… и ещё {len(kids) - max_children}"))

        for root in roots[:max_roots]:
            render(root, 0)
        if len(roots) > max_roots:
            lines.append(fmt.hint(f"… и ещё {len(roots) - max_roots}"))
        return lines

    async def cmd_subtasks(self, chat_id: int, name: str | None) -> None:
        """«Что осталось по обустройству квартиры?» — the open subtasks of a task."""
        await self._fresh_tasks()
        async with self.c.db.session() as s:
            active = list(await self.c.task_ops.active(s))
        found = match_tasks(name, active)
        with_subs = [t for t in found if descendants(t.id, active)]
        if len(with_subs) == 1 or len(found) == 1:
            await self._show_subtasks(chat_id, (with_subs or found)[0], active)
        elif not found:
            await self.reply(chat_id, f"Не нашёл задачу «{name}». Уточни название.")
        else:
            await self._ask_which_task(chat_id, "subtasks", with_subs or found, {})

    async def _show_subtasks(self, chat_id: int, task: TodoistTask, active: list[TodoistTask] | None = None) -> None:
        if active is None:
            async with self.c.db.session() as s:
                active = list(await self.c.task_ops.active(s))
        ids = {d.id for d in descendants(task.id, active)}
        subs = sorted(
            (t for t in active if t.id in ids),
            key=lambda t: (t.due_date is None, t.due_date or date.min, -t.priority, t.content),
        )
        if not subs:
            await self.reply(chat_id, f"У «{task.content}» нет открытых подзадач.")
            return
        tree = self._task_tree([task, *subs])
        await self._set_context(chat_id, last_task_id=task.id)
        await self.reply(chat_id, fmt.blocks(fmt.title("📂", task.content, f"осталось {len(subs)}"), tree[1:]))

    async def cmd_plan(self, chat_id: int) -> None:
        async with self.c.db.session() as s:
            outcome = await self.c.planner.propose(s, self.c.clock.now(), requested_by=f"telegram:{chat_id}")
        markup = approval_keyboard(outcome.approval_nonce) if outcome.approval_nonce else None
        await self.reply(chat_id, fmt.headed(outcome.text), markup)

    async def cmd_add_task(self, chat_id: int, routed: RoutedIntent, message: dict[str, Any]) -> None:
        if self.c.todoist is None:
            raise NotConfiguredError("Todoist (TODOIST_API_TOKEN)")
        title = (routed.task_text or "").strip()
        due_string = routed.due_string
        parent: TodoistTask | None = None
        if routed.parent_text:
            async with self.c.db.session() as s:
                if title:  # the LLM named the parent: resolve it by title, never by an id
                    found = await self.c.task_ops.find(s, routed.parent_text)
                    parent = found[0] if len(found) == 1 else None
                elif split := await self.c.task_ops.split_parent(s, routed.parent_text):
                    parent, title = split
            if parent is None and not title:  # «добавь в понедельник позвонить маме»: not a subtask after all
                title, due_string = split_due("в " + routed.parent_text)
            if parent is not None and not routed.task_text:
                title, due_string = split_due(title)
        if not title:
            await self.reply(chat_id, "Что добавить? Например: «Добавь купить молоко».")
            return
        title = title[:1].upper() + title[1:]
        request_id = self._request_id(chat_id, message)
        created = await self.c.todoist.create_task(
            title, due_string=due_string, request_id=request_id, parent_id=parent.id if parent else None
        )
        task_id = str(created.get("id"))
        if self.c.todoist_sync is not None:
            async with self.c.db.session() as s:
                await self.c.todoist_sync.refresh_task(s, task_id, self.c.clock.now())
                await audit(
                    s,
                    now=self.c.clock.now(),
                    actor=f"telegram:{chat_id}",
                    action=A.TASK_UPDATED,
                    target_type="todoist_task",
                    target_id=task_id,
                    details={"op": "create"},
                )
        await self._set_context(chat_id, last_task_id=task_id)
        when = f" ({due_string})" if due_string else ""
        note = "\nНапомню в Telegram в указанное время." if routed.intent == Intent.REMIND and due_string else ""
        where = f" в «{parent.content}»" if parent is not None else ""
        if parent is None and routed.parent_text and routed.task_text:
            note += f"\nЗадачу «{routed.parent_text}» не нашёл однозначно — добавил отдельной задачей."
        kind = "подзадачу" if parent is not None else "задачу"
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.line("✅", f"Добавил {kind}{where}:", fmt.b(title), when.strip() or None),
                fmt.hint(note.strip()) if note else None,
            ),
        )

    async def cmd_schedule_referenced(self, chat_id: int, routed: RoutedIntent, message: dict[str, Any]) -> None:
        await self.cmd_task_op(chat_id, "reschedule", routed, message)

    # --- task management ---------------------------------------------------------------------------

    async def _find_tasks(self, chat_id: int, name: str | None) -> list[TodoistTask]:
        """A named task is resolved in the database (never guessed); "это" means the last task of this chat."""
        if is_pronoun(name):
            ctx = await self._context(chat_id)
            if ctx is None or not ctx.last_task_id:
                return []
            async with self.c.db.session() as s:
                task = await s.get(TodoistTask, ctx.last_task_id)
            return [task] if task is not None and not task.checked and not task.is_deleted else []
        async with self.c.db.session() as s:
            return await self.c.task_ops.find(s, name)

    async def cmd_task_op(self, chat_id: int, op: str, routed: RoutedIntent, message: dict[str, Any]) -> None:
        if self.c.todoist is None:
            raise NotConfiguredError("Todoist (TODOIST_API_TOKEN)")
        name = routed.task_text
        if op == "complete" and not name:
            await self.reply(chat_id, "Что именно готово? Например: «готово: стрижка».")
            return
        if op == "priority" and routed.priority is None:
            await self.reply(
                chat_id, "Какой приоритет? 1 — самый высокий … 4 — обычный. Например: «приоритет 1: стрижка»."
            )
            return
        await self._fresh_tasks()
        found = await self._find_tasks(chat_id, name)
        if not found and name and not is_pronoun(name):
            # «перенеси поход к парикмахеру…»: the words match no title — the LLM picks from the titles, SQL resolves.
            async with self.c.db.session() as s:
                found = await self.c.task_ops.find_by_meaning(s, self.c.llm, name)
            if len(found) > 1:
                await self._several_tasks_op(chat_id, op, found, routed)
                return
        if not found:
            if is_pronoun(name):
                examples = {
                    "reschedule": "«Перенеси стрижку на пятницу»",
                    "complete": "«Готово: стрижка»",
                    "delete": "«Удали задачу стрижка»",
                    "priority": "«Приоритет 1: стрижка»",
                }
                await self.reply(chat_id, f"Не понял, какую задачу. Назови её, например: {examples[op]}.")
            else:
                await self.reply(chat_id, f"Не нашёл задачу «{name}». Уточни название.")
            return
        params = {"priority": routed.priority, "due": routed.due_string}
        if len(found) > 1:
            await self._ask_which_task(chat_id, op, found, params)
            return
        request_id = self._request_id(chat_id, message)
        await self._run_task_op(chat_id, op, found[0], params, request_id)

    async def _several_tasks_op(self, chat_id: int, op: str, tasks: list[TodoistTask], routed: RoutedIntent) -> None:
        """One request naming several tasks («перенеси стрижку и уборку на завтра»): a change to several tasks at
        once always goes through a confirmation button."""
        if op not in ("complete", "delete", "reschedule", "priority"):
            await self._ask_which_task(chat_id, op, tasks, {})
            return
        now = self.c.clock.now()
        targets = None
        if op == "reschedule":
            tasks = [t for t in tasks if not t.due_is_recurring]
            if not tasks:
                await self.reply(chat_id, "Это повторяющиеся задачи — перенос через бота отключён.")
                return
            if not (routed.due_string or "").strip():
                await self._ask_due(chat_id, tasks)
                return
            targets = await self._targets_for(chat_id, tasks, routed.due_string or "", now)
            if targets is None:
                return
        if op == "priority" and routed.priority is None:
            await self.reply(chat_id, "Какой приоритет? 1 — самый высокий … 4 — обычный.")
            return
        await self._confirm_several(chat_id, op, tasks, now, targets=targets, priority=routed.priority)

    async def _targets_for(
        self, chat_id: int, tasks: list[TodoistTask], phrase: str, now: datetime
    ) -> dict[str, DueTarget] | None:
        """New dues for several tasks from one phrase; None after telling the user what is wrong."""
        try:
            resolved = {t.id: self.c.task_ops.resolve_due(t, phrase, now) for t in tasks}
        except TargetInPastError:
            await self.reply(chat_id, f"«{phrase}» уже прошло — назовите дату в будущем.")
            return None
        if any(target is None for target in resolved.values()):
            await self.reply(
                chat_id, f"Не понял дату «{phrase}». Примеры: на завтра, на пятницу в 18, на 2 часа позже."
            )
            return None
        return {task_id: target for task_id, target in resolved.items() if target is not None}

    async def _confirm_several(
        self,
        chat_id: int,
        op: str,
        tasks: list[TodoistTask],
        now: datetime,
        *,
        targets: dict[str, DueTarget] | None = None,
        priority: int | None = None,
    ) -> None:
        async with self.c.db.session() as s:
            approval = await self.c.task_ops.request_op(
                s, op=op, tasks=tasks, now=now, chat_id=chat_id, targets=targets, priority=priority
            )
            text_msg = self.c.task_ops.confirmation_text(approval, now)
        await self.reply(chat_id, fmt.headed(text_msg), approval_keyboard(approval.nonce, "✅ Подтвердить", "✖️ Отмена"))

    async def _ask_which_task(self, chat_id: int, op: str, found: list[TodoistTask], params: dict[str, Any]) -> None:
        shown = found[:8]
        async with self.c.db.session() as s:
            choice = await offer_choice(
                s,
                chat_id=chat_id,
                kind="task_pick",
                payload={"op": op, "ids": [t.id for t in shown], "params": params},
                now=self.c.clock.now(),
            )
        verb = {
            "subtasks": "показать",
            "complete": "отметить выполненной",
            "delete": "удалить",
            "priority": "изменить",
            "reschedule": "перенести",
        }.get(op, "выбрать")
        rows = [[choice_button(choice, str(i), t.content)] for i, t in enumerate(shown)]
        rows.append([choice_button(choice, "x", "✖️ Отмена")])
        more = fmt.hint(f"Показаны первые {len(shown)} из {len(found)}.") if len(found) > len(shown) else None
        await self.reply(
            chat_id,
            fmt.lines(fmt.line("🔎", fmt.b("Нашёл несколько задач"), f"— какую {verb}?"), more),
            inline_keyboard(rows),
        )

    async def _run_task_op(
        self, chat_id: int, op: str, task: TodoistTask, params: dict[str, Any], request_id: str
    ) -> None:
        assert self.c.todoist is not None
        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        ops = self.c.task_ops
        if op == "subtasks":
            await self._show_subtasks(chat_id, task)
            return
        if op == "complete":
            async with self.c.db.session() as s:
                open_subtasks = await ops.subtasks(s, task.id)
            if open_subtasks:  # Todoist closes the subtasks together with the parent: ask first
                op = "confirm_complete"
        if op in ("delete", "confirm_complete"):
            op = "delete" if op == "delete" else "complete"
            async with self.c.db.session() as s:
                approval = await ops.request_op(s, op=op, tasks=[task], now=now, chat_id=chat_id)
                text_msg = ops.confirmation_text(approval, now)
            yes = "🗑 Удалить" if op == "delete" else "✅ Закрыть всё"
            await self.reply(chat_id, fmt.headed(text_msg), approval_keyboard(approval.nonce, yes, "✖️ Отмена"))
            return
        target = None
        if op == "reschedule":
            if task.due_is_recurring:
                await self.reply(
                    chat_id, "Это повторяющаяся задача — перенос через бота отключён, чтобы не сломать повторение."
                )
                return
            if not (params.get("due") or "").strip():
                await self._ask_due(chat_id, task)
                return
            phrase = str(params["due"]).strip()
            try:
                target = ops.resolve_due(task, phrase, now)
            except TargetInPastError:
                await self.reply(chat_id, f"«{phrase}» уже прошло — назови дату в будущем.")
                return
            if target is None:
                await self._reschedule_by_todoist(chat_id, task, phrase, request_id)
                return
        async with self.c.db.session() as s:
            outcome = await ops.execute(
                s,
                self.c.todoist,
                self.c.todoist_sync,
                op,
                task.id,
                now=now,
                request_id=request_id,
                actor=f"telegram:{chat_id}",
                target=target,
                priority=params.get("priority"),
            )
            refreshed = await s.get(TodoistTask, task.id)
            await enqueue_coalesced(s, job_type="reminders.plan", run_at=now, coalesce_key="task-op", priority=40)
        await self._set_context(chat_id, last_task_id=task.id)
        if op == "complete":
            if refreshed is not None and task.due_is_recurring and not refreshed.checked:
                await self.reply(
                    chat_id, f"✅ Отметил «{task.content}». Следующий раз: {ops.due_text(refreshed, now)}."
                )
            elif outcome == "already":
                await self.reply(chat_id, f"Задача «{task.content}» уже закрыта.")
            else:
                undo = inline_keyboard([[InlineButton("↩️ Вернуть", f"tr:{task.id}")]])
                await self.reply(chat_id, fmt.line("✅", "Готово:", fmt.b(f"«{task.content}»")), undo)
        elif op == "priority":
            p = int(params.get("priority") or 4)
            await self.reply(
                chat_id, fmt.line(PRIORITY_EMOJI.get(p, ""), f"Приоритет {p}:", fmt.b(f"«{task.content}»"))
            )
        elif op == "reschedule" and target is not None:
            await self.reply(chat_id, fmt.line("🗓", f"«{task.content}» →", fmt.b(target.describe(today))))

    async def _ask_due(self, chat_id: int, tasks: TodoistTask | list[TodoistTask]) -> None:
        """«Перенесём стрижку» without a date: ask in words (never guess one); the next message is the answer."""
        tasks = tasks if isinstance(tasks, list) else [tasks]
        async with self.c.db.session() as s:
            await offer_choice(
                s,
                chat_id=chat_id,
                kind=AWAIT_DUE,
                payload={"task_ids": [t.id for t in tasks]},
                now=self.c.clock.now(),
                ttl=AWAIT_DUE_TTL,
            )
        names = " и ".join(f"«{t.content}»" for t in tasks[:5])
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.line("🗓", fmt.b("На когда перенести"), f"{names}?"),
                fmt.hint("Скажите или напишите: «на пятницу в 18», «на 2 часа позже», «на пару дней», «на выходные»."),
            ),
        )

    async def _answer_pending_due(self, chat_id: int, text_in: str) -> bool:
        """The answer to «На когда перенести …?»: a date phrase moves that task; anything else is a new request.

        Returns True when the message was consumed here."""
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            pending = await s.scalar(
                select(PendingChoice)
                .where(
                    PendingChoice.chat_id == chat_id,
                    PendingChoice.kind == AWAIT_DUE,
                    PendingChoice.status == "pending",
                    PendingChoice.expires_at > now,
                )
                .order_by(PendingChoice.created_at.desc())
                .limit(1)
            )
        if pending is None:
            return False
        phrase = normalize_command(text_in).strip(" .!")
        phrase = re.sub(r"^(?:перенеси|сдвинь|отложи)(?:\s+(?:её|ее|его|это|их))?\s*", "", phrase, flags=re.I)
        cancel = re.fullmatch(r"(?:не\s+(?:надо|нужно|переноси)\w*|отмена|оставь\w*|никак|нет)", phrase.lower())
        ids = [str(i) for i in pending.payload.get("task_ids") or [pending.payload.get("task_id")] if i]
        async with self.c.db.session() as s:
            tasks = [t for t in [await s.get(TodoistTask, i) for i in ids] if t is not None]
        open_tasks = [t for t in tasks if not t.checked and not t.is_deleted]
        task = tasks[0] if tasks else None
        target = None
        if not cancel and task is not None and phrase:
            try:
                target = self.c.task_ops.resolve_due(task, phrase, now)
            except TargetInPastError:
                target = None
                if parse_when(phrase, to_local(now, self.c.settings.tz).date()) is not None:
                    await self.reply(chat_id, f"«{phrase}» уже прошло — назовите дату в будущем.")
                    return True
        resolution = "cancel" if cancel else ("date" if target is not None else "ignored")
        async with self.c.db.session() as s:
            taken = await take_choice(s, choice_id=pending.id, chat_id=chat_id, resolution=resolution, now=now)
        if taken is None or resolution == "ignored":
            return False  # not an answer: route the message as usual
        if cancel or task is None or not open_tasks:
            await self.reply(chat_id, "Ок, оставляю как есть." if cancel else "Эта задача уже закрыта или удалена.")
            return True
        if len(open_tasks) > 1:
            targets = await self._targets_for(chat_id, open_tasks, phrase, now)
            if targets is not None:
                await self._confirm_several(chat_id, "reschedule", open_tasks, now, targets=targets)
            return True
        task = open_tasks[0]
        token = PENDING_ACK.set(self.persona.ack())
        try:
            await self._run_task_op(
                chat_id, "reschedule", task, {"due": phrase}, request_id=f"due-{pending.id.hex[:16]}"
            )
        finally:
            PENDING_ACK.reset(token)
        return True

    async def _reschedule_by_todoist(self, chat_id: int, task: TodoistTask, phrase: str, request_id: str) -> None:
        """A phrase Jarvis does not parse itself (e.g. «в конце месяца»): Todoist's own date parser decides."""
        assert self.c.todoist is not None
        due = normalize_due_phrase(re.sub(r"^на\s+", "", phrase, flags=re.IGNORECASE)) or phrase
        await self.c.todoist.update_due_string(task.id, due, request_id=request_id)
        now = self.c.clock.now()
        result = due
        if self.c.todoist_sync is not None:
            async with self.c.db.session() as s:
                await self.c.todoist_sync.refresh_task(s, task.id, now)
                refreshed = await s.get(TodoistTask, task.id)
                if refreshed is not None and refreshed.due_date is not None:
                    result = f"{due} ({self.c.task_ops.due_text(refreshed, now)})"
        await self._set_context(chat_id, last_task_id=task.id)
        await self.reply(chat_id, fmt.line("🗓", f"«{task.content}» →", fmt.b(result)))

    async def cmd_bulk(self, chat_id: int, routed: RoutedIntent) -> None:
        if self.c.todoist is None:
            raise NotConfiguredError("Todoist (TODOIST_API_TOKEN)")
        op, scope = routed.task_op, routed.scope
        if op is None or scope is None:
            await self.reply(chat_id, "Уточни, например: «перенеси все просроченные на завтра».")
            return
        now = self.c.clock.now()
        ops = self.c.task_ops
        recurring: list[TodoistTask] = []
        await self._fresh_tasks()
        async with self.c.db.session() as s:
            tasks = await ops.scope(s, scope, now)
            targets = None
            if op == "reschedule":
                phrase = (routed.due_string or "завтра").strip()
                recurring = [t for t in tasks if t.due_is_recurring]
                tasks = [t for t in tasks if not t.due_is_recurring]
                try:
                    resolved = {t.id: ops.resolve_due(t, phrase, now) for t in tasks}
                except TargetInPastError:
                    await self.reply(chat_id, f"«{phrase}» уже прошло — назови дату в будущем.")
                    return
                if any(target is None for target in resolved.values()):
                    await self.reply(
                        chat_id, f"Не понял дату «{phrase}». Примеры: на завтра, на пятницу, на 2 дня, на 25.09."
                    )
                    return
                targets = {task_id: target for task_id, target in resolved.items() if target is not None}
            if not tasks:
                await self.reply(chat_id, f"Задач «{SCOPE_RU[scope]}» нет 👍")
                return
            approval = await ops.request_op(s, op=op, tasks=tasks, now=now, chat_id=chat_id, targets=targets)
            text_msg = ops.confirmation_text(approval, now)
        if recurring:
            text_msg += f"\n🔁 Повторяющиеся не переношу: {', '.join(t.content for t in recurring)}"
        await self.reply(chat_id, fmt.headed(text_msg), approval_keyboard(approval.nonce, "✅ Подтвердить", "✖️ Отмена"))

    async def _undo_complete(self, chat_id: int, task_id: str) -> str:
        if self.c.todoist is None:
            raise NotConfiguredError("Todoist (TODOIST_API_TOKEN)")
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            task = await s.get(TodoistTask, task_id)
            if task is None or task.is_deleted:
                await self.reply(chat_id, "Задача не найдена.")
                return "Не найдено"
            if not task.checked:
                return "Задача уже открыта"
            await self.c.task_ops.execute(
                s,
                self.c.todoist,
                self.c.todoist_sync,
                "reopen",
                task.id,
                now=now,
                request_id=f"undo-{task.id}-{int(now.timestamp())}",
                actor=f"telegram:{chat_id}",
            )
            await enqueue_coalesced(s, job_type="reminders.plan", run_at=now, coalesce_key="task-op", priority=40)
        await self.reply(chat_id, f"↩️ Вернул «{task.content}» в работу.")
        return "Вернул"

    # --- notes & AI inbox --------------------------------------------------------------------------

    async def cmd_note(
        self, chat_id: int, body: str | None, message: dict[str, Any], source: str = "telegram_text"
    ) -> None:
        body = strip_address(body or "")  # the LLM may copy «Джарвис, …» into the note
        if not body.strip():
            await self.reply(chat_id, "Напиши заметку после команды: /note …")
            return
        async with self.c.db.session() as s:
            _, created = await self.c.notes.save(
                s, text_value=body.strip(), source=source, chat_id=chat_id, message_id=message.get("message_id")
            )
        await self.reply(
            chat_id,
            fmt.lines(fmt.line("📝", fmt.b("Заметка сохранена.")), fmt.hint(body.strip()[:200]))
            if created
            else "Эта заметка уже сохранена.",
        )

    async def cmd_notes(self, chat_id: int, query: str | None) -> None:
        tz = self.c.settings.tz
        async with self.c.db.session() as s:
            notes = await self.c.notes.search(s, query) if query else await self.c.notes.recent(s)
        if not notes:
            await self.reply(chat_id, "Ничего не нашёл." if query else "Заметок пока нет. Напиши «заметка: …».")
            return
        head = fmt.title("📝", "Заметки", f"«{query}»") if query else fmt.title("📝", "Последние заметки")
        lines = [fmt.line("•", fmt.i(format_local(n.created_at, tz, "%d.%m")), n.text[:300]) for n in notes]
        await self.reply(chat_id, fmt.blocks(head, lines))

    async def cmd_inbox_ask(
        self, chat_id: int, routed: RoutedIntent, raw: str, message: dict[str, Any], *, note: str | None = None
    ) -> None:
        suggested = routed.intent.value if routed.intent.value in INBOX_INTENTS else None
        payload = {
            "text": raw[:4000],
            "message_id": message.get("message_id"),
            "task_text": routed.task_text,
            "due_string": routed.due_string,
            "suggested": suggested,
        }
        async with self.c.db.session() as s:
            choice = await offer_choice(s, chat_id=chat_id, kind="inbox", payload=payload, now=self.c.clock.now())
        o = INBOX_OPTIONS
        rows = [
            [choice_button(choice, "t", o["t"]), choice_button(choice, "i", o["i"])],
            [choice_button(choice, "s", o["s"]), choice_button(choice, "n", o["n"])],
            [choice_button(choice, "x", o["x"])],
        ]
        preview = raw if len(raw) <= 120 else raw[:117] + "…"
        text_msg = fmt.lines(
            note,
            fmt.line("📥", fmt.b("Куда записать"), f"«{preview}»?"),
            fmt.hint(f"Похоже на: {INBOX_KIND_RU[suggested]}.") if suggested else None,
        )
        await self.reply(chat_id, text_msg, inline_keyboard(rows))

    async def _inbox_apply(self, chat_id: int, payload: dict[str, Any], option: str) -> None:
        raw = str(payload.get("text") or "")
        message = {"message_id": payload.get("message_id")}
        if option == "t":
            title, due = payload.get("task_text"), payload.get("due_string")
            if not title:
                title, due = split_due(raw)
                title = title[:1].upper() + title[1:]
            await self.cmd_add_task(
                chat_id, RoutedIntent(intent=Intent.ADD_TASK, task_text=title, due_string=due), message
            )
        elif option == "i":
            await self.cmd_idea(chat_id, raw, message)
        elif option == "s":
            await self.cmd_shop(chat_id, "add " + raw)
        elif option == "n":
            await self.cmd_note(chat_id, raw, message, source="telegram_inbox")
        else:
            await self.reply(chat_id, "Ок, ничего не записываю.")

    async def _on_choice(self, chat_id: int, choice_id: uuid.UUID, option: str, callback: dict[str, Any]) -> str:
        async with self.c.db.session() as s:
            choice = await take_choice(
                s, choice_id=choice_id, chat_id=chat_id, resolution=option, now=self.c.clock.now()
            )
        if choice is None:
            return "Уже обработано"
        message_id = (callback.get("message") or {}).get("message_id")
        if self.c.telegram is not None and message_id:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
        if choice.kind == "inbox":
            await self._inbox_apply(chat_id, choice.payload, option)
            return "Готово"
        if choice.kind == "tpl_pick":
            return await self._tpl_pick(chat_id, choice.payload, option)
        if choice.kind == FILL_KIND:
            return await self._fill_pick(chat_id, dict(choice.payload), option)
        if choice.kind == "portion":
            return await self._portion_pick(chat_id, choice.payload, option)
        if choice.kind == "site_open":
            if option not in ("h", "a", "m"):
                return "Неизвестное действие"
            where = {"h": "home", "a": "away", "m": "mac"}[option]
            await self._site_open_where(chat_id, str(choice.payload.get("site_id")), where)
            return "Готово"
        if choice.kind == "as_food":
            await self.cmd_food_log(chat_id, str(choice.payload.get("text") or ""), with_pills=False)
            return "Готово"
        if choice.kind == "task_due":
            if option == "x":
                await self.reply(chat_id, "Ок, оставляю как есть.")
                return "Отменено"
            day = (choice.payload.get("dates") or {}).get(option)
            async with self.c.db.session() as s:
                task = await s.get(TodoistTask, str(choice.payload.get("task_id")))
            if day is None:
                return "Неизвестное действие"
            if task is None or task.checked or task.is_deleted:
                await self.reply(chat_id, "Эта задача уже закрыта или удалена.")
                return "Не найдено"
            await self._run_task_op(chat_id, "reschedule", task, {"due": day}, request_id=f"due-{choice.id.hex[:16]}")
            return "Готово"
        if choice.kind == "task_pick":
            if option == "x":
                await self.reply(chat_id, "Ок, ничего не меняю.")
                return "Отменено"
            ids = list(choice.payload.get("ids") or [])
            if not option.isdigit() or int(option) >= len(ids):
                return "Неизвестное действие"
            async with self.c.db.session() as s:
                task = await s.get(TodoistTask, ids[int(option)])
            if task is None or task.checked or task.is_deleted:
                await self.reply(chat_id, "Эта задача уже закрыта или удалена.")
                return "Не найдено"
            await self._run_task_op(
                chat_id,
                str(choice.payload["op"]),
                task,
                dict(choice.payload.get("params") or {}),
                request_id=f"pick-{choice.id.hex[:16]}",
            )
            return "Готово"
        return "Неизвестное действие"

    # --- ideas & development -----------------------------------------------------------------------

    async def cmd_idea(self, chat_id: int, body: str | None, message: dict[str, Any]) -> None:
        body = strip_address(body or "")
        if not body:
            await self.reply(chat_id, "Напиши идею после команды: /idea …")
            return
        async with self.c.db.session() as s:
            idea, created = await self.c.ideas.capture(
                s,
                raw_input=body,
                source="telegram_text",
                now=self.c.clock.now(),
                chat_id=chat_id,
                message_id=message.get("message_id"),
            )
        await self._set_context(chat_id, last_idea_id=idea.id)
        await self.reply(
            chat_id,
            fmt.lines(fmt.line("💡", fmt.b("Идея записана дословно.")), fmt.hint("Структурирую — пришлю разбор."))
            if created
            else "Эта идея уже сохранена.",
        )

    async def cmd_ideas(self, chat_id: int, query: str | None) -> None:
        tz = self.c.settings.tz
        async with self.c.db.session() as s:
            if query:
                hits = await self.c.ideas.search(s, query)
                notes = await self.c.notes.search(s, query, limit=3)
                if not hits and not notes:
                    await self.reply(chat_id, "Ничего не нашёл.")
                    return
                found = [
                    fmt.lines(
                        fmt.line(
                            "•", fmt.b(h.title or "(без названия)"), fmt.i(format_local(h.created_at, tz, "%d.%m.%Y"))
                        ),
                        fmt.hint(f"   «{h.snippet}»"),
                    )
                    for h in hits
                ]
                note_lines = [
                    fmt.line("•", fmt.i(format_local(n.created_at, tz, "%d.%m.%Y")), n.text[:200]) for n in notes
                ]
                text_msg = fmt.blocks(
                    fmt.title("🔎", f"«{query}»"),
                    fmt.section("Идеи", found, emoji="💡"),
                    fmt.section("Заметки", note_lines, emoji="📝"),
                )
                if hits:
                    await self._set_context(chat_id, last_idea_id=hits[0].idea_id)
            else:
                ideas = (await s.scalars(select(Idea).order_by(Idea.created_at.desc()).limit(10))).all()
                text_msg = fmt.blocks(
                    fmt.title("💡", "Последние идеи"),
                    [
                        fmt.line("•", fmt.i(format_local(i.created_at, tz, "%d.%m")), i.title or i.raw_input[:60])
                        for i in ideas
                    ],
                )
                if not ideas:
                    text_msg = fmt.lines(fmt.line("💡", "Идей пока нет."), fmt.say("Запиши идею …"))
        await self.reply(chat_id, text_msg)

    async def cmd_spec(self, chat_id: int, idea_id: uuid.UUID | None = None) -> None:
        if idea_id is None:
            ctx = await self._context(chat_id)
            idea_id = ctx.last_idea_id if ctx else None
        if idea_id is None:
            async with self.c.db.session() as s:
                idea_id = await s.scalar(select(Idea.id).order_by(Idea.created_at.desc()).limit(1))
        if idea_id is None:
            await self.reply(chat_id, "Нет идей для ТЗ.")
            return
        repo_id = self.c.settings.dev_repo_ids[0] if self.c.settings.dev_repo_ids else None
        async with self.c.db.session() as s:
            spec = await self.c.ideas.create_spec(s, idea_id, self.c.clock.now(), repo_id)
        await self._set_context(chat_id, last_idea_id=idea_id)
        preview = spec.dev_task_md[:3500]
        markup = inline_keyboard([[InlineButton("🚀 Запустить разработку", f"dv:{spec.id.hex}")]]) if repo_id else None
        await self.reply(chat_id, "📝 DEV_TASK.md готов (разработка НЕ начата):\n\n" + preview, markup, speak=False)

    async def cmd_dev(self, chat_id: int, repo_id: str | None, spec_id: uuid.UUID | None = None) -> None:
        repo = repo_id or (self.c.settings.dev_repo_ids[0] if self.c.settings.dev_repo_ids else None)
        if repo is None:
            await self.reply(chat_id, "Не настроены репозитории (DEV_REPO_IDS).")
            return
        async with self.c.db.session() as s:
            if spec_id is None:
                ctx = await s.get(ChatContext, chat_id)
                query = select(DevSpec.id).order_by(DevSpec.created_at.desc()).limit(1)
                if ctx and ctx.last_idea_id:
                    query = (
                        select(DevSpec.id)
                        .where(DevSpec.idea_id == ctx.last_idea_id)
                        .order_by(DevSpec.created_at.desc())
                        .limit(1)
                    )
                spec_id = await s.scalar(query)
            if spec_id is None:
                await self.reply(chat_id, "Сначала сделай ТЗ: «Преврати эту идею в ТЗ».")
                return
            job, approval = await self.c.development.request_job(s, spec_id, repo, self.c.clock.now())
        if approval.status != "pending":
            await self.reply(chat_id, f"Задача уже {job.status}.")
            return
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.line(
                    "🚀",
                    fmt.b("Разработка по ТЗ"),
                    "в репозитории",
                    fmt.code(repo),
                    "· ветка",
                    fmt.code(job.branch_name),
                ),
                fmt.hint("Будут: worktree, Claude Code с ограниченными правами, тесты. Без merge и деплоя."),
                "Подтвердить?",
            ),
            approval_keyboard(approval.nonce, "✅ Запустить", "✖️ Отмена"),
        )

    # --- shopping ----------------------------------------------------------------------------------

    async def cmd_shop(self, chat_id: int, arg: str) -> None:
        sub, _, rest = arg.partition(" ")
        sub = sub.lower()
        now = self.c.clock.now()
        shop = self.c.shopping
        if sub == "add" and rest:
            async with self.c.db.session() as s:
                items = await shop.add_items(s, rest, now)
            from jarvis.shopping.service import display_name

            names = ", ".join(display_name(i) for i in items) or "ничего"
            await self.reply(chat_id, fmt.line("🛒", "Добавил в покупки:", fmt.b(names)))
        elif sub == "quote":
            async with self.c.db.session() as s:
                quote, result, summary = await shop.quote(s, now)
            markup = None
            if result.best is not None and not result.best.missing:
                markup = inline_keyboard([[InlineButton("🧾 Подготовить заказ", f"sq:{quote.id.hex}")]])
            await self.reply(chat_id, summary, markup)
        elif sub == "import":
            try:
                doc = ImportDocument.model_validate(json.loads(rest))
            except (json.JSONDecodeError, ValueError) as exc:
                raise PermanentError(f"неверный JSON импорта: {str(exc)[:200]}") from exc
            async with self.c.db.session() as s:
                stats = await shop.import_offers(s, doc, now)
            await self.reply(chat_id, f"Импортировано: магазинов {stats['stores']}, предложений {stats['offers']}.")
        elif sub == "pref":
            await self.cmd_pref(chat_id, rest)
        elif sub == "clear":
            async with self.c.db.session() as s:
                for item in await shop.open_items(s):
                    item.status = "removed"
            await self.reply(chat_id, "Список очищен.")
        else:
            async with self.c.db.session() as s:
                items = await shop.open_items(s)
            if not items:
                await self.reply(
                    chat_id,
                    fmt.lines(fmt.line("🛒", fmt.b("Список пуст.")), fmt.say("Добавь яйца, молоко и 2 кг филе")),
                )
                return
            text_msg, markup = shopping_view(items, "Покупки", "Купили — нажмите на товар.")
            await self.reply(chat_id, text_msg, markup)

    async def _purchase_tasks(self) -> list[TodoistTask]:
        """Open Todoist tasks that are just purchases («Купить кухонный нож»)."""
        from jarvis.shopping.service import is_purchase_task

        async with self.c.db.session() as s:
            active = await self.c.task_ops.active(s)
        return [t for t in active if is_purchase_task(t.content, t.due_is_recurring)]

    async def cmd_shopping_trip(self, chat_id: int) -> None:
        """«Иду в магазин» → what to buy, with a «bought» button per item."""
        await self._fresh_tasks()
        async with self.c.db.session() as s:
            items = await self.c.shopping.open_items(s)
        in_tasks = await self._purchase_tasks()
        sir = f", {self.persona.address}" if self.persona.enabled else ""
        if not items and not in_tasks:
            await self.reply(chat_id, f"🛒 Список покупок пуст{sir} — ничего докупать не нужно.")
            return
        parts: list[object] = []
        markup = None
        if items:
            text_msg, markup = shopping_view(items, f"Не забудьте купить{sir}", "Купили — нажмите на товар.")
            parts.append(text_msg)
        if in_tasks:
            names = ", ".join(t.content for t in in_tasks[:6]) + ("…" if len(in_tasks) > 6 else "")
            parts.append(
                fmt.lines(
                    fmt.line("📋", fmt.b("Ещё покупки в задачах:"), f"{names}."),
                    fmt.hint("Перенести их в список — «перенеси покупки из задач»."),
                )
            )
        await self.reply(chat_id, fmt.blocks(*parts), markup)

    async def cmd_shopping_from_tasks(self, chat_id: int) -> None:
        """Purchases sitting in Todoist → the shopping list (a change to several tasks: confirmation button)."""
        await self._fresh_tasks()
        tasks = await self._purchase_tasks()
        if not tasks:
            await self.reply(chat_id, "В задачах покупок нет — всё уже в 🛒 Покупках.")
            return
        await self._confirm_several(chat_id, "to_shopping", tasks, self.c.clock.now())

    async def _bought(self, chat_id: int, which: str, callback: dict[str, Any]) -> str:
        async with self.c.db.session() as s:
            bought = await self.c.shopping.mark_bought(s, None if which == "all" else uuid.UUID(which))
            items = await self.c.shopping.open_items(s)
        if not bought:
            return "Уже отмечено"
        message_id = (callback.get("message") or {}).get("message_id")
        if items:
            text_msg, markup = shopping_view(items, "Осталось купить")
        else:
            text_msg, markup = fmt.line("🎉", fmt.b("Всё из списка куплено.")), None
        if self.c.telegram is not None and message_id:
            await self.c.telegram.edit_message_text(
                chat_id, int(message_id), text_msg, reply_markup=markup, parse_mode="HTML"
            )
        else:
            await self.reply(chat_id, text_msg, markup)
        return "Куплено" if len(bought) == 1 else f"Куплено: {len(bought)}"

    async def cmd_pref(self, chat_id: int, rest: str) -> None:
        """/shop pref <product>: only=<attr>; brands=a,b; exclude=c; min=10 шт   |   /shop pref remove <product>"""
        if rest.startswith("remove "):
            async with self.c.db.session() as s:
                removed = await self.c.shopping.remove_preference(s, rest[7:].strip())
            await self.reply(chat_id, "Предпочтение удалено." if removed else "Такого предпочтения нет.")
            return
        if ":" not in rest:
            await self.reply(chat_id, "Формат: /shop pref Coca-Cola: only=zero; exclude=Pepsi")
            return
        product, _, spec = rest.partition(":")
        rules: dict[str, Any] = {}
        for part in spec.split(";"):
            key, _, value = part.strip().partition("=")
            values = [v.strip() for v in value.split(",") if v.strip()]
            if key == "only":
                rules["required_attributes"] = [v.lower() for v in values]
            elif key == "brands":
                rules["allowed_brands"] = values
            elif key == "exclude":
                rules["excluded_brands"] = values
            elif key == "prefer":
                rules["preferred_attributes"] = [v.lower() for v in values]
            elif key == "min" and value.strip():
                rules["min_package"] = value.strip()
            elif key == "substitution" and value.strip() in ("ask", "never", "allow"):
                rules["substitution_policy"] = value.strip()
        async with self.c.db.session() as s:
            pref = await self.c.shopping.set_preference(s, product.strip(), rules)
        await self.reply(chat_id, f"Сохранил предпочтение для «{pref.product_key}»: {pref.rules}")

    # --- start, menu, settings -------------------------------------------------------------------------

    async def _stats(self) -> dict[str, Any]:
        """Numbers for /start and small talk (the cache only; nothing is guessed)."""
        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        async with self.c.db.session() as s:
            active = list(await self.c.task_ops.active(s))
            done_week = await s.scalar(
                select(func.count())
                .select_from(TodoistTask)
                .where(TodoistTask.checked.is_(True), TodoistTask.synced_at >= now - timedelta(days=7))
            )
            [view] = await self.c.agenda.days(s, Period(today, today), now)
            ideas = await s.scalar(select(func.count()).select_from(Idea))
            notes = await s.scalar(select(func.count()).select_from(Note))
            shopping = len(await self.c.shopping.open_items(s))
            attention = await self.c.agenda.attention(s, now, limit=5)
        upcoming = [e for e in view.events if e.start_at and e.start_at > now and not e.all_day]
        return {
            "open": len(active),
            "today": sum(1 for t in active if t.due_date == today),
            "overdue": sum(1 for t in active if t.due_date and t.due_date < today),
            "week": sum(1 for t in active if t.due_date and today < t.due_date <= today + timedelta(days=7)),
            "undated": sum(1 for t in active if t.due_date is None),
            "events_today": len(view.events),
            "next_event": upcoming[0] if upcoming else None,
            "done_week": int(done_week or 0),
            "ideas": int(ideas or 0),
            "notes": int(notes or 0),
            "shopping": shopping,
            "attention": attention,
        }

    async def cmd_start(self, chat_id: int) -> None:
        """Greeting + a short summary; also (re)installs the keyboard under the input field."""
        await self._fresh_tasks()
        now = self.c.clock.now()
        tz = self.c.settings.tz
        st = await self._stats()
        hello, _, rest = self.persona.greeting(to_local(now, tz)).partition(". ")
        greeting = fmt.line("👋", fmt.b(hello + ("." if rest else "")), rest)
        tasks = fmt.lines(
            fmt.head("Задачи", "📋"),
            fmt.line(
                fmt.kv("Открыто", st["open"]),
                "·",
                fmt.kv("сегодня", st["today"]),
                "·",
                fmt.kv("просрочено", st["overdue"]),
            ),
            fmt.hint(f"на неделе {st['week']} · без даты {st['undated']} · закрыто за 7 дней {st['done_week']}"),
        )
        day: list[object] = [fmt.head("Сегодня", "📅")]
        meeting = None
        if st["next_event"] is not None:
            e = st["next_event"]
            meeting = f"ближайшая в {format_local(e.start_at, tz)} — «{e.summary or 'событие'}»"
        day.append(fmt.line(fmt.kv("Встреч", st["events_today"]), "·" if meeting else None, meeting))
        async with self.c.db.session() as s:
            med_plan = await self.c.meds.today(s, now)
        if med_plan:
            taken = sum(1 for _, _, status in med_plan if status == "taken")
            day.append(f"💊 Приёмы: отмечено {taken} из {len(med_plan)}")
        if forecast := await self.c.weather.forecast():
            day.append(self.c.weather.today_text(forecast, now))
        attention = None
        if st["attention"]:
            attention = fmt.line(
                fmt.head(f"Требуют внимания: {len(st['attention'])}", "💡"), "— загляните в «📅 Сегодня»"
            )
        other = [f"🗂 Идей {st['ideas']} · заметок {st['notes']} · в покупках {st['shopping']}"]
        voice_on, voice = await self.voice_settings()
        if self._tts_available():
            other.append(f"🔊 Голосовые ответы: {'включены, голос ' + voice if voice_on else 'выключены'}")
        text_msg = fmt.blocks(
            greeting,
            tasks,
            day,
            attention,
            other,
            fmt.hint("Кнопки внизу — всё основное. Можно просто писать или говорить."),
        )
        markup = main_keyboard() if self.c.settings.menu_keyboard_enabled else None
        await self.reply(chat_id, text_msg, markup)
        if markup is not None:
            await self._save_pref(KEYBOARD_PREF_KEY, version=KEYBOARD_VERSION)

    async def cmd_menu(self, chat_id: int) -> None:
        assert self.c.telegram is not None
        await self.c.telegram.send_message(chat_id, "⌨️ Кнопки управления — внизу.", reply_markup=main_keyboard())
        await self._save_pref(KEYBOARD_PREF_KEY, version=KEYBOARD_VERSION)

    async def _ensure_keyboard(self, chat_id: int) -> None:
        """Show the reply keyboard once (and again after a layout change) without the user asking for /start."""
        if not self.c.settings.menu_keyboard_enabled or self.c.telegram is None:
            return
        async with self.c.db.session() as s:
            pref = await s.get(UserPreference, KEYBOARD_PREF_KEY)
        if pref is not None and pref.value.get("version") == KEYBOARD_VERSION:
            return
        with contextlib.suppress(JarvisError):
            await self.c.telegram.send_message(
                chat_id,
                fmt.lines(
                    fmt.line("⌨️", fmt.b("Кнопки внизу:"), " · ".join(label for row in MAIN_MENU for label in row)),
                    fmt.hint("Остальное — в «⋯ Ещё». /start — приветствие и сводка."),
                ),
                reply_markup=main_keyboard(),
                parse_mode="HTML",
            )
            await self._save_pref(KEYBOARD_PREF_KEY, version=KEYBOARD_VERSION)

    async def _save_pref(self, key: str, **changes: Any) -> None:
        async with self.c.db.session() as s:
            pref = await s.get(UserPreference, key)
            if pref is None:
                s.add(UserPreference(key=key, value=dict(changes)))
            else:
                pref.value = {**pref.value, **changes}

    async def cmd_settings(self, chat_id: int) -> None:
        voice_on, voice = await self.voice_settings()
        voices = KNOWN_VOICES.get(self.c.settings.tts_model, ())
        rows: list[object] = []
        if self._tts_available():
            rows.append(
                fmt.line("🔊", fmt.kv("Голосовые ответы", f"включены, голос {voice}" if voice_on else "выключены"))
            )
        rows.append(fmt.line("🌡", fmt.kv("Погода", self.c.settings.weather_place)))
        text_msg = fmt.blocks(fmt.title("⚙️", "Настройки"), rows)
        await self.reply(chat_id, text_msg, settings_keyboard(voice_on, voice, voices), speak=False)

    async def _settings_action(self, chat_id: int, action: str) -> str:
        if action == "von":
            await self.cmd_voice(chat_id, "on")
        elif action == "voff":
            await self.cmd_voice(chat_id, "off")
        elif action.startswith("v:"):
            await self.cmd_voice(chat_id, action[2:])
        elif action == "sync":
            await self.cmd_sync(chat_id)
        elif action == "status":  # one button: a sync is queued and the state is shown
            now = self.c.clock.now()
            async with self.c.db.session() as s:
                for job in ("todoist.reconcile", "calendar.reconcile", "reminders.plan"):
                    await enqueue_coalesced(s, job_type=job, run_at=now, coalesce_key="manual", priority=40)
            await self.cmd_status(chat_id, synced_now=True)
        elif action == "help":
            await self.cmd_help(chat_id)
        else:
            return "Неизвестное действие"
        return "Готово"

    async def _llm_context(self) -> str | None:
        """Facts the LLM may use in a conversational reply (it must not invent anything else)."""
        now = self.c.clock.now()
        tz = self.c.settings.tz
        st = await self._stats()
        lines = [
            f"Сейчас: {to_local(now, tz).strftime('%d.%m.%Y %H:%M')} ({self.c.settings.default_user_timezone})",
            f"Задачи: открыто {st['open']}, на сегодня {st['today']}, просрочено {st['overdue']}",
        ]
        if st["next_event"] is not None:
            e = st["next_event"]
            lines.append(f"Ближайшая встреча сегодня: {format_local(e.start_at, tz)} «{e.summary}»")
        if st["attention"]:
            lines.append("Требуют внимания: " + "; ".join(f"«{a.task.content}» ({a.note})" for a in st["attention"]))
        if forecast := await self.c.weather.forecast():
            lines.append("Погода: " + self.c.weather.today_text(forecast, now))
        lines.append(f"Обращение к пользователю: «{self.c.settings.persona_address}»")
        if facts := await self._profile_block():
            lines.append(facts)
        return "\n".join(lines)

    async def _profile_block(self) -> str | None:
        from jarvis.profile import service as profile

        async with self.c.db.session() as s:
            return profile.prompt_block(await profile.active(s))

    # --- attention, focus, suggestions ---------------------------------------------------------------

    async def _attention(self, limit: int = 3) -> tuple[list[fmt.Html], list[list[InlineButton]]]:
        async with self.c.db.session() as s:
            items = await self.c.agenda.attention(s, self.c.clock.now(), limit=limit)
        return attention_block(items)

    async def cmd_focus(self, chat_id: int) -> None:
        """🔥 Главное: overdue / missed / soon tasks with buttons, deadlines, top-priority tasks."""
        await self._fresh_tasks()
        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        lines_att, rows = await self._attention(limit=5)
        async with self.c.db.session() as s:
            active = list(await self.c.task_ops.active(s))
        shown = {r[0].callback_data[3:] for r in rows}
        deadlines = sorted(
            (t for t in active if t.deadline_date and today <= t.deadline_date <= today + timedelta(days=3)),
            key=lambda t: t.deadline_date or today,
        )
        urgent = [t for t in active if t.priority == 4 and t.id not in shown][:3]
        head = fmt.title("🔥", f"Главное, {self.persona.address}" if self.persona.enabled else "Главное")
        extra: list[object] = []
        if deadlines:
            extra.append(
                fmt.line(
                    "⏳", fmt.b("Дедлайны:"), "; ".join(f"«{t.content}» {t.deadline_date:%d.%m}" for t in deadlines)
                )
            )
        if urgent:
            extra.append(fmt.line("🔴", fmt.b("Приоритет 1:"), "; ".join(f"«{t.content}»" for t in urgent)))
        if not lines_att and not extra:
            calm = f"Ничего не горит, {self.persona.address}. Всё под контролем." if self.persona.enabled else ""
            await self.reply(chat_id, fmt.line("🔥", calm or "Ничего не горит — всё под контролем."))
            return
        bullets, hint_line = (lines_att[1:-1], lines_att[-1:]) if lines_att else ([], [])  # attention_block layout
        text_msg = fmt.blocks(head, bullets, extra, hint_line)
        await self.reply(chat_id, text_msg, inline_keyboard(rows) if rows else None)

    async def cmd_suggest_next(self, chat_id: int) -> None:
        """«Чем бы мне заняться?» — free time until the next meeting + up to 3 tasks, ranked by code."""
        await self._fresh_tasks()
        now = self.c.clock.now()
        tz = self.c.settings.tz
        today = to_local(now, tz).date()
        st = await self._stats()
        async with self.c.db.session() as s:
            active = list(await self.c.task_ops.active(s))
        picks: list[tuple[TodoistTask, str]] = [(a.task, a.note) for a in st["attention"]]
        taken = {t.id for t, _ in picks}

        def add(tasks: list[TodoistTask], why: str) -> None:
            for task in tasks:
                if task.id not in taken and len(picks) < 3 and not descendants(task.id, active):
                    picks.append((task, why))
                    taken.add(task.id)

        add(sorted((t for t in active if t.due_date == today), key=lambda t: -t.priority), "на сегодня")
        add(
            sorted(
                (t for t in active if t.deadline_date and t.deadline_date <= today + timedelta(days=7)),
                key=lambda t: t.deadline_date or today,
            ),
            "скоро дедлайн",
        )
        add(sorted((t for t in active if t.due_date is None and t.priority >= 3), key=lambda t: -t.priority), "важная")
        add(sorted((t for t in active if t.due_date is None), key=lambda t: t.content), "давно ждёт")
        free_line = None
        if st["next_event"] is not None:
            e = st["next_event"]
            free = int((e.start_at - now).total_seconds() // 60)
            free_line = fmt.line(
                "⏳",
                f"До «{e.summary}» в {format_local(e.start_at, tz)} свободно",
                fmt.Html(f"{fmt.b(human_minutes(free))}."),
            )
        if not picks:
            await self.reply(chat_id, "Все задачи закрыты — можно отдохнуть. 👏")
            return
        rows = []
        items = []
        for i, (task, why) in enumerate(picks, 1):
            size = f", ~{task.duration_minutes} мин" if task.duration_minutes else ""
            items.append(fmt.line(f"{i}.", fmt.b(f"«{task.content}»"), fmt.Html(f"— {fmt.i(why + size)}")))
            title = task.content if len(task.content) <= 26 else task.content[:25] + "…"
            rows.append([InlineButton(f"✅ {title}", f"sd:{task.id}")])
        head = fmt.title("🎯", f"Предлагаю, {self.persona.address}" if self.persona.enabled else "Предлагаю")
        text_msg = fmt.blocks(fmt.lines(free_line, head), items, fmt.hint(MOVE_HINT))
        await self.reply(chat_id, text_msg, inline_keyboard(rows))

    async def _suggested_done(self, chat_id: int, task_id: str) -> str:
        async with self.c.db.session() as s:
            task = await s.get(TodoistTask, task_id)
        if task is None or task.checked or task.is_deleted:
            await self.reply(chat_id, "Эта задача уже закрыта.")
            return "Уже закрыта"
        token = PENDING_ACK.set(self.persona.ack())
        try:
            day = self.c.clock.now().date().isoformat()
            await self._run_task_op(chat_id, "complete", task, {}, request_id=f"sd-{task.id}-{day}")
        finally:
            PENDING_ACK.reset(token)
        return "Готово"

    async def _suggested_move(self, chat_id: int, task_id: str) -> str:
        async with self.c.db.session() as s:
            task = await s.get(TodoistTask, task_id)
        if task is None or task.checked or task.is_deleted:
            await self.reply(chat_id, "Эта задача уже закрыта.")
            return "Уже закрыта"
        if task.due_is_recurring:
            await self.reply(chat_id, "Это повторяющаяся задача — её лучше отметить выполненной.")
            return "Повторяющаяся"
        await self._ask_due(chat_id, task)
        return "Выберите дату"

    # --- medications & supplements --------------------------------------------------------------------

    async def cmd_med_add(self, chat_id: int, routed: RoutedIntent) -> None:
        from jarvis.meds.service import parse_course, parse_times, split_dose, split_name_and_times

        body = " ".join(part for part in (routed.task_text, routed.due_string) if part).strip()
        today = to_local(self.c.clock.now(), self.c.settings.tz).date()
        body, course, course_ok = parse_course(body, today)  # «курсом 8 недель» / «до 15 января» — before the times
        if not course_ok:
            await self.reply(
                chat_id,
                "Срок курса не понял: дата окончания уже прошла или курс длиннее 3 лет. "
                "Например: «напоминай пить магний в 21:00 курсом 8 недель» или «… до 15 января».",
            )
            return
        name_part, times_part = split_name_and_times(body)
        times = parse_times(times_part) or parse_times(body)
        name, dose = split_dose(name_part) if name_part else ("", None)
        if routed.note_text and not dose:
            dose = routed.note_text.strip()[:60]
        if not name:
            await self.reply(chat_id, "Что напоминать принимать? Например: «напоминай пить омегу-3 в 9:00 и 21:00».")
            return
        if not times:
            await self.reply(
                chat_id,
                f"Во сколько напоминать про «{name}»? Например: «напоминай пить {name.lower()} в 9:00 и 21:00».",
            )
            return
        item = await self._lmf_item(f"{name} {dose or ''}".strip())
        async with self.c.db.session() as s:
            med, created = await self.c.meds.add(
                s, name, dose, times, course.ends_on if course else None, now=self.c.clock.now()
            )
            if item is not None:
                med.items = [{**item, "label": med.name}]
        when = " и ".join(med.times)
        dose_text = f" — {med.dose}" if med.dose else ""
        head = "Буду напоминать:" if created else "Обновил время:"
        course_line = None
        if med.ends_on is not None:
            length = "" if course is None or course.said.startswith("до ") else f"{course.said}, "
            course_line = fmt.line(
                "🗓 Курс:", fmt.b(f"{length}до {med.ends_on:%d.%m.%Y}"), "— потом напоминания остановлю и скажу."
            )
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.line("💊", head, fmt.b(med.name), f"{dose_text.strip()} · {when}.".strip(" ·")),
                course_line,
                fmt.hint("В напоминании будут кнопки: ✅ принял · ⏰ через 30 мин · ✖️ пропустить."),
            ),
        )

    async def _lmf_item(self, said: str) -> dict[str, Any] | None:
        """A single course («омега-3 1000 мг») linked to its LMF supplement, so «✅ Принял» lands in TLT."""
        from jarvis.tracker.supplements import resolve, split_items

        try:
            supplements = (await self.c.tracker.dictionary()).supplement_list
        except (TransientError, NotConfiguredError):
            return None
        phrases = split_items(said)
        if len(phrases) != 1:
            return None
        taken = resolve(phrases[0], supplements, {})
        return taken.item() if taken.supplement is not None else None

    async def cmd_meds(self, chat_id: int) -> None:
        from jarvis.meds.service import is_pack, items_text, pack_emoji

        now = self.c.clock.now()
        tz = self.c.settings.tz
        async with self.c.db.session() as s:
            meds = list(await self.c.meds.active(s))
            plan = await self.c.meds.today(s, now)
            adherence = {m.id: await self.c.meds.adherence(s, m, now) for m in meds}
        if not meds:
            await self.reply(
                chat_id,
                "💊 Напоминаний о лекарствах пока нет.\nДобавить: «напоминай пить омегу-3 в 9:00 и 21:00» "
                "или «добавь витамин D утром».",
            )
            return
        marks = {"taken": "✅", "skipped": "✖️", "missed": "⚠️", "pending": "⏳", "planned": "·"}
        cards = []
        for med in meds:
            today = ", ".join(
                f"{format_local(at, tz)} {marks.get(status, '')}".strip() for m, at, status in plan if m.id == med.id
            )
            taken, due = adherence[med.id]
            week = f"за 7 дней {taken}/{due}" if due else None
            if med.ends_on is not None:
                left = (med.ends_on - to_local(now, tz).date()).days + 1
                ends = f"курс до {med.ends_on:%d.%m.%Y}" + (f", осталось {left} дн." if left > 0 else "")
                week = f"{week} · {ends}" if week else ends
            what = items_text(med) if is_pack(med) else med.dose
            cards.append(
                fmt.lines(
                    fmt.line(pack_emoji(med.name), fmt.b(med.name), fmt.Html(f"— {fmt.i(what)}") if what else None),
                    fmt.line(today or "сегодня нет", fmt.i(f"· {week}") if week else None),
                )
            )
        text_msg = fmt.blocks(
            fmt.title("💊", "Лекарства и БАДы"),
            *cards,
            fmt.lines(
                fmt.hint("✅ принят · ⏳ ждёт отметки · ✖️ пропущен · ⚠️ не отмечен"),
                fmt.hint("Убрать — «больше не напоминай про …», время — «утренние таблетки в 8:30»."),
            ),
        )
        await self.reply(chat_id, text_msg)

    async def cmd_med_extend(self, chat_id: int, name: str | None, duration: str | None) -> None:
        """«продли магний на 2 недели»: a course (running or ended within 30 days) gets more days; reopened if ended."""
        from jarvis.meds.service import parse_course

        now = self.c.clock.now()
        today = to_local(now, self.c.settings.tz).date()
        _, course, _ = parse_course(f"на {duration or ''}", today)
        if course is None or course.said.startswith("до "):
            await self.reply(chat_id, "На сколько продлить? Например: «продли магний на 2 недели».")
            return
        n_text, _, unit = course.said.partition(" ")
        async with self.c.db.session() as s:
            found = await self.c.meds.find(s, name, ended_since=today - timedelta(days=30))
            if len(found) != 1:
                names = ", ".join(m.name for m in found[:6])
                await self.reply(
                    chat_id,
                    f"Какой курс продлить: {names}?" if found else f"Не нашёл курс «{name}» среди напоминаний.",
                )
                return
            med = found[0]
            if med.ends_on is None:
                await self.reply(chat_id, f"«{med.name}» без срока — напоминаю каждый день, продлевать не нужно.")
                return
            end = self.c.meds.extend(med, float(n_text.replace(",", ".")), unit, now)
        await self.reply(
            chat_id,
            fmt.line(
                "🗓 Продлил курс",
                fmt.b(med.name),
                f"на {course.said.replace('неделя', 'неделю')}: теперь до {end:%d.%m.%Y}.",
            ),
        )

    async def cmd_med_remove(self, chat_id: int, name: str | None) -> None:
        async with self.c.db.session() as s:
            found = await self.c.meds.find(s, name)
        if not found:
            await self.reply(chat_id, f"Не нашёл «{name}» среди напоминаний о лекарствах.")
            return
        if len(found) > 1:
            rows = [[InlineButton(f"🗑 {m.name[:30]}", f"mx:{m.id.hex}")] for m in found[:6]]
            await self.reply(chat_id, "Нашёл несколько — какое убрать?", inline_keyboard(rows))
            return
        await self._confirm_med_removal(chat_id, found[0].id)

    async def _confirm_med_removal(self, chat_id: int, medication_id: uuid.UUID) -> str:
        from jarvis.db.models import Medication

        async with self.c.db.session() as s:
            med = await s.get(Medication, medication_id)
        if med is None or not med.active:
            await self.reply(chat_id, "Этого напоминания уже нет.")
            return "Уже убрано"
        keyboard = inline_keyboard(
            [[InlineButton("🗑 Да, убрать", f"my:{med.id.hex}"), InlineButton("✖️ Оставить", "mn:0")]]
        )
        await self.reply(chat_id, f"Больше не напоминать про «{med.name}»?", keyboard)
        return "Подтвердите"

    async def _remove_med(self, chat_id: int, medication_id: uuid.UUID) -> str:
        async with self.c.db.session() as s:
            med = await self.c.meds.remove(s, medication_id)
        if med is None:
            await self.reply(chat_id, "Этого напоминания уже нет.")
            return "Уже убрано"
        await self.reply(chat_id, f"🗑 Больше не напоминаю про «{med.name}».")
        return "Убрано"

    async def cmd_med_taken(self, chat_id: int, name: str | None, day_text: str | None = None) -> None:
        """«выпил утренние (кроме цинка)» marks a reminder; anything else («выпил энергетик», «креатин 5 г») is a
        supplement taken «по факту» → TLT after «✅ Записать».

        A dictated list may be long: sentences come joined by «;», several names in one sentence are split, packs and
        courses among them are marked and everything else is one draft. Another day («вчера выпил утренние»): nothing
        is marked — a pack goes into the draft by its items."""
        from jarvis.tracker.supplements import split_items

        text = (name or "").strip()
        now = self.c.clock.now()
        day = self.c.tracker.resolve_day(day_text, now) if day_text else None
        if day_text and day is None:
            await self.reply(chat_id, f"Не понял день «{day_text}» (будущие дни не записываю).")
            return
        if day == self.c.tracker.today(now):
            day = None
        parts = [p.strip(" .!") for p in text.split(";") if p.strip(" .!")] or [text]
        rest: list[str] = []
        for part in parts:
            base, except_part = part, ""
            if m := re.search(r"\s+кроме\s+", part):
                base, except_part = part[: m.start()], part[m.end() :]
            phrases = split_items(base)
            if len(phrases) <= 1:
                async with self.c.db.session() as s:
                    found = await self.c.meds.find(s, base)
                if len(found) == 1:
                    rest += await self._med_said_taken(chat_id, found[0], except_part, day)
                    continue
                if len(found) > 1 and len(parts) == 1:
                    await self.reply(chat_id, "Уточните, что именно: " + ", ".join(m.name for m in found))
                    return
                rest.append(part)
                continue
            for phrase in phrases:  # «выпил утренние и креатин 5 г»: the pack is marked, the rest is drafted
                found = []
                if phrase.amount is None:
                    async with self.c.db.session() as s:
                        found = await self.c.meds.find(s, " ".join(phrase.words))
                if len(found) == 1:
                    rest += await self._med_said_taken(chat_id, found[0], "", day)
                else:
                    rest.append(phrase.said)
        if rest:
            await self._supplements_by_fact(chat_id, "; ".join(rest), day)

    async def _med_said_taken(self, chat_id: int, med: Any, except_part: str, day: date | None) -> list[str]:
        """A pack or a course named in «выпил …»: today it is marked (and written to TLT at once); for another day its
        items are returned for the draft."""
        from jarvis.meds.service import items_text

        indexes = None
        if except_part and med.items:
            skip = re.split(r",|\s+и\s+", except_part)
            indexes = [
                i
                for i, item in enumerate(med.items)
                if not any(matches_stems(w, f"{item.get('label')} {item.get('said')}", 4) for w in skip if w.strip())
            ]
        if day is not None:
            chosen = med.items if indexes is None else [med.items[i] for i in indexes]
            return [str(i.get("said") or i.get("label")) for i in chosen] if med.items else [med.name]
        async with self.c.db.session() as s:
            intake = await self.c.meds.mark_taken_now(s, med, self.c.clock.now(), items=indexes)
        if intake is None:
            await self.reply(chat_id, f"Сегодняшние приёмы «{med.name}» уже отмечены.")
            return []
        at = format_local(intake.due_at, self.c.settings.tz)
        what = f": {items_text(med, indexes)}" if indexes is not None else ""
        head = f"✅ Отметил приём: {med.name} ({at}){what}."
        await self.reply(chat_id, fmt.lines(head, await self._log_intake(med, intake)))
        return []

    async def _supplements_by_fact(
        self, chat_id: int, text: str, day: date | None = None, kind: Literal["pill", "food"] = "pill"
    ) -> None:
        """Supplements said by fact — and food said with them («выпил магний 400 и стакан кефира») → one draft.
        Names Jarvis does not know stay out of it («❔ Не понял») — the rest is still offered; only when nothing is
        known it asks whether this is food or a new LMF supplement."""
        from jarvis.meds.service import split_dose
        from jarvis.tracker.food import meal_of
        from jarvis.tracker.service import TrackerDraft, supplement_entry

        portions = await self._portions()
        intake = await self._tracker_call(chat_id, self.c.tracker.intake(text, portions, kind))
        if intake is None:
            return
        taken, food = intake.taken, intake.food
        if not taken and not food:
            await self.reply(chat_id, "Что именно выпили? Например: «выпил креатин 5 г», «выпил энергетик».")
            return
        if need := next((t for t in taken if t.needs_portion), None):
            await self._ask_portion(chat_id, str(need.portion), text, day, kind)
            return
        known = [t for t in taken if t.supplement is not None]
        if not known and not food and (unclear := next((t for t in taken if t.candidates), None)):
            names = ", ".join(c.title for c in unclear.candidates[:8])
            await self.reply(chat_id, f"Уточните «{unclear.said}»: {names}.")
            return
        if not known and not food:
            if await self._is_food(text):  # «выпил йогурт»: a product of the food directory
                await self.cmd_food_log(chat_id, text, with_pills=False)
                return
            said = taken[0].said
            label = split_dose(said)[0]  # «ашваганду 600 мг» → «Ашваганда»
            async with self.c.db.session() as s:
                approval = await self._lmf_supplement_approval(s, chat_id, label)
                choice = await offer_choice(
                    s, chat_id=chat_id, kind="as_food", payload={"text": text}, now=self.c.clock.now()
                )
            rows = [
                [choice_button(choice, "f", "🍽 Это еда — посчитать КБЖУ")],
                [
                    InlineButton(f"➕ Добавить «{label[:24]}»", f"ap:a:{approval.nonce}"),
                    InlineButton("✖️ Не надо", f"ap:r:{approval.nonce}"),
                ],
            ]
            await self.reply(
                chat_id,
                f"Не нашёл «{said}» ни в напоминаниях, ни в справочнике добавок LMF. Это еда или добавка?",
                inline_keyboard(rows),
            )
            return
        now = self.c.clock.now()
        day = day or self.c.tracker.today(now)
        draft = TrackerDraft(day=day.isoformat())
        if food:
            prepared = await self._tracker_call(
                chat_id, self.c.tracker.prepare_food("; ".join(food), now, day=day, meal=meal_of(text))
            )
            if prepared is None:
                return
            if prepared.error and not known:
                await self.reply(chat_id, prepared.error)
                return
            draft = prepared.draft
        draft.supplements_add = [supplement_entry(t) for t in known]
        draft.food_add += [
            {**item, "meal": None, "time": format_local(now, self.c.settings.tz)}
            for item in await self.c.tracker.drink_food(known)
        ]
        extra: list[list[InlineButton]] = []
        for t in taken:
            if t.supplement is not None:
                continue
            if t.candidates:
                draft.unknown.append(f"{t.said} — какой: {', '.join(c.title for c in t.candidates[:4])}?")
                continue
            draft.unknown.append(t.said)
            label = split_dose(t.said)[0]
            if len(extra) < 2 and re.search(r"\d", t.said):  # «ашваганда 600 мг» — a dose: likely a new supplement
                async with self.c.db.session() as s:
                    approval = await self._lmf_supplement_approval(s, chat_id, label)
                extra.append([InlineButton(f"➕ Добавить «{label[:24]}» в LMF", f"ap:a:{approval.nonce}")])
        await self._tracker_confirm(chat_id, draft, extra)

    async def _lmf_supplement_approval(self, s: Any, chat_id: int, label: str) -> Any:
        return await request_approval(
            s,
            kind="lmf_supplement",
            target_type="lmf_supplement",
            target_id=label[:80],
            now=self.c.clock.now(),
            ttl=timedelta(days=1),
            payload={"name": label, "unit": "", "chat_id": chat_id},
        )

    async def _is_food(self, text: str) -> bool:
        """The words name a product of the food directory (LMF «Питание»)."""
        from jarvis.tracker.food import find_product, split_food

        try:
            products = await self.c.lmf_api.food_products() if self.c.lmf_api is not None else []
        except Exception:
            return False
        return any(find_product(p.words, products) for p in split_food(text))

    async def _log_intake(self, med: Any, intake: Any) -> fmt.Html | None:
        """A taken reminder → TLT now; LMF down → a job retries and the user is told it will follow."""
        if not med.items:
            return None
        items = med.items if intake.items_taken is None else [med.items[i] for i in intake.items_taken]
        day = intake.due_at.astimezone(self.c.settings.tz).date()
        try:
            return await self.c.tracker.log_items(items, day, self.c.clock.now())
        except (TransientError, NotConfiguredError) as exc:
            if isinstance(exc, NotConfiguredError):
                return None
            async with self.c.db.session() as s:
                await enqueue(
                    s,
                    job_type="supplements.log",
                    run_at=self.c.clock.now() + timedelta(minutes=1),
                    payload={"intake_id": str(intake.id)},
                    dedupe_key=f"supplements.log:{intake.id}",
                    priority=30,
                )
            return fmt.hint("TLT сейчас недоступен — запишу туда, как только LMF ответит.")

    async def _portions(self) -> dict[str, Any]:
        async with self.c.db.session() as s:
            pref = await s.get(UserPreference, PORTIONS_KEY)
        return dict(pref.value) if pref is not None else {}

    async def _save_portion(self, name: str, supplement: str, dose: float) -> None:
        async with self.c.db.session() as s:
            pref = await s.get(UserPreference, PORTIONS_KEY)
            value = {name: {"supplement": supplement, "dose": dose}}
            if pref is None:
                s.add(UserPreference(key=PORTIONS_KEY, value=value))
            else:
                pref.value = {**pref.value, **value}

    async def _ask_portion(
        self, chat_id: int, portion: str, text: str, day: date | None = None, kind: str = "pill"
    ) -> None:
        payload = {"portion": portion, "text": text, "day": day.isoformat() if day else None, "kind": kind}
        async with self.c.db.session() as s:
            choice = await offer_choice(s, chat_id=chat_id, kind="portion", payload=payload, now=self.c.clock.now())
        rows = [[choice_button(choice, key, f"{dose} мг") for key, dose in PORTION_OPTIONS.items()]]
        rows.append([choice_button(choice, "x", "✖️ Отмена")])
        await self.reply(
            chat_id,
            f"☕ Сколько кофеина в «{portion}»? Запомню и дальше буду считать сам.\n"
            f"Или скажите точно: «{portion} — 160 мг кофеина».",
            inline_keyboard(rows),
        )

    async def _portion_pick(self, chat_id: int, payload: dict[str, Any], option: str) -> str:
        if option == "x" or option not in PORTION_OPTIONS:
            await self.reply(chat_id, "Ок, ничего не записываю.")
            return "Отменено"
        caffeine = await self._caffeine_name()
        await self._save_portion(str(payload["portion"]), caffeine, float(PORTION_OPTIONS[option]))
        day = date.fromisoformat(payload["day"]) if payload.get("day") else None
        kind: Literal["pill", "food"] = "food" if payload.get("kind") == "food" else "pill"
        await self._supplements_by_fact(chat_id, str(payload.get("text") or payload["portion"]), day, kind)
        return "Запомнил"

    async def _caffeine_name(self) -> str:
        try:
            dictionary = await self.c.tracker.dictionary()
        except (TransientError, NotConfiguredError):
            return "Caffeine"
        found = next((x for x in dictionary.supplement_list if {"caffeine", "кофеин"} & x.tokens), None)
        return found.name if found else "Caffeine"

    async def cmd_med_portion(self, chat_id: int, text: str) -> None:
        """«энергетик — 160 мг кофеина» → remembered; «выпил 2 энергетика» = кофеин 320 мг."""
        from jarvis.tracker.supplements import portion_key

        m = re.match(
            r"^(?:в\s+(?:мо[её]м\s+|одн\w+\s+)?)?(?P<drink>.+?)\s*(?:—|–|-|:|=|это|содержит)?\s*"
            r"(?P<dose>\d+(?:[.,]\d+)?)\s*(?P<unit>мг|г|мкг)\s+кофеин\w*$",
            text.strip().lower(),
        )
        if not m:
            await self.reply(chat_id, "Скажите так: «энергетик — 160 мг кофеина».")
            return
        drink = m.group("drink").strip(" —-:")
        name = portion_key(drink.split(), {}) or drink
        dose = float(m.group("dose").replace(",", ".")) * {"г": 1000.0, "мг": 1.0, "мкг": 0.001}[m.group("unit")]
        await self._save_portion(name, await self._caffeine_name(), dose)
        await self.reply(
            chat_id, f"☕ Запомнил: {name} = кофеин {dose:g} мг. «Выпил {name}» запишу в TLT как кофеин {dose:g} мг."
        )

    async def cmd_med_pack(self, chat_id: int, text: str) -> None:
        """«утренние таблетки: D3 6000, B12 500, цинк 25 в 9:00» · «добавь в утренние омегу» · «убери из утренних
        цинк» · «утренние таблетки в 8:30». One reminder for the whole pack; taken items go to TLT."""
        from jarvis.db.models import Medication
        from jarvis.meds.service import item_label, items_text, pack_emoji, parse_pack_command
        from jarvis.tracker.supplements import resolve, split_items

        cmd = parse_pack_command(text)
        if cmd is None:
            await self.reply(chat_id, "Например: «утренние таблетки: D3 6000, B12 500, цинк 25 в 9:00».")
            return
        async with self.c.db.session() as s:
            existing = [m for m in await self.c.meds.active(s) if m.name == cmd.name]
        pack = existing[0] if existing else None
        if cmd.action in ("show", "time", "remove") and pack is None:
            await self.reply(
                chat_id, f"Набора «{cmd.name}» ещё нет. Задайте: «{cmd.name.lower()} таблетки: D3 6000, цинк 25»."
            )
            return
        items: list[dict[str, Any]] = []
        unknown: list[str] = []
        if cmd.action in ("set", "add"):
            try:
                supplements = (await self.c.tracker.dictionary()).supplement_list
            except (TransientError, NotConfiguredError):
                supplements = []  # LMF is down: remember the words, match them when writing to TLT
            for phrase in split_items(cmd.items):
                taken = resolve(phrase, supplements, {})
                if taken.candidates:
                    names = ", ".join(c.title for c in taken.candidates[:8])
                    await self.reply(chat_id, f"Уточните «{taken.said}»: {names}.")
                    return
                if taken.supplement is not None:
                    items.append(taken.item())
                    continue
                label = " ".join(phrase.words)
                label = label[:1].upper() + label[1:]
                amount = f"{phrase.amount:f}".rstrip("0").rstrip(".") if phrase.amount is not None else ""
                dose_text = f"{amount} {phrase.unit or ''}".strip() or None
                items.append({"said": phrase.said, "supplement": None, "label": label, "dose": None, "unit": None,
                              "dose_text": dose_text})  # fmt: skip
                if supplements:
                    unknown.append(label)
        async with self.c.db.session() as s:
            if cmd.action == "set":
                med, created = await self.c.meds.set_pack(
                    s, cmd.name, items, cmd.times or [cmd.default_at], now=self.c.clock.now()
                )
                head = "Буду напоминать" if created else "Обновил набор"
            elif pack is None:  # «добавь в утренние омегу» before the pack exists → create it
                med, _ = await self.c.meds.set_pack(s, cmd.name, items, [cmd.default_at], now=self.c.clock.now())
                head = "Буду напоминать"
            else:
                loaded = await s.get(Medication, pack.id)
                assert loaded is not None
                med = loaded
                if cmd.action == "time":
                    med.times = sorted({t.strftime("%H:%M") for t in cmd.times})
                    head = "Новое время"
                elif cmd.action == "add":
                    await self.c.meds.edit_pack(s, med, add=items)
                    head = "Добавил"
                elif cmd.action == "remove":
                    _, removed = await self.c.meds.edit_pack(s, med, remove=re.split(r",|\s+и\s+", cmd.items))
                    if not removed:
                        await self.reply(chat_id, f"В наборе «{med.name}» нет «{cmd.items}»: {items_text(med)}.")
                        return
                    head = "Убрал " + ", ".join(removed) + ";"
                else:
                    head = "Набор"
            when = " и ".join(med.times)
            listing = " · ".join(item_label(i) for i in med.items or []) or "пусто"
        lines: list[object] = [fmt.line(pack_emoji(med.name), head, fmt.b(med.name), f"· {when}:", f"{listing}.")]
        if cmd.action in ("set", "time") or head == "Буду напоминать":
            lines.append(
                fmt.hint(
                    f"Каждый день в {when} пришлю напоминание: ✅ Выпил всё · ☑ Не всё · ⏰ 30 мин · ✖️ Пропустить. "
                    "Отмеченное запишу в TLT само."
                )
            )
        rows: list[list[InlineButton]] = []
        for label in unknown[:3]:
            lines.append(f"«{label}» нет в справочнике LMF — напоминать буду, в TLT не попадёт.")
            async with self.c.db.session() as s:
                approval = await request_approval(
                    s,
                    kind="lmf_supplement",
                    target_type="lmf_supplement",
                    target_id=label[:80],
                    now=self.c.clock.now(),
                    ttl=timedelta(days=1),
                    payload={"name": label, "unit": "", "chat_id": chat_id},
                )
            rows.append([InlineButton(f"➕ Добавить «{label[:24]}» в LMF", f"ap:a:{approval.nonce}")])
        await self.reply(chat_id, fmt.lines(*lines), inline_keyboard(rows) if rows else None)

    async def _pack_picker(self, chat_id: int, intake_id: uuid.UUID) -> str:
        """«☑ Не всё»: a message with one toggle per item and «✅ Готово»."""
        from jarvis.db.models import Medication, MedicationIntake

        async with self.c.db.session() as s:
            intake = await s.get(MedicationIntake, intake_id)
            med = await s.get(Medication, intake.medication_id) if intake is not None else None
        if intake is None or med is None or intake.status != "pending":
            return "Уже отмечено"
        at = format_local(intake.due_at, self.c.settings.tz)
        await self.reply(
            chat_id,
            f"Что выпили из «{med.name}» ({at})? Отметьте и нажмите «Готово».",
            self._picker_markup(med, intake),
        )
        return "Выберите"

    @staticmethod
    def _picker_markup(med: Any, intake: Any) -> dict[str, Any]:
        from jarvis.meds.service import item_label

        chosen = set(intake.items_taken or [])
        hexid = intake.id.hex
        rows = [
            [InlineButton(("☑ " if i in chosen else "☐ ") + item_label(item)[:50], f"mi:{hexid}:{i}")]
            for i, item in enumerate(med.items or [])
        ]
        rows.append([InlineButton("✅ Готово", f"mf:{hexid}"), InlineButton("✖️ Ничего", f"md:s:{hexid}")])
        return inline_keyboard(rows)

    async def _pack_toggle(self, chat_id: int, intake_id: uuid.UUID, index: int, callback: dict[str, Any]) -> str:
        from jarvis.db.models import Medication

        async with self.c.db.session() as s:
            intake = await self.c.meds.toggle_item(s, intake_id, index)
            med = await s.get(Medication, intake.medication_id) if intake is not None else None
        if intake is None or med is None:
            return "Уже отмечено"
        message_id = (callback.get("message") or {}).get("message_id")
        if self.c.telegram is not None and message_id:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), self._picker_markup(med, intake))
        return "Отмечено"

    async def _pack_done(self, chat_id: int, intake_id: uuid.UUID, callback: dict[str, Any]) -> str:
        from jarvis.db.models import MedicationIntake
        from jarvis.meds.service import items_text

        async with self.c.db.session() as s:
            current = await s.get(MedicationIntake, intake_id)
            chosen = list(current.items_taken or []) if current is not None else []
        if current is not None and current.status == "pending" and not chosen:
            return "Отметьте хотя бы одну — или «✖️ Ничего»"
        async with self.c.db.session() as s:
            result = await self.c.meds.answer(s, intake_id, "taken", self.c.clock.now(), items=chosen)
        if result is None:
            return "Уже отмечено"
        intake, med = result
        message_id = (callback.get("message") or {}).get("message_id")
        if self.c.telegram is not None and message_id:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
        await self.reply(
            chat_id,
            fmt.lines(f"✅ Отметил: {med.name} — {items_text(med, chosen)}.", await self._log_intake(med, intake)),
        )
        return "Готово"

    async def _med_answer(self, chat_id: int, action: str, intake_id: uuid.UUID, callback: dict[str, Any]) -> str:
        choice = {"t": "taken", "s": "skipped", "z": "snooze"}[action]
        async with self.c.db.session() as s:
            result = await self.c.meds.answer(s, intake_id, choice, self.c.clock.now())
        if result is None:
            return "Уже отмечено"
        _intake, med = result
        message_id = (callback.get("message") or {}).get("message_id")
        if self.c.telegram is not None and message_id:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
        text = {
            "taken": f"✅ Отметил: {med.name}.",
            "skipped": f"Пропуск отмечен: {med.name}.",
            "snooze": f"⏰ Напомню про {med.name} через 30 минут.",
        }[choice]
        logged = await self._log_intake(med, _intake) if choice == "taken" else None
        token = PENDING_ACK.set(self.persona.ack() if choice != "skipped" else None)
        try:
            await self.reply(chat_id, fmt.lines(text, logged))
        finally:
            PENDING_ACK.reset(token)
        return "Готово"

    # --- Jarvis for Mac ------------------------------------------------------------------------------

    async def _mac_env(self, chat_id: int) -> Any:
        async with self.c.db.session() as s:
            env = await self.c.desktop.env(s)
        if env is None:
            await self.reply(chat_id, MAC_NOT_CONNECTED)
        return env

    def _mac_offline_note(self, env: Any) -> str:
        if env.online(self.c.clock.now()):
            return ""
        return "\nMac сейчас не на связи — выполню, как только он появится (в течение 10 минут)."

    async def cmd_mac_open(self, chat_id: int, said: str | None) -> None:
        """«Открой телегу / ютуб / папку загрузки / проект джарвис» → an action the Mac app pulls."""
        from jarvis.desktop.service import Ambiguous, resolve_open

        if not (said or "").strip():
            await self.reply(chat_id, "Что открыть? Например: «открой Safari», «открой папку Загрузки».")
            return
        env = await self._mac_env(chat_id)
        if env is None:
            return
        target = resolve_open(str(said), env)
        if isinstance(target, Ambiguous):
            await self.reply(chat_id, f"Уточните, что открыть: {', '.join(target.options)}.")
            return
        if target is None and self.c.sites.api is not None:
            found = await self._site_by_name(str(said))
            if found is not None:
                await self.cmd_site_open(chat_id, str(said))
                return
        if target is None:
            await self.reply(
                chat_id,
                f"Не нашёл на Mac «{said}». Например: «открой Safari», «открой папку Загрузки», "
                "«открой сайт youtube.com», «открой проект JarvisOS».",
            )
            return
        async with self.c.db.session() as s:
            await self.c.desktop.enqueue(s, target, self.c.clock.now(), chat_id=chat_id)
        await self.reply(chat_id, f"🖥 Открываю на Mac: {target.label}.{self._mac_offline_note(env)}")

    async def cmd_profile_remember(self, chat_id: int, fact: str) -> None:
        """«Запомни, что мой барбер — Гоги» → a fact about the user (router and agent context)."""
        from jarvis.profile import service as profile

        if not fact:
            await self.reply(chat_id, "Что запомнить? Например: «запомни, что мой барбер — Гоги».")
            return
        async with self.c.db.session() as s:
            saved, new = await profile.add(s, fact)
        head = "🧠 Запомнил" if new else "🧠 Уже помню"
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.line(head + ":", fmt.b(saved.text)), fmt.hint("Что я о вас знаю — «что ты обо мне знаешь?».")
            ),
            inline_keyboard([[InlineButton("↩️ Забыть", f"pf:{saved.id.hex}")]]),
            speak=False,
        )

    async def cmd_profile_show(self, chat_id: int) -> None:
        from jarvis.profile import service as profile

        async with self.c.db.session() as s:
            facts = await profile.active(s)
        if not facts:
            await self.reply(
                chat_id,
                fmt.lines(
                    "🧠 Пока я ничего о вас не запоминал.",
                    fmt.hint("Скажите: «запомни, что мой барбер — Гоги», «запомни: у меня аллергия на орехи»."),
                ),
            )
            return
        await self.reply(
            chat_id,
            fmt.lines(
                fmt.title("🧠", "Что я о вас знаю", str(len(facts))),
                [fmt.bullet(f.text) for f in facts[-30:]],
                fmt.hint("Забыть — «забудь про …». Это видят только разбор ваших фраз и ответы на вопросы."),
            ),
        )

    async def cmd_profile_forget(self, chat_id: int, query: str) -> None:
        from jarvis.profile import service as profile

        async with self.c.db.session() as s:
            found = profile.find(await profile.active(s), query)
            if len(found) == 1:
                await profile.forget(s, found[0].id, self.c.clock.now())
        if not found:
            await self.reply(chat_id, f"🧠 Не нашёл ничего про «{query}» в том, что помню о вас.")
            return
        if len(found) > 1:
            rows = [[InlineButton(f"↩️ {f.text[:40]}", f"pf:{f.id.hex}")] for f in found[:6]]
            await self.reply(chat_id, "🧠 Что именно забыть?", inline_keyboard(rows))
            return
        await self.reply(chat_id, fmt.line("🧠 Забыл:", fmt.i(found[0].text)))

    async def _forget_fact(self, chat_id: int, fact_id: uuid.UUID) -> str:
        from jarvis.profile import service as profile

        async with self.c.db.session() as s:
            fact = await profile.forget(s, fact_id, self.c.clock.now())
        if fact is None:
            return "Уже забыто"
        await self.reply(chat_id, fmt.line("🧠 Забыл:", fmt.i(fact.text)), speak=False)
        return "Забыл"

    async def cmd_lab_open(self, chat_id: int, command: str) -> None:
        """«Покажи / разбери макбук», «покажи плату макбука» → Jarvis Lab opens on the Mac (#explode, #part=…)."""
        what = {"explode": "разбираю MacBook", "assemble": "собираю MacBook"}.get(command, "")
        if command.startswith("part="):
            what = "показываю деталь"
        note = f": {what}" if what else ""
        page = "/lab" + (f"#{command}" if command else "")
        await self._open_jarvis(chat_id, page, f"🧪 Открываю лабораторию на Mac{note}.")

    async def cmd_os_open(self, chat_id: int, path: str) -> None:
        """«открой интерфейс» → the Jarvis OS map, «открой сайты» → the sites panel, «открой TLT» → TLT (16.10),
        «открой CRM» → the CRM on its server (24.10)."""
        if path == "/crm":
            await self.cmd_crm_open(chat_id)
            return
        text = {
            "/": "🛰 Открываю Jarvis OS на Mac — карта Jarvis.",
            "/sites": "🌐 Открываю сайты в Jarvis OS на Mac.",
            "/tlt": "📓 Открываю TLT на Mac.",
        }.get(path, "🛰 Открываю Jarvis OS на Mac.")
        await self._open_jarvis(chat_id, path, text)

    async def cmd_crm_open(self, chat_id: int) -> None:
        """The CRM on its server: opened on the Mac when it is connected, and a link button for the phone."""
        from jarvis.desktop.service import Target
        from jarvis.integrations.telegram.client import UrlButton

        url = self.c.settings.crm_url
        async with self.c.db.session() as s:
            env = await self.c.desktop.env(s)
            if env is not None:
                await self.c.desktop.enqueue(
                    s, Target("open_url", {"url": url}, "CRM"), self.c.clock.now(), chat_id=chat_id
                )
        text = f"💼 Открываю CRM на Mac.{self._mac_offline_note(env)}" if env is not None else "💼 CRM — по кнопке."
        await self.reply(chat_id, text, inline_keyboard([[UrlButton("💼 Открыть CRM", url)]]), speak=False)

    async def _open_jarvis(self, chat_id: int, path: str, text: str) -> None:
        """A Jarvis OS page in Safari on the Mac: the Mac app starts the page server (and TLT) when needed and signs
        in with the key it derives itself — no link, no key travels through here."""
        from jarvis.desktop.service import Target

        env = await self._mac_env(chat_id)
        if env is None:
            return
        async with self.c.db.session() as s:
            await self.c.desktop.enqueue(
                s, Target("open_jarvis", {"path": path}, "Jarvis OS"), self.c.clock.now(), chat_id=chat_id
            )
        await self.reply(chat_id, f"{text}{self._mac_offline_note(env)}")

    async def cmd_mac_volume(self, chat_id: int, text: str) -> None:
        """«громкость 30», «потише», «выключи звук» — an action the Mac app runs (values checked there again)."""
        from jarvis.desktop.service import Target, parse_volume, volume_label

        payload = parse_volume(text)
        if payload is None:
            await self.reply(chat_id, "🔊 Не понял громкость. Например: «громкость 30», «потише», «выключи звук».")
            return
        env = await self._mac_env(chat_id)
        if env is None:
            return
        target = Target("volume", payload, volume_label(payload))
        async with self.c.db.session() as s:
            await self.c.desktop.enqueue(s, target, self.c.clock.now(), chat_id=chat_id)
        await self.reply(chat_id, f"🔊 На Mac: {target.label}.{self._mac_offline_note(env)}")

    async def cmd_mac_copy(self, chat_id: int, text: str) -> None:
        """«скопируй на мак: …» → the Mac clipboard (only copying; pasting would need the Accessibility permission)."""
        from jarvis.desktop.service import COPY_MAX, Target

        body = text.strip()
        if not body:
            await self.reply(chat_id, "📋 Что скопировать? Например: «скопируй на мак: адрес Руставели 12».")
            return
        if len(body) > COPY_MAX:
            await self.reply(chat_id, f"📋 Слишком длинный текст (больше {COPY_MAX} символов).")
            return
        env = await self._mac_env(chat_id)
        if env is None:
            return
        target = Target("copy_text", {"text": body}, "текст в буфер обмена")
        async with self.c.db.session() as s:
            await self.c.desktop.enqueue(s, target, self.c.clock.now(), chat_id=chat_id)
        note = self._mac_offline_note(env)
        await self.reply(chat_id, f"📋 Скопирую на Mac в буфер обмена ({len(body)} симв.) — вставьте ⌘V.{note}")

    async def cmd_mac_remind(self, chat_id: int, text: str, message: dict[str, Any]) -> None:
        """«напомни на маке через 10 минут выключить чайник» → at that time a notice on the Mac (Telegram if the Mac
        is not online then)."""
        from jarvis.bot.intents import _MAC_REMIND
        from jarvis.desktop.service import mac_reminder

        m = _MAC_REMIND.match(text.lower().strip())
        rest = text.strip()[m.start("rest") :] if m else text
        now = self.c.clock.now()
        parsed = mac_reminder(rest, now, self.c.settings.tz)
        if parsed is None:
            await self.reply(
                chat_id, "🔔 Не понял, когда напомнить. Например: «напомни на маке через 10 минут выключить чайник»."
            )
            return
        title, at = parsed
        async with self.c.db.session() as s:
            await enqueue(
                s,
                job_type="desktop.notify",
                run_at=at,
                payload={"text": title[:300], "chat_id": chat_id},
                dedupe_key=f"desktop.notify:{self._request_id(chat_id, message)}",
            )
        local = to_local(at, self.c.settings.tz)
        today = to_local(now, self.c.settings.tz).date()
        day = "" if local.date() == today else f" ({human_day(local.date(), today)})"
        await self.reply(chat_id, f"🔔 Напомню на Mac в {local:%H:%M}{day}: {title}.")

    async def _site_by_name(self, said: str) -> Any:
        from jarvis.sites.service import split_where

        try:
            found, _ = await self.c.sites.find(split_where(said)[0])
        except Exception:
            return None
        return found.site

    async def cmd_claude_code(self, chat_id: int, routed: RoutedIntent) -> None:
        """Claude Code in one of the user's projects. With a task it starts only after the confirmation button
        (starting dev work needs a human «yes»); without one it just opens an interactive session there."""
        from jarvis.desktop.service import Ambiguous, Target, resolve_project

        env = await self._mac_env(chat_id)
        if env is None:
            return
        names = ", ".join(p.name for p in env.projects[:12])
        if not env.projects:
            await self.reply(
                chat_id, "Mac не сообщил ни одного проекта: в приложении Jarvis укажите папки с проектами."
            )
            return
        prompt = (routed.prompt_text or "").strip() or None
        if prompt is not None and len(prompt) > CLAUDE_PROMPT_MAX:
            await self.reply(
                chat_id,
                fmt.lines(
                    fmt.line("🤖", fmt.b("Задача для Claude Code слишком длинная.")),
                    f"В ней {len(prompt)} символов, а перед запуском я показываю задачу целиком — "
                    f"не больше {CLAUDE_PROMPT_MAX}. Сократите её или откройте Claude Code без задачи "
                    "и вставьте текст там.",
                ),
            )
            return
        project_said = routed.project_text
        if not project_said:
            async with self.c.db.session() as s:
                last = await s.get(UserPreference, CLAUDE_PROJECT_KEY)
            known = {p.name for p in env.projects}
            if last is not None and last.value.get("name") in known:
                project_said = str(last.value["name"])  # «открой Claude Code» → where it was opened last time
            else:
                async with self.c.db.session() as s:
                    await offer_choice(
                        s,
                        chat_id=chat_id,
                        kind=AWAIT_PROJECT,
                        payload={"prompt": prompt},
                        now=self.c.clock.now(),
                        ttl=AWAIT_DUE_TTL,
                    )
                await self.reply(chat_id, f"В каком проекте запустить Claude Code? Скажите название: {names}.")
                return
        project = resolve_project(project_said, env)
        if isinstance(project, Ambiguous):
            await self.reply(chat_id, f"Уточните проект: {', '.join(project.options)}.")
            return
        if project is None:
            await self.reply(chat_id, f"Не нашёл проект «{routed.project_text}». Есть: {names}.")
            return
        target = Target(
            "claude_code",
            {"path": project.path, "project": project.name, "prompt": prompt},
            f"Claude Code в «{project.name}»",
        )
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            action = await self.c.desktop.enqueue(s, target, now, chat_id=chat_id, proposed=prompt is not None)
            stmt = insert(UserPreference).values(key=CLAUDE_PROJECT_KEY, value={"name": project.name})
            await s.execute(
                stmt.on_conflict_do_update(index_elements=[UserPreference.key], set_={"value": stmt.excluded.value})
            )
        if prompt is None:
            await self.reply(chat_id, f"🤖 Открываю Claude Code в «{project.name}».{self._mac_offline_note(env)}")
            return
        keyboard = inline_keyboard(
            [[InlineButton("▶️ Запустить", f"mc:{action.id.hex}"), InlineButton("✖️ Отмена", f"mk:{action.id.hex}")]]
        )
        await self.reply(
            chat_id,
            fmt.lines(
                f"🤖 Запустить Claude Code в «{project.name}» с задачей:",
                fmt.quote(prompt, expandable=len(prompt) > 300),  # the whole task — exactly what will run
                fmt.hint("Claude откроется в Терминале на Mac и будет спрашивать разрешение на изменения."),
            ),
            keyboard,
        )

    async def _answer_pending_project(self, chat_id: int, text_in: str) -> bool:
        """The answer to «В каком проекте запустить Claude Code?»: a project name continues the launch; anything
        else is routed as a new request. Returns True when the message was consumed here."""
        from jarvis.desktop.service import resolve_project

        now = self.c.clock.now()
        async with self.c.db.session() as s:
            pending = await s.scalar(
                select(PendingChoice)
                .where(
                    PendingChoice.chat_id == chat_id,
                    PendingChoice.kind == AWAIT_PROJECT,
                    PendingChoice.status == "pending",
                    PendingChoice.expires_at > now,
                )
                .order_by(PendingChoice.created_at.desc())
                .limit(1)
            )
            env = await self.c.desktop.env(s) if pending is not None else None
        if pending is None or env is None:
            return False
        said = re.sub(r"^(?:в|во|для|на)\s+", "", normalize_command(text_in), flags=re.I)
        said = re.sub(r"^(?:проект\w*|папк\w*)\s+", "", said, flags=re.I).strip(" .!?")
        project = resolve_project(said, env) if said else None
        resolution = "project" if project is not None else "ignored"
        async with self.c.db.session() as s:
            taken = await take_choice(s, choice_id=pending.id, chat_id=chat_id, resolution=resolution, now=now)
        if taken is None or project is None:
            return False
        prompt = pending.payload.get("prompt")
        routed = RoutedIntent(intent=Intent.CLAUDE_CODE, project_text=said, prompt_text=prompt)
        token = PENDING_ACK.set(self.persona.ack())
        try:
            await self.cmd_claude_code(chat_id, routed)
        finally:
            PENDING_ACK.reset(token)
        return True

    async def _confirm_mac(self, chat_id: int, run: bool, action_id: uuid.UUID, callback: dict[str, Any]) -> str:
        async with self.c.db.session() as s:
            action = await self.c.desktop.confirm(s, action_id, self.c.clock.now(), run=run)
            env = await self.c.desktop.env(s)
        if action is None:
            return "Уже обработано или устарело"
        message_id = (callback.get("message") or {}).get("message_id")
        if self.c.telegram is not None and message_id:
            with contextlib.suppress(Exception):
                await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
        label = str(action.payload.get("label") or "действие")
        if not run:
            await self.reply(chat_id, "Ок, не запускаю.")
            return "Отменено"
        note = self._mac_offline_note(env) if env is not None else ""
        token = PENDING_ACK.set(self.persona.ack())
        try:
            await self.reply(chat_id, f"▶️ Запускаю {label}.{note}")
        finally:
            PENDING_ACK.reset(token)
        return "Запускаю"

    # --- usage statistics ----------------------------------------------------------------------------

    async def cmd_usage(self, chat_id: int, period_key: str = DEFAULT_PERIOD, *, edit_id: int | None = None) -> None:
        """📊 Usage and spend: the chosen period next to «всё время» (an aligned table) + OpenRouter balance.

        Period buttons edit the same message. HTML; every value is a number Jarvis computed itself."""
        from jarvis.db.models import AgentRun, AuditEvent, UsageCounter
        from jarvis.usage.service import PERIODS, Spend, UsageService, to_micro, usd

        period = PERIODS.get(period_key) or PERIODS[DEFAULT_PERIOD]
        now = self.c.clock.now()
        columns = [period] if period.key == "all" else [period, PERIODS["all"]]
        starts = [p.since(now) for p in columns]

        async def counts(query_for: Any) -> list[str]:
            async with self.c.db.session() as s:
                return [str(int(await s.scalar(query_for(since)) or 0)) for since in starts]

        def count(model: Any, column: Any, *where: Any) -> Any:
            return lambda since: (
                select(func.count()).select_from(model).where(*where, *([column >= since] if since else []))
            )

        def counter(provider: str) -> Any:
            return lambda since: select(func.coalesce(func.sum(UsageCounter.count), 0)).where(
                UsageCounter.provider == provider, *([UsageCounter.day >= since.date()] if since else [])
            )

        def task_op(op: str) -> Any:
            return count(
                AuditEvent,
                AuditEvent.occurred_at,
                AuditEvent.action == "TASK_UPDATED",
                AuditEvent.details["op"].astext == op,
            )

        def tokens(since: datetime | None) -> Any:
            return select(func.coalesce(func.sum(AgentRun.input_tokens + AgentRun.output_tokens), 0)).where(
                *([AgentRun.created_at >= since] if since else [])
            )

        reminders = count(
            Notification,
            Notification.sent_at,
            Notification.status == "sent",
            Notification.dedupe_key.like("reminder:%")
            | Notification.dedupe_key.like("missed:%")
            | Notification.dedupe_key.like("med:%"),
        )
        rows = [
            ("Сообщения", await counts(count(TelegramUpdate, TelegramUpdate.received_at))),
            ("Запросы к ИИ", await counts(count(AgentRun, AgentRun.created_at))),
            ("Токены ИИ", [compact_number(int(v)) for v in await counts(tokens)]),
            ("Ответы голосом", await counts(counter("tts"))),
            ("Голосовые ваши", await counts(counter("stt"))),
            ("Напоминания", await counts(reminders)),
            ("Задач создано", await counts(task_op("create"))),
            ("Задач закрыто", await counts(task_op("complete"))),
        ]
        extra: list[str] = []
        usage_api = getattr(self.c.llm, "usage", None)
        if usage_api is not None:
            try:
                key = await usage_api()
                service = UsageService()
                async with self.c.db.session() as s:
                    total = await service.record(s, key, now)  # a fresh sample with every look
                async with self.c.db.session() as s:
                    spends: list[Spend] = [await service.spend_since(s, since, total) for since in starts]
                    tracked = await service.tracked_since(s)
                rows.append(("Расход", [spend.text() for spend in spends]))
                if not all(spend.exact for spend in spends) and tracked is not None:
                    day = format_local(tracked, self.c.settings.tz, "%d.%m")
                    extra.append(f"≤ — не больше этой суммы: точный учёт расходов ведётся с {day}.")
                credits_api = getattr(self.c.llm, "credits", None)
                if credits_api is not None:
                    with contextlib.suppress(Exception):
                        credit = await credits_api()
                        balance = to_micro(credit.get("total_credits")) - to_micro(credit.get("total_usage"))
                        extra.append(f"💳 Баланс OpenRouter: <b>{usd(balance)}</b> (весь аккаунт)")
                if key.get("limit"):
                    left, limit = to_micro(key.get("limit_remaining")), to_micro(key["limit"])
                    filled = max(0, min(10, round(10 * left / limit))) if limit else 0
                    bar = "▰" * filled + "▱" * (10 - filled)
                    reset = {"monthly": " на месяц", "weekly": " на неделю", "daily": " на день"}.get(
                        str(key.get("limit_reset") or ""), ""
                    )
                    extra.append(f"🔒 Лимит Jarvis{reset}: {bar} <b>{usd(left)}</b> из {usd(limit)}")
            except Exception as exc:  # the rest of the statistics is still useful
                log.warning("openrouter usage unavailable: %s", exc)
        headers = [p.label for p in columns]
        widths = [max(len(h), *(len(values[i]) for _, values in rows)) for i, h in enumerate(headers)]
        label_width = max(len(label) for label, _ in rows)
        table = [f"{'':<{label_width}} " + "  ".join(f"{h:>{w}}" for h, w in zip(headers, widths, strict=True))]
        table += [
            f"{label:<{label_width}} " + "  ".join(f"{v:>{w}}" for v, w in zip(values, widths, strict=True))
            for label, values in rows
        ]
        lines = [
            f"📊 <b>Статистика JARVIS</b> · {period.title}",
            "",
            "<pre>" + html.escape("\n".join(table)) + "</pre>",
        ]
        lines += extra
        markup = inline_keyboard([[usage_button(PERIODS[k], period.key) for k in ("d1", "d7", "d30", "all")]])
        text_msg = "\n".join(lines)
        if edit_id is not None and self.c.telegram is not None:
            await self.c.telegram.edit_message_text(chat_id, edit_id, text_msg, reply_markup=markup, parse_mode="HTML")
            return
        await self.reply(chat_id, text_msg, markup, parse_mode="HTML")

    # --- search, small talk, weather -------------------------------------------------------------------

    async def cmd_search(self, chat_id: int, query: str) -> None:
        """«Найди …» across tasks, calendar, ideas, notes and the shopping list."""
        query = query.strip(' ?.!«»"')
        if not query:
            await self.reply(chat_id, "Что найти? Например: «найди барбера» или «где у меня код домофона».")
            return
        now = self.c.clock.now()
        tz = self.c.settings.tz
        async with self.c.db.session() as s:  # by words + by meaning (jarvis/search/service.py)
            found = await self.c.search.find_all(s, query, now)
        tasks, events, ideas, notes, shopping = found.tasks, found.events, found.ideas, found.notes, found.shopping
        sections = [
            fmt.section("Задачи", [fmt.line("•", self._task_line(t)) for t in tasks[:5]], emoji="📋"),
            fmt.section(
                "Календарь",
                [
                    fmt.line("•", fmt.b(format_local(e.start_at, tz, "%d.%m %H:%M")), e.summary)
                    for e in events
                    if e.start_at
                ],
                emoji="📅",
            ),
            fmt.section("Идеи", [fmt.bullet(i.title or i.raw_input[:80]) for i in ideas], emoji="💡"),
            fmt.section("Заметки", [fmt.bullet(n.text[:200]) for n in notes], emoji="📝"),
            fmt.section("Покупки", [fmt.bullet(i.requested_name) for i in shopping], emoji="🛒"),
        ]
        if not any(sections):
            await self.reply(
                chat_id,
                fmt.lines(
                    self.persona.sorry(f"ничего не нашёл по «{query}» в задачах, календаре, идеях и заметках."),
                    fmt.hint("Поискать в интернете — скажите «изучи …»."),
                ),
            )
            return
        if len(tasks) == 1:
            await self._set_context(chat_id, last_task_id=tasks[0].id)
        await self.reply(chat_id, fmt.blocks(fmt.title("🔎", f"«{query}»"), *sections))

    async def _mini_status(self) -> str:
        st = await self._stats()
        parts = [f"на сегодня {plural(st['today'], 'задача', 'задачи', 'задач')}"]
        if st["overdue"]:
            parts.append(f"просрочено {st['overdue']}")
        if st["next_event"] is not None:
            e = st["next_event"]
            parts.append(f"ближайшая встреча в {format_local(e.start_at, self.c.settings.tz)} — «{e.summary}»")
        text = "; ".join(parts)
        if forecast := await self.c.weather.forecast():
            from jarvis.weather.service import label, temp

            text += f". На улице {temp(forecast.current_temperature)}, {label(forecast.current_code)}"
        return text

    async def cmd_ask(self, chat_id: int, routed: RoutedIntent, raw: str) -> None:
        """A real question → the question agent (general knowledge, or the user's data through read-only looks).
        Any failure → the router's own short reply, or an honest one."""
        from jarvis.agent.service import QuestionAgent

        question = (routed.query or raw).strip()
        if self.c.telegram is not None and CHANNEL.get() == "telegram":
            with contextlib.suppress(Exception):
                await self.c.telegram.send_chat_action(chat_id, "typing")
        try:
            answer = (await QuestionAgent(self.c).answer(question)).text
        except (JarvisError, TransientError) as exc:
            log.warning("question agent failed: %s", exc)
            answer = ""
        if not answer:
            answer = routed.reply or self.persona.sorry("Не смог ответить сейчас. Спросите ещё раз чуть позже.")
        await self.reply(chat_id, answer.strip()[:3500])

    async def cmd_small_talk(self, chat_id: int, routed: RoutedIntent, raw: str) -> None:
        if routed.reply:  # the LLM wrote it (facts only from the given context)
            await self.reply(chat_id, routed.reply.strip()[:1500])
            return
        low = raw.lower()
        a = self.persona.address if self.persona.enabled else ""
        tail = f", {a}" if a else ""
        if re.search(r"спасибо|благодарю", low):
            await self.reply(chat_id, f"Всегда рад помочь{tail}.")
            return
        status = await self._mini_status()
        if re.search(r"как\s+(?:у\s+тебя\s+)?дела|как\s+ты|как\s+жизнь|поживаешь|что\s+нового", low):
            await self.reply(chat_id, f"Всё в порядке{tail}, системы работают. У вас {status}. Чем могу помочь?")
            return
        greeting = self.persona.greeting(to_local(self.c.clock.now(), self.c.settings.tz))
        await self.reply(chat_id, f"{greeting}\nУ вас {status}.")

    async def cmd_calculate(self, chat_id: int, text: str) -> None:
        """«10 000 000 ÷ 1500» — computed by code (exact fractions), answered at once."""
        try:
            result = calculate(text)
        except CalcError as exc:
            await self.reply(chat_id, f"🧮 Не посчитать: {exc}.")
            return
        if result is None:
            await self.reply(
                chat_id,
                self.persona.sorry("Не смог разобрать выражение. Например: «10 000 000 ÷ 1500», «15% от 2000»."),
            )
            return
        sign = "≈" if result.approximate else "="
        await self.reply(chat_id, fmt.line("🧮", f"{result.expression} {sign}", fmt.b(result.result)))

    async def cmd_currency(self, chat_id: int, text: str) -> None:
        """«10 000 рублей в долларах», «курс лари» — rates of open.er-api.com / CoinGecko, Decimal arithmetic."""
        request = parse_conversion(text)
        if request is None:
            await self.reply(
                chat_id,
                self.persona.sorry("Не понял, какие валюты. Например: «10 000 рублей в долларах», «курс лари»."),
            )
            return
        try:
            answer = await self.c.fx.convert(request)
        except TransientError as exc:
            await self.reply(chat_id, self.persona.sorry(f"Курсы сейчас недоступны: {exc}."))
            return
        await self.reply(chat_id, fmt.headed(answer) if answer.startswith("💱") else answer)

    # --- TLT / TPL tracker (LMF) -----------------------------------------------------------------------

    async def _tracker_call(self, chat_id: int, call: Any, retry: Any = None) -> Any:
        """LMF is a local app on the Mac: when it is down, Jarvis starts the TLT site in LocalSites and tries once
        more (`retry` builds the call again); otherwise it says so instead of failing the whole message."""
        try:
            return await call
        except (NotConfiguredError, FoodNotSupportedError) as exc:
            await self.reply(chat_id, f"📓 {exc}")
        except AuthError:
            await self.reply(chat_id, LMF_ACCESS_DENIED)
        except TransientError:
            if retry is not None and await self.c.sites.ensure_running(self.c.settings.lmf_site_name):
                await asyncio.sleep(self.LMF_START_WAIT)
                return await self._tracker_call(chat_id, retry())
            await self.reply(
                chat_id,
                self.persona.sorry(
                    "Трекер TLT сейчас недоступен: LMF не отвечает (Jarvis OS на Маке запущен?). Ничего не записал."
                ),
            )
        return None

    async def _tracker_reply(self, chat_id: int, call: Any) -> None:
        answer = await self._tracker_call(chat_id, call)
        if answer:
            await self.reply(chat_id, answer)

    async def cmd_food_log(self, chat_id: int, text: str, *, with_pills: bool = True) -> None:
        """«съел 200 г гречки»; with pills in the same phrase («съел 2 яйца и выпил D3 6000») — one draft for both.
        `with_pills=False`: the user already said it is food (the «🍽 Это еда» button)."""
        if with_pills:
            from jarvis.tracker.parse import parse_day_report

            portions = await self._portions()
            intake = await self._tracker_call(
                chat_id,
                self.c.tracker.intake(text, portions, "food"),
                lambda: self.c.tracker.intake(text, portions, "food"),
            )
            if intake is None:
                return
            if intake.taken:
                now = self.c.clock.now()
                day = self.c.tracker.resolve_day(parse_day_report(text).day_text, now)
                if day is None:
                    await self.reply(chat_id, "Будущие дни не записываю.")
                    return
                await self._supplements_by_fact(chat_id, text, day, "food")
                return
        prepared = await self._tracker_call(
            chat_id,
            self.c.tracker.prepare_food(text, self.c.clock.now()),
            lambda: self.c.tracker.prepare_food(text, self.c.clock.now()),
        )
        if prepared is not None:
            await self._tracker_next(chat_id, prepared)

    async def cmd_health(self, chat_id: int, text: str | None) -> None:
        """«какой у меня пульс», «шаги вчера», /health — what the watch and the scale sent (Health Connect)."""
        from jarvis.tracker.service import parse_past

        now = self.c.clock.now()
        today = self.c.health.today(now)
        span = parse_past(text or "", today)
        async with self.c.db.session() as s:
            answer = await self.c.health.day_text(s, span[1] if span else today, now)
        await self.reply(chat_id, answer)

    async def cmd_call_me(self, chat_id: int, rest: str) -> None:
        """«позвони мне через 10 минут» — a Telegram call from the caller account (pilot)."""
        from jarvis.calls.service import enabled, parse_call_me, request

        if not enabled(self.c.settings):
            await self.reply(
                chat_id,
                fmt.lines(
                    "📞 Звонки в Telegram ещё не подключены.",
                    fmt.hint("Нужен вход аккаунта-звонаря: `uv run jarvis caller-login` в папке Jarvis на Маке."),
                ),
            )
            return
        now = self.c.clock.now()
        at, say = parse_call_me(rest, now, self.c.settings.tz)
        if at is None:
            await self.reply(chat_id, "Когда позвонить? Например: «позвони мне через 10 минут» или «… в 7:30».")
            return
        async with self.c.db.session() as s:
            status = await request(
                s,
                self.c.settings,
                key=f"tg-call:ask:{uuid.uuid4().hex}",
                text=say,
                at=at,
                now=now,
            )  # fmt: skip — no message = a call to talk (2026-10-15)
        if status == "budget":
            await self.reply(chat_id, f"📞 Лимит звонков на сегодня исчерпан ({self.c.settings.max_calls_per_day}).")
            return
        when = "сейчас" if at - now < timedelta(minutes=1) else f"в {format_local(at, self.c.settings.tz)}"
        if to_local(at, self.c.settings.tz).date() != to_local(now, self.c.settings.tz).date():
            when = f"{to_local(at, self.c.settings.tz):%d.%m} {when}"
        talk = None if say else fmt.hint("Поговорим голосом: возьмите трубку и говорите после «Слушаю вас».")
        await self.reply(chat_id, fmt.lines(f"📞 Позвоню {when}." if when != "сейчас" else "📞 Звоню.", say or talk))

    async def cmd_labs(self, chat_id: int, query: str | None) -> None:
        """«мои анализы», «какой ферритин в анализах?», /labs — blood tests from LMF (reference, not advice)."""
        from jarvis.labs.service import labs_text

        api = self.c.lmf_api
        if api is None:
            await self.reply(chat_id, "🧪 Трекер LMF выключен (LMF_ENABLED) — анализы хранятся там.")
            return
        rows = await self._tracker_call(chat_id, api.labs(), api.labs)
        if rows is not None:
            await self.reply(chat_id, labs_text(rows, query))

    async def cmd_food_remove(self, chat_id: int, text: str) -> None:
        """«удали гречку из еды» — one entry, only after «🗑 Удалить»."""
        prepared = await self._tracker_call(
            chat_id,
            self.c.tracker.prepare_food_remove(text, self.c.clock.now()),
            lambda: self.c.tracker.prepare_food_remove(text, self.c.clock.now()),
        )
        if prepared is not None:
            await self._tracker_next(chat_id, prepared)

    async def cmd_food_show(self, chat_id: int, text: str) -> None:
        from jarvis.tracker.service import parse_past

        now = self.c.clock.now()
        today = self.c.tracker.today(now)
        span = parse_past(re.sub(r"кбжу|еда|еду|питание|калори\w*|съел\w*|ел\b|поел\w*", " ", text, flags=re.I), today)
        day = span[1] if span else today
        await self._tracker_reply(chat_id, self.c.tracker.food_text(day, now))

    async def cmd_tracker_log(self, chat_id: int, text: str) -> None:
        prepared = await self._tracker_call(
            chat_id,
            self.c.tracker.prepare_day(text, self.c.clock.now()),
            lambda: self.c.tracker.prepare_day(text, self.c.clock.now()),
        )
        if prepared is not None:
            await self._tracker_next(chat_id, prepared)

    async def cmd_tpl_mark(self, chat_id: int, text: str) -> None:
        prepared = await self._tracker_call(
            chat_id,
            self.c.tracker.prepare_tpl(text, self.c.clock.now()),
            lambda: self.c.tracker.prepare_tpl(text, self.c.clock.now()),
        )
        if prepared is not None:
            await self._tracker_next(chat_id, prepared)

    async def _tracker_next(self, chat_id: int, prepared: Any) -> None:
        """Ambiguous TPL items are asked one by one; then the draft goes to «✅ Записать»."""
        if prepared.error:
            await self.reply(chat_id, prepared.error)
            return
        if prepared.ambiguous:
            phrase, mode, ids = prepared.ambiguous[0]
            dictionary = await self.c.tracker.dictionary()
            items = [i for i in (dictionary.item(x) for x in ids) if i is not None][:6]
            async with self.c.db.session() as s:
                choice = await offer_choice(
                    s,
                    chat_id=chat_id,
                    kind="tpl_pick",
                    payload={
                        "draft": prepared.draft.model_dump(),
                        "mode": mode,
                        "ids": [i.id for i in items],
                        "rest": [list(a) for a in prepared.ambiguous[1:]],
                    },
                    now=self.c.clock.now(),
                )
            rows = [[choice_button(choice, str(n), item.title)] for n, item in enumerate(items)]
            rows.append([choice_button(choice, "b", "☑ Все"), choice_button(choice, "x", "✖️ Отмена")])
            verb = "отметить" if mode == "check" else "снять"
            await self.reply(
                chat_id, f"«{phrase}» есть в нескольких сферах TPL — какой пункт {verb}?", inline_keyboard(rows)
            )
            return
        await self._tracker_confirm(chat_id, prepared.draft)

    async def _tracker_confirm(
        self, chat_id: int, draft: Any, extra_rows: list[list[InlineButton]] | None = None
    ) -> None:
        now = self.c.clock.now()
        text_msg = await self._tracker_call(chat_id, self.c.tracker.describe(draft, now))
        if text_msg is None:
            return
        if draft.empty():
            await self.reply(chat_id, text_msg)
            return
        async with self.c.db.session() as s:
            approval = await request_approval(
                s,
                kind="tracker_write",
                target_type="lmf_day" if draft.day else "lmf_tpl",
                target_id=draft.day or draft.tpl_period or "",
                now=now,
                ttl=timedelta(days=1),
                payload={"draft": draft.model_dump(), "chat_id": chat_id},
            )
        label = "🗑 Удалить" if draft.only_removal() else "✅ Записать"
        rows = [[InlineButton(label, f"ap:a:{approval.nonce}"), InlineButton("✖️ Отмена", f"ap:r:{approval.nonce}")]]
        await self.reply(chat_id, text_msg, inline_keyboard([*rows, *(extra_rows or [])]))

    async def _record_receipt(self, s: Any, approval: Any, now: datetime) -> str:
        from decimal import Decimal

        from jarvis.finance.parse import ExpenseItem

        draft = approval.payload["draft"]
        media_id = None
        if ref := approval.payload.get("media_ref"):
            media_id = await s.scalar(select(RawMedia.id).where(RawMedia.telegram_file_unique_id == ref))
        item = ExpenseItem(Decimal(draft["amount"]), draft["currency"], draft["description"], draft["category"])
        rows = await self.c.finance.add(
            s,
            [item],
            date.fromisoformat(draft["day"]),
            source="receipt",
            source_ref=f"receipt-{approval.id}",
            merchant=draft.get("merchant"),
            raw_media_id=media_id,
        )
        return await self.c.finance.added_text(s, rows, now)

    async def _tpl_pick(self, chat_id: int, payload: dict[str, Any], option: str) -> str:
        from jarvis.tracker.service import Prepared, TrackerDraft

        if option == "x":
            await self.reply(chat_id, "Ок, в TPL ничего не отмечаю.")
            return "Отменено"
        ids = [int(x) for x in payload.get("ids") or []]
        if option == "b":
            chosen = ids
        elif option.isdigit() and int(option) < len(ids):
            chosen = [ids[int(option)]]
        else:
            return "Неизвестное действие"
        draft = TrackerDraft.model_validate(payload.get("draft") or {})
        target = draft.tpl_check if payload.get("mode") == "check" else draft.tpl_uncheck
        target.extend(i for i in chosen if i not in target)
        rest = [(str(a[0]), str(a[1]), [int(x) for x in a[2]]) for a in payload.get("rest") or []]
        await self._tracker_next(chat_id, Prepared(draft, rest))
        return "Готово"

    async def cmd_tracker_show(self, chat_id: int, text: str) -> None:
        from jarvis.tracker.service import parse_past

        now = self.c.clock.now()
        today = self.c.tracker.today(now)
        span = parse_past(text, today)
        if span is None and re.search(r"спал|\bсон|\bсна\b|оценк|настроени|средн", text, re.IGNORECASE):
            span = (today - timedelta(days=6), today)  # «как я спал?» — the last 7 days
        start, end = span or (today, today)
        if start == end:
            answer = await self._tracker_call(chat_id, self.c.tracker.day_text(start, now))
        else:
            answer = await self._tracker_call(chat_id, self.c.tracker.period_text(start, end, now))
        if answer:
            await self.reply(chat_id, answer)

    async def cmd_tpl_show(self, chat_id: int, text: str) -> None:
        from jarvis.tracker.parse import parse_month

        today = self.c.tracker.today(self.c.clock.now())
        period, _ = parse_month(text, today.year, today.month)
        answer = await self._tracker_call(chat_id, self.c.tracker.tpl_text(period))
        if answer:
            await self.reply(chat_id, answer)

    # --- 📓 Дневник: today's TLT card and step-by-step filling ----------------------------------------------------

    async def cmd_diary(self, chat_id: int) -> None:
        """📓 Дневник: what today's TLT has, what is missing, today's pills, what to say; one tap to fill by steps
        and a few frequent tags (each tap → the usual draft with «✅ Записать»)."""
        from jarvis.tracker.card import card_text, missing_steps, quick_tags, tag_button_label

        now = self.c.clock.now()
        day = self.c.tracker.today(now)
        snap = await self._tracker_call(chat_id, self.c.tracker.snapshot(day), lambda: self.c.tracker.snapshot(day))
        if snap is None:
            return
        data, dictionary, food = snap
        text_msg = card_text(day, self.c.tracker.today(now), data, dictionary, food, await self._pills_today(now))
        rows: list[list[InlineButton]] = []
        if len(missing_steps(data)) > 1 or not data.get("tags"):
            rows.append([InlineButton("✍️ Заполнить по шагам", "tl:f")])
        have = {int(t) for t in data.get("tags") or []}
        if tags := quick_tags(dictionary, have):
            rows.append([InlineButton(tag_button_label(t), f"tl:t:{t.id}") for t in tags])
        await self.reply(chat_id, text_msg, inline_keyboard(rows) if rows else None)

    async def _pills_today(self, now: datetime) -> list[Any]:
        from jarvis.meds.service import pack_emoji
        from jarvis.tracker.card import Pill

        async with self.c.db.session() as s:
            plan = await self.c.meds.today(s, now)
        return [
            Pill(pack_emoji(med.name), med.name, format_local(at, self.c.settings.tz), status)
            for med, at, status in plan
        ]

    async def _diary_tag(self, chat_id: int, tag_id: int) -> str:
        """A frequent tag tapped on the card → a draft for today (written only after «✅ Записать»)."""
        from jarvis.tracker.service import TrackerDraft

        dictionary = await self._tracker_call(chat_id, self.c.tracker.dictionary())
        if dictionary is None:
            return "TLT недоступен"
        if dictionary.tag_by_id(tag_id) is None:
            return "Этой отметки больше нет в TLT"
        day = self.c.tracker.today(self.c.clock.now())
        await self._tracker_confirm(chat_id, TrackerDraft(day=day.isoformat(), tags_add=[tag_id]))
        return "Подтвердите"

    async def cmd_diary_fill(self, chat_id: int, text: str) -> None:
        """«Заполни день»: with values it is the usual report; without — one question at a time (sleep → score →
        mood → what happened, skipping what TLT already has), then one draft «✅ Записать»."""
        from jarvis.bot.intents import is_fill_request
        from jarvis.tracker.card import flow_payload, missing_steps

        if text.strip() and not is_fill_request(text):
            await self.cmd_tracker_log(chat_id, text)
            return
        now = self.c.clock.now()
        day = self.c.tracker.today(now)
        snap = await self._tracker_call(chat_id, self.c.tracker.snapshot(day), lambda: self.c.tracker.snapshot(day))
        if snap is None:
            return
        data, dictionary, _food = snap
        payload = flow_payload(day, missing_steps(data))
        payload["have_tags"] = [int(t) for t in data.get("tags") or []]
        await self._fill_ask(chat_id, payload, dictionary)

    async def cmd_checkin(self, chat_id: int) -> bool:
        """21:00 evening check-in: the day's score and mood by buttons — only what today's TLT lacks (sleep comes from
        the watch, tags are rare) — then the usual «✅ Записать». False when there is nothing to ask or LMF is down."""
        from jarvis.tracker.card import flow_payload

        day = self.c.tracker.today(self.c.clock.now())
        try:
            data = await self.c.tracker.day_values(day)
        except (TransientError, NotConfiguredError):
            return False
        steps = [step for step in ("score", "mood") if data.get(step) in (None, "")]
        if not steps:
            return False
        payload = flow_payload(day, steps)
        payload["have_tags"] = [int(t) for t in data.get("tags") or []]
        await self._fill_ask(chat_id, payload, note="🌙 Как прошёл день?")
        return True

    async def _fill_ask(
        self, chat_id: int, payload: dict[str, Any], dictionary: Any = None, *, note: str | None = None
    ) -> None:
        from jarvis.tracker.card import quick_tags, step_options, step_prompt

        steps, index = list(payload["steps"]), int(payload["index"])
        fields = payload["fields"]
        while index < len(steps) and STEP_FIELD.get(steps[index]) in fields:  # said already in an earlier answer
            index += 1
        payload["index"] = index
        if index >= len(steps):
            await self._fill_finish(chat_id, payload)
            return
        step = steps[index]
        tags: list[Any] = []
        chosen: list[str] = []
        if step == "tags" or payload["tags"]:
            try:
                dictionary = dictionary or await self.c.tracker.dictionary()
            except (TransientError, NotConfiguredError):
                dictionary = None
            if dictionary is not None:
                chosen = [t.code for t in (dictionary.tag_by_id(i) for i in payload["tags"]) if t]
                if step == "tags":
                    tags = quick_tags(dictionary, {*payload.get("have_tags", []), *payload["tags"]})
        payload["tag_ids"] = [t.id for t in tags]
        async with self.c.db.session() as s:
            choice = await offer_choice(
                s, chat_id=chat_id, kind=FILL_KIND, payload=payload, now=self.c.clock.now(), ttl=FILL_TTL
            )
        rows = [[choice_button(choice, option, label) for option, label in step_options(step, tags)]]
        last = index == len(steps) - 1
        rows.append(
            [choice_button(choice, "d", "✅ Готово")]
            if last
            else [choice_button(choice, "n", "⏭ Пропустить"), choice_button(choice, "d", "✅ Готово")]
        )
        prompt = step_prompt(step, index + 1, len(steps), fields, chosen)
        hint = fmt.hint(f"{note} Ответьте кнопкой или словами — «пропусти», «готово», «стоп».") if note else None
        await self.reply(chat_id, fmt.lines(hint, prompt), inline_keyboard([row for row in rows if row]))

    async def _fill_pick(self, chat_id: int, payload: dict[str, Any], option: str) -> str:
        from jarvis.tracker.card import option_value

        steps, index = list(payload["steps"]), int(payload["index"])
        if index >= len(steps):
            return "Уже обработано"
        step = steps[index]
        if option == "d":
            await self._fill_finish(chat_id, payload)
            return "Готово"
        if option == "n":
            payload["index"] = index + 1
        elif step == "tags":
            ids = list(payload.get("tag_ids") or [])
            if not option.isdigit() or int(option) >= len(ids):
                return "Неизвестное действие"
            payload["tags"] = [*payload["tags"], int(ids[int(option)])]  # stay: more tags or «✅ Готово»
        else:
            value = option_value(step, option)
            if value is None:
                return "Неизвестное действие"
            payload["fields"] = {**payload["fields"], value[0]: value[1]}
            payload["index"] = index + 1
        await self._fill_ask(chat_id, payload)
        return "Записываю"

    async def _answer_pending_fill(self, chat_id: int, text_in: str) -> bool:
        """Words in the middle of «заполни день»: the answer to the current step, «готово» / «пропусти» / «стоп» —
        or (anything else) a new request: then the filling ends quietly. True when the message was consumed."""
        from jarvis.bot.intents import keyword_route
        from jarvis.tracker.card import parse_answer

        now = self.c.clock.now()
        async with self.c.db.session() as s:
            pending = await s.scalar(
                select(PendingChoice)
                .where(
                    PendingChoice.chat_id == chat_id,
                    PendingChoice.kind == FILL_KIND,
                    PendingChoice.status == "pending",
                    PendingChoice.expires_at > now,
                )
                .order_by(PendingChoice.created_at.desc())
                .limit(1)
            )
        if pending is None:
            return False
        payload = dict(pending.payload)
        steps, index = list(payload["steps"]), int(payload["index"])
        step = steps[index] if index < len(steps) else "tags"
        low = normalize_command(text_in).lower().strip(" .!?")
        control = next((name for name, pattern in FILL_CONTROL.items() if re.fullmatch(pattern, low)), None)
        answer = None
        routed = keyword_route(text_in).intent
        if control is None and routed in (Intent.UNKNOWN, Intent.TRACKER_LOG):
            answer = parse_answer(step, text_in)
        # «7:30» to «оценка дня?»: a short or numeric reply that fits no step is asked again, not filed elsewhere
        unclear = (
            control is None
            and answer is None
            and routed == Intent.UNKNOWN
            and (len(text_in.split()) <= 3 or bool(re.search(r"\d", text_in)))
        )
        resolution = control or ("answer" if answer is not None else ("retry" if unclear else "ignored"))
        async with self.c.db.session() as s:
            taken = await take_choice(s, choice_id=pending.id, chat_id=chat_id, resolution=resolution, now=now)
        if taken is None or resolution == "ignored":
            return False  # the step's buttons stay on screen; a tap on them now answers «Уже обработано»
        if resolution == "retry":
            await self._fill_ask(chat_id, payload, note=f"Не понял «{text_in.strip()[:40]}».")
        elif control == "stop":
            await self.reply(chat_id, "Ок, остановился — в TLT ничего не записал.")
        elif control == "done":
            await self._fill_finish(chat_id, payload)
        elif control == "skip":
            payload["index"] = index + 1
            await self._fill_ask(chat_id, payload)
        elif answer is not None:
            payload["fields"] = {**payload["fields"], **answer.fields}
            if answer.keep_text:
                payload["texts"] = [*payload["texts"], text_in]
            if step == "tags":
                await self._fill_finish(chat_id, payload)
            else:
                payload["index"] = index + 1
                await self._fill_ask(chat_id, payload)
        return True

    async def _fill_finish(self, chat_id: int, payload: dict[str, Any]) -> None:
        """Everything collected → one draft; free words go through the usual report parser (tags, supplements)."""
        from jarvis.tracker.service import TrackerDraft

        texts = [str(x) for x in payload.get("texts") or []]
        draft = TrackerDraft(day=str(payload["day"]))
        if texts:
            prepared = await self._tracker_call(
                chat_id, self.c.tracker.prepare_day(" ; ".join(texts), self.c.clock.now(), await self._portions())
            )
            if prepared is None:
                return
            if not prepared.error:
                draft = prepared.draft
                draft.day = str(payload["day"])
        draft.fields = {**draft.fields, **payload["fields"]}  # the answers to the steps win
        draft.tags_add = [*draft.tags_add, *(t for t in payload["tags"] if t not in draft.tags_add)]
        if draft.empty():
            await self.reply(chat_id, "Ок, ничего не записываю — нечего было добавить.")
            return
        await self._tracker_confirm(chat_id, draft)

    # --- «⋯ Ещё» ---------------------------------------------------------------------------------------------

    async def cmd_more(self, chat_id: int) -> None:
        await self.reply(
            chat_id,
            fmt.lines(fmt.title("⋯", "Ещё"), fmt.hint("Или просто скажите: «что у меня в пятницу?», «мои лекарства».")),
            more_keyboard(),
            speak=False,
        )

    async def _more_action(self, chat_id: int, key: str) -> str:
        found = more_intent(key)
        if found is None:
            return "Неизвестное действие"
        label, routed = found
        if routed is None:
            await self.cmd_settings(chat_id)
        else:
            await self.dispatch(chat_id, routed, label, {})
        return label

    # --- LocalSites ------------------------------------------------------------------------------------

    async def _sites_call(self, chat_id: int, call: Any) -> Any:
        try:
            return await call
        except NotConfiguredError as exc:
            await self.reply(chat_id, f"🌐 {exc}")
        except TransientError as exc:
            await self.reply(
                chat_id,
                self.persona.sorry(f"LocalSites не отвечает ({exc}). Он запущен, и включён ли «Пульт» (Доступ с ПК)?"),
            )
        return None

    async def _find_site(self, chat_id: int, said: str) -> Any:
        found = await self._sites_call(chat_id, self.c.sites.find(said))
        if found is None:
            return None
        result, state = found
        if result.options:
            await self.reply(chat_id, "Уточните, какой сайт: " + ", ".join(result.options[:6]) + ".")
            return None
        if result.site is None:
            names = ", ".join(s.name for s in state.sites if not s.archived)
            await self.reply(chat_id, f"Не нашёл сайт «{said}» в LocalSites. Есть: {names}.")
            return None
        return result.site

    async def cmd_site_open(self, chat_id: int, said: str) -> None:
        """«открой сайт TLT» → started if needed; then where: on this Mac, at home (LAN) or away (tunnel)."""
        from jarvis.sites.service import split_where

        name, where = split_where(said)
        if name.strip(" .!").lower() in ("tlt", "тлт", self.c.settings.lmf_site_name.lower()):
            # 16.10: TLT runs under Jarvis OS now, not LocalSites — it opens on the Mac (no home / away links yet)
            away = where in ("home", "away")
            note = " Ссылки «дома / не дома» для TLT пока нет — он открывается на Маке." if away else ""
            await self._open_jarvis(chat_id, "/tlt", f"📓 Открываю TLT на Mac.{note}")
            return
        site = await self._find_site(chat_id, name)
        if site is None:
            return
        if where is None and getattr(self.c.telegram, "desktop", False):
            where = "mac"  # said to Jarvis on the Mac: open it right there
        if where is not None:
            await self._site_open_where(chat_id, site.id, where, site=site)
            return
        site = await self._sites_call(chat_id, self.c.sites.start(site))
        if site is None:
            return
        async with self.c.db.session() as s:
            choice = await offer_choice(
                s, chat_id=chat_id, kind="site_open", payload={"site_id": site.id}, now=self.c.clock.now()
            )
        rows = [
            [choice_button(choice, "h", "🏠 Я дома"), choice_button(choice, "a", "🌍 Я не дома")],
            [choice_button(choice, "m", "💻 Открыть на Mac")],
        ]
        await self.reply(
            chat_id,
            f"🌐 {site.name} запущен (на Mac: http://localhost:{site.port}). Где открыть?",
            inline_keyboard(rows),
        )

    async def _site_open_where(self, chat_id: int, site_id: str, where: str, site: Any = None) -> None:
        from jarvis.desktop.service import Target

        if site is None:
            state = await self._sites_call(chat_id, self.c.sites.state())
            site = state.site(site_id) if state is not None else None
            if site is None:
                if state is not None:
                    await self.reply(chat_id, "Этого сайта больше нет в LocalSites.")
                return
        if where == "mac":
            site = await self._sites_call(chat_id, self.c.sites.start(site))
            if site is None:
                return
            env = await self._mac_env(chat_id)
            if env is None:
                return
            url = f"http://localhost:{site.port}"
            async with self.c.db.session() as s:
                await self.c.desktop.enqueue(
                    s, Target("open_url", {"url": url}, f"сайт {site.name}"), self.c.clock.now(), chat_id=chat_id
                )
            await self.reply(chat_id, f"🖥 {site.name} запущен — открываю на Mac: {url}.{self._mac_offline_note(env)}")
            return
        if where == "home":
            site = await self._sites_call(chat_id, self.c.sites.open_home(site))
            if site is None:
                return
            link = site.lan_url or f"http://<IP Mac>:{site.port}"
            await self.reply(
                chat_id,
                f"🏠 {site.name} в домашней сети: {link}\nСсылка с ключом сайта — не пересылайте её. "
                "Доступ по сети для этого сайта LocalSites запомнит.",
            )
            return
        await self.reply(chat_id, f"🌍 Открываю туннель для {site.name}… (несколько секунд)")
        site = await self._sites_call(chat_id, self.c.sites.open_away(site))
        if site is None:
            return
        if not site.tunnel_url:
            await self.reply(
                chat_id, f"Туннель для {site.name} ещё поднимается — спросите «какие сайты запущены?» через минуту."
            )
            return
        hours = self.c.settings.sites_tunnel_hours
        async with self.c.db.session() as s:
            await enqueue(
                s,
                job_type="sites.tunnel_close",
                run_at=self.c.clock.now() + timedelta(hours=hours),
                payload={"site_id": site.id, "name": site.name},
                dedupe_key=f"sites.tunnel_close:{site.id}:{int(self.c.clock.now().timestamp())}",
                priority=60,
            )
        open_note = (
            "\n⚠️ У этого сайта туннель пускает без ключа — ссылка открыта всем, у кого она есть."
            if site.tunnel_open
            else ""
        )
        await self.reply(
            chat_id,
            f"🌍 {site.name} через интернет: {site.tunnel_url}\nЗакрою туннель сам через {hours:g} ч — или скажите "
            f"«закрой туннель».{open_note}",
        )

    async def cmd_site_stop(self, chat_id: int, said: str) -> None:
        site = await self._find_site(chat_id, said)
        if site is None:
            return
        if not site.running:
            await self.reply(chat_id, f"{site.name} и так остановлен.")
            return
        if site.name.lower() == self.c.settings.lmf_site_name.lower():
            note = "\nЭто LMF (TLT): пока он выключен, записи в трекер будут ждать — или я запущу его сам при записи."
        else:
            note = ""
        stopped = await self._sites_call(chat_id, self.c.sites.stop(site))
        if stopped is not None:
            await self.reply(chat_id, f"⏹ {site.name} остановлен.{note}")

    async def cmd_sites_list(self, chat_id: int) -> None:
        state = await self._sites_call(chat_id, self.c.sites.state())
        if state is not None:
            await self.reply(chat_id, fmt.headed(self.c.sites.list_text(state)))

    async def cmd_tunnel_close(self, chat_id: int, said: str | None) -> None:
        state = await self._sites_call(chat_id, self.c.sites.state())
        if state is None:
            return
        open_ = [s for s in state.sites if s.tunnel_status != "off"]
        if said:
            site = await self._find_site(chat_id, said)
            if site is None:
                return
            open_ = [s for s in open_ if s.id == site.id]
        if not open_:
            await self.reply(chat_id, "Открытых туннелей нет.")
            return
        for site in open_:
            await self._sites_call(chat_id, self.c.sites.close_tunnel(site))
        await self.reply(chat_id, "🔒 Туннель закрыт: " + ", ".join(s.name for s in open_) + ".")

    async def cmd_weather(self, chat_id: int) -> None:
        forecast = await self.c.weather.forecast()
        if forecast is None:
            await self.reply(chat_id, self.persona.sorry("прогноз погоды сейчас недоступен."))
            return
        now = self.c.clock.now()
        tomorrow = to_local(now, self.c.settings.tz).date() + timedelta(days=1)
        lines: list[object] = [self.c.weather.today_text(forecast, now)]
        if later := self.c.weather.day_text(forecast, tomorrow):
            lines.append(fmt.line("🗓", fmt.b("Завтра:"), later))
        address = self.persona.address if self.persona.enabled else None
        if change := self.c.weather.upcoming_change(forecast, now, within_hours=3, address=address):
            lines.append(fmt.i(change.text))
        await self.reply(chat_id, fmt.lines(*lines))

    # --- voice replies ------------------------------------------------------------------------------

    async def cmd_help(self, chat_id: int) -> None:
        await self.reply(chat_id, await self.help_text(), speak=False)

    async def cmd_voice(self, chat_id: int, arg: str) -> None:
        """/voice — state; /voice on|off; /voice <name> — another voice of the configured TTS model."""
        choice = arg.strip()
        known = KNOWN_VOICES.get(self.c.settings.tts_model, ())
        if not self._tts_available():
            await self.reply(chat_id, "Голосовые ответы недоступны: нужен ключ OpenRouter (OPENROUTER_API_KEY).")
            return
        on_mac = CHANNEL.get() == "desktop"
        if on_mac and choice.lower() in ("on", "вкл", "включи", "да", "off", "выкл", "выключи", "нет"):
            enabled = choice.lower() in ("on", "вкл", "включи", "да")
            await self._save_voice_settings(mac=enabled)
            await self.reply(
                chat_id,
                "🔊 На Mac отвечаю голосом на всё. В Telegram — без изменений."
                if enabled
                else "🔇 На Mac отвечаю текстом. В Telegram — без изменений.",
            )
            return
        if choice.lower() in ("on", "вкл", "включи", "да"):
            await self._save_voice_settings(enabled=True, forbidden_hint_sent=None)
            SPEAK_REPLIES.set((await self.voice_settings())[1])
            await self.reply(
                chat_id,
                "🔊 В Telegram отвечаю голосом на разговор и идеи. Выключить: «отвечай текстом» или /voice off.",
            )
            return
        if choice.lower() in ("off", "выкл", "выключи", "нет"):
            await self._save_voice_settings(enabled=False)
            SPEAK_REPLIES.set(None)
            await self.reply(
                chat_id, "🔇 В Telegram отвечаю только текстом. Включить: «отвечай голосом» или /voice on."
            )
            return
        if choice:
            name = next((v for v in known if v.lower() == choice.lower()), None)
            if name is None and (known or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,40}", choice)):
                await self.reply(chat_id, f"Нет такого голоса. Доступны: {', '.join(known) or 'см. модель TTS_MODEL'}.")
                return
            await self._save_voice_settings(voice=name or choice, enabled=True)
            SPEAK_REPLIES.set(name or choice)
            await self.reply(chat_id, f"🎙 Теперь мой голос — {name or choice}.")
            return
        enabled, voice = await self.voice_settings()
        mac = "все ответы голосом" if await self.mac_voice_enabled() else "текстом"
        voices = f"\nДругие голоса: {', '.join(v for v in known if v != voice)} — /voice <имя>" if known else ""
        await self.reply(
            chat_id,
            fmt.blocks(
                fmt.title("🔊", f"Голос {voice}", f"модель {self.c.settings.tts_model}"),
                fmt.lines(
                    fmt.line(
                        "• Telegram:",
                        fmt.b("голосом — разговор и идеи" if enabled else "текстом"),
                        fmt.i("(/voice on · /voice off)"),
                    ),
                    fmt.line("• Mac:", fmt.b(mac), fmt.i("(на Mac: «отвечай текстом / голосом» или пункт меню)")),
                ),
                fmt.hint(voices.strip()) if voices else None,
            ),
            speak=False,
        )

    # --- status / sync -----------------------------------------------------------------------------

    async def cmd_status(self, chat_id: int, *, synced_now: bool = False) -> None:
        """Plain-language health summary (the technical view is `jarvis doctor`)."""
        tz = self.c.settings.tz
        now = self.c.clock.now()
        day_ago = now - timedelta(hours=24)
        async with self.c.db.session() as s:
            syncs = {
                row.provider: row.last_success_at
                for row in (
                    await s.execute(
                        select(IntegrationSyncState.provider, IntegrationSyncState.last_success_at).where(
                            IntegrationSyncState.provider.in_(["todoist", "google_calendar"])
                        )
                    )
                ).all()
            }
            tasks = await s.scalar(
                select(func.count())
                .select_from(TodoistTask)
                .where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
            )
            events = await s.scalar(
                select(func.count())
                .select_from(CalendarEvent)
                .where(CalendarEvent.status != "cancelled", CalendarEvent.start_at > now)
            )
            next_reminder = await s.scalar(
                select(Reminder).where(Reminder.status == "scheduled").order_by(Reminder.fire_at).limit(1)
            )
            sent = await s.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.status == "sent", Notification.sent_at > day_ago)
            )
            failures = (
                await s.scalars(
                    select(ScheduledJob)
                    .where(ScheduledJob.status == "dead", ScheduledJob.finished_at > day_ago)
                    .order_by(ScheduledJob.finished_at.desc())
                    .limit(3)
                )
            ).all()
            ideas = await s.scalar(select(func.count()).select_from(Idea))

        def connected(key: str) -> bool:
            value = self.c.status.get(key, "")
            return value.startswith(("CONFIGURED", "IMPLEMENTED_MOCK_TESTED"))

        def synced(provider: str) -> str:
            at = syncs.get(provider)
            return f"обновлено в {format_local(at, tz)}" if at else "ещё не синхронизировано"

        works = [fmt.bullet("Telegram", "на связи")]
        if connected("todoist"):
            works.append(fmt.bullet("Todoist", f"{synced('todoist')}, активных задач: {tasks}"))
        if connected("google_calendar"):
            works.append(fmt.bullet("Google Calendar", f"{synced('google_calendar')}, событий впереди: {events}"))
        works.append(fmt.bullet("Напоминания, планировщик, идеи, список покупок"))
        voice_on, voice_name = await self.voice_settings()
        if self._tts_available() and voice_on:
            works.append(fmt.bullet("Голосовые ответы", f"голос {voice_name}"))
        missing = [
            (not connected("todoist"), "Todoist", "задачи не видны"),
            (not connected("google_calendar"), "Google Calendar", "не вижу встреч"),
            (not connected("llm"), "ИИ для свободного текста", "понимаю команды и типовые фразы (ключ OpenRouter)"),
            (not connected("maps"), "Время в пути", "напоминаю от начала встречи (ключ Google Maps)"),
            (not connected("voice"), "Звонки", "напоминаю только в Telegram"),
            (not connected("stt"), "Голосовые", "сохраняю, но не расшифровываю"),
            (not self._tts_available(), "Голосовые ответы", "нужен ключ OpenRouter"),
            (
                not self.c.status.get("embeddings", "").startswith("CONFIGURED"),
                "Поиск идей",
                "по словам, не по смыслу",
            ),
        ]
        off = [fmt.bullet(name, why) for flag, name, why in missing if flag]
        if next_reminder is not None:
            what = next_reminder.payload.get("summary") or "напоминание"
            when = format_local(next_reminder.fire_at, tz, "%d.%m %H:%M")
            reminder = fmt.line("⏰", fmt.kv("Ближайшее напоминание", when), f"— «{what}»")
        else:
            reminder = fmt.line("⏰", "Запланированных напоминаний нет")
        activity = fmt.lines(reminder, f"📨 Отправлено за сутки: {sent} · идей сохранено: {ideas}")
        if failures:
            errors = fmt.section(
                f"Не удалось за сутки: {len(failures)}",
                [
                    fmt.bullet(
                        f"{format_local(job.finished_at or job.updated_at, tz)} "
                        f"{JOB_NAMES_RU.get(job.job_type, job.job_type)}",
                        explain_error(job.last_error),
                    )
                    for job in failures
                ],
                emoji="⚠️",
            )
        else:
            errors = fmt.hint("Ошибок за сутки нет.")
        text_msg = fmt.blocks(
            fmt.title("🩺", "Состояние JARVIS"),
            fmt.section("Работает", works, emoji="✅"),
            fmt.section("Не подключено — всё остальное работает", off, emoji="⚪️"),
            activity,
            errors,
            fmt.hint("🔄 Синхронизация запущена — свежие данные через минуту.") if synced_now else None,
        )
        await self.reply(chat_id, text_msg)

    async def cmd_sync(self, chat_id: int) -> None:
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            for job in ("todoist.reconcile", "calendar.reconcile", "reminders.plan"):
                await enqueue_coalesced(s, job_type=job, run_at=now, coalesce_key="manual", priority=40)
        await self.reply(chat_id, "🔄 Синхронизация поставлена в очередь.")

    # --- callbacks ---------------------------------------------------------------------------------

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        user_id = int(callback["from"]["id"])
        signer = self.c.callback_signer
        if signer is not None:
            verified = signer.verify(data)
            if verified is None:  # forged/edited button (modified clients show and alter callback_data) or stale
                async with self.c.db.session() as s:
                    await audit(
                        s,
                        now=self.c.clock.now(),
                        actor=f"telegram:{user_id}",
                        action=A.CALLBACK_REJECTED,
                        result="denied",
                        details={"data": data[:64]},
                    )
                if self.c.telegram is not None:
                    await self.c.telegram.answer_callback_query(
                        str(callback["id"]), "Кнопка недействительна или устарела — повторите запрос."
                    )
                return
            data = verified
        chat_id = int(((callback.get("message") or {}).get("chat") or {}).get("id") or user_id)
        now = self.c.clock.now()
        answer = "Готово"
        if m := re.fullmatch(r"ap:([ar]):([A-Za-z0-9_-]{8,24})", data):
            decision_name = APPROVE if m.group(1) == "a" else REJECT
            async with self.c.db.session() as s:
                decision = await decide(
                    s, nonce=m.group(2), decision=decision_name, actor=f"telegram:{user_id}", now=now
                )
                follow_up = await self._after_decision(s, decision, now) if decision else None
            if decision is None:
                answer = "Не найдено"
            elif not decision.changed:
                answer = f"Уже обработано ({decision.approval.status})"
            else:
                answer = "Подтверждено" if decision_name == APPROVE else "Отклонено"
            if follow_up:
                await self.reply(chat_id, follow_up)
        elif m := re.fullmatch(r"sp:([0-9a-f]{32})", data):
            await self.cmd_spec(chat_id, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"dv:([0-9a-f]{32})", data):
            await self.cmd_dev(chat_id, None, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"pc:([0-9a-f]{32}):([a-z0-9])", data):
            answer = await self._on_choice(chat_id, uuid.UUID(m.group(1)), m.group(2), callback)
        elif m := re.fullmatch(r"md:([tsz]):([0-9a-f]{32})", data):
            answer = await self._med_answer(chat_id, m.group(1), uuid.UUID(m.group(2)), callback)
        elif m := re.fullmatch(r"mp:([0-9a-f]{32})", data):
            answer = await self._pack_picker(chat_id, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"mi:([0-9a-f]{32}):(\d{1,2})", data):
            answer = await self._pack_toggle(chat_id, uuid.UUID(m.group(1)), int(m.group(2)), callback)
        elif m := re.fullmatch(r"mf:([0-9a-f]{32})", data):
            answer = await self._pack_done(chat_id, uuid.UUID(m.group(1)), callback)
        elif m := re.fullmatch(r"pf:([0-9a-f]{32})", data):
            answer = await self._forget_fact(chat_id, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"mx:([0-9a-f]{32})", data):
            answer = await self._confirm_med_removal(chat_id, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"my:([0-9a-f]{32})", data):
            answer = await self._remove_med(chat_id, uuid.UUID(m.group(1)))
        elif data == "mn:0":
            answer = "Оставил"
        elif m := re.fullmatch(r"sd:([A-Za-z0-9_-]{1,40})", data):
            answer = await self._suggested_done(chat_id, m.group(1))
        elif m := re.fullmatch(r"sm:([A-Za-z0-9_-]{1,40})", data):
            answer = await self._suggested_move(chat_id, m.group(1))
        elif m := re.fullmatch(r"m([ck]):([0-9a-f]{32})", data):
            answer = await self._confirm_mac(chat_id, m.group(1) == "c", uuid.UUID(m.group(2)), callback)
        elif m := re.fullmatch(r"sb:([0-9a-f]{32}|all)", data):
            answer = await self._bought(chat_id, m.group(1), callback)
        elif m := re.fullmatch(r"us:(d1|d7|d30|m3|m6|all)", data):
            message_id = (callback.get("message") or {}).get("message_id")
            await self.cmd_usage(chat_id, m.group(1), edit_id=int(message_id) if message_id else None)
            answer = "Готово"
        elif m := re.fullmatch(r"st:(von|voff|sync|status|help|v:[A-Za-z]{2,20})", data):
            answer = await self._settings_action(chat_id, m.group(1))
        elif m := re.fullmatch(r"mm:([a-z]{2,8})", data):
            answer = await self._more_action(chat_id, m.group(1))
        elif data == "tl:f":
            await self.cmd_diary_fill(chat_id, "")
            answer = "Заполняем"
        elif m := re.fullmatch(r"tl:t:(\d{1,9})", data):
            answer = await self._diary_tag(chat_id, int(m.group(1)))
        elif data == HOME_CALLBACK:  # «🏠 Я дома» under a shared location
            await self.reply(chat_id, await self.travel_desk.forget(), speak=False)
            message_id = (callback.get("message") or {}).get("message_id")
            if message_id and self.c.telegram is not None:
                with contextlib.suppress(Exception):
                    await self.c.telegram.edit_reply_markup(chat_id, int(message_id), None)
            answer = "🏠 Считаю от дома"
        elif m := re.fullmatch(r"r([kz]):([0-9a-f]{32})", data):
            answer = await self._reminder_answer(chat_id, uuid.UUID(m.group(2)), m.group(1) == "z", callback)
        elif m := re.fullmatch(r"ex:([0-9a-f]{32})", data):
            answer = await self._undo_expense(chat_id, uuid.UUID(m.group(1)))
        elif m := re.fullmatch(r"tr:([A-Za-z0-9_-]{1,40})", data):
            answer = await self._undo_complete(chat_id, m.group(1))
        elif m := re.fullmatch(r"sq:([0-9a-f]{32})", data):
            async with self.c.db.session() as s:
                _, approval = await self.c.shopping.request_checkout(s, uuid.UUID(m.group(1)), now)
            await self.reply(
                chat_id,
                "🧾 Подтвердить подготовку заказа? Оплата никогда не выполняется автоматически.",
                approval_keyboard(approval.nonce, "✅ Подтвердить", "✖️ Отмена"),
            )
        else:
            answer = "Неизвестное действие"
        if self.c.telegram is not None:
            await self.c.telegram.answer_callback_query(str(callback["id"]), answer)

    async def _after_decision(self, s: Any, decision: Any, now: datetime) -> str | None:
        if not decision.changed:
            return None
        approval = decision.approval
        if approval.kind == "plan_apply":
            if approval.status == "approved":
                await self.c.planner.approve(s, uuid.UUID(approval.target_id), now)
                return "✅ Применяю план…"
            return "План отклонён."
        if approval.kind == "dev_job":
            job = await self.c.development.on_decision(s, approval, now)
            if job and job.status == "queued":
                return f"🚀 Задача поставлена в очередь Dev Worker (ветка {job.branch_name})."
            return "Разработка отменена."
        if approval.kind == "purchase":
            return await self.c.shopping.on_decision(s, approval, now)
        if approval.kind == "task_op":
            if approval.status == "approved":
                await enqueue(
                    s,
                    job_type="tasks.apply_op",
                    run_at=now,
                    payload={"approval_id": str(approval.id)},
                    dedupe_key=f"tasks.apply_op:{approval.id}",
                    priority=20,
                )
                return None  # the job reports the result
            return "Отменено — задачи не трогаю."
        if approval.kind == "tracker_write":
            if approval.status == "approved":
                await enqueue(
                    s,
                    job_type="tracker.apply",
                    run_at=now,
                    payload={"approval_id": str(approval.id)},
                    dedupe_key=f"tracker.apply:{approval.id}",
                    priority=20,
                )
                return None  # the job reports what was written
            if (approval.payload.get("draft") or {}).get("food_remove"):
                return "Отменено — запись еды не трогаю."
            return "Отменено — в TLT ничего не пишу."
        if approval.kind == "research":
            if approval.status != "approved":
                return "Ок, не ищу."
            await enqueue(
                s,
                job_type="research.run",
                run_at=now,
                payload={"approval_id": str(approval.id)},
                dedupe_key=f"research.run:{approval.id}",
                priority=20,
            )
            return "🔎 Ищу в интернете… (обычно 10–40 с)"
        if approval.kind == "expense_add":
            if approval.status != "approved":
                return "Ок, в расходы не записываю."
            return await self._record_receipt(s, approval, now)
        if approval.kind == "expense_delete":
            if approval.status != "approved":
                return "Ок, расход оставил."
            removed = await self.c.finance.remove(s, uuid.UUID(str(approval.payload["expense_id"])), now)
            return f"🗑 Удалил расход: {removed.description}" if removed else "Этого расхода уже нет."
        if approval.kind == "lmf_supplement":
            if approval.status != "approved":
                return "Ок, справочник LMF не трогаю."
            name = str(approval.payload.get("name") or "")
            try:
                await self.c.tracker.add_supplement(name, str(approval.payload.get("unit") or ""))
            except (TransientError, NotConfiguredError):
                return "LMF сейчас недоступен — не добавил. Повторите, когда сайт TLT запущен."
            return f"➕ Добавил «{name}» в справочник LMF. Теперь «выпил {name.lower()}» запишется в TLT."
        return None
