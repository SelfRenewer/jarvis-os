"""Who besides the owner may get messages from the Jarvis bot — only on command (`scripts/tg_send.sh --to …`).

A recipient only receives: they are not in TELEGRAM_ALLOWED_USER_IDS, so whatever they write to the bot is ignored.
Telegram delivers nothing until the person has pressed Start in the bot once. Stored in `user_preferences`
(`telegram_recipients`: {key: {"name", "chat_id"}}), added from the Mac's shell (`jarvis tg-recipient add`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import UserPreference

PREF_KEY = "telegram_recipients"
OWNER = frozenset({"я", "мне", "me", "owner"})
EVERYONE = frozenset({"все", "всем", "all"})
_NAME = re.compile(r"^[\w .-]{1,32}$")


def key_of(name: str) -> str:
    return " ".join(name.strip().lower().replace("ё", "е").split())


@dataclass(frozen=True)
class Recipient:
    name: str
    chat_id: int


async def load(session: AsyncSession) -> dict[str, Recipient]:
    pref = await session.get(UserPreference, PREF_KEY)
    raw: dict[str, Any] = dict(pref.value) if pref is not None and isinstance(pref.value, dict) else {}
    return {k: Recipient(str(v["name"]), int(v["chat_id"])) for k, v in raw.items() if isinstance(v, dict)}


async def _save(session: AsyncSession, recipients: dict[str, Recipient]) -> None:
    value = {k: {"name": r.name, "chat_id": r.chat_id} for k, r in recipients.items()}
    pref = await session.get(UserPreference, PREF_KEY)
    if pref is None:
        session.add(UserPreference(key=PREF_KEY, value=value))
    else:
        pref.value = value


async def add(session: AsyncSession, name: str, chat_id: int) -> Recipient:
    """A person by their numeric Telegram id (not @username: bots cannot write by username)."""
    clean = " ".join(name.split())
    if not _NAME.match(clean) or key_of(clean) in OWNER | EVERYONE:
        raise ValueError(f"имя «{name}» не подходит: буквы/цифры, до 32 символов, не «я»/«все»")
    if chat_id <= 0:
        raise ValueError("нужен числовой Telegram ID человека (положительное число), не @ник и не id группы")
    recipients = await load(session)
    recipients[key_of(clean)] = Recipient(clean, chat_id)
    await _save(session, recipients)
    return recipients[key_of(clean)]


async def remove(session: AsyncSession, name: str) -> bool:
    recipients = await load(session)
    if recipients.pop(key_of(name), None) is None:
        return False
    await _save(session, recipients)
    return True


def resolve(names: list[str], owner_id: int, recipients: dict[str, Recipient]) -> list[Recipient]:
    """«--to анна --to я», «--to всем» → recipients; no names = the owner. Each person once."""
    owner = Recipient("я", owner_id)
    wanted: list[Recipient] = []
    for raw in names or ["я"]:
        k = key_of(raw)
        if k in EVERYONE:
            wanted += [owner, *recipients.values()]
        elif k in OWNER:
            wanted.append(owner)
        elif k in recipients:
            wanted.append(recipients[k])
        else:
            known = ", ".join(["я", *(r.name for r in recipients.values()), "всем"])
            raise ValueError(f"нет получателя «{raw}». Есть: {known}")
    unique: dict[int, Recipient] = {}
    for r in wanted:
        unique.setdefault(r.chat_id, r)
    return list(unique.values())
