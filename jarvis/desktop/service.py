"""Jarvis for Mac: what the Mac reported, resolving «открой …» to a closed set of actions, and the action queue.

The Mac app (desktop/JarvisMac) registers the installed apps and the user's projects (`hello`), then pulls actions
over the loopback API. Resolution happens here, deterministically: aliases («телегу» → Telegram), known sites and
folders, fuzzy matching of names (with Russian → Latin transliteration: «джарвис» → «jarvis»). An action carries
only what Jarvis resolved itself — an installed app's exact name, an http(s) URL, a path the Mac reported — and the
app validates it again before acting. Starting Claude Code with a task needs the user's confirmation button.
"""

from __future__ import annotations

import contextvars
import difflib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import MacAction, UserPreference

MAC_ENV_KEY = "mac_env"
ONLINE_WINDOW = timedelta(minutes=2)
ACTION_TTL = timedelta(minutes=10)  # a Mac that wakes up later does not suddenly open things
PROPOSAL_TTL = timedelta(hours=2)
CLAIM_TIMEOUT = timedelta(minutes=5)
KINDS = ("open_app", "open_url", "open_path", "claude_code", "volume", "notify", "copy_text", "open_jarvis")
# open_jarvis: a Jarvis OS page; the Mac app starts the page server if needed and opens it signed in (its key never
# leaves the Mac). The same closed list is checked again there and by the page server.
JARVIS_PAGES = re.compile(r"^/(?:lab|sites|tlt)?(?:#[a-z0-9=_-]{1,40})?$")
NOTIFY_MAX = 300  # characters shown in the Mac's small window
COPY_MAX = 10_000

# Where the current request came from (the desktop channel sets "desktop").
CHANNEL: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_channel", default="telegram")

APP_ALIASES = {
    "телеграм": "Telegram",
    "телеграмм": "Telegram",
    "телегу": "Telegram",
    "телега": "Telegram",
    "сафари": "Safari",
    "хром": "Google Chrome",
    "гугл хром": "Google Chrome",
    "chrome": "Google Chrome",
    "файрфокс": "Firefox",
    "арк": "Arc",
    "яндекс браузер": "Yandex",
    "музыку": "Music",
    "музыка": "Music",
    "эпл мьюзик": "Music",
    "спотифай": "Spotify",
    "почту": "Mail",
    "почта": "Mail",
    "календарь": "Calendar",
    "заметки": "Notes",
    "напоминания": "Reminders",
    "терминал": "Terminal",
    "айтерм": "iTerm",
    "настройки": "System Settings",
    "системные настройки": "System Settings",
    "финдер": "Finder",
    "файндер": "Finder",
    "фото": "Photos",
    "фотографии": "Photos",
    "сообщения": "Messages",
    "ватсап": "WhatsApp",
    "вотсап": "WhatsApp",
    "слак": "Slack",
    "зум": "zoom.us",
    "дискорд": "Discord",
    "икскод": "Xcode",
    "вскод": "Visual Studio Code",
    "вс код": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "курсор": "Cursor",
    "клод": "Claude",
    "клауд": "Claude",
    "клоуд": "Claude",
    "cloud": "Claude",  # how speech recognition hears «Claude»
    "claud": "Claude",
    "чатгпт": "ChatGPT",
    "чат гпт": "ChatGPT",
    "докер": "Docker",
    "ноушен": "Notion",
    "фигму": "Figma",
    "фигма": "Figma",
    "обсидиан": "Obsidian",
    "тудуист": "Todoist",
    "калькулятор": "Calculator",
    "превью": "Preview",
    "апп стор": "App Store",
    "мониторинг системы": "Activity Monitor",
    "подкасты": "Podcasts",
    "карты": "Maps",
    "контакты": "Contacts",
    "фейстайм": "FaceTime",
}
SITES = {
    "ютуб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "гугл": "https://www.google.com",
    "google": "https://www.google.com",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "джимейл": "https://mail.google.com",
    "gmail": "https://mail.google.com",
    "гугл календарь": "https://calendar.google.com",
    "гугл диск": "https://drive.google.com",
    "опенроутер": "https://openrouter.ai/activity",
    "openrouter": "https://openrouter.ai/activity",
    "википедию": "https://ru.wikipedia.org",
    "википедия": "https://ru.wikipedia.org",
    "яндекс": "https://ya.ru",
    "переводчик": "https://translate.google.com",
    "гугл переводчик": "https://translate.google.com",
    "инстаграм": "https://www.instagram.com",
    "твиттер": "https://x.com",
    "кинопоиск": "https://www.kinopoisk.ru",
}
FOLDERS = {
    "загрузки": "~/Downloads",
    "документы": "~/Documents",
    "рабочий стол": "~/Desktop",
    "изображения": "~/Pictures",
    "картинки": "~/Pictures",
    "фильмы": "~/Movies",
    "домашнюю папку": "~",
    "домашняя папка": "~",
    "программы": "/Applications",
    "приложения": "/Applications",
    "icloud": "~/Library/Mobile Documents/com~apple~CloudDocs",
    "айклауд": "~/Library/Mobile Documents/com~apple~CloudDocs",
    "облако": "~/Library/Mobile Documents/com~apple~CloudDocs",
}
_NOISE = re.compile(
    r"\b(?:мне|пожалуйста|на\s+(?:маке|макбуке|компьютере|ноутбуке|mac)|приложение|программу|в\s+браузере)\b"
)
_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})(/[^\s]*)?\b", re.IGNORECASE)


