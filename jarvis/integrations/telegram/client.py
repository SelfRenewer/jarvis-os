"""Telegram Bot API adapter (plain HTTPS; no framework)."""

from __future__ import annotations

import contextlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from jarvis.core.errors import PermanentError, RateLimitedError
from jarvis.core.http import ResilientHttp
from jarvis.core.security import CallbackSigner

MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
# `edited_message`: live-location updates (the bot never re-runs an edited text).
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.callback_data.encode()) <= 64:
            raise ValueError("callback_data must be 1..64 bytes")


@dataclass(frozen=True)
class UrlButton:
    """An inline button that opens a link. Not signed (nothing comes back to the bot); Telegram accepts only
    http(s) and tg:// links here."""

    text: str
    url: str

    def __post_init__(self) -> None:
        if not self.url.startswith(("https://", "http://", "tg://")):
            raise ValueError("url buttons take only http(s):// or tg:// links")


def _button(b: InlineButton | UrlButton) -> dict[str, str]:
    if isinstance(b, UrlButton):
        return {"text": b.text, "url": b.url}
    return {"text": b.text, "callback_data": b.callback_data}


def inline_keyboard(rows: Sequence[Sequence[InlineButton | UrlButton]]) -> dict[str, Any]:
    return {"inline_keyboard": [[_button(b) for b in row] for row in rows]}


class TelegramApi(Protocol):
    async def get_me(self) -> dict[str, Any]: ...

    async def get_chat(self, chat_id: int) -> dict[str, Any]: ...

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]: ...

    async def send_voice(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "voice.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None: ...

    async def send_chat_action(self, chat_id: int, action: str) -> None: ...

    async def send_audio(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "jarvis.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        title: str = "JARVIS",
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    async def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None) -> None: ...

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None: ...

    async def download_file(self, file_id: str) -> bytes: ...


def _split_text(text: str) -> list[str]:
    if len(text) <= MAX_TEXT:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        cut = remaining.rfind("\n", 0, MAX_TEXT)
        if cut <= 0:
            cut = MAX_TEXT
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts


_HTML_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z-]*)([^<>]*)>")


def _open_tags(text: str) -> list[tuple[str, str]]:
    """Tags still open at the end of `text`: [(name, the opening tag as written)]."""
    stack: list[tuple[str, str]] = []
    for m in _HTML_TAG.finditer(text):
        name = m.group(2).lower()
        if not m.group(1):
            stack.append((name, m.group(0)))
            continue
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == name:
                del stack[index:]
                break
    return stack


