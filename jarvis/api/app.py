"""HTTP API: health, provider webhooks, Dev Worker pull API, admin API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from jarvis.bot.service import BotService
from jarvis.core.clock import format_local, local_day_bounds, to_local
from jarvis.core.logging import correlation_id_var, new_correlation_id
from jarvis.core.security import constant_time_equals, desktop_token, sha256_hex, verify_twilio_signature
from jarvis.db.models import CalendarEvent, DevWorkerHeartbeat
from jarvis.desktop.channel import DesktopChannel, plain_text
from jarvis.development.service import DevResult
from jarvis.services.container import Container
from jarvis.shopping.providers import ImportDocument
from jarvis.sync.todoist_sync import WebhookRejected, accept_todoist_webhook

log = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 1_000_000


# Request bodies live at module level: with postponed annotations FastAPI cannot see classes defined inside
# create_app and would read them as query parameters (422).


class HeartbeatIn(BaseModel):
    version: str = Field(default="unknown", max_length=40)
    repos: list[str] = Field(default_factory=list, max_length=50)


class IdeaIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class DesktopMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    spoken: bool = False


class DesktopVoiceIn(BaseModel):
    enabled: bool


class DesktopCallbackIn(BaseModel):
    data: str = Field(min_length=1, max_length=200)
    message_id: int | None = None


class DesktopProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=400)


class DesktopHelloIn(BaseModel):
    apps: list[str] | None = Field(default=None, max_length=1000)
    projects: list[DesktopProjectIn] | None = Field(default=None, max_length=200)
    version: str | None = Field(default=None, max_length=40)


class DesktopResultIn(BaseModel):
    ok: bool
    detail: str | None = Field(default=None, max_length=500)


class SystemAskIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SystemIdentifyIn(BaseModel):
    """A camera frame from the lab: base64 JPEG/PNG/WebP (≤ 2 MB decoded)."""

    image: str = Field(min_length=100, max_length=2_900_000)
    mime: str = Field(pattern=r"^image/(jpeg|png|webp)$")


# The `health` service listens on the home network (security review 2026-10-14, L1): the body is read before the
# signature can be checked, so at most this many requests are read at once (≤ 4 × 12 MB in memory) and a slow sender
# holds a slot for at most this long. The phone keeps unsent payloads and retries later.
HEALTH_MAX_IN_FLIGHT = 4
HEALTH_READ_TIMEOUT_SECONDS = 60.0


class InFlight:
    """A non-blocking cap on concurrent requests (one event loop: a plain counter is enough)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0

    def try_acquire(self) -> bool:
        if self.count >= self.limit:
            return False
        self.count += 1
        return True

    def release(self) -> None:
        self.count = max(0, self.count - 1)


async def accept_health(container: Container, request: Request, gate: InFlight | None = None) -> Response:
    """Health Connect data from the phone (Life Dashboard Companion): signed, stored as received, read by a job."""
    if gate is not None and not gate.try_acquire():
        return JSONResponse({"error": "busy, retry later"}, status_code=503, headers={"Retry-After": "30"})
    try:
        return await _accept_health(container, request)
    finally:
        if gate is not None:
            gate.release()


