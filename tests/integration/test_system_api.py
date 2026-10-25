"""Jarvis OS (2026-10-14): the system page's two endpoints — the map and the live layer — behind the Mac app's token.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi (05:00 UTC).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects.postgresql import insert

from jarvis.api.app import create_app
from jarvis.core.security import TokenCipher, desktop_token
from jarvis.db.models import HealthPayload, IntegrationSyncState, ScheduledJob, TodoistTask, UserPreference
from jarvis.desktop.service import MAC_ENV_KEY
from jarvis.services.container import build_container
from jarvis.system import map as system
from tests.conftest import make_settings

pytestmark = pytest.mark.db

GRAPH = "/api/desktop/v1/system/graph"
LIVE = "/api/desktop/v1/system/live"
ASK = "/api/desktop/v1/system/ask"
LEAKY_ERROR = "TransientError: GET https://calendar.example/ical/private-0123456789abcdef/basic.ics timed out"


@pytest.fixture
def mac(db: Any, clock: Any) -> Any:
    system.reset_cache()
    c = build_container(make_settings(jarvis_encryption_key=TokenCipher.generate_key()), db=db, clock=clock)
    c.desktop_token = desktop_token(c.settings.jarvis_encryption_key)
    return c


def client(c: Any, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token or c.desktop_token}"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url="http://t", headers=headers)


async def test_token_is_required(mac: Any) -> None:
    async with client(mac, token="wrong") as http:
        assert (await http.get(GRAPH)).status_code == 401
        assert (await http.get(LIVE)).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(mac)), base_url="http://t") as http:
        assert (await http.get(GRAPH)).status_code == 401  # no header at all
    plain = build_container(make_settings(), db=mac.db, clock=mac.clock)  # no secret to derive a token from
    async with client(plain, token="x") as http:
        assert (await http.get(GRAPH)).status_code == 503


async def test_ask_does_not_mark_the_mac_online(mac: Any) -> None:
    """The page's «Спросить» runs the Mac conversation but is not the Mac app: no `seen_at`, so reminders keep
    going to Telegram while the app is closed (`/message` from the app does mark it)."""
    async with client(mac, token="wrong") as http:
        assert (await http.post(ASK, json={"text": "привет"})).status_code == 401
    async with client(mac) as http:
        assert (await http.post(ASK, json={"text": ""})).status_code == 422
        reply = (await http.post(ASK, json={"text": "что у меня завтра?"})).json()
        assert reply["messages"] and "Завтра" in reply["messages"][0]["text"]
        async with mac.db.session() as s:
            assert await s.get(UserPreference, MAC_ENV_KEY) is None
        await http.post("/api/desktop/v1/message", json={"text": "что у меня завтра?"})
        async with mac.db.session() as s:
            assert (await s.get(UserPreference, MAC_ENV_KEY)) is not None
    assert mac.telegram.sent == []  # the answer went back to the page, not to Telegram


async def test_graph_shape(mac: Any) -> None:
    async with client(mac) as http:
        response = await http.get(GRAPH)
    assert response.status_code == 200
    graph = response.json()
    assert {"types", "nodes", "links", "hubs", "link_kinds", "stats"} <= set(graph)
    assert [t["id"] for t in graph["types"]] == list(system.TYPES)
    node = next(n for n in graph["nodes"] if n["id"] == "command:research")
    assert {"id", "type", "label", "description", "path", "extra", "degree"} <= set(node)
    assert node["extra"]["examples"] and node["extra"]["handler"] == "cmd_research"
    known = {n["id"] for n in graph["nodes"]}
    assert all(link["source"] in known and link["target"] in known for link in graph["links"])
    assert set(graph["hubs"]) <= known


async def test_live_layer(mac: Any) -> None:
    now = mac.clock.now()
    async with mac.db.session() as s:
        s.add_all(
            [
                ScheduledJob(
                    job_type="todoist.reconcile",
                    run_at=now - timedelta(minutes=3),
                    status="succeeded",
                    finished_at=now - timedelta(minutes=2),
                ),
                ScheduledJob(
                    job_type="calendar.reconcile",
                    run_at=now - timedelta(hours=2),
                    status="dead",
                    attempts=3,
                    finished_at=now - timedelta(hours=1),
                    last_error=LEAKY_ERROR,
                ),
                ScheduledJob(job_type="meds.tick", run_at=now, status="running", locked_at=now),
                ScheduledJob(job_type="reminders.plan", run_at=now + timedelta(minutes=5), status="pending"),
                TodoistTask(id="t1", content="Постричься", synced_at=now),
                HealthPayload(body={"n": 1}, received_at=now - timedelta(minutes=10)),
            ]
        )
        for role, age in (("worker", 20), ("scheduler", 30), ("bot", 600)):
            await s.execute(
                insert(IntegrationSyncState).values(
                    provider="process",
                    resource=role,
                    state={"mode": "polling", "secret": "never-shown"},
                    last_success_at=now - timedelta(seconds=age),
                )
            )
    async with client(mac) as http:
        response = await http.get(LIVE)
    assert response.status_code == 200
    live = response.json()
    nodes = live["nodes"]
    assert nodes["job:todoist.reconcile"]["state"] == "ok" and nodes["job:todoist.reconcile"]["active"]
    failed = nodes["job:calendar.reconcile"]
    assert (failed["state"], failed["failed_24h"], failed["error"]) == ("failed", 1, "TransientError")
    assert nodes["job:meds.tick"]["running"] == 1 and "job:meds.tick" in live["running"]
    assert nodes["job:reminders.plan"]["pending"] == 1 and nodes["job:reminders.plan"]["next_run"]
    assert nodes["agent:sync"]["state"] == "failed"  # the worst of the jobs that run through it
    assert nodes["agent:meds"]["active"] is True
    assert nodes["core:scheduler"]["state"] == "ok"
    assert nodes["core:bot"]["state"] == "failed"  # its heartbeat is 10 minutes old
    assert nodes["channel:telegram"]["state"] == "failed"
    assert nodes["channel:phone"]["state"] == "ok"
    assert nodes["channel:mac"] == {"state": "idle", "online": False}
    assert nodes["integration:todoist"]["state"] == "ok"  # the test container's adapters are fakes
    assert "rows" in nodes["data:scheduled_jobs"]
    assert live["counts"]["open_tasks"] == 1
    assert live["counts"]["dead_jobs_24h"] == 1 and live["counts"]["running_jobs"] == 1
    assert {p["role"]: p["ok"] for p in live["processes"]} == {"worker": True, "scheduler": True, "bot": False}
    text = json.dumps(live)
    assert "private-0123456789abcdef" not in text and "calendar.example" not in text  # error text never leaves
    assert "never-shown" not in text and "Постричься" not in text  # no process state, no task titles
