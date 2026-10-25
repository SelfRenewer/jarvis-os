"""`jarvis` command line: process entrypoints and operational tools."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import secrets
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text

from jarvis.core.config import CalendarStrategy, Settings, get_settings
from jarvis.core.logging import REDACTOR, configure_logging
from jarvis.db.models import DevWorkerHeartbeat, IntegrationSyncState
from jarvis.db.session import Database

if TYPE_CHECKING:
    from jarvis.labs.parse import LabReport

log = logging.getLogger("jarvis.cli")


def _setup(settings: Settings) -> None:
    configure_logging(settings.log_level, settings.log_json, settings.secret_values())


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop


async def _process_heartbeat(db: Database, role: str, now_fn: Any, info: dict[str, Any] | None = None) -> None:
    from sqlalchemy.dialects.postgresql import insert

    async with db.session() as s:
        stmt = insert(IntegrationSyncState).values(
            provider="process", resource=role, state=info or {}, last_success_at=now_fn()
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[IntegrationSyncState.provider, IntegrationSyncState.resource],
                set_={"last_success_at": stmt.excluded.last_success_at, "state": stmt.excluded.state},
            )
        )


# --- commands ----------------------------------------------------------------------------------------


def cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    import os

    from alembic import command
    from alembic.config import Config

    os.environ.setdefault("DATABASE_URL", settings.database_url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, args.revision)
    print(f"migrations applied up to {args.revision}")
    return 0


def cmd_api(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from jarvis.api.app import create_app
    from jarvis.services.container import build_container

    app = create_app(build_container(settings), manage_lifecycle=True)
    uvicorn.run(
        app, host=args.host, port=args.port, log_config=None, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )
    return 0


def cmd_health_api(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from jarvis.api.app import create_health_app
    from jarvis.services.container import build_container

    # On the home network: a bounded number of open connections (more get 503 at once) and short idle keep-alive;
    # the app itself caps bodies being read (HEALTH_MAX_IN_FLIGHT) and how long one may take.
    uvicorn.run(
        create_health_app(build_container(settings)),
        host=args.host,
        port=args.port,
        log_config=None,
        limit_concurrency=32,
        timeout_keep_alive=5,
    )
    return 0


def cmd_caller_login(settings: Settings, args: argparse.Namespace) -> int:
    """Telegram calls: sign in the second account (api_id/api_hash from my.telegram.org, phone, code, cloud password —
    all typed in the user's terminal) and save the session to .env (mode 600). Nothing is printed."""
    import getpass

    from jarvis.integrations.telegram.caller import login

    api_id = settings.telegram_caller_api_id or int(input("api_id с my.telegram.org: ").strip())
    api_hash = (
        settings.telegram_caller_api_hash.get_secret_value()
        if settings.telegram_caller_api_hash
        else getpass.getpass("api_hash с my.telegram.org (не отображается): ").strip()
    )
    session = asyncio.run(login(api_id, api_hash))
    _env_file_update(
        {
            "TELEGRAM_CALLER_API_ID": str(api_id),
            "TELEGRAM_CALLER_API_HASH": api_hash,
            "TELEGRAM_CALLER_SESSION": session,
            "TELEGRAM_CALLS_ENABLED": "true",
        }
    )
    print("Готово: сессия сохранена в .env (права 600). Теперь `make restart` — сервис caller начнёт звонить.")
    return 0


def _env_file_update(values: dict[str, str], path: Path = Path(".env")) -> None:
    """Set keys in .env in place (others untouched): written to a 0600 temp file, then renamed over."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(values)
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in left:
            out.append(f"{key}={left.pop(key)}")
        else:
            out.append(line)
    out += [f"{key}={value}" for key, value in left.items()]
    tmp = path.with_name(f".env.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)


LMF_TOKEN_FILE = Path.home() / "Sites" / "TLT" / "app" / "data" / "api-token"
_LMF_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,200}")


def cmd_lmf_setup(settings: Settings, args: argparse.Namespace) -> int:
    """Run on the Mac from the repo root: copy LMF's machine token (written by LMF on start into app/data/api-token,
    mode 600) into .env as LMF_API_TOKEN. Nothing secret is printed; then `make restart`."""
    source = Path(args.token_file).expanduser()
    if not Path(".env").exists():
        print("No .env here: run `uv run jarvis lmf-setup` from the JarvisOS repo root.", file=sys.stderr)
        return 2
    try:
        token = source.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        print(
            f"No LMF token at {source}: restart the TLT site in LocalSites (LMF writes the token when it starts).",
            file=sys.stderr,
        )
        return 1
    if not _LMF_TOKEN.fullmatch(token):
        print(f"{source} does not look like an LMF token (43+ characters, base64url).", file=sys.stderr)
        return 1
    _env_file_update({"LMF_API_TOKEN": token})
    print(f"LMF_API_TOKEN saved to .env (mode 600) from {source}.")
    if args.check_url:
        print(f"LMF at {args.check_url}: {_lmf_check(args.check_url, token)}")
    print("Now `make restart`; `make doctor` shows whether LMF accepts it.")
    return 0


def _lmf_check(url: str, token: str) -> str:
    """Ask the running LMF how it sees the token (from the Mac, so 127.0.0.1); never prints the token."""
    import httpx

    try:
        response = httpx.get(url.rstrip("/") + "/api/auth", headers={"Authorization": f"Bearer {token}"}, timeout=3.0)
    except httpx.HTTPError as exc:
        return f"not reachable ({type(exc).__name__})"
    if response.status_code == 404:
        return "answers without the login (an old LMF): restart the TLT site"
    try:
        access = response.json().get("access")
    except ValueError:
        return f"unexpected answer (HTTP {response.status_code})"
    return "token accepted" if access == "token" else f"token NOT accepted (access={access})"


def cmd_health_pair(settings: Settings, args: argparse.Namespace) -> int:
    """A pairing page for Life Dashboard Companion: a QR code with the address and the signing secret. The secret is
    written only into that file (mode 600) — never printed; delete the file after scanning."""
    import json
    from urllib.parse import quote

    if settings.health_webhook_secret is None or not settings.health_webhook_secret.get_secret_value():
        print("HEALTH_WEBHOOK_SECRET is not set: add it to .env and run `make restart` first", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}/webhooks/health"
    secret = settings.health_webhook_secret.get_secret_value()
    link = (
        "https://owen282000.github.io/life-dashboard-companion-app/pair"
        f"#v=1&url={quote(url, safe='')}&secret={quote(secret, safe='')}&name=Jarvis&sources=health_connect"
    )
    page = Path(args.out)
    page.touch(mode=0o600, exist_ok=True)
    page.chmod(0o600)
    page.write_text(PAIR_PAGE.replace("__LINK__", json.dumps(link)).replace("__URL__", url), encoding="utf-8")
    print(f"pairing page written: {page} (address {url}); delete it after scanning")
    return 0


def cmd_health_sleep_quality(settings: Settings, args: argparse.Namespace) -> int:
    """Learn the personal sleep-quality estimate from a Samsung Health export; `--fill-tlt` also writes Samsung's own
    scores into TLT days that have sleep but no quality (nothing else is touched)."""
    return asyncio.run(
        _health_sleep_quality(settings, Path(args.export), fill=args.fill_tlt, fill_sleep=args.fill_sleep)
    )


async def _health_sleep_quality(settings: Settings, export: Path, *, fill: bool, fill_sleep: bool = False) -> int:
    from jarvis.health.quality import fit, read_export
    from jarvis.services.container import build_container

    c = build_container(settings)
    nights = read_export(export)
    model = fit(nights, c.clock.now())
    async with c.db.session() as s:
        await c.health.save_quality_model(s, model)
    print(
        f"model: {model.nights} nights with score parts → out-of-sample MAE ≈{model.mae} points, "
        f"{(model.within10 or 0):.0%} within ±10 (saved)"
    )
    scores = c.health.export_scores(nights)
    print(f"export: {len(nights)} scored sessions → {len(scores)} nights ({min(scores)} … {max(scores)})")
    if not fill:
        return 0
    if c.lmf_api is None:
        print("LMF is off (LMF_ENABLED)", file=sys.stderr)
        return 1
    written = kept = no_sleep = 0
    for day in sorted(scores):
        current = await c.lmf_api.day(day)
        if current.get("sleep_minutes") in (None, ""):
            no_sleep += 1
        elif current.get("sleep_quality") not in (None, ""):
            kept += 1
        else:
            await c.lmf_api.save_day(day, {"sleep_quality": scores[day]})
            written += 1
    print(f"TLT: quality written {written}, kept (already set) {kept}, skipped (no sleep that day) {no_sleep}")
    if fill_sleep:  # days with no sleep in TLT get the export's night: sleep minutes, and its score if quality is empty
        added = 0
        for day, (minutes, score) in sorted(c.health.export_nights(nights).items()):
            current = await c.lmf_api.day(day)  # re-read right before writing
            if current.get("sleep_minutes") not in (None, ""):
                continue
            values: dict[str, object] = {"sleep_minutes": minutes}
            if current.get("sleep_quality") in (None, ""):
                values["sleep_quality"] = score
            await c.lmf_api.save_day(day, values)
            added += 1
        print(f"TLT: nights added (sleep + quality) {added}")
    return 0


def cmd_labs_import(settings: Settings, args: argparse.Namespace) -> int:
    """A lab report's text → LMF «Анализы» (parsed by code; the out-of-range marks are the lab's own)."""
    from jarvis.labs.parse import parse_lab_text

    report = parse_lab_text(Path(args.text).read_text(encoding="utf-8"))
    source = args.source or report.source
    if report.taken_on is None or not source or not report.rows:
        print("not a lab report I can read: no date / lab / rows (pass --source if the lab is not printed)")
        return 1
    flagged = [r for r in report.rows if r.flag]
    print(
        f"{source}, {report.taken_on:%d.%m.%Y}: {len(report.rows)} rows, out of range {len(flagged)}, "
        f"unreadable {report.skipped}"
    )
    for r in flagged:
        print(f"  {r.name}: {r.value_text} {r.unit or ''} (norm {r.reference or '—'})")
    if args.dry_run:
        return 0
    return asyncio.run(_labs_import(settings, report, source))


async def _labs_import(settings: Settings, report: LabReport, source: str) -> int:
    from jarvis.labs.service import lmf_rows
    from jarvis.services.container import build_container

    c = build_container(settings)
    if c.lmf_api is None:
        print("LMF is off (LMF_ENABLED)", file=sys.stderr)
        return 1
    saved = await c.lmf_api.save_labs(lmf_rows(report, source))
    print(f"LMF: saved {saved.get('saved')} rows → «Анализы»")
    return 0


# The page holds HEALTH_WEBHOOK_SECRET: the QR library from the CDN is pinned by Subresource Integrity (sha384 of
# qrcodejs 1.0.0 qrcode.min.js, computed 2026-10-14 from the file; it matches cdnjs's published sha512), so a changed
# file is refused by the browser instead of running next to the secret.
PAIR_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Jarvis · часы и весы</title>
<style>body{font:16px -apple-system,system-ui,sans-serif;background:#fff;color:#111;margin:0;padding:24px 16px;
text-align:center}#qr{display:inline-block;padding:16px;background:#fff}p{max-width:520px;margin:12px auto}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"
integrity="sha384-3zSEDfvllQohrq0PHL1fOXJuC/jSOO34H46t6UQfobFOmxE5BpjjaIJY5F2/bMnU" crossorigin="anonymous"
referrerpolicy="no-referrer"></script></head><body>
<h2>⌚ Подключить телефон к Джарвису</h2><div id="qr"></div>
<p>Life Dashboard Companion → <b>Scan pairing code</b> → наведите камеру на код.<br>Адрес: <code>__URL__</code></p>
<p>В приложении включите только нужное: сон, пульс, шаги, вес, жир, SpO₂, давление.</p>
<script>new QRCode(document.getElementById("qr"),
{text:__LINK__,width:320,height:320,correctLevel:QRCode.CorrectLevel.M});</script></body></html>"""