async def _accept_health(container: Container, request: Request) -> Response:
    from jarvis.health.service import MAX_PAYLOAD_BYTES
    from jarvis.scheduler.queue import enqueue_coalesced

    too_large = JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        if int(request.headers.get("content-length") or 0) > MAX_PAYLOAD_BYTES:
            return too_large
    except ValueError:
        return JSONResponse({"error": "bad content-length"}, status_code=400)
    # Read with the cap: a chunked body has no Content-Length, and the whole of it must not land in memory first.
    received = bytearray()
    try:
        async with asyncio.timeout(HEALTH_READ_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                received += chunk
                if len(received) > MAX_PAYLOAD_BYTES:
                    return too_large
    except TimeoutError:
        return JSONResponse({"error": "request body too slow"}, status_code=408, headers={"Connection": "close"})
    body = bytes(received)
    async with container.db.session() as s:
        status, reason = await container.health.accept(s, body, request.headers.get("x-signature"))
        if status == 204 and reason == "stored":
            await enqueue_coalesced(
                s, job_type="health.ingest", run_at=container.clock.now(), coalesce_key="health.ingest"
            )
    if status == 204:
        return Response(status_code=204)
    return JSONResponse({"error": reason}, status_code=status)


def create_health_app(container: Container) -> FastAPI:
    """The only thing the home network sees (service `health`): GET /health and POST /webhooks/health.
    The full API stays on loopback."""
    app = FastAPI(title="JARVIS health intake", docs_url=None, redoc_url=None, openapi_url=None)
    gate = InFlight(HEALTH_MAX_IN_FLIGHT)
    app.state.health_gate = gate

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/health")
    async def health_webhook(request: Request) -> Response:
        return await accept_health(container, request, gate)

    return app


def create_app(container: Container, *, manage_lifecycle: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if manage_lifecycle:
            await container.aclose()

    app = FastAPI(title="JARVIS", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.container = container
    bot = BotService(container)
    settings = container.settings

    @app.middleware("http")
    async def correlation(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if incoming.isalnum() and len(incoming) <= 40 else new_correlation_id()
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        return response

    async def read_body(request: Request) -> bytes:
        """At most MAX_WEBHOOK_BYTES, refused while reading — webhooks are read before their signature is checked
        (L5, review 17.10: the whole body used to be buffered first)."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "payload too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "payload too large")
        return bytes(body)

    def now() -> datetime:
        return container.clock.now()

    # --- auth dependencies -------------------------------------------------------------------------

    def require_admin(authorization: str = Header(default="")) -> None:
        token = settings.jarvis_api_token
        if token is None:
            raise HTTPException(503, "admin API disabled (JARVIS_API_TOKEN not set)")
        if not constant_time_equals(authorization, f"Bearer {token.get_secret_value()}"):
            raise HTTPException(401, "unauthorized")

    def require_worker(authorization: str = Header(default=""), x_worker_id: str = Header(default="")) -> str:
        token = settings.dev_worker_token
        if token is None:
            raise HTTPException(503, "dev worker API disabled (DEV_WORKER_TOKEN not set)")
        provided = authorization.removeprefix("Bearer ")
        if not provided or not constant_time_equals(sha256_hex(provided), sha256_hex(token.get_secret_value())):
            raise HTTPException(401, "unauthorized")
        worker_id = x_worker_id.strip()
        if not worker_id or len(worker_id) > 100 or not all(ch.isalnum() or ch in "-_." for ch in worker_id):
            raise HTTPException(400, "invalid X-Worker-Id")
        return worker_id

    # --- health ------------------------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        checks: dict[str, Any] = {}
        ok = True
        try:
            async with container.db.session() as s:
                await s.execute(text("SELECT 1"))
                version = await s.scalar(text("SELECT version_num FROM alembic_version"))
            checks["database"] = "ok"
            checks["migration"] = version
        except Exception as exc:
            ok = False
            checks["database"] = f"error: {type(exc).__name__}"
        checks["integrations"] = container.status
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
        )

    # --- webhooks ----------------------------------------------------------------------------------

    @app.post("/webhooks/todoist")
    async def todoist_webhook(request: Request) -> JSONResponse:
        body = await read_body(request)
        secret = settings.todoist_client_secret.get_secret_value() if settings.todoist_client_secret else None
        rejected: WebhookRejected | None = None
        result: dict[str, Any] = {}
        async with container.db.session() as s:
            try:
                result = await accept_todoist_webhook(
                    s,
                    body=body,
                    signature=request.headers.get("x-todoist-hmac-sha256"),
                    delivery_id=request.headers.get("x-todoist-delivery-id"),
                    client_secret=secret,
                    now=now(),
                )
            except WebhookRejected as exc:
                rejected = exc  # commit the audit row, then answer with the error
        if rejected is not None:
            return JSONResponse({"error": rejected.reason}, status_code=rejected.status_code)
        return JSONResponse(result)

    health_gate = InFlight(HEALTH_MAX_IN_FLIGHT)

    @app.post("/webhooks/health")
    async def health_webhook(request: Request) -> Response:
        return await accept_health(container, request, health_gate)

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> JSONResponse:
        secret = settings.telegram_webhook_secret
        if secret is None:
            return JSONResponse({"error": "telegram webhook disabled"}, status_code=503)
        provided = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not constant_time_equals(provided, secret.get_secret_value()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await read_body(request)
        try:
            update = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        status = await bot.handle_update(update)
        return JSONResponse({"status": status})

    @app.post("/webhooks/google-calendar")
    async def google_webhook(request: Request) -> JSONResponse:
        if container.calendar_sync is None:
            return JSONResponse({"error": "calendar not configured"}, status_code=503)
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            async with container.db.session() as s:
                result = await container.calendar_sync.accept_notification(s, headers, now())
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        except Exception as exc:
            log.warning("google push rejected: %s", exc)
            return JSONResponse({"error": "disabled"}, status_code=503)
        return JSONResponse(result)

    def _vapi_authorized(request: Request) -> bool:
        secret = settings.vapi_webhook_secret
        if secret is None:
            return False
        value = secret.get_secret_value()
        auth = request.headers.get("authorization", "")
        legacy = request.headers.get("x-vapi-secret", "")
        return constant_time_equals(auth, f"Bearer {value}") or constant_time_equals(legacy, value)

    @app.post("/webhooks/vapi")
    async def vapi_webhook(request: Request) -> JSONResponse:
        if settings.vapi_webhook_secret is None:
            return JSONResponse({"error": "vapi webhook disabled"}, status_code=503)
        if not _vapi_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = json.loads(await read_body(request))
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        message = payload.get("message") or {}
        mtype = message.get("type")
        call = message.get("call") or {}
        call_id = str(call.get("id") or "")
        if mtype == "status-update" and call_id:
            return JSONResponse(
                await container.call_service.on_provider_status(call_id, str(message.get("status")), None)
            )
        if mtype == "end-of-call-report" and call_id:
            return JSONResponse(
                await container.call_service.on_provider_status(call_id, "ended", str(message.get("endedReason") or ""))
            )
        if mtype == "tool-calls":
            return JSONResponse({"results": await voice_tool_results(message)})
        return JSONResponse({"status": "ignored"})

    async def voice_tool_results(message: dict[str, Any]) -> list[dict[str, str]]:
        """Narrow, read-only tools for the voice agent. Anything else is refused."""
        results = []
        for call in message.get("toolCallList") or message.get("toolCalls") or []:
            name = (call.get("function") or {}).get("name") or call.get("name")
            if name == "get_today_schedule":
                result = await today_schedule_text()
            else:
                result = "Этот инструмент недоступен."
            results.append({"toolCallId": str(call.get("id")), "result": result})
        return results

    async def today_schedule_text() -> str:
        tz = settings.tz
        current = now()
        _, end = local_day_bounds(to_local(current, tz).date(), tz)
        async with container.db.session() as s:
            events = (
                await s.scalars(
                    select(CalendarEvent)
                    .where(
                        CalendarEvent.status != "cancelled",
                        CalendarEvent.all_day.is_(False),
                        CalendarEvent.jarvis_managed.is_(False),
                        CalendarEvent.end_at > current,
                        CalendarEvent.start_at < end,
                    )
                    .order_by(CalendarEvent.start_at)
                    .limit(8)
                )
            ).all()
        if not events:
            return "На сегодня больше событий нет."
        return "; ".join(
            f"{format_local(e.start_at, tz)} {e.summary or 'событие'}" + (f", {e.location}" if e.location else "")  # type: ignore[arg-type]
            for e in events
        )

    @app.post("/webhooks/twilio/status")
    async def twilio_status(request: Request) -> JSONResponse:
        token = settings.twilio_auth_token
        base = settings.jarvis_public_base_url
        if token is None or not base:
            return JSONResponse({"error": "twilio callbacks disabled"}, status_code=503)
        form = dict(parse_qsl((await read_body(request)).decode("utf-8", "replace"), keep_blank_values=True))
        url = base.rstrip("/") + "/webhooks/twilio/status"
        if not verify_twilio_signature(token.get_secret_value(), url, form, request.headers.get("x-twilio-signature")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        status = form.get("CallStatus", "")
        mapped = "ended" if status in ("completed", "busy", "failed", "no-answer", "canceled") else status
        reason = status if status in ("busy", "failed", "no-answer", "canceled") else None
        return JSONResponse(await container.call_service.on_provider_status(form.get("CallSid", ""), mapped, reason))

    # --- dev worker (pull) --------------------------------------------------------------------------

    async def _beat(worker_id: str, info: dict[str, Any]) -> None:
        async with container.db.session() as s:
            stmt = insert(DevWorkerHeartbeat).values(worker_id=worker_id, last_seen_at=now(), info=info)
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[DevWorkerHeartbeat.worker_id],
                    set_={"last_seen_at": stmt.excluded.last_seen_at, "info": stmt.excluded.info},
                )
            )

    @app.post("/api/dev-worker/v1/heartbeat")
    async def worker_heartbeat(body: HeartbeatIn, worker_id: str = Depends(require_worker)) -> dict[str, str]:
        await _beat(worker_id, body.model_dump())
        return {"status": "ok"}

    @app.post("/api/dev-worker/v1/claim")
    async def worker_claim(worker_id: str = Depends(require_worker)) -> Response:
        await _beat(worker_id, {"last_action": "claim"})
        async with container.db.session() as s:
            claimed = await container.development.claim(s, worker_id, now())
        if claimed is None:
            return Response(status_code=204)
        return JSONResponse(claimed)

    @app.post("/api/dev-worker/v1/jobs/{job_id}/heartbeat")
    async def worker_job_heartbeat(job_id: uuid.UUID, worker_id: str = Depends(require_worker)) -> dict[str, bool]:
        async with container.db.session() as s:
            ok = await container.development.heartbeat(s, job_id, worker_id, now())
        return {"ok": ok}

    @app.post("/api/dev-worker/v1/jobs/{job_id}/result")
    async def worker_result(
        job_id: uuid.UUID, request: Request, worker_id: str = Depends(require_worker)
    ) -> JSONResponse:
        try:
            result = DevResult.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": "invalid result", "detail": str(exc)[:500]}, status_code=422)
        if result.job_id != job_id:
            return JSONResponse({"error": "job id mismatch"}, status_code=400)
        try:
            async with container.db.session() as s:
                job = await container.development.complete(s, result, worker_id, now())
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        except LookupError:
            return JSONResponse({"error": "not found"}, status_code=404)
        from jarvis.development.service import format_report

        try:
            await container.notifier.send(format_report(result), dedupe_key=f"dev-result:{job_id}")
        except Exception:
            log.warning("could not notify dev result")
        return JSONResponse({"status": job.status})

    # --- admin API ---------------------------------------------------------------------------------

    @app.get("/api/v1/status", dependencies=[Depends(require_admin)])
    async def api_status() -> dict[str, Any]:
        async with container.db.session() as s:
            jobs = (await s.execute(text("SELECT status, count(*) FROM scheduled_jobs GROUP BY status"))).all()
            workers = (await s.scalars(select(DevWorkerHeartbeat))).all()
        return {
            "integrations": container.status,
            "jobs": {r[0]: r[1] for r in jobs},
            "dev_workers": {w.worker_id: w.last_seen_at.isoformat() for w in workers},
        }

    @app.post("/api/v1/ideas", dependencies=[Depends(require_admin)])
    async def api_idea(body: IdeaIn) -> dict[str, str]:
        async with container.db.session() as s:
            idea, _ = await container.ideas.capture(s, raw_input=body.text, source="api", now=now())
        return {"idea_id": str(idea.id)}

    @app.get("/api/v1/ideas/search", dependencies=[Depends(require_admin)])
    async def api_search(q: str) -> list[dict[str, Any]]:
        async with container.db.session() as s:
            hits = await container.ideas.search(s, q[:500])
        return [
            {
                "idea_id": str(h.idea_id),
                "title": h.title,
                "snippet": h.snippet,
                "created_at": h.created_at.isoformat(),
                "score": h.score,
            }
            for h in hits
        ]

    @app.post("/api/v1/plan/propose", dependencies=[Depends(require_admin)])
    async def api_plan() -> dict[str, Any]:
        async with container.db.session() as s:
            outcome = await container.planner.propose(s, now(), requested_by="api")
        return {
            "proposal_id": str(outcome.proposal_id) if outcome.proposal_id else None,
            "text": outcome.text,
            "requires_approval": outcome.approval_nonce is not None,
        }

    @app.post("/api/v1/shopping/import", dependencies=[Depends(require_admin)])
    async def api_shop_import(doc: ImportDocument) -> dict[str, int]:
        async with container.db.session() as s:
            return await container.shopping.import_offers(s, doc, now())

    # --- Jarvis for Mac (loopback only; bearer token derived from existing secrets) ----------------------

    desktop = DesktopChannel(container)
    desktop_secret = desktop_token(settings.jarvis_encryption_key, settings.telegram_bot_token)
    last_seen: dict[str, datetime] = {}

    def require_desktop(authorization: str = Header(default="")) -> None:
        if desktop_secret is None:
            raise HTTPException(503, "desktop API disabled (no secret to derive its token from)")
        if not constant_time_equals(authorization, f"Bearer {desktop_secret}"):
            raise HTTPException(401, "unauthorized")

    async def mac_seen(**reported: Any) -> None:
        """Mark the Mac as online (at most every 20 s unless it reports something)."""
        moment = now()
        previous = last_seen.get("mac")
        if not reported and previous is not None and (moment - previous).total_seconds() < 20:
            return
        last_seen["mac"] = moment
        async with container.db.session() as s:
            await container.desktop.hello(s, moment, **reported)

    @app.post("/api/desktop/v1/hello", dependencies=[Depends(require_desktop)])
    async def desktop_hello(body: DesktopHelloIn) -> dict[str, Any]:
        await mac_seen(
            apps=body.apps,
            projects=[p.model_dump() for p in body.projects] if body.projects is not None else None,
            version=body.version,
        )
        return {
            "status": "ok",
            "address": settings.persona_address if settings.persona_enabled else None,
            "voice_mac": await desktop.voice_state(),
        }

    @app.post("/api/desktop/v1/voice", dependencies=[Depends(require_desktop)])
    async def desktop_voice(body: DesktopVoiceIn) -> dict[str, Any]:
        """The Mac's own voice switch (every reply spoken); Telegram's /voice is separate."""
        return await desktop.set_voice(body.enabled)

    @app.post("/api/desktop/v1/message", dependencies=[Depends(require_desktop)])
    async def desktop_message(body: DesktopMessageIn) -> dict[str, Any]:
        await mac_seen()
        return await desktop.message(body.text, spoken=body.spoken)

    @app.post("/api/desktop/v1/callback", dependencies=[Depends(require_desktop)])
    async def desktop_callback(body: DesktopCallbackIn) -> dict[str, Any]:
        await mac_seen()
        return await desktop.callback(body.data, body.message_id)

    @app.get("/api/desktop/v1/actions", dependencies=[Depends(require_desktop)])
    async def desktop_actions(wait: int = Query(default=0, ge=0, le=25)) -> dict[str, Any]:
        """Long poll: actions for the Mac, each handed out once."""
        deadline = now().timestamp() + wait
        while True:
            await mac_seen()
            async with container.db.session() as s:
                claimed = await container.desktop.claim(s, now())
                actions = [{"id": str(a.id), "kind": a.kind, "payload": a.payload, "source": a.source} for a in claimed]
            if actions or now().timestamp() >= deadline:
                return {"actions": actions}
            await asyncio.sleep(1)

    @app.post("/api/desktop/v1/actions/{action_id}/result", dependencies=[Depends(require_desktop)])
    async def desktop_action_result(action_id: uuid.UUID, body: DesktopResultIn) -> dict[str, str]:
        async with container.db.session() as s:
            action = await container.desktop.finish(s, action_id, now(), ok=body.ok, detail=body.detail)
        if action is None:
            raise HTTPException(404, "no such claimed action")
        if not body.ok and action.source == "telegram":  # the Mac app shows its own errors
            label = str(action.payload.get("label") or action.kind)
            with contextlib.suppress(Exception):
                await container.notifier.send(
                    plain_text(f"⚠️ На Mac не получилось: {label} — {body.detail or 'без подробностей'}"),
                    dedupe_key=f"mac-action:{action.id}",
                    chat_id=action.chat_id,
                )
        return {"status": action.status}

    # --- Jarvis OS: the map of Jarvis and its live state (the system page; same token as the Mac app) ------------

    @app.get("/api/desktop/v1/system/graph", dependencies=[Depends(require_desktop)])
    async def system_graph() -> dict[str, Any]:
        """Nodes and links read from the code (built once per process, in a thread: ≈ 1 s of parsing)."""
        from jarvis.system.map import system_map

        return await asyncio.to_thread(system_map, settings)

    @app.get("/api/desktop/v1/system/live", dependencies=[Depends(require_desktop)])
    async def system_live() -> dict[str, Any]:
        """What runs or fails now: cheap SQL and the status registry only (no probes, nothing billable)."""
        from jarvis.system.live import live_status
        from jarvis.system.map import system_map

        return await live_status(container, await asyncio.to_thread(system_map, settings))

    @app.post("/api/desktop/v1/system/identify", dependencies=[Depends(require_desktop)])
    async def system_identify(body: SystemIdentifyIn) -> dict[str, Any]:
        """Jarvis Lab «Что это за деталь?»: one vision call (≈ $0.002); the frame is not stored."""
        import base64
        import binascii

        from jarvis.photos.parts import MAX_FRAME_BYTES, identify_part

        try:
            image = base64.b64decode(body.image, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="image: not base64") from exc
        if len(image) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="image: larger than 2 MB")
        from jarvis.core.errors import JarvisError

        if container.llm is None:
            raise HTTPException(status_code=503, detail="распознавание недоступно: нет ключа OpenRouter")
        try:
            reading = await identify_part(container.llm, image, body.mime)
        except JarvisError as exc:
            raise HTTPException(status_code=503, detail=f"распознавание недоступно: {exc}") from exc
        return reading.model_dump(mode="json")

    @app.post("/api/desktop/v1/system/ask", dependencies=[Depends(require_desktop)])
    async def system_ask(body: SystemAskIn) -> dict[str, Any]:
        """The page's «Спросить»: the Mac conversation's path, but it does not mark the Mac app online (the page may
        be open while the app is closed — reminders must keep going to Telegram then)."""
        return await desktop.message(body.text)

    return app
