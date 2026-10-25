"""Jarvis OS live layer: what is running or failing right now, laid over the map's node ids.

Cheap reads only: a few aggregate SQL queries, the process heartbeats (`integration_sync_state`, provider 'process'),
and the container's in-memory status registry — the same sources `/status` and `jarvis doctor` use, but without any
network call or anything billable. No personal data: counts, job types, times and error class names; never message
texts, payloads, reminder titles or raw error messages (they may quote a URL with a key in it).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from jarvis.core.clock import local_day_bounds, to_local
from jarvis.db.models import (
    Approval,
    CalendarEvent,
    DevWorkerHeartbeat,
    HealthPayload,
    Idea,
    IntegrationSyncState,
    Note,
    Notification,
    Reminder,
    ShoppingItem,
    TodoistTask,
)
from jarvis.system.map import INTEGRATIONS, PROCESS_NODES

PROCESS_STALE = timedelta(seconds=180)  # the same threshold as `jarvis doctor`
ACTIVE_WINDOW = timedelta(minutes=15)  # a job that ran this recently still glows on the map
PHONE_STALE = timedelta(minutes=90)  # Life Dashboard sends every 30 min
_ERROR_CLASS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout|Job))\b")
_URL = re.compile(r"(https?://)([^/\s:]+)(:\d+)?[^\s)]*")

# Health of a node; the page colours by it. `active` (running now / just ran) is separate: it pulses.
SEVERITY = {"failed": 4, "warn": 3, "ok": 2, "idle": 1, "off": 0}

_JOBS_SQL = text(
    """
    SELECT job_type,
           count(*) FILTER (WHERE status = 'running') AS running,
           count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'pending' AND attempts > 0) AS retrying,
           min(run_at) FILTER (WHERE status = 'pending') AS next_run,
           count(*) FILTER (WHERE status IN ('succeeded', 'skipped') AND finished_at > :since) AS done_24h,
           count(*) FILTER (WHERE status = 'dead' AND finished_at > :since) AS dead_24h
      FROM scheduled_jobs
     WHERE status IN ('pending', 'running') OR finished_at > :since
     GROUP BY job_type
    """
)
_LAST_SQL = text(
    """
    SELECT DISTINCT ON (job_type) job_type, status, finished_at, last_error
      FROM scheduled_jobs
     WHERE finished_at > :since AND status IN ('succeeded', 'skipped', 'dead', 'cancelled')
     ORDER BY job_type, finished_at DESC
    """
)
_ROWS_SQL = text(
    """
    SELECT c.relname, greatest(c.reltuples, 0)::bigint
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relkind = 'r'
    """
)


def error_class(error: str | None) -> str | None:
    """«TransientError: GET https://…?key=… timed out» → «TransientError». Never the message itself."""
    if not error:
        return None
    match = _ERROR_CLASS.match(error.strip())
    return match.group(1).rsplit(".", 1)[-1] if match else "ошибка"


def status_level(status: str) -> str:
    """A `Container.status` value → ok / warn / off."""
    if status.startswith(("CONFIGURED", "IMPLEMENTED_MOCK_TESTED")):
        return "ok"
    if status.startswith(("DISABLED", "IMPLEMENTED_NEEDS_CREDENTIALS")):
        return "off"
    return "warn"


def status_detail(status: str) -> str:
    """The part in brackets, with URLs cut to their host («http://host.docker.internal:4321/…» → host:port)."""
    match = re.search(r"\((.*)\)", status)
    detail = match.group(1) if match else ""
    return _URL.sub(lambda m: f"{m.group(2)}{m.group(3) or ''}", detail)[:120]


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _worst(states: list[str]) -> str:
    return max(states, key=lambda s: SEVERITY.get(s, 0)) if states else "idle"


