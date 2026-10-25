"""Jarvis OS: a map of what Jarvis is made of — built by code from the codebase itself, never kept by hand.

Commands are the `Intent` enum and the bot's dispatch table, background jobs are the handler registry and the periodic
schedule, data is the SQLAlchemy metadata, and every link comes from the source read with `ast`: imports, the
container's wiring (which adapter each service gets), attribute use (`self.c.tracker`), `enqueue(job_type=…)` calls and
SQL table names. The few tables below only name and group packages for the page; `tests/unit/test_system_map.py`
fails when one of them names something that no longer exists or when a new package is left unplaced.

Structure only: no settings values except the schedule, no rows, no secrets. The map changes only on deploy, so it is
built once per process (`system_map`).
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Table

import jarvis

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

# Node types in the legend (order = legend order).
TYPES: dict[str, str] = {
    "agent": "Агенты",
    "command": "Команды",
    "job": "Фоновые задачи",
    "integration": "Интеграции",
    "data": "Данные",
    "channel": "Каналы",
    "core": "Ядро",
}

# What a link means (shown on the node card).
LINK_KINDS: dict[str, str] = {
    "routes": "распознаёт",
    "handled_by": "выполняет",
    "uses": "использует",
    "enqueues": "ставит в очередь",
    "schedules": "запускает по расписанию",
    "runs": "работает через",
    "stores": "хранит в",
    "refers": "ссылается на",
    "feeds": "передаёт данные",
}

# --- placement of packages (names for the page; everything else is read from the code) ----------------------------

# Agents: what the user would call one of Jarvis's skills. id → (label, module prefixes; the first one that names a
# module is the main one).
AGENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "agenda": ("Повестка и брифинги", ("jarvis.agenda",)),
    "planner": ("Планировщик дня", ("jarvis.planner",)),
    "tasks": ("Задачи", ("jarvis.tasks",)),
    "reminders": ("Напоминания и дорога", ("jarvis.reminders", "jarvis.bot.location")),
    "inbox": ("AI Inbox и заметки", ("jarvis.inbox",)),
    "ideas": ("Идеи", ("jarvis.ideas",)),
    "search": ("Поиск по смыслу", ("jarvis.search",)),
    "tracker": ("Дневник TLT / TPL", ("jarvis.tracker",)),
    "health": ("Часы и весы", ("jarvis.health",)),
    "meds": ("Таблетки и курсы", ("jarvis.meds",)),
    "labs": ("Анализы", ("jarvis.labs",)),
    "finance": ("Расходы", ("jarvis.finance",)),
    "calc": ("Калькулятор и валюты", ("jarvis.calc",)),
    "research": ("Исследователь", ("jarvis.research",)),
    "agent": ("Вопросы (агент)", ("jarvis.agent",)),
    "profile": ("Память о вас", ("jarvis.profile",)),
    "shopping": ("Покупки", ("jarvis.shopping",)),
    "weather": ("Погода", ("jarvis.weather",)),
    "photos": ("Фото", ("jarvis.photos",)),
    "calls": ("Звонки в Telegram", ("jarvis.calls",)),
    "voice": ("Телефонные звонки", ("jarvis.voice",)),
    "desktop": ("Jarvis для Mac", ("jarvis.desktop",)),
    "sites": ("Сайты LocalSites", ("jarvis.sites",)),
    "development": ("Разработка", ("jarvis.development",)),
    "dev_worker": ("Dev Worker", ("dev_worker",)),
    "sync": ("Синхронизация", ("jarvis.sync",)),
    "usage": ("Статистика и траты", ("jarvis.usage",)),
}

# The core: what every agent stands on.
CORE: dict[str, tuple[str, tuple[str, ...]]] = {
    "bot": ("Бот и роутер команд", ("jarvis.bot",)),
    "scheduler": ("Очередь и расписание", ("jarvis.scheduler",)),
    "notify": ("Уведомления", ("jarvis.notify",)),
    "approvals": ("Подтверждения", ("jarvis.approvals",)),
    "audit": ("Журнал действий", ("jarvis.audit",)),
    "budget": ("Дневные лимиты", ("jarvis.core.budget",)),
    "security": ("Подписи и шифрование", ("jarvis.core.security",)),
    "api": ("HTTP API", ("jarvis.api",)),
}

# External services. id → (label, module prefixes, key in `Container.status` or None).
INTEGRATIONS: dict[str, tuple[str, tuple[str, ...], str | None]] = {
    "todoist": ("Todoist", ("jarvis.integrations.todoist",), "todoist"),
    "google_calendar": (
        "Google Calendar (iCal)",
        ("jarvis.integrations.google_calendar.ics", "jarvis.integrations.google_calendar"),
        "google_calendar",
    ),
    "telegram_bot": ("Telegram Bot API", ("jarvis.integrations.telegram.client",), "telegram"),
    "telegram_caller": ("Telegram-звонарь", ("jarvis.integrations.telegram.caller",), None),
    "llm": ("OpenRouter · ИИ", ("jarvis.integrations.llm",), "llm"),
    "tts": ("OpenRouter · голос", ("jarvis.integrations.tts",), "tts"),
    "stt": ("OpenRouter · Whisper", ("jarvis.integrations.stt",), "stt"),
    "embeddings": ("OpenRouter · эмбеддинги", ("jarvis.integrations.embeddings",), "embeddings"),
    "lmf": ("LMF (TLT / TPL)", ("jarvis.integrations.lmf",), "lmf"),
    "localsites": ("LocalSites", ("jarvis.integrations.localsites",), "localsites"),
    "weather": ("Open-Meteo", ("jarvis.integrations.weather",), "weather"),
    "fx": ("Курсы валют", ("jarvis.integrations.fx",), "fx"),
    "maps": (
        "OpenRouteService · Nominatim",
        ("jarvis.integrations.maps.openrouteservice", "jarvis.integrations.maps"),
        "maps",
    ),
    "twogis": ("2ГИС", ("jarvis.integrations.maps.twogis",), None),
    "voice_providers": ("Vapi / Twilio", ("jarvis.integrations.voice",), "voice"),
}

# Services Jarvis talks to without an adapter module of its own: (label, module that describes it, links).
EXTERNAL: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "health_connect": ("Health Connect", "jarvis.health.parse", ("agent:health",)),
}

# Ways in: (label, module whose docstring describes it, API route it arrives by or None, links).
CHANNELS: dict[str, tuple[str, str, str | None, tuple[str, ...]]] = {
    "telegram": (
        "Чат в Telegram",
        "jarvis.bot.service",
        "/webhooks/telegram",
        ("integration:telegram_bot", "core:bot"),
    ),
    "mac": ("Приложение на Mac", "jarvis.desktop.channel", "/api/desktop/v1/message", ("core:api", "core:bot")),
    "calls": ("Звонки", "jarvis.calls.service", None, ("agent:calls", "agent:voice", "integration:telegram_caller")),
    "phone": ("Телефон · часы · весы", "jarvis.db.models.health", "/webhooks/health", ("integration:health_connect",)),
    "system": ("Jarvis OS", "jarvis.system.map", "/api/desktop/v1/system/graph", ("core:api",)),
}

# What each part does for the user, in Russian (16.10: the map showed the English docstrings and «ничего не понятно»).
# The docstring stays in `extra.code_doc`; a part missing here keeps its docstring (tests list the gaps).
RU: dict[str, str] = {
    "agent:agenda": "Повестка на любой день и период, утренний брифинг в 08:00 и вечерний обзор в 21:00.",
    "agent:planner": "Предлагает план дня: раскладывает задачи по свободному времени, применяет после «да».",
    "agent:tasks": "Задачи из чата: добавить, отметить, перенести словами, приоритет; удаление и массовые — с кнопкой.",
    "agent:reminders": "Напоминания о встречах и задачах с повтором до «Понял», время в пути и «пора выходить».",
    "agent:inbox": "Заметки по команде «Заметка: …» и кнопки «куда записать?», когда фраза непонятна.",
    "agent:ideas": "Идеи: запись, разбор ИИ, версии и спецификация для разработки.",
    "agent:search": "Поиск по словам и по смыслу в задачах, календаре, идеях, заметках и покупках.",
    "agent:tracker": "Дневник TLT / TPL: отчёт о дне словами → черновик → «✅ Записать» в LMF; сводки и напоминания.",
    "agent:health": "Часы и весы: шаги, пульс, сон, вес и состав тела с телефона; предлагает записать в дневник.",
    "agent:meds": "Таблетки и курсы: напоминания, наборы «утренние», «выпил …», продление курса.",
    "agent:labs": "Анализы крови из PDF лаборатории: «мои анализы», «какой ферритин» — без толкований.",
    "agent:finance": "Расходы: «потратил 45 лари на такси», чеки по фото, итоги по категориям и валютам.",
    "agent:calc": "Калькулятор и курсы валют: «10 000 руб. в долларах», «15% от 2000», числа словами.",
    "agent:research": "Исследователь: «изучи …» — поиск в интернете с источниками, после кнопки с ценой.",
    "agent:agent": "Отвечает на любые вопросы как ChatGPT; о вашей жизни — заглядывая в ваши данные, только чтение.",
    "agent:profile": "Память о вас: «запомни, что …», «что ты обо мне знаешь?», «забудь про …».",
    "agent:shopping": "Список покупок: «добавь молоко», «иду в магазин» с кнопками «куплено».",
    "agent:weather": "Погода на сегодня и завтра и предупреждения, когда она вот-вот изменится.",
    "agent:photos": "Фото: читает этикетку, блюдо или чек одним запросом к ИИ.",
    "agent:calls": "Звонки в Telegram: звонит, если напоминание пропущено; «позвони мне»; разговор голосом.",
    "agent:voice": "Телефонные звонки через Vapi / Twilio (не подключено, вместо них — звонки в Telegram).",
    "agent:desktop": "Mac: «открой …», громкость, буфер обмена, уведомления, Claude Code — закрытый набор действий.",
    "agent:sites": "Сайты: открыть, запустить, остановить, ссылка для дома или туннель вне дома.",
    "agent:development": "Разработка: задания для Claude Code только после подтверждения.",
    "agent:dev_worker": "Исполнитель разработки на Mac: забирает одобренные задания (не подключён).",
    "agent:sync": "Синхронизация: Todoist каждые 2 минуты, календарь каждые 5 — в локальную копию.",
    "agent:usage": "Статистика и траты на ИИ за любой период.",
    "core:bot": "Принимает каждое сообщение: сначала разбирает код, ИИ — только непонятные фразы; подтверждения.",
    "core:scheduler": "Надёжная очередь задач и расписание: ничего не теряется при перезапуске.",
    "core:notify": "Отправка сообщений без дублей; во время звонка — беззвучно.",
    "core:approvals": "Подтверждения: рискованное выполняется только после кнопки.",
    "core:audit": "Журнал всех внешних действий.",
    "core:budget": "Дневные лимиты платных вызовов (ИИ, голос, звонки, карты).",
    "core:security": "Подписи кнопок и вебхуков, шифрование токенов.",
    "core:api": "HTTP API для приложения на Mac, часов и Jarvis OS (только этот Mac и домашняя сеть).",
    "integration:todoist": "Ваши задачи (Todoist).",
    "integration:google_calendar": "Основной календарь Google — только чтение, по секретной iCal-ссылке.",
    "integration:telegram_bot": "Бот в Telegram: сообщения, голосовые, кнопки.",
    "integration:telegram_caller": "Второй аккаунт Telegram, который звонит вам и принимает ваши звонки.",
    "integration:llm": "ИИ через OpenRouter: понимание фраз, ответы, фото.",
    "integration:tts": "Голос Джарвиса (Gemini, голос Charon).",
    "integration:stt": "Распознавание голосовых (Whisper).",
    "integration:embeddings": "Поиск по смыслу (векторы bge-m3).",
    "integration:lmf": "Ваш трекер LMF: дневник TLT, TPL, еда, анализы.",
    "integration:localsites": "LocalSites: запуск ваших сайтов, ссылки и туннели.",
    "integration:weather": "Прогноз погоды Open-Meteo (бесплатно).",
    "integration:fx": "Курсы валют и криптовалют.",
    "integration:maps": "Время в пути по OpenStreetMap и поиск адресов.",
    "integration:twogis": "Кнопка маршрута в 2ГИС (там пробки и транспорт Тбилиси).",
    "integration:voice_providers": "Телефония Vapi / Twilio (не подключена).",
    "integration:health_connect": "Health Connect на телефоне: данные часов и весов.",
    "channel:telegram": "Чат с ботом: текст, голосовые, фото, геолокация.",
    "channel:mac": "Приложение в строке меню Mac: ⌥Space или «Джарвис», маленькое окно с голосом.",
    "channel:calls": "Голосовые звонки в Telegram — в обе стороны.",
    "channel:phone": "Телефон присылает данные часов и весов каждые 30 минут.",
    "channel:system": "Эта страница: карта Джарвиса, лаборатория и сайты.",
}

# Shared plumbing: never a node and never a link (formatting, clock, errors, the container, the DB layer…).
PLUMBING: tuple[str, ...] = (
    "jarvis",
    "jarvis.core",
    "jarvis.db",
    "jarvis.services",
    "jarvis.jobs",
    "jarvis.cli",
    "jarvis.doctor",
    "jarvis.system",
    "jarvis.integrations",
    "jarvis.bot.fmt",
    "jarvis.bot.speech",
    "jarvis.bot.persona",
)

# Live process roles (`integration_sync_state`, provider 'process') → the node they keep alive.
PROCESS_NODES: dict[str, str] = {
    "worker": "core:scheduler",
    "scheduler": "core:scheduler",
    "bot": "core:bot",
    "caller": "agent:calls",
}

MAX_EXAMPLES = 6
FOLLOW_MAX_CALLERS = 4  # a helper used by more places than this is generic (reply, _context…): not followed
FOLLOW_DEPTH = 3
CONTAINER_NAMES = frozenset({"c", "container", "_base"})
_ENQUEUE = frozenset({"enqueue", "enqueue_coalesced"})
_SQL_TABLE = re.compile(r"\b(?:from|join|into|update|truncate)\s+\"?([a-z_][a-z0-9_]*)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ«(`*\"'])")
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def first_sentence(doc: str | None, limit: int = 260) -> str:
    """The first sentence of a docstring's first paragraph, whitespace collapsed."""
    if not doc:
        return ""
    paragraph = doc.strip().split("\n\n")[0]
    text = " ".join(paragraph.split())
    if match := _SENTENCE_END.search(text):
        text = text[: match.start()]
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