async def _worker(settings: Settings) -> None:
    from jarvis.jobs.handlers import build_registry
    from jarvis.scheduler.queue import JobQueue
    from jarvis.scheduler.runner import Worker
    from jarvis.services.container import build_container

    container = build_container(settings)
    queue = JobQueue(
        container.db, container.clock, worker_id=settings.worker_id, lease_seconds=settings.job_lease_seconds
    )
    worker = Worker(
        queue,
        build_registry(),
        container,
        concurrency=settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval_seconds,
    )
    stop = _stop_event()
    log.info("worker started (%s)", settings.worker_id)
    try:
        await worker.run_forever(
            stop, heartbeat=lambda: _process_heartbeat(container.db, "worker", container.clock.now)
        )
    finally:
        await container.aclose()


async def _scheduler(settings: Settings) -> None:
    from jarvis.jobs.handlers import periodic_tasks
    from jarvis.scheduler.periodic import PeriodicScheduler
    from jarvis.scheduler.queue import enqueue_coalesced
    from jarvis.services.container import build_container

    container = build_container(settings)
    scheduler = PeriodicScheduler(container.db, container.clock, settings.tz, periodic_tasks(settings))
    stop = _stop_event()
    # Startup: plan reminders immediately so overdue/lost state is re-evaluated after downtime.
    async with container.db.session() as s:
        now = container.clock.now()
        await enqueue_coalesced(s, job_type="reminders.plan", run_at=now, coalesce_key="startup")
    log.info("scheduler started with %d periodic tasks", len(scheduler.tasks))

    async def beat() -> None:
        await _process_heartbeat(container.db, "scheduler", container.clock.now)

    try:
        await scheduler.run_forever(stop, extra=beat)
    finally:
        await container.aclose()