def translit(value: str) -> str:
    """«джарвис» → «jarvis», «портфолио» → «portfolio» (good enough to match English names by ear)."""
    value = value.lower().replace("дж", "j")
    value = re.sub(r"ай(?=[бвгджзклмнпрстфхцчшщ]|$)", "i", value)  # «сайт» → site, «майкрософт» → microsoft
    return value.translate(_TRANSLIT)


def squash(value: str) -> str:
    return re.sub(r"[^a-z0-9а-я]", "", translit(value))


@dataclass(frozen=True)
class Project:
    name: str
    path: str


@dataclass
class MacEnv:
    apps: list[str] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    seen_at: datetime | None = None
    version: str | None = None

    def online(self, now: datetime) -> bool:
        return self.seen_at is not None and now - self.seen_at < ONLINE_WINDOW


@dataclass(frozen=True)
class Target:
    kind: str
    payload: dict[str, Any]
    label: str  # what the user hears: «Safari», «папку Загрузки», «сайт youtube.com»


@dataclass(frozen=True)
class Ambiguous:
    options: list[str]


def _clean(said: str) -> str:
    value = said.lower().replace("ё", "е").strip(" .!?«»\"'")
    return re.sub(r"\s+", " ", _NOISE.sub(" ", value)).strip()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _words(value: str) -> list[str]:
    """«JarvisOS» → [jarvis, os], «portfolio-site» → [portfolio, site], «портфолио сайт» → [portfolio, sayt]."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return [w for w in (squash(part) for part in re.split(r"[\s_\-./]+", spaced)) if w]


def _score(said: str, name: str) -> float:
    """The whole name by ear, or word by word (each said word against its closest word of the name)."""
    key, target = squash(said), squash(name)
    whole = _ratio(key, target) + (0.1 if target.startswith(key) else 0)
    said_words, name_words = _words(said), _words(name)
    if not said_words or not name_words or _ratio(said_words[0], name_words[0]) < 0.7:
        return whole  # word by word only from the distinctive first word («сайт» ≠ «portfolio-site»)
    by_word = sum(max(_ratio(w, n) for n in name_words) for w in said_words) / len(said_words)
    return max(whole, by_word)


def _best(said: str, names: list[str], threshold: float = 0.72) -> str | Ambiguous | None:
    """Fuzzy pick among `names` (transliterated, whole or word by word); a near tie is ambiguous."""
    key = squash(said)
    if not key:
        return None
    exact = [n for n in names if squash(n) == key]
    if len(exact) == 1:
        return exact[0]
    scored = sorted(((_score(said, n), n) for n in names), reverse=True)
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[1][0] >= threshold and scored[0][0] - scored[1][0] < 0.08:
        return Ambiguous([n for score, n in scored[:4] if score >= threshold])
    return scored[0][1]


def best_match(said: str, names: list[str], threshold: float = 0.72) -> str | Ambiguous | None:
    """Public form of the fuzzy name match (sites in LocalSites use it too)."""
    return _best(said, names, threshold)


def resolve_project(said: str | None, env: MacEnv) -> Project | Ambiguous | None:
    if not said or not env.projects:
        return None
    value = re.sub(r"^(?:проект[еау]?|репозитори[йия]|папк[еау])\s+", "", _clean(said))
    found = _best(value, [p.name for p in env.projects], threshold=0.74)
    if isinstance(found, str):
        return next(p for p in env.projects if p.name == found)
    return found


def resolve_open(said: str, env: MacEnv) -> Target | Ambiguous | None:
    """«телегу» → Telegram, «ютуб» → youtube.com, «папку загрузки» → ~/Downloads, «проект джарвис» → its folder."""
    value = _clean(said)
    if not value:
        return None
    if m := _DOMAIN.search(value):
        url = "https://" + m.group(1) + (m.group(2) or "")
        return Target("open_url", {"url": url}, f"сайт {m.group(1)}")
    is_site = bool(re.match(r"^сайт\s+", value))
    is_folder = bool(re.match(r"^(?:папк[уа]|директори[юя]|каталог)\s+", value))
    is_project = bool(re.match(r"^(?:проект[еау]?|репозитори[йия])\s+", value))
    bare = re.sub(r"^(?:сайт|папк[уа]|директори[юя]|каталог)\s+(?:с\s+|со\s+)?", "", value).strip()
    if is_project:
        project = resolve_project(value, env)
        if isinstance(project, Project):
            return Target("open_path", {"path": project.path}, f"проект «{project.name}»")
        return project
    for table, kind in ((FOLDERS, "open_path"), (SITES, "open_url")):
        if (kind == "open_url" and is_folder) or (kind == "open_path" and is_site):
            continue
        for name, where in table.items():
            if bare == name or (len(bare) >= 5 and len(name) >= 5 and bare[:5] == name[:5]):
                key = "path" if kind == "open_path" else "url"
                label = f"папку «{name.capitalize()}»" if kind == "open_path" else f"сайт {where.split('//')[1]}"
                return Target(kind, {key: where}, label)
    if is_site or is_folder:
        return None
    alias = APP_ALIASES.get(bare)
    installed = {a.lower(): a for a in env.apps}
    if alias is not None and alias.lower() in installed:
        name = installed[alias.lower()]
        return Target("open_app", {"app": name}, name)
    found = _best(alias or bare, env.apps, threshold=0.8)
    if isinstance(found, str):
        return Target("open_app", {"app": found}, found)
    if isinstance(found, Ambiguous):
        return found
    project = resolve_project(bare, env)
    if isinstance(project, Project):
        return Target("open_path", {"path": project.path}, f"проект «{project.name}»")
    return None


class DesktopService:
    async def env(self, session: AsyncSession) -> MacEnv | None:
        pref = await session.get(UserPreference, MAC_ENV_KEY)
        if pref is None or "apps" not in pref.value:
            return None  # the app has not registered yet (a bare «seen» mark is not a connected Mac)
        value = pref.value
        seen = value.get("seen_at")
        return MacEnv(
            apps=[str(a) for a in value.get("apps") or []],
            projects=[Project(str(p["name"]), str(p["path"])) for p in value.get("projects") or [] if p.get("path")],
            seen_at=datetime.fromisoformat(seen) if seen else None,
            version=value.get("version"),
        )

    async def hello(
        self,
        session: AsyncSession,
        now: datetime,
        *,
        apps: list[str] | None = None,
        projects: list[dict[str, str]] | None = None,
        version: str | None = None,
    ) -> None:
        """The Mac checks in: refresh what it reported (when sent) and when it was last seen."""
        pref = await session.get(UserPreference, MAC_ENV_KEY)
        value = dict(pref.value) if pref is not None else {}
        value["seen_at"] = now.isoformat()
        if apps is not None:
            value["apps"] = sorted({a.strip()[:80] for a in apps if a.strip()})[:600]
        if projects is not None:
            value["projects"] = [
                {"name": p["name"].strip()[:80], "path": p["path"].strip()[:400]}
                for p in projects[:200]
                if p.get("name") and str(p.get("path", "")).startswith("/")
            ]
        if version is not None:
            value["version"] = version[:40]
        if pref is None:
            session.add(UserPreference(key=MAC_ENV_KEY, value=value))
        else:
            pref.value = value

    async def enqueue(
        self,
        session: AsyncSession,
        target: Target,
        now: datetime,
        *,
        chat_id: int | None,
        proposed: bool = False,
    ) -> MacAction:
        if target.kind not in KINDS:
            raise ValueError(f"unknown mac action {target.kind}")
        if target.kind == "open_jarvis" and not JARVIS_PAGES.match(str(target.payload.get("path", ""))):
            raise ValueError("open_jarvis: not a Jarvis OS page")
        action = MacAction(
            kind=target.kind,
            payload={**target.payload, "label": target.label},
            status="proposed" if proposed else "pending",
            source=CHANNEL.get(),
            chat_id=chat_id,
            expires_at=now + (PROPOSAL_TTL if proposed else ACTION_TTL),
        )
        session.add(action)
        await session.flush()
        return action

    async def confirm(
        self, session: AsyncSession, action_id: uuid.UUID, now: datetime, *, run: bool
    ) -> MacAction | None:
        """The user's button on a proposed action: run it (→ pending, a fresh TTL) or cancel. Exactly once."""
        result = await session.execute(
            update(MacAction)
            .where(MacAction.id == action_id, MacAction.status == "proposed", MacAction.expires_at > now)
            .values(
                status="pending" if run else "cancelled",
                expires_at=now + ACTION_TTL,
                finished_at=None if run else now,
            )
            .returning(MacAction.id)
        )
        if result.first() is None:
            return None
        return await session.scalar(
            select(MacAction).where(MacAction.id == action_id).execution_options(populate_existing=True)
        )

    async def claim(self, session: AsyncSession, now: datetime, limit: int = 5) -> list[MacAction]:
        """Pending actions for the Mac, oldest first; each is handed out once (expired ones are dropped)."""
        await session.execute(
            update(MacAction)
            .where(MacAction.status.in_(("pending", "proposed")), MacAction.expires_at <= now)
            .values(status="expired", finished_at=now)
        )
        await session.execute(  # claimed but never reported (the app was quit or hung): not retried — at most once
            update(MacAction)
            .where(MacAction.status == "claimed", MacAction.claimed_at <= now - CLAIM_TIMEOUT)
            .values(status="failed", finished_at=now, detail="Mac не сообщил результат")
        )
        rows = (
            await session.scalars(
                select(MacAction)
                .where(MacAction.status == "pending")
                .order_by(MacAction.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for action in rows:
            action.status = "claimed"
            action.claimed_at = now
        return list(rows)

    async def finish(
        self, session: AsyncSession, action_id: uuid.UUID, now: datetime, *, ok: bool, detail: str | None
    ) -> MacAction | None:
        action = await session.get(MacAction, action_id)
        if action is None or action.status != "claimed":
            return None
        action.status = "done" if ok else "failed"
        action.detail = (detail or "")[:500] or None
        action.finished_at = now
        return action


# --- volume, clipboard and notifications on the Mac (closed set, values checked again by the app) ------------------

_VOLUME_SET = re.compile(r"(?:громкост\w*|звук\w*)\s*(?:на|в|до)?\s*(?P<n>\d{1,3})\s*(?:%|процент\w*)?$")
_VOLUME_STEP = re.compile(r"(?:на\s+)?(?P<n>\d{1,2})\s*(?:%|процент\w*)?")
_MUTE = re.compile(
    r"^(?:выключи|отключи|убери|вырубить|выруби)\s+(?:весь\s+)?звук(?:\s+на\s+(?:маке|mac))?$|^без\s+звука$"
)
_UNMUTE = re.compile(r"^(?:включи|верни)\s+(?:обратно\s+)?звук(?:\s+на\s+(?:маке|mac))?$")
_LOUDER = re.compile(r"^(?:сделай\s+)?(?:погромче|громче|прибавь\s+(?:звук|громкость)|увеличь\s+(?:звук|громкость))")
_QUIETER = re.compile(r"^(?:сделай\s+)?(?:потише|тише|убавь\s+(?:звук|громкость)|уменьши\s+(?:звук|громкость))")
_VOLUME_PREFIX = re.compile(r"^(?:сделай|поставь|установи|выставь|включи)?\s*")


def parse_volume(text: str) -> dict[str, str] | None:
    """«громкость 30», «звук на 50%», «погромче», «потише на 20», «выключи звук», «включи звук» → an action payload."""
    low = re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip(" .!?")
    low = re.sub(r"\s+на\s+(?:маке|mac|компьютере)$", "", low)
    if _MUTE.match(low):
        return {"op": "mute"}
    if _UNMUTE.match(low):
        return {"op": "unmute"}
    for pattern, op in ((_LOUDER, "up"), (_QUIETER, "down")):
        if m := pattern.match(low):
            rest = low[m.end() :].strip()
            if not rest:
                return {"op": op, "step": "10"}
            if step := _VOLUME_STEP.fullmatch(rest):
                return {"op": op, "step": str(min(max(int(step.group("n")), 1), 50))}
            return None
    if m := _VOLUME_SET.match(_VOLUME_PREFIX.sub("", low, count=1)):
        level = int(m.group("n"))
        return {"op": "set", "level": str(level)} if 0 <= level <= 100 else None
    return None


def volume_label(payload: dict[str, str]) -> str:
    op = payload.get("op")
    if op == "mute":
        return "звук выключен"
    if op == "unmute":
        return "звук включён"
    if op in ("up", "down"):
        return f"громкость {'+' if op == 'up' else '−'}{payload.get('step', '10')}%"
    return f"громкость {payload.get('level')}%"


_MAC_REMIND_LEAD = re.compile(
    r"^(?P<due>через\s+(?:полчаса|полтора\s+часа|(?:\d{1,3}\s+|пару\s+)?(?:минут\w*|мин\b|час\w*))"
    r"|(?:(?:сегодня|завтра)\s+)?в\s+\d{1,2}(?::\d{2})?(?:\s*(?:утра|дня|вечера|ночи))?)\s+(?P<title>.+)$"
)


def mac_reminder(rest: str, now: datetime, tz: Any) -> tuple[str, datetime] | None:
    """«через 10 минут выключить чайник» / «позвонить маме в 15:00» → (text, aware moment in the future)."""
    from jarvis.bot.intents import split_due
    from jarvis.core.clock import to_local
    from jarvis.core.dates import parse_shift, parse_when

    text = rest.strip(" .!")
    if m := _MAC_REMIND_LEAD.match(text.lower()):
        due, title = m.group("due"), text[m.start("title") :]
    else:
        title, due_or_none = split_due(text)
        if not due_or_none:
            return None
        due = due_or_none
    title = title.strip(" .,:—-")
    if not title:
        return None
    if (shift := parse_shift(due)) is not None and shift.from_now:
        return title, now + timedelta(minutes=shift.minutes)
    local_now = to_local(now, tz)
    when = parse_when(due, local_now.date())
    if when is None or when.at is None:
        return None
    moment = datetime.combine(when.day or local_now.date(), when.at, tzinfo=tz)
    if moment <= now:
        if when.day is not None:
            return None
        moment += timedelta(days=1)  # «в 9» said at 15:00 → tomorrow at 9
    return title, moment
