"""Structured logging with correlation ids and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

_PATTERNS = [
    re.compile(r"(bot)(\d{5,}:[A-Za-z0-9_-]{20,})"),  # Telegram bot token inside URLs
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]{8,})"),
    re.compile(r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,&]{8,})"),
    re.compile(r"(sk-ant-[A-Za-z0-9_-]{4})([A-Za-z0-9_-]+)"),
]


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def add(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted(set(self._secrets) | {s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***")
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        return text


REDACTOR = Redactor()


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def get_correlation_id() -> str:
    value = correlation_id_var.get()
    if value is None:
        value = new_correlation_id()
        correlation_id_var.set(value)
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = REDACTOR.redact(message)
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = correlation_id_var.get()
        if cid:
            payload["correlation_id"] = cid
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = REDACTOR.redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = correlation_id_var.get() or "-"
        base = f"{self.formatTime(record)} {record.levelname:<7} {record.name} [{cid}] {record.getMessage()}"
        if record.exc_info:
            base += "\n" + REDACTOR.redact(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", json_output: bool = True, secrets: Iterable[str] = ()) -> None:
    REDACTOR.add(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO; Telegram URLs embed the bot token.
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_extra(**fields: object) -> dict[str, object]:
    return {"extra_fields": fields}