async def _bot(settings: Settings) -> None:
    from jarvis.bot.service import BotService
    from jarvis.services.container import build_container

    container = build_container(settings)
    stop = _stop_event()
    if container.telegram is None or not settings.telegram_allowed_user_ids:
        log.warning("telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_IDS); bot idle")
        while not stop.is_set():
            await _process_heartbeat(container.db, "bot", container.clock.now, {"mode": "idle"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60)
        await container.aclose()
        return
    if settings.telegram_mode == "webhook":
        log.info("telegram webhook mode: updates arrive via the API service; bot process idle")
        while not stop.is_set():
            await _process_heartbeat(container.db, "bot", container.clock.now, {"mode": "webhook"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60)
        await container.aclose()
        return
    bot = BotService(container)
    try:
        await run_polling(container, bot, stop)
    finally:
        await container.aclose()


async def _caller(settings: Settings) -> None:
    """Telegram calls: one connected second account; rings the queued calls one at a time."""
    from jarvis.calls.service import configured, converse, ring_next
    from jarvis.integrations.telegram.caller import TelegramDialer
    from jarvis.services.container import build_container

    container = build_container(settings)
    stop = _stop_event()

    async def idle(mode: str, seconds: float) -> None:
        await _process_heartbeat(container.db, "caller", container.clock.now, {"mode": mode})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    if not configured(settings):
        log.warning("telegram calls off (TELEGRAM_CALLS_ENABLED, `jarvis caller-login`); caller idle")
        while not stop.is_set():
            await idle("off", 60)
        await container.aclose()
        return
    api_id, api_hash, session = (
        settings.telegram_caller_api_id,
        settings.telegram_caller_api_hash,
        settings.telegram_caller_session,
    )
    assert api_id is not None
    assert api_hash is not None
    assert session is not None
    dialer = TelegramDialer(
        api_id,
        api_hash.get_secret_value(),
        session.get_secret_value(),
        settings.telegram_allowed_user_ids[0],
    )

    async def answer(line: Any) -> None:
        await converse(container, line)

    async def talk(line: Any) -> None:
        await converse(container, line, outgoing=True)

    dialer.on_incoming = answer  # the user calls Jarvis: a conversation (only the owner's account is answered)
    dialer.on_talk = talk  # «позвони мне, поболтаем»: Jarvis rings, then the same conversation
    loop = asyncio.get_running_loop()
    retry_at, mode = 0.0, "starting"
    while not stop.is_set():  # a failed start (network, revoked session) is retried every 10 minutes
        if loop.time() >= retry_at:
            try:
                await dialer.start()
                break
            except Exception as exc:
                log.error("telegram caller cannot start: %s", exc)
                retry_at, mode = loop.time() + 600, f"error: {str(exc)[:120]}"
        await idle(mode, 60)
    try:
        while not stop.is_set():
            await _process_heartbeat(
                container.db, "caller", container.clock.now, {"mode": "talking" if dialer.busy else "ready"}
            )
            try:  # a queued call waits while the user is talking to Jarvis
                rung = None if dialer.busy else await ring_next(container, dialer, container.clock.now())
            except Exception:
                log.exception("telegram caller loop")
                rung = None
            if rung is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=3)
    finally:
        await dialer.aclose()
        await container.aclose()


async def run_polling(container: Any, bot: Any, stop: asyncio.Event) -> None:
    from sqlalchemy.dialects.postgresql import insert

    async with container.db.session() as s:
        state = await s.scalar(
            select(IntegrationSyncState).where(
                IntegrationSyncState.provider == "telegram", IntegrationSyncState.resource == "offset"
            )
        )
        offset = int(state.cursor) if state and state.cursor else None
    log.info("telegram polling started")
    while not stop.is_set():
        try:
            updates = await container.telegram.get_updates(offset, timeout=25)
        except Exception as exc:
            log.warning("getUpdates failed: %s", REDACTOR.redact(str(exc)))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
            continue
        for update in updates:
            try:
                await bot.handle_update(update)
            except Exception:
                log.exception("update handling failed")
            offset = int(update["update_id"]) + 1
            async with container.db.session() as s:
                stmt = insert(IntegrationSyncState).values(
                    provider="telegram",
                    resource="offset",
                    cursor=str(offset),
                    state={},
                    last_success_at=container.clock.now(),
                )
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[IntegrationSyncState.provider, IntegrationSyncState.resource],
                        set_={"cursor": stmt.excluded.cursor, "last_success_at": stmt.excluded.last_success_at},
                    )
                )
        await _process_heartbeat(container.db, "bot", container.clock.now, {"mode": "polling"})


