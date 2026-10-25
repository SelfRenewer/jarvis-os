"""Messages on command to the owner and to added recipients (scripts/tg_send.sh --to …)."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.cli import _tg_send
from jarvis.notify import recipients

pytestmark = pytest.mark.db
OWNER = 1001
ANNA = 222000111


def test_names_resolve_to_people_once() -> None:
    known = {"анна": recipients.Recipient("Анна", ANNA)}
    assert recipients.resolve([], OWNER, known) == [recipients.Recipient("я", OWNER)]
    assert [r.chat_id for r in recipients.resolve(["АННА"], OWNER, known)] == [ANNA]
    assert [r.chat_id for r in recipients.resolve(["всем", "анна", "я"], OWNER, known)] == [OWNER, ANNA]
    with pytest.raises(ValueError, match="Есть: я, Анна, всем"):
        recipients.resolve(["пётр"], OWNER, known)


async def test_add_list_remove(container: Any) -> None:
    async with container.db.session() as s:
        await recipients.add(s, "Анна", ANNA)
    async with container.db.session() as s:
        assert await recipients.load(s) == {"анна": recipients.Recipient("Анна", ANNA)}
        with pytest.raises(ValueError, match="не подходит"):
            await recipients.add(s, "всем", 5)  # a reserved word
        with pytest.raises(ValueError, match="числовой Telegram ID"):
            await recipients.add(s, "Группа", -100123)  # a group id, not a person
        assert await recipients.remove(s, "анна") and not await recipients.remove(s, "анна")


async def test_send_to_a_recipient_and_to_everyone(container: Any, monkeypatch: Any, capsys: Any) -> None:
    c = container
    monkeypatch.setattr("jarvis.services.container.build_container", lambda settings: c)
    monkeypatch.setattr(c, "aclose", _noop)
    async with c.db.session() as s:
        await recipients.add(s, "Анна", ANNA)
    assert await _tg_send(c.settings, ["анна"], "<b>Контакты на сегодня</b>", "HTML") == 0
    assert await _tg_send(c.settings, ["всем"], "Сводка", "HTML") == 0
    assert [(m["chat_id"], m["text"]) for m in c.telegram.sent] == [
        (ANNA, "<b>Контакты на сегодня</b>"), (OWNER, "Сводка"), (ANNA, "Сводка"),
    ]  # fmt: skip
    assert await _tg_send(c.settings, ["пётр"], "x", None) == 1  # unknown name: nothing sent
    assert len(c.telegram.sent) == 3
    assert "sent Анна" in capsys.readouterr().out


async def _noop() -> None:
    return None
