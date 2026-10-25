"""«Открой …» on the Mac: aliases, sites, folders, projects by ear (no LLM, no network)."""

from __future__ import annotations

import pytest

from jarvis.desktop.service import Ambiguous, MacEnv, Project, Target, resolve_open, resolve_project, translit

ENV = MacEnv(
    apps=["Telegram", "Safari", "Google Chrome", "Claude", "Cursor", "Visual Studio Code", "Music", "Notes", "Notion"],
    projects=[
        Project("JarvisOS", "/Users/u/Projects/JarvisOS"),
        Project("portfolio-site", "/Users/u/Projects/portfolio-site"),
        Project("budget-bot", "/Users/u/Projects/budget-bot"),
    ],
)


@pytest.mark.parametrize(
    ("said", "kind", "value"),
    [
        ("телегу", "open_app", "Telegram"),
        ("телеграм на маке", "open_app", "Telegram"),
        ("приложение сафари", "open_app", "Safari"),
        ("хром", "open_app", "Google Chrome"),
        ("курсор", "open_app", "Cursor"),
        ("Claude", "open_app", "Claude"),
        ("notion", "open_app", "Notion"),
        ("ютуб", "open_url", "https://www.youtube.com"),
        ("сайт openrouter.ai/activity", "open_url", "https://openrouter.ai/activity"),
        ("github.com", "open_url", "https://github.com"),
        ("папку загрузки", "open_path", "~/Downloads"),
        ("папку с документами", "open_path", "~/Documents"),
        ("рабочий стол", "open_path", "~/Desktop"),
        ("проект джарвис", "open_path", "/Users/u/Projects/JarvisOS"),
        ("budget bot", "open_path", "/Users/u/Projects/budget-bot"),
    ],
)
def test_resolve_open(said: str, kind: str, value: str) -> None:
    target = resolve_open(said, ENV)
    assert isinstance(target, Target)
    assert target.kind == kind
    assert value in target.payload.values()


def test_unknown_and_not_installed() -> None:
    assert resolve_open("фотошоп", ENV) is None  # not installed: never guessed
    assert resolve_open("сайт", ENV) is None
    assert resolve_open("ватсап", ENV) is None  # an alias, but the app is not on this Mac


def test_projects_by_ear() -> None:
    assert translit("джарвис") == "jarvis"
    assert resolve_project("джарвис", ENV) == ENV.projects[0]
    assert resolve_project("джарвисе", ENV) == ENV.projects[0]  # «в проекте джарвисе»
    assert resolve_project("портфолио", ENV) == ENV.projects[1]
    assert resolve_project("проект портфолио сайт", ENV) == ENV.projects[1]
    assert resolve_project("кулинария", ENV) is None
    two = MacEnv(projects=[Project("jarvis-app", "/a"), Project("jarvis-api", "/b")])
    assert isinstance(resolve_project("jarvis", two), Ambiguous)


@pytest.mark.parametrize(
    ("said", "intent", "app", "project", "prompt"),
    [
        # Found live 2026-10-13: Apple's recognizer hears «Claude» as «cloud» and ends a phrase with a period.
        ("Открой Cloud.", "mac_open", "Claude", None, None),
        ("открой клоуд", "mac_open", "Claude", None, None),
        ("открой cloud code", "claude_code", None, None, None),
        ("Запусти Cloud Code в проекте Джарвис: почини тесты.", "claude_code", None, "Джарвис", "почини тесты"),
        ("Иду в магазин.", "shopping_trip", None, None, None),
    ],
)
def test_spoken_claude_and_trailing_period(
    said: str, intent: str, app: str | None, project: str | None, prompt: str | None
) -> None:
    from jarvis.bot.intents import keyword_route

    routed = keyword_route(said)
    assert (routed.intent.value, routed.app_text, routed.project_text, routed.prompt_text) == (
        intent,
        app,
        project,
        prompt,
    )


def test_cloud_opens_claude_but_icloud_stays_a_folder() -> None:
    claude = resolve_open("cloud", ENV)
    assert isinstance(claude, Target) and claude.payload == {"app": "Claude"}
    icloud = resolve_open("icloud", ENV)
    assert isinstance(icloud, Target) and icloud.kind == "open_path"


@pytest.mark.parametrize(
    ("heard", "canonical"),
    [
        ("Открой cloud code.", "Открой Claude Code."),
        ("открой клод кот", "открой Claude Code"),
        ("запусти Клауд-код в проекте", "запусти Claude Code в проекте"),
        ("скажи клоду: проверь тесты", "скажи Claude: проверь тесты"),
        ("открой icloud", "открой icloud"),  # not Claude
    ],
)
def test_spoken_claude_is_canonical(heard: str, canonical: str) -> None:
    from jarvis.bot.intents import canonical_claude

    assert canonical_claude(heard) == canonical