def _safe_cut(text: str, budget: int) -> int:
    """Where to cut an HTML text of more than `budget` characters: a blank line (section) or a line end with no
    tag open, else any line end, else a point outside a tag and an entity."""
    newlines = [i for i in range(min(len(text), budget) - 1, 0, -1) if text[i] == "\n"]
    closed = [i for i in newlines if not _open_tags(text[:i])]
    sections = [i for i in closed if text[i - 1] == "\n" and i >= budget // 2]
    if sections or closed or newlines:
        return (sections or closed or newlines)[0]
    cut = budget
    lt, gt = text.rfind("<", 0, cut), text.rfind(">", 0, cut)
    if lt > gt:  # inside a tag
        cut = lt
    amp = text.rfind("&", 0, cut)
    if amp != -1 and ";" not in text[amp:cut]:  # inside an entity
        cut = amp
    return max(cut, 1)


def split_html(text: str, limit: int = MAX_TEXT) -> list[str]:
    """A long HTML message → messages under the limit, never cut inside a tag: at a section/line boundary where
    possible; a block longer than a message is cut inside and its open tags are closed and opened again."""
    if len(text) <= limit:
        return [text]
    budget = max(limit - 200, limit // 2)  # room for closing and reopening tags
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _safe_cut(rest, budget)
        head, rest = rest[:cut].rstrip("\n"), rest[cut:].lstrip("\n")
        open_tags = _open_tags(head)
        if open_tags:
            head += "".join(f"</{name}>" for name, _ in reversed(open_tags))
            rest = "".join(tag for _, tag in open_tags) + rest
        if head.strip():
            chunks.append(head)
    if rest.strip():
        chunks.append(rest)
    return chunks


def html_to_plain(text: str) -> str:
    """What the reader sees in an HTML message (tags dropped, entities decoded)."""
    return html.unescape(re.sub(r"<[^<>]+>", "", text))


class HttpTelegramApi:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        signer: CallbackSigner | None = None,
    ) -> None:
        self._signer = signer
        # The token is part of the URL path; never log URLs (see core.logging).
        self._token = token
        self._http = ResilientHttp(
            "telegram", base_url=f"https://api.telegram.org/bot{token}/", timeout=35.0, transport=transport
        )
        self._files = ResilientHttp(
            "telegram-files", base_url=f"https://api.telegram.org/file/bot{token}/", timeout=60.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._files.aclose()

    def _markup(self, markup: dict[str, Any] | None) -> dict[str, Any] | None:
        return self._signer.sign_markup(markup) if self._signer is not None else markup

    async def _call(self, method: str, payload: dict[str, Any], *, retry_post: bool = False) -> Any:
        response = await self._http.post(method, json=payload, retry_non_idempotent=retry_post)
        return self._result(method, response)

    @staticmethod
    def _result(method: str, response: httpx.Response) -> Any:
        data = response.json()
        if not data.get("ok"):
            params = data.get("parameters") or {}
            if data.get("error_code") == 429:
                raise RateLimitedError("telegram: rate limited", retry_after_seconds=params.get("retry_after"))
            raise PermanentError(f"telegram {method}: {data.get('description', 'error')}")
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return dict(await self._call("getMe", {}, retry_post=True))

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        """Read-only: succeeds for a private chat only after the user pressed Start in the bot."""
        return dict(await self._call("getChat", {"chat_id": chat_id}, retry_post=True))

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        # getUpdates is idempotent with respect to the offset → safe to retry.
        return list(await self._call("getUpdates", payload, retry_post=True))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """`parse_mode="HTML"` only for texts Jarvis composed itself (dynamic parts escaped by the caller).
        `silent`: delivered without a sound (the user is on a call with Jarvis)."""
        chunks = split_html(text) if parse_mode == "HTML" else _split_text(text)  # never cut inside a tag
        result: dict[str, Any] = {}
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if silent:
                payload["disable_notification"] = True
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = self._markup(reply_markup)
            result = dict(await self._call("sendMessage", payload))
        return result

    async def send_voice(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "voice.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """A playable voice message (OGG/Opus, MP3 or M4A). Not retried on timeout: at most once, like replies."""
        fields: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption[:MAX_CAPTION]
            if parse_mode:
                fields["parse_mode"] = parse_mode
        if reply_markup:
            fields["reply_markup"] = json.dumps(self._markup(reply_markup))
        response = await self._http.post("sendVoice", data=fields, files={"voice": (filename, audio, mime_type)})
        return dict(self._result("sendVoice", response))

    async def send_audio(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "jarvis.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        title: str = "JARVIS",
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """An audio file (player in the chat). Unlike voice notes it is not blocked by voice-message privacy."""
        fields: dict[str, Any] = {"chat_id": str(chat_id), "title": title, "performer": "JARVIS"}
        if caption:
            fields["caption"] = caption[:MAX_CAPTION]
            if parse_mode:
                fields["parse_mode"] = parse_mode
        if reply_markup:
            fields["reply_markup"] = json.dumps(self._markup(reply_markup))
        response = await self._http.post("sendAudio", data=fields, files={"audio": (filename, audio, mime_type)})
        return dict(self._result("sendAudio", response))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        """ "record_voice" / "typing" indicator for a few seconds; purely cosmetic, failures are ignored."""
        with contextlib.suppress(PermanentError):
            await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, retry_post=True)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:190]
        await self._call("answerCallbackQuery", payload, retry_post=True)

    async def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = self._markup(reply_markup) if reply_markup else {"inline_keyboard": []}
        with contextlib.suppress(PermanentError):  # "message is not modified" etc. is harmless
            await self._call("editMessageReplyMarkup", payload, retry_post=True)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Replace a message Jarvis sent (e.g. 📊 switched to another period) — same rules as `send_message`."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": split_html(text)[0] if parse_mode == "HTML" else text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = self._markup(reply_markup)
        try:
            await self._call("editMessageText", payload, retry_post=True)
        except PermanentError as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def download_file(self, file_id: str) -> bytes:
        info = await self._call("getFile", {"file_id": file_id}, retry_post=True)
        size = int(info.get("file_size") or 0)
        if size > MAX_FILE_BYTES:
            raise PermanentError("telegram: file larger than 20 MB cannot be downloaded via Bot API")
        path = info.get("file_path")
        if not path or ".." in path:
            raise PermanentError("telegram: invalid file path")
        response = await self._files.get(path)
        return response.content


@dataclass
class FakeTelegramApi:
    """In-memory Telegram used by tests and by `JARVIS_ENV=test`."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    answered: list[tuple[str, str | None]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    markup_edits: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    fail_next_send: Exception | None = None
    fail_next_voice: Exception | None = None
    signer: CallbackSigner | None = None  # sign like the real adapter (tests of forged buttons)
    _message_id: int = 100

    async def get_me(self) -> dict[str, Any]:
        return {"id": 1, "is_bot": True, "username": "jarvis_test_bot"}

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        return {"id": chat_id, "type": "private"}

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        pending = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return pending

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        if self.fail_next_send is not None:
            exc, self.fail_next_send = self.fail_next_send, None
            raise exc
        self._message_id += 1
        if self.signer is not None:
            reply_markup = self.signer.sign_markup(reply_markup)
        entry = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": self._message_id}
        if silent:
            entry["silent"] = True
        if parse_mode:
            entry["parse_mode"] = parse_mode
        self.sent.append(entry)
        return {"message_id": self._message_id, "chat": {"id": chat_id}}

    async def send_voice(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "voice.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        if self.fail_next_voice is not None:
            exc, self.fail_next_voice = self.fail_next_voice, None
            raise exc
        if self.signer is not None:
            reply_markup = self.signer.sign_markup(reply_markup)
        self._message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": caption or "",
                "voice": audio,
                "mime_type": mime_type,
                "reply_markup": reply_markup,
                "message_id": self._message_id,
                **({"parse_mode": parse_mode} if parse_mode and caption else {}),
            }
        )
        return {"message_id": self._message_id, "chat": {"id": chat_id}}

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))

    async def send_audio(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "jarvis.mp3",
        mime_type: str = "audio/mpeg",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        title: str = "JARVIS",
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        if self.signer is not None:
            reply_markup = self.signer.sign_markup(reply_markup)
        self._message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": caption or "",
                "audio": audio,
                "reply_markup": reply_markup,
                "message_id": self._message_id,
                **({"parse_mode": parse_mode} if parse_mode and caption else {}),
            }
        )
        return {"message_id": self._message_id, "chat": {"id": chat_id}}

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        self.answered.append((callback_query_id, text))

    async def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None) -> None:
        if self.signer is not None:
            reply_markup = self.signer.sign_markup(reply_markup)
        self.markup_edits.append({"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if self.signer is not None:
            reply_markup = self.signer.sign_markup(reply_markup)
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )

    async def download_file(self, file_id: str) -> bytes:
        if file_id not in self.files:
            raise PermanentError("fake telegram: unknown file")
        return self.files[file_id]

    def texts(self) -> list[str]:
        """Message texts and voice captions as the user reads them (HTML tags dropped), in order."""
        return [html_to_plain(m["text"]) if m.get("parse_mode") == "HTML" else m["text"] for m in self.sent]

    def edited_texts(self) -> list[str]:
        """Texts of edited messages as the user reads them."""
        return [html_to_plain(e["text"]) if e.get("parse_mode") == "HTML" else e["text"] for e in self.edits]

    def voices(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if "voice" in m]