# --- the source index ------------------------------------------------------------------------------------------------


@dataclass
class Source:
    name: str  # dotted module name
    rel: str  # path from the repository root, e.g. "jarvis/tracker/service.py"
    tree: ast.Module
    lines: int
    imports: dict[str, str] = field(default_factory=dict)  # local name → dotted target
    defs: dict[str, FunctionNode] = field(default_factory=dict)  # "func" / "Class.method"
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)

    @property
    def doc(self) -> str:
        return ast.get_docstring(self.tree) or ""

    @property
    def is_package(self) -> bool:
        return self.rel.endswith("__init__.py")


def _module_name(path: Path, base: Path) -> str:
    parts = list(path.relative_to(base).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_table(nodes: Iterable[ast.AST], package: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    table[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, base] if base else parts)
            for alias in node.names:
                if alias.name != "*":
                    table[alias.asname or alias.name] = f"{base}.{alias.name}"
    return table


def _top_level_imports(tree: ast.Module) -> list[ast.AST]:
    """Imports at module level, including those under `if TYPE_CHECKING:` / try blocks (not inside functions)."""
    found: list[ast.AST] = []

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                found.append(node)
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for handler in node.handlers:
                    visit(handler.body)

    visit(tree.body)
    return found


def package_roots() -> list[Path]:
    """`jarvis` and, when installed next to it, `dev_worker` (both are in the image and in the repository)."""
    roots = [Path(jarvis.__file__).resolve().parent]
    spec = importlib.util.find_spec("dev_worker")
    if spec is not None and spec.origin:
        roots.append(Path(spec.origin).resolve().parent)
    return roots


class Index:
    """Every module of the given packages, parsed once."""

    def __init__(self, roots: Iterable[Path]) -> None:
        self.modules: dict[str, Source] = {}
        for root in roots:
            base = root.parent
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
                name = _module_name(path, base)
                src = Source(name=name, rel=path.relative_to(base).as_posix(), tree=tree, lines=text.count("\n") + 1)
                package = name if src.is_package else name.rpartition(".")[0]
                src.imports = _import_table(_top_level_imports(tree), package)
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                        src.defs[node.name] = node
                    elif isinstance(node, ast.ClassDef):
                        src.classes[node.name] = node
                        for item in node.body:
                            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                                src.defs[f"{node.name}.{item.name}"] = item
                self.modules[name] = src

    def module_of(self, target: str | None) -> str | None:
        """The longest module prefix of a dotted target («jarvis.db.models.Idea» → «jarvis.db.models»)."""
        if not target:
            return None
        parts = target.split(".")
        for n in range(len(parts), 0, -1):
            candidate = ".".join(parts[:n])
            if candidate in self.modules:
                return candidate
        return None


# --- placement -------------------------------------------------------------------------------------------------------


class Placement:
    """Dotted module → node id (agent / core / integration) by the longest matching prefix; plumbing → None."""

    def __init__(self) -> None:
        self.prefixes: dict[str, str | None] = dict.fromkeys(PLUMBING)
        for key, (_label, prefixes) in AGENTS.items():
            self.prefixes.update(dict.fromkeys(prefixes, f"agent:{key}"))
        for key, (_label, prefixes) in CORE.items():
            self.prefixes.update(dict.fromkeys(prefixes, f"core:{key}"))
        for key, (_label, prefixes, _status) in INTEGRATIONS.items():
            self.prefixes.update(dict.fromkeys(prefixes, f"integration:{key}"))

    def prefix_of(self, dotted: str) -> str | None:
        """The placement entry a module falls under (the longest matching prefix)."""
        parts = dotted.split(".")
        for n in range(len(parts), 0, -1):
            prefix = ".".join(parts[:n])
            if prefix in self.prefixes:
                return prefix
        return None

    def node_of(self, dotted: str | None) -> str | None:
        prefix = self.prefix_of(dotted) if dotted else None
        return self.prefixes[prefix] if prefix is not None else None


# --- what a piece of code touches ------------------------------------------------------------------------------------


@dataclass
class Touches:
    nodes: set[str] = field(default_factory=set)  # agent / core / integration ids
    tables: set[str] = field(default_factory=set)
    jobs: set[str] = field(default_factory=set)  # job types it enqueues
    calls: list[tuple[str, str]] = field(default_factory=list)  # (container attr, method), in source order

    def update(self, other: Touches) -> None:
        self.nodes |= other.nodes
        self.tables |= other.tables
        self.jobs |= other.jobs
        self.calls += [call for call in other.calls if call not in self.calls]


def _body_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """Nodes of a function body or a lambda — not its signature annotations (`ctx: JobContext` is not a use)."""
    if isinstance(fn, ast.Lambda):
        yield from ast.walk(fn.body)
    elif isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
        for stmt in fn.body:
            yield from ast.walk(stmt)


def _attr_base(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class Analyzer:
    def __init__(self, index: Index, placement: Placement, tables: dict[str, str], job_types: set[str]) -> None:
        self.index = index
        self.placement = placement
        self.model_tables = tables  # model class name → table name
        self.table_names = set(tables.values())
        self.job_types = job_types
        self.container_attrs: dict[str, set[str]] = {}  # container attr → nodes behind it (see `wiring`)
        self.attr_classes: dict[str, list[tuple[str, str]]] = {}  # container attr → (module, class) behind it
        self.injected: set[tuple[str, str]] = set()  # (service node, adapter/service node) by constructor injection
        self.api_routes: list[str] = []  # Jarvis's own HTTP routes (a module that names one talks to the API)
        self._callers: dict[str, Counter[str]] = {}
        self._self_attrs: dict[tuple[str, str], dict[str, str]] = {}

    # -- names → nodes / tables

    def touches_of_target(self, target: str, into: Touches) -> None:
        module = self.index.module_of(target)
        if module is None:
            return
        if module == "jarvis.db.models" or module.startswith("jarvis.db.models."):
            attr = target[len(module) + 1 :].split(".")[0]
            if attr in self.model_tables:
                into.tables.add(self.model_tables[attr])
            return
        if (node := self.placement.node_of(module)) is not None:
            into.nodes.add(node)

    def names_api_route(self, value: str) -> bool:
        """A string such as «/api/dev-worker/v1/jobs/» (a client of Jarvis's own API, e.g. the Dev Worker)."""
        if len(value) < 8 or not value.startswith("/"):
            return False
        return any(route.startswith(value) or value.startswith(route) for route in self.api_routes)

    def self_attrs(self, src: Source, cls: str | None) -> dict[str, str]:
        """`self.travel_desk = TravelDesk(container)` in a class's __init__ → {"travel_desk": node of TravelDesk}."""
        if cls is None:
            return {}
        key = (src.name, cls)
        if key not in self._self_attrs:
            found: dict[str, str] = {}
            init = src.defs.get(f"{cls}.__init__")
            for node in _body_nodes(init) if init is not None else ():
                if not (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                ):
                    continue
                owner = self.placement.node_of(self.index.module_of(src.imports.get(node.value.func.id)))
                for tgt in node.targets:
                    is_self = isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                    if owner is not None and is_self and tgt.value.id == "self":  # type: ignore[attr-defined]
                        found[tgt.attr] = owner  # type: ignore[attr-defined]
            self._self_attrs[key] = found
        return self._self_attrs[key]

    # -- functions

    def touches(self, src: Source, fn: ast.AST, cls: str | None = None) -> Touches:
        """What one function (or lambda) uses directly: imported names, container attributes, `self.<service>`,
        enqueued job types, SQL table names, Jarvis's own API routes."""
        out = Touches()
        package = src.name if src.is_package else src.name.rpartition(".")[0]
        local = _import_table((n for n in _body_nodes(fn) if isinstance(n, ast.Import | ast.ImportFrom)), package)
        imports = {**src.imports, **local}
        own = self.self_attrs(src, cls)
        enqueues = False
        constants: list[str] = []
        for node in _body_nodes(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if (target := imports.get(node.id)) is not None:
                    self.touches_of_target(target, out)
            elif isinstance(node, ast.Attribute):
                base = _attr_base(node.value)
                if base in CONTAINER_NAMES and node.attr in self.container_attrs:
                    out.nodes |= self.container_attrs[node.attr]
                elif isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr in own:
                    out.nodes.add(own[node.attr])
            elif isinstance(node, ast.Call):
                enqueues = enqueues or _call_name(node) in _ENQUEUE
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Attribute)
                    and _attr_base(func.value.value) in CONTAINER_NAMES
                    and func.value.attr in self.container_attrs
                    and (func.value.attr, func.attr) not in out.calls
                ):
                    out.calls.append((func.value.attr, func.attr))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                constants.append(node.value)
                self._strings(node.value, out)
        if enqueues:
            out.jobs |= {value for value in constants if value in self.job_types}
        return out

    def _strings(self, value: str, out: Touches) -> None:
        for match in _SQL_TABLE.finditer(value):
            if match.group(1).lower() in self.table_names:
                out.tables.add(match.group(1).lower())
        if self.names_api_route(value):
            out.nodes.add("core:api")

    @staticmethod
    def callees(src: Source, fn: ast.AST, cls: str | None) -> list[str]:
        """Functions of the same module this one refers to (`helper(…)`, `self.method`), in source order."""
        found: dict[str, None] = {}
        for node in _body_nodes(fn):
            if isinstance(node, ast.Name) and node.id in src.defs:
                found[node.id] = None
            elif (
                cls is not None
                and isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("self", "cls")
                and f"{cls}.{node.attr}" in src.defs
            ):
                found[f"{cls}.{node.attr}"] = None
        return list(found)

    def callers(self, src: Source) -> Counter[str]:
        """How many other functions of the module refer to each function (a widely used helper is generic)."""
        if src.name not in self._callers:
            counts: Counter[str] = Counter()
            for qual, fn in src.defs.items():
                cls = qual.split(".")[0] if "." in qual else None
                counts.update(set(self.callees(src, fn, cls)) - {qual})
            self._callers[src.name] = counts
        return self._callers[src.name]

    def reach(self, src: Source, root: ast.AST, cls: str | None) -> tuple[Touches, list[str]]:
        """What a function uses, following the helpers it calls a few levels down (all of its direct callees, then
        only helpers that few functions use — a generic one such as `reply` would link everything to everything).
        Returns the touches and the helpers visited, in call order."""
        counts = self.callers(src)
        out = self.touches(src, root, cls)
        visited: list[str] = []
        frontier = [(name, 0) for name in self.callees(src, root, cls)]
        while frontier:
            name, depth = frontier.pop(0)
            if name in visited or (depth > 0 and counts[name] > FOLLOW_MAX_CALLERS):
                continue
            visited.append(name)
            fn = src.defs[name]
            out.update(self.touches(src, fn, cls))
            if depth + 1 < FOLLOW_DEPTH:
                frontier += [(callee, depth + 1) for callee in self.callees(src, fn, cls) if callee not in visited]
        return out, visited

    def module_touches(self, src: Source) -> Touches:
        """Everything a module uses: its imports, what its functions touch (function-level imports included) and
        the strings it holds at module level (SQL, routes)."""
        out = Touches()
        for target in src.imports.values():
            self.touches_of_target(target, out)
        for qual, fn in src.defs.items():
            out.update(self.touches(src, fn, qual.split(".")[0] if "." in qual else None))
        for node in ast.walk(src.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self._strings(node.value, out)
        return out

    # -- the container's wiring

    def wiring(self) -> None:
        """Container attribute → nodes behind it (and their classes), plus links made by constructor injection:
        `self.tracker = TrackerService(self.settings, self.lmf_api, self.llm)` → tracker uses LMF and the LLM."""
        src = self.index.modules["jarvis.services.container"]
        attr_nodes: dict[str, set[str]] = defaultdict(set)
        attr_classes: dict[str, list[tuple[str, str]]] = defaultdict(list)
        build = src.defs["build_container"]
        assigns: dict[str, list[ast.expr]] = defaultdict(list)
        for node in ast.walk(build):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        assigns[tgt.id].append(node.value)
            elif (
                isinstance(node, ast.AnnAssign | ast.NamedExpr)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                assigns[node.target.id].append(node.value)

        def classes_of(expr: ast.expr, depth: int = 0) -> list[tuple[str, str]]:
            """The real classes an expression of build_container can hold (fakes for tests left out)."""
            if depth > 5:
                return []
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                name = expr.func.id
                module = self.index.module_of(src.imports.get(name))
                return [(module, name)] if module is not None and not name.startswith("Fake") else []
            if isinstance(expr, ast.Name):
                return [found for value in assigns.get(expr.id, []) for found in classes_of(value, depth + 1)]
            if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
                return classes_of(expr.value, depth + 1)
            if isinstance(expr, ast.IfExp):
                return classes_of(expr.body, depth + 1) + classes_of(expr.orelse, depth + 1)
            return []

        for node in ast.walk(build):
            if isinstance(node, ast.Call) and _call_name(node) == "Container":
                for kw in node.keywords:
                    for found in classes_of(kw.value) if kw.arg else []:
                        if found not in attr_classes[str(kw.arg)]:
                            attr_classes[str(kw.arg)].append(found)
        for attr, classes in attr_classes.items():
            for module, _cls in classes:
                if (owner := self.placement.node_of(module)) is not None:
                    attr_nodes[attr].add(owner)

        injected: set[tuple[str, str]] = set()
        for stmt in src.defs["Container.__post_init__"].body:
            if not isinstance(stmt, ast.Assign):
                continue
            value = stmt.value.body if isinstance(stmt.value, ast.IfExp) else stmt.value
            target = stmt.targets[0]
            if not (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and isinstance(target, ast.Attribute)
            ):
                continue
            defined_in = self.index.module_of(src.imports.get(value.func.id))
            if defined_in is None:
                continue
            attr_classes[target.attr].append((defined_in, value.func.id))
            owner = self.placement.node_of(defined_in)
            if owner is None:
                continue
            attr_nodes[target.attr].add(owner)
            for arg in [*value.args, *(kw.value for kw in value.keywords)]:
                for sub in ast.walk(arg):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"
                        and sub.attr in attr_nodes
                    ):
                        injected |= {(owner, dep) for dep in attr_nodes[sub.attr] if dep != owner}
        self.container_attrs = dict(attr_nodes)
        self.attr_classes = dict(attr_classes)
        self.injected = injected

    def wired_tables(self) -> set[tuple[str, str]]:
        """Tables reached through a helper class of the container itself (`DbTokenStore` keeps the Google token in
        `integration_accounts`) belong to whatever the helper is handed to (`HttpCalendarApi(…, token_store)`)."""
        src = self.index.modules["jarvis.services.container"]
        local_tables: dict[str, set[str]] = {}
        for name, cls in src.classes.items():
            touched = Touches()
            for item in cls.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    touched.update(self.touches(src, item, name))
            if touched.tables:
                local_tables[name] = touched.tables
        build = src.defs["build_container"]
        holders: dict[str, str] = {}  # variable → the local class it holds
        for node in ast.walk(build):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                called = _call_name(node.value)
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and called in local_tables:
                        holders[tgt.id] = str(called)
        found: set[tuple[str, str]] = set()
        for node in ast.walk(build):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            owner = self.placement.node_of(self.index.module_of(src.imports.get(node.func.id)))
            if owner is None:
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name) and arg.id in holders:
                    found |= {(owner, table) for table in local_tables[holders[arg.id]]}
        return found


# --- the graph -------------------------------------------------------------------------------------------------------


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # links to nodes that do not exist (a bug)

    def add_node(
        self, node_id: str, type_: str, label: str, description: str, path: str, **extra: Any
    ) -> dict[str, Any]:
        node = {"id": node_id, "type": type_, "label": label, "description": description, "path": path, "extra": extra}
        self.nodes[node_id] = node
        return node

    def link(self, source: str, target: str, kind: str) -> None:
        if source != target:
            self.links.setdefault((source, target), set()).add(kind)

    def prune(self) -> None:
        """A link to a missing node would break the page; keep it out and remember it (tests assert none)."""
        self.dropped = [pair for pair in self.links if pair[0] not in self.nodes or pair[1] not in self.nodes]
        for pair in self.dropped:
            del self.links[pair]


def _schedule_text(every: timedelta | None, daily_at: time | None) -> str:
    if daily_at is not None:
        return f"ежедневно в {daily_at.strftime('%H:%M')}"
    if every is None:
        return ""
    minutes = int(every.total_seconds() // 60)
    if minutes % 60 == 0:
        hours = minutes // 60
        return "каждый час" if hours == 1 else f"каждые {hours} ч"
    return f"каждые {minutes} мин"


def _intent_router_notes(router_text: str, names: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """From the LLM router prompt: a short English definition per intent («show_focus: what needs attention now»,
    «capture_idea (a thought or concept to explore)») and the quoted phrases next to each intent name (the nearest
    defining mention before them on the same line, else the next one)."""
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b")
    definition = re.compile(r"^\s*(?::|=)\s*([^(;'\n]+)|^\s*\(\s*([^'«(;)\n][^;)\n]*)")
    notes: dict[str, str] = {}
    phrases: dict[str, list[str]] = defaultdict(list)
    for line in router_text.split("\n"):
        mentions = []
        for m in pattern.finditer(line):
            # «weather» or «plan» inside an English sentence is not the intent: a mention defines one only when it
            # is a snake_case name or is followed by «:», «=», «(» or «,»
            if "_" in m.group(1) or re.match(r"\s*[:=(,]", line[m.end() :]):
                mentions.append((m.start(), m.end(), m.group(1)))
        for _start, end, name in mentions:
            if name in notes or not (found := definition.match(line[end:])):
                continue
            text = found.group(1) or found.group(2) or ""
            if nxt := pattern.search(text):
                text = text[: nxt.start()]
            text = re.split(r"(?<=[A-Za-z)])\.\s", text)[0].strip(" .,:—-")
            if len(text) >= 6:
                notes[name] = text
        for quote in re.finditer(r"'([^']{3,90})'", line):
            before = line[max(0, quote.start() - 14) : quote.start()]
            if re.search(r"[a-z]+_[a-z_]+\s*=?\s*$", before):
                continue  # a field value (task_text 'постригся'), not something the user says
            owner = [name for start, _end, name in mentions if start < quote.start()]
            after = [name for start, _end, name in mentions if start > quote.start()]
            if owner or after:
                phrases[owner[-1] if owner else after[0]].append(quote.group(1))
    return notes, phrases


def _quoted(text: str) -> list[str]:
    return [" ".join(m.group(1).split()) for m in re.finditer(r"«([^«»]{3,90})»", text)]


def _probe_phrase(phrase: str) -> str:
    """«Удали задачу …» → «Удали задачу текст»: the placeholder must not stop the keyword router."""
    return re.sub(r"\s*…\s*$", " текст", phrase).replace("…", "текст").strip()


def _sayable(phrase: str, min_words: int = 2) -> bool:
    """Something a person would say as a whole message: not one word, not a button («✅ Записать»), not a bare date
    («на пятницу в 18»), not a template («давай лучше…») or two requests in one («… перенесём. Добавим …»)."""
    from jarvis.bot.intents import is_due_phrase

    if not phrase[:1].isalnum() or not _CYRILLIC.search(phrase) or "…" in phrase or ". " in phrase:
        return False
    if len(phrase.split()) < min_words and not phrase.endswith("?"):
        return False
    return not is_due_phrase(phrase.lower())


@dataclass
class _Phrases:
    """What to say for each intent: (phrase, understood without AI)."""

    items: dict[str, list[tuple[str, bool]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, intent: str, phrase: str, no_ai: bool) -> None:
        phrase = phrase.strip()
        if phrase and phrase.lower() not in {p.lower() for p, _ in self.items[intent]}:
            self.items[intent].append((phrase, no_ai))

    def offer(self, phrase: str, owner: str | None, min_words: int = 2) -> None:
        """What the keyword router (no AI) maps a phrase to wins; otherwise it belongs to `owner` (via the LLM)."""
        from jarvis.bot.intents import Intent, keyword_route

        routed = keyword_route(_probe_phrase(phrase)).intent
        if routed != Intent.UNKNOWN:
            self.add(routed.value, phrase, True)
        elif owner is not None and _sayable(phrase, min_words):
            self.add(owner, phrase, False)

    def examples(self, intent: str) -> list[dict[str, Any]]:
        found = self.items.get(intent, [])
        no_ai = [{"text": p, "no_ai": True} for p, ok in found if ok][:MAX_EXAMPLES]
        llm = [{"text": p, "no_ai": False} for p, ok in found if not ok]
        return no_ai + llm[: MAX_EXAMPLES - len(no_ai)]


def _dispatch_table(src: Source) -> dict[str, ast.Lambda]:
    """`BotService._dispatch`: {Intent.X: lambda: self.cmd_x(…)} → {"x": lambda}."""
    from jarvis.bot.intents import Intent

    handlers: dict[str, ast.Lambda] = {}
    for node in ast.walk(src.defs["BotService._dispatch"]):
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                if isinstance(key_node, ast.Attribute) and isinstance(value_node, ast.Lambda):
                    handlers[Intent[key_node.attr].value] = value_node
    return handlers


def _slash_commands(src: Source, by_handler: dict[str, list[str]]) -> dict[str, list[str]]:
    """`handle_command`: the mapping {"/today": RoutedIntent(intent=Intent.SHOW_TODAY)} and the special cases
    `if command == "/voice": await self.cmd_voice(…)` (→ the intents that method handles)."""
    from jarvis.bot.intents import Intent

    slash: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(src.defs["BotService.handle_command"]):
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                if not (isinstance(key_node, ast.Constant) and str(key_node.value).startswith("/")):
                    continue
                for kw in value_node.keywords if isinstance(value_node, ast.Call) else []:
                    if kw.arg == "intent" and isinstance(kw.value, ast.Attribute):
                        slash[Intent[kw.value.attr].value].append(str(key_node.value))
        elif (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and str(node.test.comparators[0].value).startswith("/")
        ):
            command = str(node.test.comparators[0].value)
            for sub in (n for stmt in node.body for n in ast.walk(stmt)):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                    for intent in by_handler.get(f"BotService.{sub.attr}", []):
                        if command not in slash[intent]:
                            slash[intent].append(command)
    return slash


def _commands(graph: Graph, index: Index, analyzer: Analyzer) -> dict[str, Touches]:
    """One node per `Intent`: what to say (checked against the keyword router), buttons, slash commands, the
    handler and what it reaches. Returns what each intent's handler touches."""
    from jarvis.bot import menu
    from jarvis.bot.intents import ROUTER_SYSTEM, Intent
    from jarvis.bot.service import ACK_INTENTS, HELP_SECTIONS, INBOX_INTENTS, QUERY_INTENTS, VOICE_INTENTS

    src = index.modules["jarvis.bot.service"]
    notes, router_phrases = _intent_router_notes(ROUTER_SYSTEM, [i.value for i in Intent])
    handlers = _dispatch_table(src)
    reach = {intent: analyzer.reach(src, lam, "BotService") for intent, lam in handlers.items()}

    def handler_of(intent: str) -> str | None:
        visited = reach[intent][1] if intent in reach else []
        return next((v for v in visited if v.startswith("BotService.cmd_")), visited[0] if visited else None)

    by_handler: dict[str, list[str]] = defaultdict(list)
    for intent in handlers:
        if (method := handler_of(intent)) is not None:
            by_handler[method].append(intent)

    # «what to say»: /help, the router prompt, the handlers' own docstrings
    phrases = _Phrases()
    for _emoji, _section, lines in HELP_SECTIONS:
        for phrase in (p for line in lines for p in _quoted(line)):
            phrases.offer(phrase, None)
    for owner, quoted in router_phrases.items():
        for phrase in quoted:
            if _CYRILLIC.search(phrase):
                phrases.offer(phrase, owner)
    for method, intents in by_handler.items():
        for phrase in _quoted(ast.get_docstring(src.defs[method]) or ""):
            phrases.offer(phrase, intents[0] if len(intents) == 1 else None, min_words=3)

    buttons: dict[str, list[str]] = defaultdict(list)
    for label, routed in menu._ACTIONS.items():
        buttons[routed.intent.value].append(label)
    for _key, label, more in menu.MORE_ITEMS:
        if more is not None and label not in buttons[more.intent.value]:
            buttons[more.intent.value].append(label)
    slash = _slash_commands(src, by_handler)

    dispatch = src.defs["BotService._dispatch"]
    reached: dict[str, Touches] = {}
    for member in Intent:
        intent = member.value
        lam = handlers.get(intent)
        touched = reach[intent][0] if intent in reach else Touches()
        method = handler_of(intent) or "BotService._dispatch"  # no handler: the LLM's reply or «не понял»
        fn = src.defs.get(method) if method.startswith("BotService.cmd_") else None
        line = fn.lineno if fn is not None else (lam or dispatch).lineno
        description = (
            (first_sentence(ast.get_docstring(fn)) if fn is not None else "")
            or notes.get(intent, "")
            or _agent_doc(graph, touched, analyzer.container_attrs)
            or f"Обработчик {method.split('.')[-1]}()."
        )
        examples = phrases.examples(intent)
        commands = sorted(slash.get(intent, []), key=len)
        said = [e["text"] for e in examples if e["no_ai"]] + buttons.get(intent, []) + commands
        said += [e["text"] for e in examples if not e["no_ai"]]
        label = said[0][:1].upper() + said[0][1:] if said else intent  # nothing to say it with: its own name
        flags = [
            text
            for present, text in (
                (member in ACK_INTENTS, "делает сразу"),
                (member in QUERY_INTENTS, "показывает"),
                (member in VOICE_INTENTS, "отвечает голосом"),
                (intent in INBOX_INTENTS, "AI Inbox"),
                ("core:approvals" in touched.nodes, "с подтверждением"),
                (lam is None, "ответ ИИ"),
            )
            if present
        ]
        graph.add_node(
            f"command:{intent}",
            "command",
            label,
            description,
            f"{src.rel}:{line}",
            intent=intent,
            handler=method.split(".")[-1],
            examples=examples,
            slash=commands,
            buttons=buttons.get(intent, []),
            flags=flags,
        )
        reached[intent] = touched
    return reached


def _agent_doc(graph: Graph, touched: Touches, attr_nodes: dict[str, set[str]]) -> str:
    """A command without a description of its own: the description of the agent it calls first (or of its only
    agent)."""
    for attr, _method in touched.calls:
        for node in sorted(attr_nodes.get(attr, ())):
            if node.startswith("agent:") and node in graph.nodes:
                return str(graph.nodes[node]["description"])
    agents = sorted(n for n in touched.nodes if n.startswith("agent:") and n in graph.nodes)
    return str(graph.nodes[agents[0]]["description"]) if len(agents) == 1 else ""


def _called_doc(index: Index, analyzer: Analyzer, touched: Touches, job_type: str) -> str:
    """For a job without a docstring: the docstring of the first agent method it calls through the container
    (`c.todoist_sync.reconcile(…)`), else that agent's module docstring. An agent named like the job
    («reminder.fire» → reminders) goes first."""
    prefix = job_type.split(".")[0]

    def named_like(attr: str) -> bool:
        keys = [n.split(":")[1] for n in analyzer.container_attrs.get(attr, ())]
        return any(key.startswith(prefix) or prefix.startswith(key) for key in keys)

    backup = ""
    for attr, method in sorted(touched.calls, key=lambda call: not named_like(call[0])):
        if not any(n.startswith(("agent:", "integration:")) for n in analyzer.container_attrs.get(attr, ())):
            continue  # the notifier, the approvals…: not what the job is about
        for module, cls in analyzer.attr_classes.get(attr, []):
            src = index.modules.get(module)
            if src is None:
                continue
            fn = src.defs.get(f"{cls}.{method}")
            if fn is not None and (doc := first_sentence(ast.get_docstring(fn))):
                return doc
            backup = backup or first_sentence(src.doc)
    return backup


def _api_routes(src: Source) -> list[str]:
    """Route paths of the API (`@app.get("/api/…")`), up to their first path parameter."""
    routes: list[str] = []
    for node in ast.walk(src.tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and deco.args
                    and isinstance(deco.args[0], ast.Constant)
                    and str(deco.args[0].value).startswith("/api/")
                ):
                    routes.append(str(deco.args[0].value).split("{")[0])
    return routes


def _components(graph: Graph, index: Index, placement: Placement) -> dict[str, list[Source]]:
    """Agent, core and integration nodes (one per placement entry with code); returns each node's modules."""

    def described(prefixes: tuple[str, ...]) -> tuple[str, str, list[Source]]:
        """(description, path of the main module, own modules). The main module: the first prefix when it names a
        module, else `service.py`, `main.py`, the largest one; the description prefers the package docstring."""
        modules = sorted(
            (
                s
                for s in index.modules.values()
                if any(s.name == p or s.name.startswith(p + ".") for p in prefixes) and placement.node_of(s.name)
            ),
            key=lambda s: s.name,
        )
        code = [s for s in modules if not s.is_package]
        if not code:
            return "", "", []
        first = index.modules.get(prefixes[0])
        main = (
            (first if first is not None and not first.is_package else None)
            or next((s for s in code if s.name.endswith(".service")), None)
            or next((s for s in code if s.name.endswith(".main")), None)
            or max(code, key=lambda s: s.lines)
        )
        package = index.modules.get(main.name.rpartition(".")[0])
        own_package = package is not None and any(package.name.startswith(p) for p in prefixes)
        doc = (
            (first_sentence(package.doc) if package is not None and own_package and first is not main else "")
            or first_sentence(main.doc)
            or next((first_sentence(s.doc) for s in modules if s.doc), "")
        )
        return doc, f"{main.rel}:1", modules

    entries: list[tuple[str, str, str, tuple[str, ...], dict[str, Any]]] = [
        *((f"agent:{key}", "agent", label, prefixes, {}) for key, (label, prefixes) in AGENTS.items()),
        *((f"core:{key}", "core", label, prefixes, {}) for key, (label, prefixes) in CORE.items()),
        *(
            (f"integration:{key}", "integration", label, prefixes, {"status_key": status})
            for key, (label, prefixes, status) in INTEGRATIONS.items()
        ),
    ]
    modules_of: dict[str, list[Source]] = {}
    for node_id, type_, label, prefixes, extra in entries:
        doc, path, modules = described(prefixes)
        own = [m for m in modules if placement.node_of(m.name) == node_id]
        if not own:
            continue
        modules_of[node_id] = own
        graph.add_node(
            node_id,
            type_,
            label,
            doc,
            path,
            modules=[{"path": m.rel, "lines": m.lines} for m in own],
            lines=sum(m.lines for m in own),
            **extra,
        )
    for key, (label, module_name, links) in EXTERNAL.items():
        described_by = index.modules[module_name]
        graph.add_node(
            f"integration:{key}", "integration", label, first_sentence(described_by.doc), f"{described_by.rel}:1"
        )
        for target in links:
            graph.link(f"integration:{key}", target, "feeds")
    return modules_of


def _tables(graph: Graph, index: Index) -> None:
    """Every table of the SQLAlchemy metadata (and its foreign keys)."""
    from jarvis.db.base import Base

    for mapper in sorted(Base.registry.mappers, key=lambda m: str(m.local_table.name)):  # type: ignore[attr-defined]
        model = mapper.class_
        table = cast(Table, mapper.local_table)
        defined_in = index.modules.get(model.__module__)
        try:
            line = inspect.getsourcelines(model)[1]
        except (OSError, TypeError):  # pragma: no cover - models always have source
            line = 1
        columns = [str(c.name) for c in table.columns]
        listed = ", ".join(columns[:8]) + ("…" if len(columns) > 8 else "")
        graph.add_node(
            f"data:{table.name}",
            "data",
            str(table.name),
            first_sentence(model.__doc__) or f"Колонки: {listed}",
            f"{defined_in.rel if defined_in else model.__module__}:{line}",
            model=model.__name__,
            columns=len(columns),
            column_names=columns[:40],
        )
        for fk in table.foreign_keys:
            graph.link(f"data:{table.name}", f"data:{fk.column.table.name}", "refers")


def _component_links(graph: Graph, analyzer: Analyzer, modules_of: dict[str, list[Source]]) -> None:
    for node_id, modules in modules_of.items():
        merged = Touches()
        for src in modules:
            merged.update(analyzer.module_touches(src))
        for target in merged.nodes:
            if node_id == "core:bot" and target.startswith("agent:"):
                continue  # the bot reaches agents through its commands (shown as command → agent)
            if node_id.startswith("integration:") and not target.startswith("core:"):
                continue  # adapters do not depend on agents
            graph.link(node_id, target, "uses")
        for table in merged.tables:
            graph.link(node_id, f"data:{table}", "stores")
        for job in merged.jobs:
            graph.link(node_id, f"job:{job}", "enqueues")
    for owner, dep in analyzer.injected:
        graph.link(owner, dep, "uses")
    for owner, table in analyzer.wired_tables():
        graph.link(owner, f"data:{table}", "stores")


def _jobs(graph: Graph, index: Index, analyzer: Analyzer, handlers: dict[str, Any]) -> dict[str, str]:
    """A node per registered job type; links from what its handler touches. Returns {job type: periodic task name}
    for everything `periodic_tasks` can schedule (whether or not the settings switch it on)."""
    from jarvis.bot.service import JOB_NAMES_RU

    src = index.modules["jarvis.jobs.handlers"]
    periodic: dict[str, str] = {}
    for node in ast.walk(src.defs["periodic_tasks"]):
        if isinstance(node, ast.Call) and _call_name(node) == "PeriodicTask" and len(node.args) >= 2:
            name_arg, job_arg = node.args[0], node.args[1]
            if isinstance(name_arg, ast.Constant) and isinstance(job_arg, ast.Constant):
                periodic[str(job_arg.value)] = str(name_arg.value)
    for job_type, handler in sorted(handlers.items()):
        fn = src.defs.get(handler.__name__)
        if fn is None:  # pragma: no cover - every handler lives in jarvis/jobs/handlers.py
            continue
        touched, _visited = analyzer.reach(src, fn, None)
        description = first_sentence(ast.get_docstring(fn)) or _called_doc(index, analyzer, touched, job_type)
        if not description:
            russian = JOB_NAMES_RU.get(job_type)
            description = f"{russian[:1].upper()}{russian[1:]}." if russian else f"Обработчик {handler.__name__}()."
        node_id = f"job:{job_type}"
        graph.add_node(
            node_id,
            "job",
            JOB_NAMES_RU.get(job_type, job_type),
            description,
            f"{src.rel}:{fn.lineno}",
            job_type=job_type,
            handler=handler.__name__,
            periodic=job_type in periodic,
        )
        for target in touched.nodes - {"core:scheduler"}:  # the queue runs every job: not a link of its own
            graph.link(node_id, target, "runs" if target.startswith("agent:") else "uses")
        for table in touched.tables:
            graph.link(node_id, f"data:{table}", "stores")
        for job in touched.jobs:
            graph.link(node_id, f"job:{job}", "enqueues")
    for job_type in periodic:
        graph.link("core:scheduler", f"job:{job_type}", "schedules")
    return periodic


@dataclass
class _Built:
    graph: Graph
    periodic: dict[str, str]  # job type → its periodic task (everything the code can schedule)


def build_graph() -> _Built:
    """Read the code and build the graph (≈ 1 s; `system_map` caches it)."""
    import jarvis.db.models  # noqa: F401  (registers every model on the metadata)
    from jarvis.db.base import Base
    from jarvis.jobs.handlers import build_registry

    index = Index(package_roots())
    placement = Placement()
    model_tables = {m.class_.__name__: str(m.local_table.name) for m in Base.registry.mappers}  # type: ignore[attr-defined]
    handlers = build_registry().handlers
    analyzer = Analyzer(index, placement, model_tables, set(handlers))
    analyzer.wiring()
    analyzer.api_routes = _api_routes(index.modules["jarvis.api.app"])

    graph = Graph()
    modules_of = _components(graph, index, placement)
    _tables(graph, index)
    _component_links(graph, analyzer, modules_of)
    periodic = _jobs(graph, index, analyzer, dict(handlers))
    for intent, touched in _commands(graph, index, analyzer).items():
        command = f"command:{intent}"
        graph.link("core:bot", command, "routes")
        for target in touched.nodes - {"core:bot", "core:scheduler"}:
            graph.link(command, target, "handled_by" if target.startswith("agent:") else "uses")
        for job in touched.jobs:
            graph.link(command, f"job:{job}", "enqueues")
    for key, (label, module_name, route, links) in CHANNELS.items():
        described_by = index.modules[module_name]
        graph.add_node(
            f"channel:{key}", "channel", label, first_sentence(described_by.doc), f"{described_by.rel}:1", route=route
        )
        for target in links:
            graph.link(f"channel:{key}", target, "feeds")
    graph.prune()
    return _Built(graph=graph, periodic=periodic)


_lock = threading.Lock()
_static: _Built | None = None
_cache: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}


def _static_graph() -> _Built:
    global _static
    with _lock:
        if _static is None:
            _static = build_graph()
        return _static


def system_map(settings: Any) -> dict[str, Any]:
    """The whole map as JSON-ready data. Built once per process; the schedule follows this process's settings."""
    from jarvis.jobs.handlers import periodic_tasks

    built = _static_graph()
    active = {task.job_type: _schedule_text(task.every, task.daily_at) for task in periodic_tasks(settings)}
    key = tuple(sorted(active.items()))
    with _lock:
        if key in _cache:
            return _cache[key]
    graph = built.graph
    degree: Counter[str] = Counter()
    for source, target in graph.links:
        degree[source] += 1
        degree[target] += 1
    nodes = []
    for node in graph.nodes.values():
        extra = dict(node["extra"])
        described = node["description"]
        if node["id"] in RU:
            extra["code_doc"] = described
            described = RU[node["id"]]
        if node["type"] == "job":
            job_type = extra["job_type"]
            if job_type in active:
                extra["schedule"] = active[job_type]
            elif job_type in built.periodic:
                extra["schedule"] = "выключено настройками"
        nodes.append({**node, "description": described, "extra": extra, "degree": degree[node["id"]]})
    counts = Counter(node["type"] for node in nodes)
    hubs = sorted(nodes, key=lambda n: (-n["degree"], n["label"]))[:12]
    result = {
        "version": 1,
        "types": [{"id": type_, "label": label, "count": counts.get(type_, 0)} for type_, label in TYPES.items()],
        "link_kinds": LINK_KINDS,
        "nodes": nodes,
        "links": [
            {"source": source, "target": target, "kinds": sorted(kinds)}
            for (source, target), kinds in sorted(graph.links.items())
        ],
        "hubs": [n["id"] for n in hubs],
        "stats": {"nodes": len(nodes), "links": len(graph.links)},
    }
    with _lock:
        _cache[key] = result
    return result


def reset_cache() -> None:
    """Tests: read the code again on the next call."""
    global _static
    with _lock:
        _static = None
        _cache.clear()