def cmd_heartbeat_check(settings: Settings, args: argparse.Namespace) -> int:
    async def check() -> int:
        db = Database.from_url(settings.database_url, pool_size=1)
        try:
            async with db.session() as s:
                state = await s.scalar(
                    select(IntegrationSyncState).where(
                        IntegrationSyncState.provider == "process", IntegrationSyncState.resource == args.role
                    )
                )
                now = await s.scalar(text("SELECT now()"))
        finally:
            await db.dispose()
        if state is None or state.last_success_at is None:
            return 1
        return 0 if (now - state.last_success_at) <= timedelta(seconds=args.max_age) else 1

    return asyncio.run(check())


def cmd_gen_key(settings: Settings, args: argparse.Namespace) -> int:
    from jarvis.core.security import TokenCipher

    print("JARVIS_ENCRYPTION_KEY=" + TokenCipher.generate_key())
    print("JARVIS_API_TOKEN=" + secrets.token_urlsafe(32))
    print("DEV_WORKER_TOKEN=" + secrets.token_urlsafe(32))
    print("DEV_WORKER_SIGNING_KEY=" + secrets.token_urlsafe(32))
    print("TELEGRAM_WEBHOOK_SECRET=" + secrets.token_urlsafe(32))
    print("VAPI_WEBHOOK_SECRET=" + secrets.token_urlsafe(32))
    print("GOOGLE_CHANNEL_TOKEN=" + secrets.token_urlsafe(32))
    return 0


