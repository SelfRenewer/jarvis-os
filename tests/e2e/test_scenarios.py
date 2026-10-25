"""End-to-end scenarios 1–7 from the specification (all external services faked, real PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from jarvis.api.app import create_app
from jarvis.approvals.service import APPROVE, decide
from jarvis.core.clock import FrozenClock
from jarvis.core.security import hmac_sha256_b64
from jarvis.db.models import (
    AuditEvent,
    CallRequest,
    DevelopmentArtifact,
    DevelopmentJob,
    Idea,
    PlanningProposal,
    PlanningProposalItem,
    PurchaseRequest,
    Reminder,
    ScheduledJob,
    TodoistTask,
)
from jarvis.db.session import Database
from jarvis.scheduler.queue import JobQueue, enqueue
from jarvis.scheduler.runner import Worker
from jarvis.shopping.providers import ImportDocument

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")


def local(h: int, m: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def run_job(c: Any, worker: Worker, job_type: str, payload: dict[str, Any] | None = None) -> None:
    async with c.db.session() as s:
        await enqueue(s, job_type=job_type, run_at=c.clock.now(), payload=payload or {})
    await worker.drain()


# --------------------------------------------------------------------------------------------------
# Scenario 1 — barbershop: find a legal slot around conflicts, propose, approve, apply, persist
# --------------------------------------------------------------------------------------------------


async def test_scenario1_barbershop_planning(container: Any, worker: Worker) -> None:
    c = container
    c.todoist.add(
        id="T1",
        content="Сходить в барбершоп",
        priority=3,
        due={"date": "2026-09-23", "is_recurring": False},
        duration={"amount": 60, "unit": "minute"},
    )
    c.calendar.calendars.append({"id": "todoist-cal", "summary": "Todoist"})
    for start, end, title in [((9, 0), (12, 30), "Работа"), ((13, 0), (14, 0), "Обед"), ((14, 30), (18, 0), "Клиент")]:
        c.calendar.add_event(
            "primary", summary=title, start={"dateTime": iso(local(*start))}, end={"dateTime": iso(local(*end))}
        )
    # A task-rendered event in the native Todoist calendar must NOT count as busy (no double counting).
    c.calendar.add_event(
        "todoist-cal",
        summary="Другая задача",
        start={"dateTime": iso(local(18, 15))},
        end={"dateTime": iso(local(19, 15))},
    )
    c.settings.google_calendar_ids = ["primary", "todoist-cal"]

    await run_job(c, worker, "todoist.reconcile")
    await run_job(c, worker, "calendar.reconcile")

    async with c.db.session() as s:
        outcome = await c.planner.propose(s, c.clock.now(), requested_by="test")
    assert outcome.scheduled == 1
    assert outcome.approval_nonce is not None  # default policy: never auto-apply
    async with c.db.session() as s:
        item = await s.scalar(select(PlanningProposalItem).where(PlanningProposalItem.action == "schedule"))
        assert item is not None
        assert item.start_at == local(18, 15).astimezone(UTC)  # first legal slot after 18:00 + 10 min break
        assert item.end_at == local(19, 15).astimezone(UTC)
    assert c.todoist.calls.count(("list", None)) == 1
    assert not any(call[0] == "schedule" for call in c.todoist.calls)  # nothing written before approval

    async with c.db.session() as s:
        decision = await decide(s, nonce=outcome.approval_nonce, decision=APPROVE, actor="test", now=c.clock.now())
        assert decision is not None
        assert decision.changed
        await c.planner.approve(s, outcome.proposal_id, c.clock.now())
    await worker.drain()
    schedule_calls = [call for call in c.todoist.calls if call[0] == "schedule"]
    assert schedule_calls == [("schedule", ("T1", "2026-09-23T14:15:00Z", 60))]
    async with c.db.session() as s:
        task = await s.get(TodoistTask, "T1")
        assert task is not None
        assert task.due_datetime == local(18, 15).astimezone(UTC)
        proposal = await s.get(PlanningProposal, outcome.proposal_id)
        assert proposal is not None
        assert proposal.status == "applied"
    # Idempotent re-apply: no second write.
    await run_job(c, worker, "planner.apply", {"proposal_id": str(outcome.proposal_id)})
    assert len([call for call in c.todoist.calls if call[0] == "schedule"]) == 1


# --------------------------------------------------------------------------------------------------
# Scenario 2 — 15:00 appointment, 22 min travel, 10 min buffer → leave 14:28; reminders + call (fake)
# --------------------------------------------------------------------------------------------------


async def _setup_appointment(c: Any, worker: Worker, start: datetime, title: str = "Встреча с клиентом") -> None:
    c.routes.durations[(c.settings.default_origin, "Vake Park, Tbilisi")] = 22 * 60
    c.calendar.add_event(
        "primary",
        summary=title,
        location="Vake Park, Tbilisi",
        start={"dateTime": iso(start)},
        end={"dateTime": iso(start + timedelta(hours=1))},
    )
    await run_job(c, worker, "calendar.reconcile")  # also enqueues reminders.plan


async def test_scenario2_departure_time_and_reminders(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    await _setup_appointment(c, worker, local(15, 0))
    async with c.db.session() as s:
        reminders = (await s.scalars(select(Reminder).order_by(Reminder.fire_at))).all()
    assert [(r.fire_at, r.channel, r.kind) for r in reminders] == [
        (local(13, 58).astimezone(UTC), "telegram", "leave"),
        (local(14, 20).astimezone(UTC), "telegram", "leave"),
        (local(14, 28).astimezone(UTC), "call", "leave"),
    ]
    assert reminders[0].payload["leave_at"] == local(14, 28).astimezone(UTC).isoformat()

    clock.set(local(13, 58))
    await worker.drain()
    clock.set(local(14, 20))
    await worker.drain()
    texts = c.telegram.texts()
    assert any("Через 30 мин нужно выходить" in t for t in texts)
    assert any("Через 8 мин нужно выходить" in t for t in texts)
    assert c.call_provider.placed == []

    clock.set(local(14, 28))
    await worker.drain()
    assert len(c.call_provider.placed) == 1  # fake provider: nothing billable
    spec = c.call_provider.placed[0]
    assert spec.variables["event_title"] == "Встреча с клиентом"
    assert spec.variables["event_start_local"] == "15:00"
    assert spec.variables["leave_at_local"] == "14:28"
    assert spec.variables["minutes_until_leave"] == "0"
    assert spec.variables["destination"] == "Vake Park, Tbilisi"
    assert spec.to_number == c.settings.user_phone_number

    # A duplicated fire (e.g. redelivered job) never produces a second call.
    async with c.db.session() as s:
        call_reminder = await s.scalar(select(Reminder).where(Reminder.channel == "call"))
        assert call_reminder is not None
        await enqueue(s, job_type="reminder.fire", run_at=c.clock.now(), payload={"reminder_id": str(call_reminder.id)})
    await worker.drain()
    assert len(c.call_provider.placed) == 1
    async with c.db.session() as s:
        calls = (await s.scalars(select(CallRequest))).all()
        assert [(r.status, r.external_call_id) for r in calls] == [("requested", "fake-call-1")]


async def test_scenario2_event_moved_reschedules_reminders(container: Any, worker: Worker) -> None:
    c = container
    await _setup_appointment(c, worker, local(15, 0))
    event = c.calendar.events["primary"][0]
    event["start"] = {"dateTime": iso(local(16, 0))}
    event["end"] = {"dateTime": iso(local(17, 0))}
    event["etag"] = '"2"'
    await run_job(c, worker, "calendar.reconcile")
    async with c.db.session() as s:
        rows = (await s.scalars(select(Reminder).order_by(Reminder.fire_at))).all()
        cancelled = [r for r in rows if r.status == "cancelled"]
        scheduled = [r for r in rows if r.status == "scheduled"]
        jobs = (await s.scalars(select(ScheduledJob).where(ScheduledJob.job_type == "reminder.fire"))).all()
    assert len(cancelled) == 3
    assert [r.fire_at for r in scheduled] == [local(h, m).astimezone(UTC) for h, m in ((14, 58), (15, 20), (15, 28))]
    assert sorted(j.status for j in jobs) == ["cancelled"] * 3 + ["pending"] * 3


async def test_scenario2_no_route_never_invents_travel_time(container: Any, worker: Worker) -> None:
    c = container
    c.calendar.add_event(
        "primary",
        summary="Врач",
        location="Unknown place 123",
        start={"dateTime": iso(local(12, 0))},
        end={"dateTime": iso(local(13, 0))},
    )
    await run_job(c, worker, "calendar.reconcile")
    async with c.db.session() as s:
        rows = (await s.scalars(select(Reminder).order_by(Reminder.fire_at))).all()
    assert [(r.kind, r.fire_at) for r in rows] == [
        ("start", local(11, 0).astimezone(UTC)),
        ("start", local(11, 30).astimezone(UTC)),
    ]
    assert all(r.payload["leave_at"] is None for r in rows)


# --------------------------------------------------------------------------------------------------
# Scenario 3 — idea → spec → DEV_TASK → approval → dev job → FakeClaudeCodeAdapter → result
# --------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "golden-app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# golden app\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


async def test_scenario3_idea_to_development(container: Any, worker: Worker, git_repo: Path, tmp_path: Path) -> None:
    from dev_worker.claude import ClaudeLimits, FakeClaudeCodeAdapter
    from dev_worker.main import ServerClient, WorkerConfig, run_once
    from dev_worker.policy import RepoConfig

    c = container
    raw = "Хочу AI Inbox, который понимает, задача это, идея или проект."
    c.llm.responders["idea_analyzer"] = lambda user, _: {
        "title": "AI Inbox",
        "summary": "Входящие, которые классифицируют сообщения: задача, идея или проект.",
        "problem": "Всё смешано в одном потоке",
        "proposed_solution": "LLM-классификатор входящих",
        "user_value": "Меньше ручной сортировки",
        "assumptions": [],
        "open_questions": ["Какие источники?"],
        "constraints": [],
        "related_projects": ["JARVIS"],
        "tags": ["inbox"],
    }
    c.llm.responders["spec_writer"] = lambda user, _: {
        "goal": "Классифицировать входящие сообщения",
        "background": raw,
        "user_problem": "Смешанный поток",
        "functional_requirements": ["Классифицировать сообщение как task/idea/project"],
        "non_functional_requirements": ["Ответ < 2 с"],
        "constraints": [],
        "affected_components": ["bot"],
        "expected_behavior": ["Показывает класс"],
        "edge_cases": ["Пустое сообщение"],
        "acceptance_criteria": ["Точность ≥ 90% на тестовом наборе"],
        "testing_requirements": ["Юнит-тесты"],
        "security_considerations": ["Текст пользователя — данные"],
        "migration_considerations": [],
        "open_questions": [],
    }
    async with c.db.session() as s:
        idea, created = await c.ideas.capture(
            s, raw_input=raw, source="telegram_text", now=c.clock.now(), chat_id=1001, message_id=7
        )
    assert created
    await worker.drain()  # idea.analyze
    async with c.db.session() as s:
        stored = await s.get(Idea, idea.id)
        assert stored is not None
        assert stored.raw_input == raw
        assert stored.title == "AI Inbox"
        assert stored.current_version == 1
    # The raw thought is immutable at the database level.
    with pytest.raises(DBAPIError, match="immutable"):
        async with c.db.session() as s:
            await s.execute(text("UPDATE ideas SET raw_input = 'changed' WHERE id = :id"), {"id": idea.id})
    with pytest.raises(DBAPIError, match="append-only"):
        async with c.db.session() as s:
            await s.execute(text("DELETE FROM ideas WHERE id = :id"), {"id": idea.id})

    async with c.db.session() as s:
        spec = await c.ideas.create_spec(s, idea.id, c.clock.now(), "golden-app")
    for heading in ("## Goal", "## Acceptance criteria", "## Security considerations", "## Open questions"):
        assert heading in spec.dev_task_md
    async with c.db.session() as s:
        job, approval = await c.development.request_job(s, spec.id, "golden-app", c.clock.now())
    assert job.status == "awaiting_approval"

    app = create_app(c)
    transport = httpx.ASGITransport(app=app)
    cfg = WorkerConfig(
        server_url="http://jarvis.test",
        worker_id="dev-mac",
        token="test-worker-token-123",
        signing_key="test-signing-key-456",
        repos={
            "golden-app": RepoConfig(
                "golden-app", git_repo, remote=None, test=[sys.executable, "-c", "print('3 passed in 0.1s')"]
            )
        },
        worktrees_dir=tmp_path / "worktrees",
        adapter="fake",
        limits=ClaudeLimits(),
    )
    client = ServerClient(cfg, transport=transport)
    adapter = FakeClaudeCodeAdapter()
    # Not approved yet → nothing to claim, Claude never runs.
    assert await run_once(cfg, client, adapter) == "idle"
    assert adapter.calls == []

    async with c.db.session() as s:
        decision = await decide(s, nonce=approval.nonce, decision=APPROVE, actor="telegram:1001", now=c.clock.now())
        assert decision is not None
        await c.development.on_decision(s, decision.approval, c.clock.now())
    assert await run_once(cfg, client, adapter) == "succeeded"
    await client.aclose()
    assert len(adapter.calls) == 1

    async with c.db.session() as s:
        stored_job = await s.get(DevelopmentJob, job.id)
        assert stored_job is not None
        assert stored_job.status == "succeeded"
        summary = stored_job.result_summary
        report = await s.scalar(
            select(DevelopmentArtifact).where(
                DevelopmentArtifact.job_id == job.id, DevelopmentArtifact.kind == "report"
            )
        )
    assert summary["tests"] == {"status": "passed", "passed": 3, "failed": 0, "summary": "3 passed in 0.1s"}
    assert summary["changed_files"] == 1
    assert report is not None
    assert "Development completed" in report.content
    # main untouched, feature branch has exactly one new commit
    assert _git(git_repo, "log", "--oneline", "main").count("\n") == 0
    assert _git(git_repo, "log", "--oneline", f"main..{job.branch_name}").count("\n") == 0
    assert any("Development completed" in t for t in c.telegram.texts())


# --------------------------------------------------------------------------------------------------
# Scenario 4 — shopping: normalization, unit pricing, fees, no false cheapest claim, approval
# --------------------------------------------------------------------------------------------------


def _offers_doc(b_delivery_fee: str | None) -> ImportDocument:
    return ImportDocument.model_validate(
        {
            "stores": [
                {"store_key": "a", "name": "Store A", "delivery_fee": "3.99", "service_fee": "1.00"},
                {"store_key": "b", "name": "Store B", "delivery_fee": b_delivery_fee, "service_fee": "0.50"},
            ],
            "offers": [
                {
                    "store_key": "a",
                    "product_name": "Milk 1L",
                    "brand": "Sante",
                    "package_quantity": "1",
                    "package_unit": "l",
                    "price": "4.50",
                },
                {
                    "store_key": "a",
                    "product_name": "Chicken fillet 1kg",
                    "package_quantity": "1",
                    "package_unit": "kg",
                    "price": "14.00",
                },
                {
                    "store_key": "a",
                    "product_name": "Eggs 10",
                    "package_quantity": "10",
                    "package_unit": "pcs",
                    "price": "5.50",
                },
                {
                    "store_key": "b",
                    "product_name": "Milk 900ml",
                    "brand": "Parmalat",
                    "package_quantity": "900",
                    "package_unit": "ml",
                    "price": "4.20",
                },
                {
                    "store_key": "b",
                    "product_name": "Chicken fillet 500g",
                    "package_quantity": "500",
                    "package_unit": "g",
                    "price": "6.40",
                },
                {
                    "store_key": "b",
                    "product_name": "Eggs 30",
                    "package_quantity": "30",
                    "package_unit": "pcs",
                    "price": "15.00",
                },
            ],
        }
    )


async def test_scenario4_shopping(container: Any) -> None:
    c = container
    now = c.clock.now()
    async with c.db.session() as s:
        items = await c.shopping.add_items(s, "Milk 2 × 1L\nChicken fillet 2kg\nEggs 20", now)
        await c.shopping.import_offers(s, _offers_doc(None), now)
    assert [(i.normalized_name, i.base_quantity, i.dimension) for i in items] == [
        ("milk", Decimal(2000), "volume"),
        ("chicken fillet", Decimal(2000), "mass"),
        ("eggs", Decimal(20), "count"),
    ]

    async with c.db.session() as s:
        quote, result, summary = await c.shopping.quote(s, now)
    assert not result.cheapest_claim_allowed
    assert "Самая дешёвая" not in summary
    assert "неполные" in summary
    assert quote.is_complete is False
    best = result.best
    assert best is not None
    assert best.stores == (next(iter(best.stores)),)
    assert str(best.total.amount) == "52.99"
    assert "4.5000 GEL/L" in summary  # deterministic unit price

    async with c.db.session() as s:
        await c.shopping.import_offers(s, _offers_doc("2.49"), now)
        quote, result, summary = await c.shopping.quote(s, now)
    assert result.cheapest_claim_allowed
    assert summary.startswith("✅ Самая дешёвая корзина")

    async with c.db.session() as s:
        request, approval = await c.shopping.request_checkout(s, quote.id, now)
    assert request.status == "awaiting_approval"
    async with c.db.session() as s:
        decision = await decide(s, nonce=approval.nonce, decision=APPROVE, actor="telegram:1001", now=now)
        assert decision is not None
        message = await c.shopping.on_decision(s, decision.approval, now)
    assert "Автоматическое оформление недоступно" in message
    async with c.db.session() as s:
        again = await decide(s, nonce=approval.nonce, decision=APPROVE, actor="telegram:1001", now=now)
        assert again is not None
        assert not again.changed  # double tap → no second purchase flow
        stored = await s.get(PurchaseRequest, request.id)
        assert stored is not None
        assert stored.status == "manual_steps_sent"


# --------------------------------------------------------------------------------------------------
# Scenario 5 — duplicate Todoist webhook delivery is processed once
# --------------------------------------------------------------------------------------------------


async def test_scenario5_duplicate_webhook(container: Any, worker: Worker) -> None:
    c = container
    c.todoist.add(id="T9", content="Купить молоко")
    app = create_app(c)
    body = json.dumps(
        {"event_name": "item:updated", "user_id": "1", "event_data": {"id": "T9"}, "version": "10"}
    ).encode()
    headers = {
        "X-Todoist-Hmac-SHA256": hmac_sha256_b64("todoist-client-secret", body),
        "X-Todoist-Delivery-ID": "delivery-42",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        first = await http.post("/webhooks/todoist", content=body, headers=headers)
        second = await http.post("/webhooks/todoist", content=body, headers=headers)
        forged = await http.post("/webhooks/todoist", content=body, headers={**headers, "X-Todoist-Hmac-SHA256": "x"})
    assert first.json() == {"status": "queued", "task_id": "T9"}
    assert second.json() == {"status": "duplicate"}
    assert forged.status_code == 401
    await worker.drain()
    assert c.todoist.calls.count(("get", "T9")) == 1
    async with c.db.session() as s:
        assert (
            await s.scalar(
                select(func.count()).select_from(ScheduledJob).where(ScheduledJob.job_type == "todoist.refresh_task")
            )
            == 1
        )
        task = await s.get(TodoistTask, "T9")
        assert task is not None
        assert task.content == "Купить молоко"


# --------------------------------------------------------------------------------------------------
# Scenario 6 — scheduler restart: grace-period recovery, no duplicate execution
# --------------------------------------------------------------------------------------------------


async def test_scenario6_restart_within_grace(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    await _setup_appointment(c, worker, local(11, 0))  # leave 10:28; reminders 09:58, 10:20, 10:28(call)
    # The "scheduler" is down from 09:00 until 10:30 → then restarted.
    clock.set(local(10, 30))
    await worker.drain()
    assert len(c.call_provider.placed) == 1  # 2 min late: within the 5 min call grace → call
    texts = c.telegram.texts()
    assert any("Напоминание запоздало" in t for t in texts)  # 09:58 one is 32 min late → recovery notice
    assert any("Через" in t or "Пора" in t for t in texts)
    # Restart again: nothing executes twice.
    await worker.drain()
    assert len(c.call_provider.placed) == 1
    assert len(c.telegram.texts()) == len(texts)


async def test_scenario6_hours_late_never_calls(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    await _setup_appointment(c, worker, local(11, 0))
    clock.set(local(11, 20))  # the appointment already started 20 minutes ago
    await worker.drain()
    clock.advance(timedelta(minutes=2))  # concurrent duplicates wait for the in-flight message, then settle
    await worker.drain()
    assert c.call_provider.placed == []
    missed = [t for t in c.telegram.texts() if "Пропущено напоминание" in t]
    assert len(missed) == 1  # one recovery notification for the whole event, not three
    async with c.db.session() as s:
        statuses = sorted((await s.scalars(select(Reminder.status))).all())
    assert statuses == ["missed", "missed", "missed"]


async def test_scenario6_worker_crash_mid_job_recovers_once(
    container: Any, db: Database, clock: FrozenClock, worker: Worker
) -> None:
    c = container
    await _setup_appointment(c, worker, local(11, 0))
    clock.set(local(10, 28))
    crashed = JobQueue(db, clock, worker_id="crashed", lease_seconds=60)
    claimed = await crashed.claim(limit=10)
    fires = [j for j in claimed if j.job_type == "reminder.fire"]
    assert len(fires) == 3  # all three reminders are due; the worker dies holding them
    clock.advance(timedelta(seconds=61))
    assert (await worker.queue.reap_expired_leases())["requeued"] == len(claimed)
    await worker.drain()
    assert len(c.call_provider.placed) == 1


# --------------------------------------------------------------------------------------------------
# Scenario 7 — database / network failures: bounded retry, meaningful failure state
# --------------------------------------------------------------------------------------------------


async def test_scenario7_network_failure_bounded_retry(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    c.todoist.unavailable = True
    async with c.db.session() as s:
        job_id = await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now(), max_attempts=3)
    await worker.drain()
    for _ in range(3):
        clock.advance(timedelta(seconds=60))
        await worker.drain()
    async with c.db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None
        assert job.status == "dead"
        assert job.attempts == 3
        assert "fake outage" in (job.last_error or "")
        dead_audit = await s.scalar(select(AuditEvent).where(AuditEvent.action == "JOB_DEAD"))
        assert dead_audit is not None
    # Service recovers: a new reconcile succeeds.
    c.todoist.unavailable = False
    await run_job(c, worker, "todoist.reconcile")
    async with c.db.session() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(ScheduledJob)
                .where(ScheduledJob.job_type == "todoist.reconcile", ScheduledJob.status == "succeeded")
            )
            == 1
        )


async def test_scenario7_telegram_outage_retries_and_delivers_once(
    container: Any, worker: Worker, clock: FrozenClock
) -> None:
    from jarvis.core.errors import TransientError

    c = container
    c.calendar.add_event(
        "primary", summary="Созвон", start={"dateTime": iso(local(10, 0))}, end={"dateTime": iso(local(10, 30))}
    )
    await run_job(c, worker, "calendar.reconcile")
    clock.set(local(9, 45))
    c.telegram.fail_next_send = TransientError("telegram: HTTP 502")
    await worker.drain()
    assert c.telegram.texts() == []
    clock.advance(timedelta(seconds=15))
    await worker.drain()
    assert [t for t in c.telegram.texts() if "Созвон" in t] == [c.telegram.texts()[0]]
    assert len(c.telegram.texts()) == 1


async def test_scenario7_worker_survives_database_outage() -> None:
    class BrokenQueue:
        clock = FrozenClock(datetime(2026, 9, 23, tzinfo=UTC))

        def __init__(self) -> None:
            self.calls = 0

        async def reap_expired_leases(self) -> dict[str, int]:
            raise OSError("connection refused")

        async def claim(self, limit: int = 1) -> list[Any]:
            self.calls += 1
            raise OSError("connection refused")

    queue = BrokenQueue()
    stop = asyncio.Event()
    w = Worker(
        queue,
        __import__("jarvis.jobs.handlers", fromlist=["x"]).build_registry(),
        object(),  # type: ignore[arg-type]
        poll_interval=0.01,
    )
    task = asyncio.create_task(w.run_forever(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert queue.calls >= 1  # kept looping without crashing
