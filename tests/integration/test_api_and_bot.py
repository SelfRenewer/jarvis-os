"""API authentication/webhooks and Telegram bot behaviour (fakes + real PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from jarvis.api.app import create_app
from jarvis.bot.intents import Intent, keyword_route
from jarvis.bot.service import BotService
from jarvis.db.models import AuditEvent, CallRequest, Idea, RawMedia, ShoppingItem
from jarvis.scheduler.queue import enqueue

pytestmark = pytest.mark.db
USER = 1001


def msg(text: str, update_id: int, user: int = USER, chat_type: str = "private", message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "text": text,
            "from": {"id": user},
            "chat": {"id": user, "type": chat_type},
        },
    }


async def _client(container: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(container)), base_url="http://t")


async def test_health_and_ready(container: Any) -> None:
    async with await _client(container) as http:
        assert (await http.get("/health")).json() == {"status": "ok"}
        ready = await http.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["migration"] == "0013"
    assert ready.headers["x-correlation-id"]


async def test_admin_api_requires_token(container: Any) -> None:
    async with await _client(container) as http:
        assert (await http.get("/api/v1/status")).status_code == 401
        assert (await http.get("/api/v1/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await http.get("/api/v1/status", headers={"Authorization": "Bearer admin-token-123456"})
    assert ok.status_code == 200


async def test_dev_worker_api_requires_token_and_valid_id(container: Any) -> None:
    async with await _client(container) as http:
        assert (await http.post("/api/dev-worker/v1/claim")).status_code == 401
        bad_id = await http.post(
            "/api/dev-worker/v1/claim",
            headers={"Authorization": "Bearer test-worker-token-123", "X-Worker-Id": "../etc"},
        )
        ok = await http.post(
            "/api/dev-worker/v1/claim",
            headers={"Authorization": "Bearer test-worker-token-123", "X-Worker-Id": "dev-mac"},
        )
    assert bad_id.status_code == 400
    assert ok.status_code == 204


async def test_webhook_authentication(container: Any) -> None:
    async with await _client(container) as http:
        tg = await http.post("/webhooks/telegram", json=msg("/help", 1))
        tg_ok = await http.post(
            "/webhooks/telegram", json=msg("/help", 2), headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret-token"}
        )
        vapi = await http.post("/webhooks/vapi", json={"message": {"type": "status-update"}})
        vapi_ok = await http.post(
            "/webhooks/vapi",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCallList": [
                        {"id": "c1", "function": {"name": "get_today_schedule"}},
                        {"id": "c2", "function": {"name": "delete_everything"}},
                    ],
                }
            },
            headers={"Authorization": "Bearer vapi-secret-123"},
        )
        gcal = await http.post("/webhooks/google-calendar", headers={"X-Goog-Channel-Token": "wrong"})
    assert tg.status_code == 401
    assert tg_ok.json() == {"status": "processed"}
    assert vapi.status_code == 401
    results = vapi_ok.json()["results"]
    assert results[0]["toolCallId"] == "c1"
    assert results[1]["result"] == "Этот инструмент недоступен."  # voice agent has no unrestricted tools
    assert gcal.status_code == 401


async def test_webhook_bodies_are_capped_while_reading(container: Any) -> None:
    # L5 (review 17.10): bodies were buffered whole before the size check and before the signature check.
    from jarvis.api.app import MAX_WEBHOOK_BYTES

    sent: list[int] = []

    async def stream() -> Any:  # 4 MB without Content-Length: refused once the cap is passed, not at the end
        for _ in range(64):
            sent.append(65536)
            yield b"x" * 65536

    async with await _client(container) as http:
        declared = await http.post("/webhooks/todoist", content=b"{}", headers={"content-length": str(10**9)})
        streamed = await http.post("/webhooks/todoist", content=stream())
    assert (declared.status_code, streamed.status_code) == (413, 413)
    assert MAX_WEBHOOK_BYTES < sum(sent) <= MAX_WEBHOOK_BYTES + 2 * 65536


async def test_vapi_end_of_call_failure_falls_back_to_telegram(container: Any) -> None:
    c = container
    async with c.db.session() as s:
        s.add(
            CallRequest(
                idempotency_key="call:x",
                provider="fake",
                to_number_masked="+***1",
                requested_for=c.clock.now(),
                status="requested",
                external_call_id="vc-1",
            )
        )
    async with await _client(c) as http:
        response = await http.post(
            "/webhooks/vapi",
            headers={"X-Vapi-Secret": "vapi-secret-123"},  # gitleaks:allow (test value)
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-did-not-answer",
                    "call": {"id": "vc-1"},
                }
            },
        )
    assert response.json() == {"status": "ended"}
    async with c.db.session() as s:
        req = await s.scalar(select(CallRequest))
        assert req is not None
        assert req.status == "failed"
    assert any("не дозвонился" in t for t in c.telegram.texts())


# --- bot --------------------------------------------------------------------------------------------


async def test_unauthorized_users_are_ignored(container: Any) -> None:
    bot = BotService(container)
    assert await bot.handle_update(msg("/today", 10, user=666)) == "unauthorized"
    assert await bot.handle_update(msg("/today", 11, chat_type="group")) == "unauthorized"
    assert container.telegram.sent == []  # no reply at all → no information leak
    async with container.db.session() as s:
        denied = await s.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "TELEGRAM_UNAUTHORIZED")
        )
    assert denied == 2


async def test_duplicate_update_processed_once(container: Any) -> None:
    bot = BotService(container)
    assert await bot.handle_update(msg("/help", 20)) == "processed"
    assert await bot.handle_update(msg("/help", 20)) == "duplicate"
    assert len(container.telegram.sent) == 1


async def test_natural_language_task_and_reference(container: Any) -> None:
    bot = BotService(container)
    await bot.handle_update(msg("Добавь задачу купить молоко", 30, message_id=30))
    creates = [call for call in container.todoist.calls if call[0] == "create"]
    assert creates == [("create", ("Купить молоко", None))]
    await bot.handle_update(msg("Запланируй это на завтра", 31, message_id=31))
    # The date is computed by Jarvis (23.09 → 24.09) and sent explicitly, not left to a text parser.
    updates = [call for call in container.todoist.calls if call[0] == "update"]
    assert updates and updates[-1][1][1] == {"due_date": "2026-09-24"}
    assert "→ завтра" in container.telegram.texts()[-1]


async def test_shopping_via_chat(container: Any) -> None:
    bot = BotService(container)
    await bot.handle_update(msg("Добавь яйца, молоко и 2 кг куриного филе", 40))
    async with container.db.session() as s:
        items = (await s.scalars(select(ShoppingItem))).all()
    assert sorted(i.normalized_name for i in items) == ["chicken fillet", "eggs", "milk"]
    await bot.handle_update(msg("/shop quote", 41))
    assert "Нет данных о ценах" in container.telegram.texts()[-1]


async def test_voice_note_becomes_idea_with_audio_preserved(container: Any) -> None:
    container.telegram.files["voice-1"] = b"OggS-audio"
    container.stt.transcripts[b"OggS-audio"] = "Может добавить штуку, которая группирует мысли по проектам"
    bot = BotService(container)
    update = {
        "update_id": 50,
        "message": {
            "message_id": 5,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
            "voice": {"file_id": "voice-1", "file_unique_id": "u1", "duration": 3, "mime_type": "audio/ogg"},
        },
    }
    assert await bot.handle_update(update) == "processed"
    async with container.db.session() as s:
        idea = await s.scalar(select(Idea))
        media = await s.scalar(select(RawMedia))
    assert idea is not None
    assert media is not None
    assert idea.transcript == "Может добавить штуку, которая группирует мысли по проектам"
    assert idea.raw_media_id == media.id
    assert media.data == b"OggS-audio"


async def test_approval_callback_double_tap(container: Any, worker: Any) -> None:
    c = container
    c.todoist.add(id="T1", content="Задача", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
    await worker.drain()
    bot = BotService(c)
    await bot.handle_update(msg("/plan", 60))
    markup = c.telegram.sent[-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    callback = {
        "id": "cb1",
        "data": data,
        "from": {"id": USER},
        "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
    }
    await bot.handle_update({"update_id": 61, "callback_query": callback})
    await bot.handle_update({"update_id": 62, "callback_query": {**callback, "id": "cb2"}})
    assert c.telegram.answered[0] == ("cb1", "Подтверждено")
    assert c.telegram.answered[1][1].startswith("Уже обработано")
    await worker.drain()
    assert len([call for call in c.todoist.calls if call[0] == "schedule"]) == 1


async def test_callback_from_stranger_rejected(container: Any) -> None:
    bot = BotService(container)
    callback = {
        "id": "cb",
        "data": "ap:a:AAAAAAAAAAAA",
        "from": {"id": 777},
        "message": {"message_id": 1, "chat": {"id": 777, "type": "private"}},
    }
    assert await bot.handle_update({"update_id": 70, "callback_query": callback}) == "unauthorized"


def test_keyword_router() -> None:
    assert keyword_route("Добавь яйца, молоко и 2 кг куриного филе").intent == Intent.SHOPPING_ADD
    assert keyword_route("Добавь купить молоко").intent == Intent.SHOPPING_ADD  # purchases go to the list
    assert keyword_route("Добавь задачу купить молоко").intent == Intent.ADD_TASK
    remind = keyword_route("Напомни мне позвонить в банк завтра в 10")
    assert (remind.intent, remind.task_text, remind.due_string) == (Intent.REMIND, "Позвонить в банк", "завтра в 10")
    assert keyword_route("Что у меня сегодня?").intent == Intent.SHOW_TODAY
    assert keyword_route("Через сколько мне выходить?").intent == Intent.LEAVE_TIME
    assert keyword_route("Запиши идею: AI inbox").idea_text == "AI inbox"
    assert keyword_route("Преврати эту идею в ТЗ").intent == Intent.IDEA_TO_SPEC
    assert keyword_route("Запусти разработку").intent == Intent.START_DEVELOPMENT
    assert keyword_route("Что я говорил неделю назад про AI inbox?").query == "AI inbox"


async def test_idea_search_finds_old_thought(container: Any) -> None:
    c = container
    async with c.db.session() as s:
        for text in (
            "Хочу AI inbox, который сортирует входящие",
            "Купить велосипед летом",
            "Идея: трекер привычек с напоминаниями",
        ):
            idea, _ = await c.ideas.capture(s, raw_input=text, source="api", now=datetime(2026, 9, 16, tzinfo=UTC))
            await c.ideas.analyze(s, idea.id, c.clock.now())
    async with c.db.session() as s:
        hits = await c.ideas.search(s, "AI inbox")
    assert hits
    assert "AI inbox" in hits[0].snippet


async def test_status_is_plain_language(container: Any) -> None:
    bot = BotService(container)
    await bot.handle_update(msg("/status", 800))
    text = container.telegram.texts()[-1]
    assert text.startswith("🩺 Состояние JARVIS")
    assert "IMPLEMENTED_" not in text
    assert "CONFIGURED" not in text
    assert "Ошибок за сутки нет." in text


def test_keyword_router_status_help_tasks() -> None:
    assert keyword_route("Покажи мои задачи").intent == Intent.SHOW_TASKS
    assert keyword_route("задачи?").intent == Intent.SHOW_TASKS
    assert keyword_route("Статус").intent == Intent.STATUS
    assert keyword_route("Что ты умеешь?").intent == Intent.HELP


async def test_named_task_is_resolved_not_last_task(container: Any) -> None:
    """R13 (found live): «Перенеси стрижку на пятницу» must move «Постричься», not the last added task."""
    from jarvis.bot.intents import RoutedIntent
    from jarvis.db.models import TodoistTask

    c = container
    c.todoist.add(id="H1", content="Постричься", due={"date": "2026-09-23"})
    c.todoist.add(id="M1", content="Купить молоко", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    bot = BotService(c)
    await bot._set_context(USER, last_task_id="M1")  # the most recent task is milk
    message = {"message_id": 1}
    await bot.cmd_schedule_referenced(
        USER, RoutedIntent(intent=Intent.SCHEDULE_REFERENCED_TASK, task_text="стрижку", due_string="пятницу"), message
    )
    assert ("update", ("H1", {"due_date": "2026-09-25"})) in c.todoist.calls  # Friday after Wed 23.09
    assert not any(call[0] == "update" and call[1][0] == "M1" for call in c.todoist.calls)
    await bot.cmd_schedule_referenced(
        USER, RoutedIntent(intent=Intent.SCHEDULE_REFERENCED_TASK, task_text="велосипед", due_string="завтра"), message
    )
    assert "Не нашёл задачу «велосипед»" in c.telegram.texts()[-1]
    async with c.db.session() as s:
        assert (await s.get(TodoistTask, "M1")) is not None


def test_text_helpers() -> None:
    from jarvis.core.text import is_pronoun, matches_stems, normalize_due_phrase

    assert matches_stems("стрижку", "Постричься")
    assert matches_stems("интернет", "Оплатить интернет")
    assert not matches_stems("велосипед", "Постричься")
    assert is_pronoun("это") and is_pronoun(None) and not is_pronoun("стрижку")
    assert normalize_due_phrase("пятницу") == "в пятницу"
    assert normalize_due_phrase("завтра в 10") == "завтра в 10"


async def test_json_bodies_are_read_from_the_body(container: Any) -> None:
    """R22 (found 2026-10-01): request models defined inside create_app were invisible to FastAPI under postponed
    annotations, so the Dev Worker heartbeat and POST /api/v1/ideas always answered 422."""
    async with await _client(container) as http:
        beat = await http.post(
            "/api/dev-worker/v1/heartbeat",
            json={"version": "1", "repos": []},
            headers={"Authorization": "Bearer test-worker-token-123", "X-Worker-Id": "w1"},
        )
        idea = await http.post(
            "/api/v1/ideas", json={"text": "идея из API"}, headers={"Authorization": "Bearer admin-token-123456"}
        )
    assert (beat.status_code, idea.status_code) == (200, 200)