async def live_status(container: Any, graph: dict[str, Any]) -> dict[str, Any]:
    """Live state per node id of `graph` (from `system_map`) + a few counters. Cheap enough to poll every 10 s."""
    now: datetime = container.clock.now()
    since = now - timedelta(hours=24)
    tz = container.settings.tz
    day_start, day_end = local_day_bounds(to_local(now, tz).date(), tz)
    nodes: dict[str, dict[str, Any]] = {}
    async with container.db.session() as s:
        jobs = (await s.execute(_JOBS_SQL, {"since": since})).mappings().all()
        last = {row["job_type"]: row for row in (await s.execute(_LAST_SQL, {"since": since})).mappings().all()}
        processes = (
            await s.execute(
                select(
                    IntegrationSyncState.resource, IntegrationSyncState.last_success_at, IntegrationSyncState.state
                ).where(IntegrationSyncState.provider == "process")
            )
        ).all()
        syncs = (
            await s.execute(
                select(
                    IntegrationSyncState.provider,
                    IntegrationSyncState.last_success_at,
                    IntegrationSyncState.consecutive_failures,
                ).where(IntegrationSyncState.provider.in_(["todoist", "google_calendar"]))
            )
        ).all()
        rows = dict((await s.execute(_ROWS_SQL)).all())

        async def count(stmt: Any) -> int:
            return int(await s.scalar(stmt) or 0)

        counts = {
            "open_tasks": await count(
                select(func.count())
                .select_from(TodoistTask)
                .where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
            ),
            "events_today": await count(
                select(func.count())
                .select_from(CalendarEvent)
                .where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.start_at < day_end,
                    CalendarEvent.end_at > day_start,
                )
            ),
            "reminders_today": await count(
                select(func.count())
                .select_from(Reminder)
                .where(Reminder.fire_at >= day_start, Reminder.fire_at < day_end, Reminder.status != "cancelled")
            ),
            "pending_approvals": await count(
                select(func.count())
                .select_from(Approval)
                .where(Approval.status == "pending", (Approval.expires_at.is_(None)) | (Approval.expires_at > now))
            ),
            "sent_24h": await count(
                select(func.count())
                .select_from(Notification)
                .where(Notification.status == "sent", Notification.sent_at > since)
            ),
            "ideas": await count(select(func.count()).select_from(Idea)),
            "notes": await count(select(func.count()).select_from(Note)),
            "shopping_open": await count(
                select(func.count()).select_from(ShoppingItem).where(ShoppingItem.status == "open")
            ),
        }
        dead_total = await count(text("SELECT count(*) FROM scheduled_jobs WHERE status = 'dead'"))
        health_seen = await s.scalar(select(func.max(HealthPayload.received_at)))
        workers = (await s.execute(select(func.count(), func.max(DevWorkerHeartbeat.last_seen_at)))).one()
        mac = await container.desktop.env(s)

    # -- background jobs
    known = {n["id"] for n in graph["nodes"]}
    running: list[str] = []
    dead_24h = 0
    for row in jobs:
        node_id = f"job:{row['job_type']}"
        finished = last.get(row["job_type"])
        last_status = finished["status"] if finished is not None else None
        last_run = finished["finished_at"] if finished is not None else None
        dead_24h += int(row["dead_24h"])
        if row["running"]:
            state = "ok"
            running.append(node_id)
        else:
            state = "idle"
        if last_status in ("succeeded", "skipped"):
            state = "ok"
        if row["retrying"] or (row["dead_24h"] and last_status != "dead"):
            state = "warn"
        if last_status == "dead":
            state = "failed"
        active = bool(row["running"]) or (last_run is not None and now - last_run < ACTIVE_WINDOW)
        nodes[node_id] = {
            "state": state,
            "active": active,
            "running": int(row["running"]),
            "pending": int(row["pending"]),
            "retrying": int(row["retrying"]),
            "next_run": _iso(row["next_run"]),
            "last_run": _iso(last_run),
            "last_status": last_status,
            "runs_24h": int(row["done_24h"]) + int(row["dead_24h"]),
            "failed_24h": int(row["dead_24h"]),
            "error": error_class(finished["last_error"]) if finished is not None and last_status == "dead" else None,
        }

    # -- integrations: the status registry (no probes) + when the syncs last succeeded
    synced = {row.provider: row for row in syncs}
    for key, (_label, _prefixes, status_key) in INTEGRATIONS.items():
        node_id = f"integration:{key}"
        status = container.status.get(status_key) if status_key else None
        if status is None:
            continue
        info: dict[str, Any] = {"state": status_level(status), "status": status.split(" ")[0]}
        if detail := status_detail(status):
            info["detail"] = detail
        if (sync := synced.get(status_key or "")) is not None:
            info["synced_at"] = _iso(sync.last_success_at)
            if sync.consecutive_failures:
                info["state"] = "warn"
                info["failures"] = int(sync.consecutive_failures)
        nodes[node_id] = info

    # -- processes → the nodes they keep alive
    beats: list[dict[str, Any]] = []
    for role, seen, process_state in processes:
        age = int((now - seen).total_seconds()) if seen is not None else None
        mode = str((process_state or {}).get("mode") or "")
        healthy = age is not None and age < PROCESS_STALE.total_seconds() and not mode.startswith("error")
        beats.append({"role": role, "age_s": age, "ok": healthy, "mode": mode[:40] or None})
        keeps = PROCESS_NODES.get(role)
        if keeps is not None:
            entry = nodes.setdefault(keeps, {"state": "ok", "processes": []})
            entry.setdefault("processes", []).append(role)
            if not healthy:
                entry["state"] = "failed"
    if any(b["role"] == "bot" for b in beats):
        bot_ok = all(b["ok"] for b in beats if b["role"] == "bot")
        nodes["channel:telegram"] = {"state": "ok" if bot_ok else "failed"}
    if any(b["role"] == "caller" for b in beats):
        nodes["channel:calls"] = {"state": nodes.get("agent:calls", {}).get("state", "ok")}
    mac_online = mac is not None and mac.online(now)
    nodes["channel:mac"] = {"state": "ok" if mac_online else "idle", "online": mac_online}
    if health_seen is not None:
        fresh = now - health_seen < PHONE_STALE
        nodes["channel:phone"] = {"state": "ok" if fresh else "warn", "last_seen": _iso(health_seen)}
        nodes.setdefault("integration:health_connect", {"state": "ok" if fresh else "warn"})

    # -- agents (and the other non-job nodes jobs run through): the worst of their jobs, active when one is
    for link in graph["links"]:
        source, target = link["source"], link["target"]
        if not source.startswith("job:") or source not in nodes or target not in known:
            continue
        if not (target.startswith("agent:") or target.startswith("integration:")):
            continue
        job = nodes[source]
        entry = nodes.setdefault(target, {"state": "idle"})
        if target.startswith("agent:"):
            entry["state"] = _worst([entry["state"], job["state"]])
        entry["active"] = bool(entry.get("active")) or bool(job["active"])

    # -- tables: an estimate of their size (pg_class), no rows
    for node in graph["nodes"]:
        if node["type"] == "data" and node["label"] in rows:
            nodes.setdefault(node["id"], {})["rows"] = int(rows[node["label"]])

    return {
        "now": now.isoformat(),
        "nodes": {k: v for k, v in nodes.items() if k in known},
        "running": running,
        "processes": beats,
        "counts": {
            **counts,
            "dead_jobs_24h": dead_24h,
            "dead_jobs_total": dead_total,
            "running_jobs": len(running),
            "dev_workers": int(workers[0] or 0),
        },
        "mac_online": mac_online,
    }