def cmd_google_auth(settings: Settings, args: argparse.Namespace) -> int:
    from jarvis.integrations.google_calendar.auth_flow import run_google_auth

    return asyncio.run(run_google_auth(settings, port=args.port))


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    from jarvis.doctor import run_doctor

    return asyncio.run(run_doctor(settings, live_maps=args.live_maps))


def cmd_run_job(settings: Settings, args: argparse.Namespace) -> int:
    """Enqueue a one-off job now (e.g. `jarvis run-job todoist.reconcile`)."""
    from jarvis.scheduler.queue import enqueue
    from jarvis.services.container import build_container

    async def go() -> int:
        container = build_container(settings)
        try:
            async with container.db.session() as s:
                job_id = await enqueue(
                    s, job_type=args.job_type, run_at=container.clock.now(), payload={"reason": "cli"}
                )
            print(f"enqueued {args.job_type}: {job_id}")
            return 0
        finally:
            await container.aclose()

    return asyncio.run(go())


DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Jarvis" / "config.json"
DEFAULT_PROJECT_ROOTS = ["~/Projects", "~/Developer", "~/Code", "~/Documents/GitHub"]


def _env_file_value(name: str, default: str) -> str:
    """A compose variable from the process env or the repo's .env (read only for that one key)."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name and value.strip():
                return value.strip().strip("'\"")
    return default


def _node_path() -> str | None:
    """A node binary that survives shells and upgrades: fnm's «default» alias, else the resolved `node` on PATH (an
    fnm per-shell symlink is resolved to the installation it points at)."""
    import os
    import shutil

    alias = Path.home() / ".local/share/fnm/aliases/default/bin/node"
    if alias.exists():
        return str(alias)
    found = shutil.which("node")
    return os.path.realpath(found) if found else None


def cmd_desktop_setup(settings: Settings, args: argparse.Namespace) -> int:
    """Write the Mac app's config: the local API address and its token (file mode 600; nothing is printed)."""
    import json

    from jarvis.core.security import desktop_token

    token = desktop_token(settings.jarvis_encryption_key, settings.telegram_bot_token)
    if token is None:
        print("No JARVIS_ENCRYPTION_KEY / TELEGRAM_BOT_TOKEN to derive the desktop token from.")
        return 2
    port = _env_file_value("JARVIS_API_HOST_PORT", "8080")
    config: dict[str, object] = {}
    if DESKTOP_CONFIG.exists():
        config = json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8"))
    config.update({"base_url": f"http://127.0.0.1:{port}", "token": token, "repo": str(Path.cwd())})
    if node := _node_path():
        config["node"] = node  # the Mac app starts Jarvis OS (desktop/system-site/server.mjs) with it
    config.setdefault("project_roots", DEFAULT_PROJECT_ROOTS)
    DESKTOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_CONFIG.touch(mode=0o600, exist_ok=True)
    DESKTOP_CONFIG.chmod(0o600)
    DESKTOP_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Jarvis for Mac config written: {DESKTOP_CONFIG} (mode 600, API http://127.0.0.1:{port})")
    return 0


def cmd_system_link(settings: Settings, args: argparse.Namespace) -> int:
    """The Jarvis OS page's personal link (its key is derived from the Mac app's token; the token is never printed)."""
    import json

    from jarvis.system.site import site_link

    token = None
    if DESKTOP_CONFIG.exists():
        token = json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8")).get("token")
    if not isinstance(token, str) or not token:
        print(f"No token in {DESKTOP_CONFIG}: run `uv run jarvis desktop-setup` first.")
        return 2
    print(site_link(token, args.port))
    return 0


def cmd_tg_recipient(settings: Settings, args: argparse.Namespace) -> int:
    """Who else the bot may write to on command: `add NAME CHAT_ID`, `remove NAME`, `list` (ids are not secrets)."""
    return asyncio.run(_tg_recipient(settings, args))


async def _tg_recipient(settings: Settings, args: argparse.Namespace) -> int:
    from jarvis.notify import recipients
    from jarvis.services.container import build_container

    c = build_container(settings)
    try:
        async with c.db.session() as session:
            if args.action == "add":
                if args.name is None or args.chat_id is None:
                    print("usage: jarvis tg-recipient add NAME CHAT_ID", file=sys.stderr)
                    return 2
                try:
                    added = await recipients.add(session, args.name, args.chat_id)
                except ValueError as exc:
                    print(exc, file=sys.stderr)
                    return 2
                print(f"added {added.name} ({added.chat_id}) — пусть нажмёт Start в боте, иначе Telegram не доставит")
            elif args.action == "remove":
                if args.name is None or not await recipients.remove(session, args.name):
                    print(f"нет получателя «{args.name}»", file=sys.stderr)
                    return 2
                print(f"removed {args.name}")
            else:
                found = await recipients.load(session)
                print("я (владелец)")
                for r in found.values():
                    print(f"{r.name} ({r.chat_id})")
        return 0
    finally:
        await c.aclose()


def cmd_tg_send(settings: Settings, args: argparse.Namespace) -> int:
    """A message through the Jarvis bot to the owner and/or recipients (`--to анна`, `--to всем`); text from
    $JARVIS_TG_TEXT (scripts/tg_send.sh) or stdin. HTML unless --plain."""
    text = os.environ.get("JARVIS_TG_TEXT") or sys.stdin.read()
    if not text.strip():
        print("Нечего отправлять", file=sys.stderr)
        return 1
    return asyncio.run(_tg_send(settings, args.to, text, None if args.plain else "HTML"))


async def _tg_send(settings: Settings, names: list[str], text: str, parse_mode: str | None) -> int:
    from jarvis.notify import recipients
    from jarvis.services.container import build_container

    if not settings.telegram_allowed_user_ids:
        print("TELEGRAM_ALLOWED_USER_IDS пуст — некому отправлять", file=sys.stderr)
        return 1
    c = build_container(settings)
    if c.telegram is None:
        print("Telegram не настроен (TELEGRAM_BOT_TOKEN)", file=sys.stderr)
        await c.aclose()
        return 1
    try:
        async with c.db.session() as session:
            known = await recipients.load(session)
        try:
            targets = recipients.resolve(names, settings.telegram_allowed_user_ids[0], known)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        failed = 0
        for target in targets:
            try:
                sent = await c.telegram.send_message(target.chat_id, text, parse_mode=parse_mode)
                print(f"sent {target.name} {sent.get('message_id')}")
            except Exception as exc:  # Telegram's reason: bad HTML, or the person never pressed Start
                failed += 1
                print(f"Telegram не принял сообщение для «{target.name}»: {exc}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        await c.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS personal AI operating layer")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("migrate")
    p.add_argument("--revision", default="head")
    p = sub.add_parser("api")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    sub.add_parser("worker")
    sub.add_parser("scheduler")
    sub.add_parser("bot")
    sub.add_parser("caller")
    p = sub.add_parser("doctor")
    p.add_argument(
        "--live-maps",
        action="store_true",
        help="also make one real routing request (Google: billable; OpenRouteService: from the free quota)",
    )
    p = sub.add_parser("heartbeat-check")
    p.add_argument("--role", required=True)
    p.add_argument("--max-age", type=int, default=120)
    sub.add_parser("gen-key")
    p = sub.add_parser("google-auth")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("run-job")
    p.add_argument("job_type")
    sub.add_parser("desktop-setup")
    p = sub.add_parser("system-link", help="print the Jarvis OS page's personal link (desktop/system-site)")
    p.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_SYSTEM_SITE_PORT") or 39110))
    p = sub.add_parser("health-api")
    p.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the home network; only /webhooks/health is served
    p.add_argument("--port", type=int, default=8091)
    p = sub.add_parser("health-sleep-quality")
    p.add_argument("--export", required=True, help="Samsung Health export folder («Загрузка личных данных», CSV)")
    p.add_argument("--fill-tlt", action="store_true", help="write Samsung's scores into TLT days without quality")
    p.add_argument(
        "--fill-sleep", action="store_true", help="with --fill-tlt: also add the export's nights to days without sleep"
    )
    p = sub.add_parser("labs-import")
    p.add_argument("--text", required=True, help="the report's text (./scripts/labs_import.sh makes it from the PDF)")
    p.add_argument("--source", help="the lab and order, e.g. «Гемотест №100000001» (read from the report if printed)")
    p.add_argument("--dry-run", action="store_true", help="only show what was parsed")
    sub.add_parser("caller-login", help="sign in the Telegram account that rings you (interactive, in your terminal)")
    p = sub.add_parser("lmf-setup", help="copy LMF's API token into .env (on the Mac, from the repo root)")
    p.add_argument("--token-file", default=str(LMF_TOKEN_FILE), help="LMF's app/data/api-token")
    p.add_argument(
        "--check-url", default="http://127.0.0.1:4321", help="the running LMF to verify the token against ('' = skip)"
    )
    p = sub.add_parser("health-pair")
    p.add_argument("--host", required=True, help="the Mac's address in the home network, e.g. 192.168.1.10")
    p.add_argument("--port", type=int, default=8097, help="the Mac's port for the `health` service (HEALTH_HOST_PORT)")
    p.add_argument("--out", required=True, help="where to write the pairing page (HTML with the QR code)")
    p = sub.add_parser("tg-recipient", help="who else the bot may write to on command (scripts/tg_send.sh --to)")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("name", nargs="?")
    p.add_argument("chat_id", nargs="?", type=int)
    p = sub.add_parser("tg-send", help="a message through the bot; text from $JARVIS_TG_TEXT or stdin")
    p.add_argument("--to", action="append", default=[], help="я | имя получателя | всем (repeatable; default: я)")
    p.add_argument("--plain", action="store_true", help="plain text instead of Telegram HTML")
    args = parser.parse_args(argv)
    settings = get_settings()
    _setup(settings)
    if settings.calendar_strategy == CalendarStrategy.MIRROR and not settings.google_mirror_calendar_id:
        log.warning("CALENDAR_STRATEGY=mirror requires GOOGLE_MIRROR_CALENDAR_ID")
    handlers = {
        "migrate": cmd_migrate,
        "api": cmd_api,
        "doctor": cmd_doctor,
        "heartbeat-check": cmd_heartbeat_check,
        "gen-key": cmd_gen_key,
        "google-auth": cmd_google_auth,
        "run-job": cmd_run_job,
        "desktop-setup": cmd_desktop_setup,
        "system-link": cmd_system_link,
        "lmf-setup": cmd_lmf_setup,
        "health-api": cmd_health_api,
        "health-pair": cmd_health_pair,
        "caller-login": cmd_caller_login,
        "health-sleep-quality": cmd_health_sleep_quality,
        "labs-import": cmd_labs_import,
        "tg-recipient": cmd_tg_recipient,
        "tg-send": cmd_tg_send,
    }
    if args.command in handlers:
        return handlers[args.command](settings, args)
    runners = {"worker": _worker, "scheduler": _scheduler, "bot": _bot, "caller": _caller}
    asyncio.run(runners[args.command](settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["DevWorkerHeartbeat", "main", "run_polling"]
