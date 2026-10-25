"""Bot hardening against forged inline buttons and forwarded commands.

The attack from https://t.me/overbafer1/2057 (checked 2026-09-24): modified clients (AyuGram…) show a button's
`callback_data`, and the user can send any other value (IDOR: `delete:16` → `delete:17`). Jarvis signs every
button it sends and acts only on data it signed itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from jarvis.bot.service import BotService
from jarvis.core.security import CallbackSigner
from jarvis.db.models import Approval, AuditEvent

pytestmark = pytest.mark.db
USER = 1001


def msg(text: str, update_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
            **extra,
        },
    }


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


@pytest.fixture
def signed(container: Any) -> Any:
    """The production wiring: the adapter signs every inline button, the bot verifies every tap."""
    signer = CallbackSigner("bot-token-for-tests")
    container.telegram.signer = signer
    container.callback_signer = signer
    return container


def test_signer_roundtrip_and_tampering() -> None:
    signer = CallbackSigner("k")
    token = signer.sign("tr:HAIR")
    assert signer.verify(token) == "tr:HAIR"
    assert signer.verify("tr:HAIR") is None  # unsigned
    assert signer.verify(token.replace("HAIR", "MILK")) is None  # edited id, old signature
    assert CallbackSigner("other-key").verify(token) is None  # signature from another bot
    assert len(signer.sign("pc:" + "f" * 32 + ":x").encode()) <= 64
    with pytest.raises(ValueError, match="64 bytes"):
        signer.sign("x" * 60)  # would exceed Telegram's 64-byte limit


async def test_forged_callback_is_rejected_and_audited(signed: Any) -> None:
    c = signed
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24"})
    c.todoist.add(id="MILK", content="Купить молоко", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    bot = BotService(c)
    await bot.handle_update(msg("готово: стрижка", 1))
    await bot.handle_update(msg("готово: молоко", 2))
    undo = c.telegram.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert undo.startswith("tr:HAIR|")
    # The attack: the same button with another task id (signature copied), and a hand-made unsigned one.
    await bot.handle_update(tap(undo.replace("HAIR", "MILK"), 3))
    await bot.handle_update(tap("tr:MILK", 4))
    assert not [call for call in c.todoist.calls if call[0] == "reopen"]
    assert c.telegram.answered[-1][1] == "Кнопка недействительна или устарела — повторите запрос."
    async with c.db.session() as s:
        rejected = await s.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "CALLBACK_REJECTED")
        )
    assert rejected == 2
    await bot.handle_update(tap(undo, 5))  # the genuine button still works
    assert ("reopen", "HAIR") in c.todoist.calls


async def test_forged_approval_cannot_confirm_anything(signed: Any) -> None:
    c = signed
    c.todoist.add(id="CIG", content="Купить сигару")
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    bot = BotService(c)
    await bot.handle_update(msg("удали задачу купить сигару", 10))
    async with c.db.session() as s:
        approval = await s.scalar(select(Approval))
    assert approval is not None
    await bot.handle_update(tap(f"ap:a:{approval.nonce}", 11))  # right nonce, but not a button Jarvis signed
    async with c.db.session() as s:
        assert (await s.get(Approval, approval.id)).status == "pending"  # type: ignore[union-attr]


async def test_forwarded_messages_are_never_run_as_commands(container: Any) -> None:
    c = container
    c.todoist.add(id="OLD", content="Старая задача", due={"date": "2026-09-20"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    origin = {"forward_origin": {"type": "user", "sender_user": {"id": 777}, "date": 1}}
    await BotService(c).handle_update(msg("удали все просроченные", 20, **origin))
    text = c.telegram.texts()[-1]
    assert text.startswith("↪️ Пересланное сообщение — как команду я его не выполняю.")
    assert "📥 Куда записать «удали все просроченные»?" in text
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Approval)) == 0


async def test_forwarded_commands_and_voice_notes_are_not_run_either(container: Any) -> None:
    """R31 (security audit 14.10): «/done …» and voice notes were handled before the forwarded check."""
    c = container
    c.todoist.add(id="T1", content="Купить хлеб", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    origin = {"forward_origin": {"type": "user", "sender_user": {"id": 777}, "date": 1}}
    bot = BotService(c)
    await bot.handle_update(msg("/done Купить хлеб", 30, **origin))
    assert c.telegram.texts()[-1].startswith("↪️ Пересланное сообщение — как команду я его не выполняю.")
    assert c.todoist.tasks["T1"].get("checked") is not True
    c.telegram.files["fw"] = b"OggS-forwarded"
    c.stt.transcripts[b"OggS-forwarded"] = "Закрой задачу купить хлеб"
    voice = {"file_id": "fw", "file_unique_id": "u-fw", "duration": 2, "mime_type": "audio/ogg"}
    await bot.handle_update({"update_id": 31, "message": {**msg("", 31)["message"], "voice": voice, **origin}})
    assert c.telegram.texts()[-1].startswith("↪️ Пересланное голосовое — как команду я его не выполняю.")
    assert c.todoist.tasks["T1"].get("checked") is not True


async def test_lmf_refusing_the_token_is_said_plainly(container: Any) -> None:
    """Security review 14.10 (M2): LMF answers 401 without Jarvis's token — the user is told how to fix it and LMF
    is not «restarted» as if it were down."""
    from jarvis.core.errors import AuthError

    c = container
    started: list[str] = []

    async def ensure_running(name: str) -> bool:
        started.append(name)
        return True

    c.sites.ensure_running = ensure_running

    async def refused() -> None:
        raise AuthError("lmf: HTTP 401 (authentication/authorization failed)")

    assert await BotService(c)._tracker_call(USER, refused(), retry=refused) is None
    text = c.telegram.texts()[-1]
    assert "TLT не пускает Джарвиса: нужен токен LMF." in text and "uv run jarvis lmf-setup" in text
    assert started == []
