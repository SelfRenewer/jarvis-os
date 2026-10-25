"""Jarvis OS (2026-10-14): the map of Jarvis is read from the code — every intent, job and table is a node, links
never dangle, the few hand-kept placement tables name only things that exist, and nothing secret gets in."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from jarvis.bot.intents import Intent, keyword_route
from jarvis.core.config import Settings
from jarvis.db.base import Base
from jarvis.jobs.handlers import build_registry, periodic_tasks
from jarvis.system import map as system
from jarvis.system.live import error_class, status_detail, status_level
from jarvis.system.map import (
    AGENTS,
    CHANNELS,
    CORE,
    EXTERNAL,
    INTEGRATIONS,
    PLUMBING,
    PROCESS_NODES,
    Index,
    Placement,
    build_graph,
    first_sentence,
    package_roots,
    system_map,
)
from tests.conftest import make_settings

ROOT = Path(system.__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built() -> Any:
    return build_graph()


@pytest.fixture(scope="module")
def graph() -> dict[str, Any]:
    system.reset_cache()
    return system_map(make_settings())


def ids(graph: dict[str, Any], type_: str) -> set[str]:
    return {n["id"] for n in graph["nodes"] if n["type"] == type_}


def links_from(graph: dict[str, Any], source: str) -> set[str]:
    return {link["target"] for link in graph["links"] if link["source"] == source}


# --- everything the code has is on the map --------------------------------------------------------------------------


def test_every_intent_is_a_command(graph: dict[str, Any]) -> None:
    assert ids(graph, "command") == {f"command:{i.value}" for i in Intent}
    for intent in Intent:
        assert f"command:{intent.value}" in links_from(graph, "core:bot")


def test_every_registered_job_is_a_node_and_periodic_ones_are_scheduled(graph: dict[str, Any]) -> None:
    assert ids(graph, "job") == {f"job:{t}" for t in build_registry().handlers}
    scheduled = links_from(graph, "core:scheduler")
    settings = make_settings()
    for task in periodic_tasks(settings):
        assert f"job:{task.job_type}" in scheduled
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["job:meds.tick"]["extra"]["schedule"] == "каждые 5 мин"
    assert nodes["job:briefing.morning"]["extra"]["schedule"] == f"ежедневно в {settings.briefing_morning_at}"


def test_every_table_is_a_node(graph: dict[str, Any]) -> None:
    assert ids(graph, "data") == {f"data:{name}" for name in Base.metadata.tables}


def test_no_dangling_links_and_no_isolated_nodes(built: Any, graph: dict[str, Any]) -> None:
    assert built.graph.dropped == []  # nothing pruned: every derived link pointed at a real node
    known = {n["id"] for n in graph["nodes"]}
    for link in graph["links"]:
        assert link["source"] in known and link["target"] in known, link
        assert link["kinds"] and set(link["kinds"]) <= set(graph["link_kinds"])
    assert [n["id"] for n in graph["nodes"] if n["degree"] == 0] == []


def test_every_node_is_described(graph: dict[str, Any]) -> None:
    for node in graph["nodes"]:
        assert node["label"].strip(), node["id"]
        assert node["description"].strip(), node["id"]
        assert re.match(r"^[\w/.-]+\.py:\d+$", node["path"]), (node["id"], node["path"])
        assert (ROOT / node["path"].split(":")[0]).exists(), node["path"]
    counts = {t["id"]: t["count"] for t in graph["types"]}
    assert sum(counts.values()) == len(graph["nodes"]) == graph["stats"]["nodes"]
    assert set(counts) == set(system.TYPES)
    assert all(counts[t] > 0 for t in counts)
    assert len(graph["hubs"]) == 12 and graph["hubs"][0] == "core:bot"


# --- the hand-kept tables name only what exists, and nothing new is left unplaced ------------------------------------


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(package_roots())


def test_every_module_is_placed(index: Index) -> None:
    """A new package must be put in AGENTS / CORE / INTEGRATIONS / PLUMBING (not fall through to «jarvis»)."""
    placement = Placement()
    for name, src in index.modules.items():
        prefix = placement.prefix_of(name)
        assert prefix is not None, name
        if name != "jarvis":
            assert prefix != "jarvis", f"{name}: place it in jarvis/system/map.py"
        if name.startswith("jarvis.integrations.") and not src.is_package:
            assert placement.node_of(name) is not None, f"{name}: add it to INTEGRATIONS"


def test_placement_tables_name_existing_code(index: Index) -> None:
    for table in (AGENTS, CORE):
        for key, (label, prefixes) in table.items():
            assert label and prefixes, key
            for prefix in prefixes:
                assert any(m == prefix or m.startswith(prefix + ".") for m in index.modules), prefix
    for key, (label, prefixes, _status) in INTEGRATIONS.items():
        assert label, key
        for prefix in prefixes:
            assert any(m == prefix or m.startswith(prefix + ".") for m in index.modules), prefix
    for prefix in PLUMBING:
        assert any(m == prefix or m.startswith(prefix + ".") for m in index.modules), prefix
    for _label, module, _links in EXTERNAL.values():
        assert module in index.modules
    for _label, module, _route, _links in CHANNELS.values():
        assert module in index.modules


def test_explicit_links_point_at_nodes(graph: dict[str, Any]) -> None:
    known = {n["id"] for n in graph["nodes"]}
    for key, (_label, _module, links) in EXTERNAL.items():
        assert f"integration:{key}" in known
        assert set(links) <= known
    for key, (_label, _module, _route, links) in CHANNELS.items():
        assert f"channel:{key}" in known
        assert set(links) <= known
        assert set(links) <= links_from(graph, f"channel:{key}")
    assert set(PROCESS_NODES.values()) <= known


def test_channel_routes_exist_in_the_api(index: Index) -> None:
    routes = {
        str(deco.args[0].value)
        for node in ast.walk(index.modules["jarvis.api.app"].tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for deco in node.decorator_list
        if isinstance(deco, ast.Call) and deco.args and isinstance(deco.args[0], ast.Constant)
    }
    for key, (_label, _module, route, _links) in CHANNELS.items():
        assert route is None or route in routes, key


def test_status_keys_and_process_roles_exist(index: Index) -> None:
    """INTEGRATIONS' status keys are the keys `build_container` writes; PROCESS_NODES' roles are heartbeat roles."""
    container = index.modules["jarvis.services.container"].tree
    written = {
        str(node.slice.value)
        for node in ast.walk(container)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "status"
        and isinstance(node.slice, ast.Constant)
    }
    keys = {status for _l, _p, status in INTEGRATIONS.values() if status is not None}
    assert keys <= written
    assert written - keys == set(), f"status keys without an integration node: {written - keys}"
    cli = (ROOT / "jarvis" / "cli.py").read_text(encoding="utf-8")
    beats = set(re.findall(r"_process_heartbeat\([^,]+,\s*\"(\w+)\"", cli))
    assert set(PROCESS_NODES) == beats


# --- what the links say ---------------------------------------------------------------------------------------------


def test_links_follow_the_code(graph: dict[str, Any]) -> None:
    assert {"agent:research", "core:approvals"} <= links_from(graph, "command:research")
    assert "agent:tracker" in links_from(graph, "command:food_log")
    assert "job:desktop.notify" in links_from(graph, "command:mac_remind")  # enqueued by the handler
    assert "integration:lmf" in links_from(graph, "agent:tracker")  # injected by the container
    assert "integration:todoist" in links_from(graph, "agent:sync")
    assert "agent:sync" in links_from(graph, "job:todoist.reconcile")  # `c.todoist_sync` in the handler
    assert "agent:reminders" in links_from(graph, "job:reminder.fire")
    assert "job:reminder.fire" in links_from(graph, "agent:reminders")  # enqueue(job_type="reminder.fire")
    assert "job:health.ingest" in links_from(graph, "core:api")
    assert "data:scheduled_jobs" in links_from(graph, "core:scheduler")
    assert "data:spend_snapshots" in links_from(graph, "agent:usage")
    assert "data:integration_accounts" in links_from(graph, "integration:google_calendar")  # via DbTokenStore
    assert "core:api" in links_from(graph, "agent:dev_worker")  # talks to /api/dev-worker/v1/…
    assert "agent:health" in links_from(graph, "integration:health_connect")
    assert "data:ideas" in links_from(graph, "data:idea_versions")  # foreign key
    assert not {t for t in links_from(graph, "core:bot") if t.startswith("agent:")}  # via its commands instead


def test_command_examples_are_what_the_router_understands(graph: dict[str, Any]) -> None:
    """Phrases marked «no AI» really reach their command through the keyword router."""
    marked = 0
    for node in graph["nodes"]:
        if node["type"] != "command":
            continue
        intent = node["extra"]["intent"]
        for example in node["extra"]["examples"]:
            if example["no_ai"]:
                said = re.sub(r"\s*…\s*$", " текст", example["text"]).replace("…", "текст")
                assert keyword_route(said).intent.value == intent, (intent, example)
                marked += 1
    assert marked >= 60
    nodes = {n["id"]: n for n in graph["nodes"]}
    calc = nodes["command:calculate"]["extra"]
    assert {"text": "10 000 000 ÷ 1500", "no_ai": True} in calc["examples"]
    assert nodes["command:show_today"]["extra"]["slash"] == ["/today"]
    assert "📅 Сегодня" in nodes["command:show_today"]["extra"]["buttons"]
    assert nodes["command:voice_mode"]["extra"]["slash"] == ["/voice"]
    assert "с подтверждением" in nodes["command:research"]["extra"]["flags"]


# --- nothing secret, nothing personal --------------------------------------------------------------------------------


def test_no_secrets_or_personal_settings_in_the_map() -> None:
    secrets = {
        name: f"secret-{name}-0123456789"
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation)
    }
    secrets["google_calendar_ics_url"] = "https://calendar.google.com/calendar/ical/private-abcdef0123456789/basic.ics"
    settings = make_settings(**secrets, default_origin="Unique Street 77, Tbilisi", user_phone_number="+15555550199")
    assert all(isinstance(getattr(settings, name), SecretStr) for name in secrets)
    system.reset_cache()
    text = json.dumps(system_map(settings), ensure_ascii=False)
    for value in settings.secret_values():
        assert value not in text
    for personal in ("Unique Street 77", "+15555550199", "private-abcdef"):
        assert personal not in text
    system.reset_cache()


def test_the_map_is_cached_and_follows_the_schedule_settings() -> None:
    system.reset_cache()
    first = system_map(make_settings())
    assert system_map(make_settings()) is first
    quiet = system_map(make_settings(weather_enabled=False))
    nodes = {n["id"]: n for n in quiet["nodes"]}
    assert nodes["job:weather.watch"]["extra"]["schedule"] == "выключено настройками"
    system.reset_cache()


# --- small helpers ---------------------------------------------------------------------------------------------------


def test_first_sentence() -> None:
    assert first_sentence("One thing. Another.\n\nMore.") == "One thing."
    said = "«изучи …» → the price first; then the search."
    assert first_sentence(said) == said
    assert first_sentence(None) == ""
    assert first_sentence("x" * 400).endswith("…")


def test_live_helpers_never_pass_messages_or_urls() -> None:
    assert error_class("TransientError: GET https://x.example/ical/private-123/basic.ics timed out") == "TransientError"
    assert error_class("jarvis.core.errors.AuthError: revoked") == "AuthError"
    assert error_class("something odd https://secret.example/?key=1") == "ошибка"
    assert error_class(None) is None
    assert status_level("CONFIGURED (openrouter: m)") == "ok"
    assert status_level("IMPLEMENTED_MOCK_TESTED") == "ok"
    assert status_level("DISABLED") == "off"
    assert status_level("LOCAL_FALLBACK (search by words only)") == "warn"
    assert status_detail("CONFIGURED (http://host.docker.internal:4321/api?key=abc)") == "host.docker.internal:4321"
    assert status_detail("CONFIGURED") == ""


def test_every_agent_core_service_and_channel_is_described_in_russian() -> None:
    """16.10: the map showed English docstrings; the parts a person reads about have a Russian line (`RU`)."""
    from jarvis.system.map import RU, system_map

    nodes = system_map(make_settings())["nodes"]
    shown = [n for n in nodes if n["type"] in ("agent", "core", "integration", "channel")]
    assert [n["id"] for n in shown if n["id"] not in RU] == []
    assert all(n["description"] == RU[n["id"]] for n in shown)
    assert set(RU) <= {n["id"] for n in nodes}  # no stale entries
