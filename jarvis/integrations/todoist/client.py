"""Todoist API v1 adapter + parsing into a normalized snapshot."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from jarvis.core.clock import to_utc
from jarvis.core.errors import NotFoundError, PermanentError
from jarvis.core.http import ResilientHttp

BASE_URL = "https://api.todoist.com/api/v1/"
MAX_DURATION_MINUTES = 24 * 60 - 1  # Todoist apps only display durations under 24 h


@dataclass(frozen=True)
class TaskSnapshot:
    id: str
    content: str
    description: str | None
    project_id: str | None
    section_id: str | None
    parent_id: str | None
    priority: int
    labels: tuple[str, ...]
    due_date: date | None
    due_datetime: datetime | None  # aware UTC; None for all-day / no due
    due_timezone: str | None
    due_is_recurring: bool
    due_string: str | None
    duration_minutes: int | None
    deadline_date: date | None
    checked: bool
    is_deleted: bool
    updated_at: datetime | None


def _parse_due_datetime(raw: str, due_tz: str | None, user_tz: ZoneInfo) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        # Floating time: interpret in the task's timezone if given, else in the user's zone.
        tz = user_tz
        if due_tz:
            try:
                tz = ZoneInfo(due_tz)
            except (ZoneInfoNotFoundError, ValueError):
                tz = user_tz
        value = value.replace(tzinfo=tz)
    return to_utc(value)


def parse_task(raw: dict[str, Any], user_tz: ZoneInfo) -> TaskSnapshot:
    due = raw.get("due") or None
    due_date: date | None = None
    due_dt: datetime | None = None
    due_tz: str | None = None
    recurring = False
    due_string: str | None = None
    if due:
        due_tz = due.get("timezone")
        recurring = bool(due.get("is_recurring"))
        due_string = due.get("string")
        raw_dt = due.get("datetime")
        raw_date = str(due.get("date") or "")
        if not raw_dt and "T" in raw_date:
            raw_dt = raw_date
        if raw_dt:
            due_dt = _parse_due_datetime(str(raw_dt), due_tz, user_tz)
            local_tz = user_tz
            due_date = due_dt.astimezone(local_tz).date()
        elif raw_date:
            due_date = date.fromisoformat(raw_date[:10])

    duration_minutes: int | None = None
    duration = raw.get("duration") or None
    if duration and duration.get("amount"):
        amount = int(duration["amount"])
        duration_minutes = amount * 1440 if duration.get("unit") == "day" else amount

    deadline = raw.get("deadline") or None
    deadline_date = date.fromisoformat(str(deadline["date"])[:10]) if deadline and deadline.get("date") else None

    updated_at: datetime | None = None
    if raw.get("updated_at"):
        text = str(raw["updated_at"]).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        updated_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    return TaskSnapshot(
        id=str(raw["id"]),
        content=str(raw.get("content") or ""),
        description=raw.get("description") or None,
        project_id=str(raw["project_id"]) if raw.get("project_id") else None,
        section_id=str(raw["section_id"]) if raw.get("section_id") else None,
        parent_id=str(raw["parent_id"]) if raw.get("parent_id") else None,
        priority=int(raw.get("priority") or 1),
        labels=tuple(raw.get("labels") or ()),
        due_date=due_date,
        due_datetime=due_dt,
        due_timezone=due_tz,
        due_is_recurring=recurring,
        due_string=due_string,
        duration_minutes=duration_minutes,
        deadline_date=deadline_date,
        checked=bool(raw.get("checked") or raw.get("is_completed")),
        is_deleted=bool(raw.get("is_deleted")),
        updated_at=updated_at,
    )


def _format_utc(value: datetime) -> str:
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


class TodoistApi(Protocol):
    async def list_active_tasks(self) -> list[dict[str, Any]]: ...

    async def get_task(self, task_id: str) -> dict[str, Any]: ...

    async def schedule_task(self, task_id: str, start: datetime, duration_minutes: int, request_id: str) -> None: ...

    async def create_task(
        self, content: str, *, due_string: str | None, request_id: str, parent_id: str | None = None
    ) -> dict[str, Any]: ...

    async def update_due_string(self, task_id: str, due_string: str, request_id: str) -> None: ...

    async def update_task(
        self,
        task_id: str,
        *,
        request_id: str,
        priority: int | None = None,
        due_date: date | None = None,
        due_datetime: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> None: ...

    async def close_task(self, task_id: str, request_id: str) -> None: ...

    async def reopen_task(self, task_id: str, request_id: str) -> None: ...

    async def delete_task(self, task_id: str, request_id: str) -> None: ...

    async def probe(self) -> None: ...


class HttpTodoistApi:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = ResilientHttp(
            "todoist",
            base_url=BASE_URL,
            timeout=20.0,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_active_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(200):  # hard bound: 200 pages * 200 tasks
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = (await self._http.get("tasks", params=params)).json()
            tasks.extend(data.get("results") or [])
            cursor = data.get("next_cursor")
            if not cursor:
                return tasks
        raise PermanentError("todoist: pagination did not terminate")

    async def get_task(self, task_id: str) -> dict[str, Any]:
        _validate_id(task_id)
        return dict((await self._http.get(f"tasks/{task_id}")).json())

    async def schedule_task(self, task_id: str, start: datetime, duration_minutes: int, request_id: str) -> None:
        _validate_id(task_id)
        if not 1 <= duration_minutes <= MAX_DURATION_MINUTES:
            raise PermanentError("todoist: duration must be between 1 minute and 23h59m")
        body = {"due_datetime": _format_utc(start), "duration": duration_minutes, "duration_unit": "minute"}
        # Setting the same absolute values twice is idempotent; X-Request-Id lets Todoist dedupe too.
        await self._http.post(
            f"tasks/{task_id}", json=body, headers={"X-Request-Id": request_id}, retry_non_idempotent=True
        )

    async def create_task(
        self, content: str, *, due_string: str | None, request_id: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        """`parent_id`: create a subtask (Todoist puts it into the parent's project)."""
        body: dict[str, Any] = {"content": content[:500]}
        if parent_id:
            _validate_id(parent_id)
            body["parent_id"] = parent_id
        if due_string:
            body["due_string"] = due_string[:100]
            body["due_lang"] = "ru"
        response = await self._http.post("tasks", json=body, headers={"X-Request-Id": request_id})
        return dict(response.json())

    async def update_due_string(self, task_id: str, due_string: str, request_id: str) -> None:
        """Natural-language date, parsed by Todoist itself (deterministic on their side)."""
        _validate_id(task_id)
        await self._http.post(
            f"tasks/{task_id}",
            json={"due_string": due_string[:100], "due_lang": "ru"},
            headers={"X-Request-Id": request_id},
            retry_non_idempotent=True,
        )

    async def update_task(
        self,
        task_id: str,
        *,
        request_id: str,
        priority: int | None = None,
        due_date: date | None = None,
        due_datetime: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> None:
        """Explicit values computed by Jarvis (API priority 4 = p1 … 1 = p4); unset fields are left unchanged."""
        _validate_id(task_id)
        body = _update_body(priority, due_date, due_datetime, duration_minutes)
        if not body:
            return
        await self._http.post(
            f"tasks/{task_id}", json=body, headers={"X-Request-Id": request_id}, retry_non_idempotent=True
        )

    async def close_task(self, task_id: str, request_id: str) -> None:
        """Complete a task (a recurring task moves to its next occurrence)."""
        _validate_id(task_id)
        await self._http.post(f"tasks/{task_id}/close", headers={"X-Request-Id": request_id}, retry_non_idempotent=True)

    async def reopen_task(self, task_id: str, request_id: str) -> None:
        _validate_id(task_id)
        await self._http.post(
            f"tasks/{task_id}/reopen", headers={"X-Request-Id": request_id}, retry_non_idempotent=True
        )

    async def delete_task(self, task_id: str, request_id: str) -> None:
        _validate_id(task_id)
        await self._http.request("DELETE", f"tasks/{task_id}", headers={"X-Request-Id": request_id})

    async def probe(self) -> None:
        await self._http.get("projects", params={"limit": 1})


def _update_body(
    priority: int | None, due_date: date | None, due_datetime: datetime | None, duration_minutes: int | None
) -> dict[str, Any]:
    if due_date is not None and due_datetime is not None:
        raise PermanentError("todoist: due_date and due_datetime are mutually exclusive")
    body: dict[str, Any] = {}
    if priority is not None:
        if priority not in (1, 2, 3, 4):
            raise PermanentError("todoist: priority must be 1..4")
        body["priority"] = priority
    if due_date is not None:
        body["due_date"] = due_date.isoformat()
    if due_datetime is not None:
        body["due_datetime"] = _format_utc(due_datetime)
    if duration_minutes is not None:
        if not 1 <= duration_minutes <= MAX_DURATION_MINUTES:
            raise PermanentError("todoist: duration must be between 1 minute and 23h59m")
        body["duration"] = duration_minutes
        body["duration_unit"] = "minute"
    return body


def _validate_id(task_id: str) -> None:
    if not task_id or not task_id.replace("-", "").replace("_", "").isalnum() or len(task_id) > 40:
        raise PermanentError("todoist: invalid task id")


@dataclass
class FakeTodoistApi:
    """In-memory Todoist with the same JSON shapes as API v1."""

    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    unavailable: bool = False

    def add(self, **raw: Any) -> dict[str, Any]:
        task_id = str(raw.get("id") or uuid.uuid4().hex[:12])
        task = {
            "id": task_id,
            "content": "",
            "priority": 1,
            "labels": [],
            "checked": False,
            "is_deleted": False,
            "updated_at": "2026-09-23T00:00:00Z",
            **raw,
        }
        task["id"] = task_id
        self.tasks[task_id] = task
        return task

    def _check(self) -> None:
        if self.unavailable:
            from jarvis.core.errors import TransientError

            raise TransientError("todoist: fake outage")

    async def list_active_tasks(self) -> list[dict[str, Any]]:
        self._check()
        self.calls.append(("list", None))
        return [dict(t) for t in self.tasks.values() if not t.get("checked") and not t.get("is_deleted")]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        self._check()
        self.calls.append(("get", task_id))
        task = self.tasks.get(task_id)
        if task is None or task.get("is_deleted"):
            raise NotFoundError("todoist: HTTP 404")
        return dict(task)

    async def schedule_task(self, task_id: str, start: datetime, duration_minutes: int, request_id: str) -> None:
        self._check()
        self.calls.append(("schedule", (task_id, _format_utc(start), duration_minutes)))
        task = self.tasks[task_id]
        task["due"] = {
            "date": _format_utc(start)[:10],
            "datetime": _format_utc(start),
            "is_recurring": False,
            "timezone": None,
            "string": None,
        }
        task["duration"] = {"amount": duration_minutes, "unit": "minute"}
        task["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    async def create_task(
        self, content: str, *, due_string: str | None, request_id: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        self._check()
        self.calls.append(("create", (content, due_string)))
        due = {"date": None, "string": due_string} if due_string else None
        return self.add(content=content, due=due, parent_id=parent_id)

    async def update_due_string(self, task_id: str, due_string: str, request_id: str) -> None:
        self._check()
        self.calls.append(("due_string", (task_id, due_string)))
        self.tasks[task_id]["due"] = {"date": None, "string": due_string, "is_recurring": False}

    def _existing(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None or task.get("is_deleted"):
            raise NotFoundError("todoist: HTTP 404")
        return task

    def _descendants(self, task_id: str) -> list[dict[str, Any]]:
        children = [t for t in self.tasks.values() if str(t.get("parent_id") or "") == task_id]
        return children + [d for c in children for d in self._descendants(str(c["id"]))]

    def _touch(self, task: dict[str, Any]) -> None:
        task["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    async def update_task(
        self,
        task_id: str,
        *,
        request_id: str,
        priority: int | None = None,
        due_date: date | None = None,
        due_datetime: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> None:
        self._check()
        body = _update_body(priority, due_date, due_datetime, duration_minutes)
        self.calls.append(("update", (task_id, body)))
        task = self._existing(task_id)
        if "priority" in body:
            task["priority"] = body["priority"]
        if "due_date" in body:
            task["due"] = {"date": body["due_date"], "is_recurring": False, "string": body["due_date"]}
        if "due_datetime" in body:
            stamp = body["due_datetime"]
            task["due"] = {"date": stamp[:10], "datetime": stamp, "is_recurring": False, "string": stamp}
        if "duration" in body:
            task["duration"] = {"amount": body["duration"], "unit": "minute"}
        self._touch(task)

    async def close_task(self, task_id: str, request_id: str) -> None:
        self._check()
        self.calls.append(("close", task_id))
        task = self._existing(task_id)
        due = task.get("due") or {}
        if due.get("is_recurring") and due.get("date"):
            # Recurring: the task stays open and moves to the next occurrence (daily in this fake).
            next_day = date.fromisoformat(str(due["date"])[:10]).toordinal() + 1
            task["due"] = {**due, "date": date.fromordinal(next_day).isoformat()}
        else:
            task["checked"] = True
            for child in self._descendants(task_id):  # Todoist completes the subtasks of a completed parent
                child["checked"] = True
                self._touch(child)
        self._touch(task)

    async def reopen_task(self, task_id: str, request_id: str) -> None:
        self._check()
        self.calls.append(("reopen", task_id))
        task = self._existing(task_id)
        task["checked"] = False
        self._touch(task)

    async def delete_task(self, task_id: str, request_id: str) -> None:
        self._check()
        self.calls.append(("delete", task_id))
        task = self._existing(task_id)
        task["is_deleted"] = True
        for child in self._descendants(task_id):  # and deletes the subtasks of a deleted parent
            child["is_deleted"] = True
            self._touch(child)
        self._touch(task)

    async def probe(self) -> None:
        self._check()
