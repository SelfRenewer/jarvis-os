"""Signature verification, token hashing and at-rest encryption helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def desktop_token(*secrets: Any) -> str | None:
    """Bearer token of the local Jarvis-for-Mac API, derived from the first configured secret (nothing new to keep
    in `.env`); `jarvis desktop-setup` writes it into the Mac app's config (mode 600), never prints it."""
    for secret in secrets:
        if secret is not None:
            raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
            return hmac.new(raw.encode(), b"jarvis-desktop-v1", hashlib.sha256).hexdigest()[:48]
    return None


def constant_time_equals(a: str | bytes, b: str | bytes) -> bool:
    a_bytes = a.encode() if isinstance(a, str) else a
    b_bytes = b.encode() if isinstance(b, str) else b
    return hmac.compare_digest(a_bytes, b_bytes)


def hmac_sha256_b64(key: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def verify_todoist_signature(client_secret: str, body: bytes, header_value: str | None) -> bool:
    if not header_value or not client_secret:
        return False
    return constant_time_equals(hmac_sha256_b64(client_secret, body), header_value.strip())


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(auth_token: str, url: str, params: dict[str, str], header_value: str | None) -> bool:
    if not header_value or not auth_token:
        return False
    return constant_time_equals(twilio_signature(auth_token, url, params), header_value)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_payload(key: str, payload: dict[str, Any]) -> str:
    return hmac.new(key.encode(), canonical_json(payload), hashlib.sha256).hexdigest()


def verify_payload_signature(key: str, payload: dict[str, Any], signature: str) -> bool:
    return constant_time_equals(sign_payload(key, payload), signature)


class CallbackSigner:
    """Signs inline-button `callback_data`, so a modified client cannot forge or alter it.

    Telegram forwards whatever `callback_data` a client sends; clients like AyuGram show it and let users edit it
    (IDOR: `delete:16` → `delete:17`). The bot acts only on data it signed itself. 9-byte HMAC → 13 extra chars
    (the whole value must stay ≤ 64 bytes).
    """

    SEPARATOR = "|"

    def __init__(self, key: str) -> None:
        self._key = hashlib.sha256(b"jarvis-callback:" + key.encode()).digest()

    def _signature(self, data: str) -> str:
        digest = hmac.new(self._key, data.encode(), hashlib.sha256).digest()[:9]
        return base64.urlsafe_b64encode(digest).decode()

    def sign(self, data: str) -> str:
        if self.SEPARATOR in data:
            raise ValueError("callback_data must not contain the signature separator")
        signed = f"{data}{self.SEPARATOR}{self._signature(data)}"
        if len(signed.encode()) > 64:
            raise ValueError("signed callback_data is longer than 64 bytes")
        return signed

    def verify(self, signed: str) -> str | None:
        """The original data, or None when the signature is missing or wrong."""
        data, separator, signature = signed.rpartition(self.SEPARATOR)
        if not separator or not constant_time_equals(signature, self._signature(data)):
            return None
        return data

    def sign_markup(self, markup: dict[str, Any] | None) -> dict[str, Any] | None:
        """A copy of `reply_markup` with every inline button's callback_data signed (other keyboards unchanged)."""
        if not markup or "inline_keyboard" not in markup:
            return markup
        rows = [
            [
                {**button, "callback_data": self.sign(button["callback_data"])} if "callback_data" in button else button
                for button in row
            ]
            for row in markup["inline_keyboard"]
        ]
        return {**markup, "inline_keyboard": rows}


class TokenCipher:
    """Fernet (AES-128-CBC + HMAC-SHA256) for OAuth tokens stored in the database."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("cannot decrypt token (wrong JARVIS_ENCRYPTION_KEY?)") from exc
