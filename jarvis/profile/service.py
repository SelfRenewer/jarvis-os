"""What Jarvis knows about the user (2026-10-14, the question agent's stage 4 «память о вас»).

Facts come only from the user's own words — «запомни, что мой барбер — Гоги», «запомни: у меня аллергия на
орехи» — never from forwarded messages, web pages, photos or the model's guesses. They go to the router (so «запиши
к моему барберу» is understood) and to the question agent as context. «Что ты обо мне знаешь?» lists them,
«забудь про барбера» forgets (the row stays, marked forgotten). A plain «запомни …» about anything else stays a note.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.text import matches_stems, normalize_title
from jarvis.db.models import ProfileFact

MAX_FACTS_IN_PROMPT = 40
MAX_PROMPT_CHARS = 1500

# «запомни, что я …», «запомни: мой …», «запомни что у меня …» — about the user himself
_ABOUT_ME = (
    r"(?:я|мой|моя|моё|мое|мои|мне|меня|мною|моего|моей|мою|моим|моему|моих|у\s+меня|для\s+меня|со\s+мной"
    r"|мама|маму|мамы|папа|папу|папы|жена|жену|муж|мужа|сын|сына|дочь|дочка|брат|брата|сестра|сестру"
    r"|бабушка|дедушка|девушка|парень|друг|подруга)"  # family: «запомни, что мама живёт в Тбилиси»
)
_REMEMBER = re.compile(rf"^запомни(?:\s*[,:—-])?\s*(?:что\s+|,\s*что\s+)?(?P<fact>{_ABOUT_ME}\b.+)$", re.I | re.S)
_FORGET_WORDS = {"про", "о", "об", "что", "факт", "это"}


def parse_remember(text: str) -> str | None:
    """«Запомни, что мой барбер — Гоги» → «мой барбер — Гоги»; None when it is not about the user."""
    m = _REMEMBER.match(text.strip())
    if not m:
        return None
    fact = re.sub(r"\s+", " ", m.group("fact")).strip(" .!")
    return fact[:300] or None


async def active(session: AsyncSession) -> list[ProfileFact]:
    rows = await session.scalars(
        select(ProfileFact).where(ProfileFact.status == "active").order_by(ProfileFact.created_at)
    )
    return list(rows.all())


async def add(session: AsyncSession, text: str) -> tuple[ProfileFact, bool]:
    """The fact, and whether it is new (the same words again are not stored twice)."""
    for fact in await active(session):
        if normalize_title(fact.text) == normalize_title(text):
            return fact, False
    fact = ProfileFact(text=text[:300], status="active")
    session.add(fact)
    await session.flush()
    return fact, True


def find(facts: list[ProfileFact], query: str) -> list[ProfileFact]:
    """Facts matching the words: exact, then 5- and 4-letter stems («барбера» → «мой барбер — Гоги»)."""
    words = " ".join(w for w in re.findall(r"[а-яёa-z0-9-]+", query.lower()) if w not in _FORGET_WORDS)
    if not words:
        return []
    for length in (5, 4):
        found = [f for f in facts if matches_stems(words, f.text, length)]
        if found:
            return found
    return []


async def forget(session: AsyncSession, fact_id: uuid.UUID, now: datetime) -> ProfileFact | None:
    fact = await session.get(ProfileFact, fact_id, with_for_update=True)
    if fact is None or fact.status != "active":
        return None
    fact.status, fact.forgotten_at = "forgotten", now
    return fact


def prompt_block(facts: list[ProfileFact]) -> str | None:
    """«Что известно о пользователе (он сам сказал): …» for the router / agent context, newest first, capped."""
    lines: list[str] = []
    size = 0
    for fact in reversed(facts[-MAX_FACTS_IN_PROMPT:]):
        line = f"- {fact.text}"
        if size + len(line) > MAX_PROMPT_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "Что известно о пользователе (он сам попросил запомнить):\n" + "\n".join(lines) if lines else None
