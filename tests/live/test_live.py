"""Opt-in live tests against REAL services. Never collected by the normal `pytest` run
(`--ignore=tests/live` in pyproject). Run explicitly, e.g.:

    JARVIS_LIVE_TESTS=true TODOIST_API_TOKEN=... TODOIST_TEST_PROJECT_ID=... \\
        uv run pytest tests/live -o addopts="" -k todoist

Each test uses dedicated test resources and cleans up after itself. Paid actions need their own flag:
ALLOW_LIVE_CALL_TESTS=true (+ LIVE_TEST_PHONE_NUMBER) and ALLOW_LIVE_CLAUDE_TESTS=true (+ LIVE_TEST_REPO).
No test here ever places a purchase.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if os.environ.get("JARVIS_LIVE_TESTS") != "true":
    pytest.skip("live tests disabled (set JARVIS_LIVE_TESTS=true)", allow_module_level=True)


def need(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"missing env: {', '.join(missing)}")


async def test_live_todoist_roundtrip() -> None:
    need("TODOIST_API_TOKEN", "TODOIST_TEST_PROJECT_ID")
    from jarvis.core.http import ResilientHttp
    from jarvis.integrations.todoist.client import HttpTodoistApi

    token = os.environ["TODOIST_API_TOKEN"]
    api = HttpTodoistApi(token)
    raw = ResilientHttp(
        "todoist-live", base_url="https://api.todoist.com/api/v1/", headers={"Authorization": f"Bearer {token}"}
    )
    created = (
        await raw.post(
            "tasks",
            json={
                "content": f"JARVIS live test {uuid.uuid4().hex[:6]}",
                "project_id": os.environ["TODOIST_TEST_PROJECT_ID"],
            },
        )
    ).json()
    try:
        start = datetime.now(UTC).replace(microsecond=0, second=0) + timedelta(days=1)
        await api.schedule_task(str(created["id"]), start, 30, request_id=uuid.uuid4().hex)
        fetched = await api.get_task(str(created["id"]))
        assert fetched["duration"]["amount"] == 30
    finally:
        await raw.request("DELETE", f"tasks/{created['id']}")  # only the task this test created
        await api.aclose()
        await raw.aclose()


async def test_live_telegram_get_me() -> None:
    need("TELEGRAM_BOT_TOKEN")
    from jarvis.integrations.telegram.client import HttpTelegramApi

    api = HttpTelegramApi(os.environ["TELEGRAM_BOT_TOKEN"])
    try:
        assert (await api.get_me())["is_bot"] is True
    finally:
        await api.aclose()


async def test_live_anthropic_structured_output() -> None:
    need("ANTHROPIC_API_KEY")
    from jarvis.ideas.schemas import IDEA_SYSTEM, IdeaAnalysis
    from jarvis.integrations.llm.client import AnthropicLLM

    llm = AnthropicLLM(os.environ["ANTHROPIC_API_KEY"], os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"))
    try:
        result = await llm.structured(
            capability="idea_analyzer",
            system=IDEA_SYSTEM,
            schema=IdeaAnalysis,
            user="<raw_thought>Хочу AI inbox для сортировки мыслей</raw_thought>",
        )
        assert result.title
    finally:
        await llm.aclose()


async def test_live_routes() -> None:
    need("GOOGLE_MAPS_API_KEY", "LIVE_TEST_ORIGIN", "LIVE_TEST_DESTINATION")
    from jarvis.integrations.maps.routes import HttpRoutesApi, RouteStatus

    api = HttpRoutesApi(os.environ["GOOGLE_MAPS_API_KEY"])
    try:
        result = await api.compute(
            os.environ["LIVE_TEST_ORIGIN"],
            os.environ["LIVE_TEST_DESTINATION"],
            datetime.now(UTC) + timedelta(minutes=30),
            "DRIVE",
        )
        assert result.status == RouteStatus.OK
    finally:
        await api.aclose()


async def test_live_openrouteservice() -> None:
    """Free plan: 1 geocode + 1 route (LIVE_TEST_ORIGIN may be an address, LIVE_TEST_DESTINATION «lat,lng»)."""
    need("OPENROUTESERVICE_API_KEY", "LIVE_TEST_ORIGIN", "LIVE_TEST_DESTINATION")
    from jarvis.integrations.maps.openrouteservice import OpenRouteServiceRoutesApi
    from jarvis.integrations.maps.routes import RouteStatus

    api = OpenRouteServiceRoutesApi(os.environ["OPENROUTESERVICE_API_KEY"], focus=(41.7151, 44.8271), country="GE")
    try:
        result = await api.compute(
            os.environ["LIVE_TEST_ORIGIN"], os.environ["LIVE_TEST_DESTINATION"], datetime.now(UTC), "DRIVE"
        )
        assert result.status == RouteStatus.OK, result.error
        assert result.duration_seconds is not None and result.duration_seconds > 0
    finally:
        await api.aclose()


async def test_live_phone_call() -> None:
    if os.environ.get("ALLOW_LIVE_CALL_TESTS") != "true":
        pytest.skip("paid call: set ALLOW_LIVE_CALL_TESTS=true")
    need("VAPI_API_KEY", "VAPI_ASSISTANT_ID", "VAPI_PHONE_NUMBER_ID", "LIVE_TEST_PHONE_NUMBER")
    from jarvis.integrations.voice.providers import CallSpec, VapiCallProvider

    provider = VapiCallProvider(
        os.environ["VAPI_API_KEY"],
        os.environ["VAPI_ASSISTANT_ID"],
        os.environ["VAPI_PHONE_NUMBER_ID"],
        live_enabled=True,
    )
    try:
        placed = await provider.place_call(
            CallSpec(
                f"live-test-{uuid.uuid4().hex[:8]}",
                os.environ["LIVE_TEST_PHONE_NUMBER"],
                {
                    "event_title": "Тестовый звонок JARVIS",
                    "event_start_local": "—",
                    "leave_at_local": "—",
                    "minutes_until_leave": "0",
                    "destination": "—",
                    "next_event": "нет",
                },
                "Тестовый звонок JARVIS",
            )
        )
        assert placed.external_call_id
    finally:
        await provider.aclose()


async def test_live_claude_code_on_test_repo(tmp_path: Path) -> None:
    if os.environ.get("ALLOW_LIVE_CLAUDE_TESTS") != "true":
        pytest.skip("paid nested Claude run: set ALLOW_LIVE_CLAUDE_TESTS=true")
    need("LIVE_TEST_REPO")
    from dev_worker.claude import ClaudeLimits, SdkClaudeCodeAdapter
    from dev_worker.git_ops import prepare_worktree
    from dev_worker.policy import RepoConfig

    repo = RepoConfig("live-test", Path(os.environ["LIVE_TEST_REPO"]).expanduser(), remote=None)
    ws = prepare_worktree(
        repo.path, repo.base_branch, None, f"jarvis/live-test-{uuid.uuid4().hex[:6]}", tmp_path / "wt"
    )
    result = await SdkClaudeCodeAdapter().run(
        ws.worktree,
        "# DEV_TASK\n\n## Goal\n\nCreate a file HELLO.md containing 'hello'.\n",
        repo,
        ClaudeLimits(max_turns=8, max_budget_usd=0.5),
    )
    assert result.status == "success", result.error
    assert (ws.worktree / "HELLO.md").exists()
