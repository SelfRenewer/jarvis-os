"""`jarvis doctor`: diagnostics without exposing secrets and without billable calls by default."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text

from jarvis.core.config import CalendarStrategy, Settings
from jarvis.core.logging import REDACTOR
from jarvis.db.models import DevWorkerHeartbeat, IntegrationAccount, IntegrationSyncState
from jarvis.integrations.maps.openrouteservice import DAILY_DIRECTIONS_QUOTA, DAILY_GEOCODE_QUOTA, ors_profile
from jarvis.integrations.maps.provider import GOOGLE, OPENROUTESERVICE, maps_provider_name, missing_key_hint
from jarvis.integrations.maps.routes import RouteStatus, billing_sku, format_coordinates
from jarvis.reminders.origin import load_origin, origin_status

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "NOT_CONFIGURED"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, REDACTOR.redact(detail)[:160]))

    def render(self) -> str:
        width = max(len(r[0]) for r in self.rows) + 2
        return "\n".join(f"{name:<{width}}{status:<16}{detail}" for name, status, detail in self.rows)

    @property
    def failed(self) -> bool:
        return any(status == FAIL for _, status, _ in self.rows)


def lmf_access_row(answer: dict[str, Any] | None, *, token_set: bool) -> tuple[str, str]:
    """The `lmf access` row from LMF's GET /api/auth (None = an LMF without the login). No secret is shown."""
    if answer is None:
        return WARN, "this LMF has no login (old version): restart the TLT site in LocalSites"
    password = "UI password set" if answer.get("password") else "no UI password yet (node app/set-password.mjs)"
    access = answer.get("access")
    if access == "token":
        return (OK if answer.get("password") else WARN), f"token accepted; {password}"
    if token_set:
        return FAIL, f"LMF_API_TOKEN rejected (access={access}): run `uv run jarvis lmf-setup`, then make restart"
    if access == "open":
        return WARN, f"LMF_API_TOKEN not set (uv run jarvis lmf-setup); {password} → LMF is open to any container"
    return FAIL, "LMF requires a token: run `uv run jarvis lmf-setup`, then make restart"


async def _probe(report: Report, name: str, fn: Callable[[], Awaitable[Any]], ok_detail: str = "") -> None:
    try:
        result = await fn()
        report.add(name, OK, ok_detail or (str(result) if isinstance(result, str) else ""))
    except Exception as exc:
        report.add(name, FAIL, f"{type(exc).__name__}: {exc}")


async def run_doctor(settings: Settings, *, live_maps: bool = False) -> int:
    import logging

    logging.getLogger("alembic").setLevel(logging.WARNING)  # set before importing alembic: it logs on import

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from jarvis.services.container import build_container

    report = Report()
    report.add("environment", OK, f"env={settings.jarvis_env} tz={settings.default_user_timezone}")
    container = build_container(settings)
    try:
        # --- database / migrations ---
        try:
            async with container.db.session() as s:
                await s.execute(text("SELECT 1"))
                current = await s.scalar(text("SELECT version_num FROM alembic_version"))
                db_now = await s.scalar(text("SELECT now()"))
            report.add("database", OK)
            os.environ.setdefault("DATABASE_URL", settings.database_url)
            head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            report.add("migrations", OK if current == head else FAIL, f"current={current} head={head}")
        except Exception as exc:
            report.add("database", FAIL, f"{type(exc).__name__}: {exc}")
            print(report.render())
            return 1

        # --- Todoist ---
        if container.todoist is not None:
            await _probe(report, "todoist", container.todoist.probe, "token accepted")
        else:
            report.add("todoist", SKIP, "set TODOIST_API_TOKEN")
        report.add(
            "todoist webhooks",
            OK if settings.todoist_client_secret and settings.jarvis_public_base_url else SKIP,
            "polling reconciliation active" if not settings.jarvis_public_base_url else "",
        )

        # --- Google Calendar ---
        if container.calendar is not None:
            async with container.db.session() as s:
                account = await s.scalar(select(IntegrationAccount).where(IntegrationAccount.provider == "google"))
            if container.status.get("google_calendar", "").startswith("CONFIGURED (iCal"):

                async def ics_probe() -> str:
                    now = container.clock.now()
                    events = await container.calendar.list_events(  # type: ignore[union-attr]
                        "primary", now - timedelta(days=1), now + timedelta(days=settings.calendar_sync_days)
                    )
                    return (
                        f"iCal secret address (read-only): {len(events)} events in {settings.calendar_sync_days} days"
                    )

                await _probe(report, "google calendar", ics_probe)
            elif account is None:
                report.add("google calendar", FAIL, "not authorized: run `jarvis google-auth`")
            elif account.status != "active":
                report.add("google calendar", FAIL, f"status={account.status}: re-run `jarvis google-auth`")
            else:
                calendars: list[dict[str, Any]] = []

                async def list_cals() -> str:
                    calendars.extend(await container.calendar.list_calendars())  # type: ignore[union-attr]
                    return f"{len(calendars)} calendars visible"

                await _probe(report, "google calendar", list_cals)
                todoist_cal = any(str(c.get("summary", "")).lower() == "todoist" for c in calendars)
                if settings.calendar_strategy == CalendarStrategy.MIRROR and todoist_cal:
                    report.add(
                        "calendar strategy",
                        FAIL,
                        "mirror selected but native 'Todoist' calendar exists → tasks would appear twice",
                    )
                else:
                    report.add(
                        "calendar strategy",
                        OK,
                        f"{settings.calendar_strategy.value}"
                        + (" (Todoist calendar excluded from availability)" if todoist_cal else ""),
                    )
        else:
            report.add("google calendar", SKIP, "set GOOGLE_CLIENT_ID/SECRET + JARVIS_ENCRYPTION_KEY")

        # --- Telegram ---
        if container.telegram is not None:

            async def me() -> str:
                info = await container.telegram.get_me()  # type: ignore[union-attr]
                return f"@{info.get('username')}"

            await _probe(report, "telegram", me)
            for user_id in settings.telegram_allowed_user_ids:
                try:
                    await container.telegram.get_chat(user_id)
                    report.add(f"telegram chat {user_id}", OK, "bot can message you")
                except Exception:
                    report.add(
                        f"telegram chat {user_id}",
                        FAIL,
                        "bot cannot message you yet: open the bot in Telegram and press Start",
                    )
            report.add(
                "telegram allowlist",
                OK if settings.telegram_allowed_user_ids else FAIL,
                f"{len(settings.telegram_allowed_user_ids)} user(s)",
            )
        else:
            report.add("telegram", SKIP, "set TELEGRAM_BOT_TOKEN")

        # --- Anthropic ---
        llm = container.llm
        if llm is not None and hasattr(llm, "probe"):

            async def model() -> str:
                info = await llm.probe()
                return f"model(s) {info['id']} available"

            await _probe(report, "llm", model)
        else:
            report.add("llm", SKIP, "set OPENROUTER_API_KEY or ANTHROPIC_API_KEY (deterministic fallbacks active)")

        # --- voice ---
        provider = container.call_provider
        if hasattr(provider, "get_assistant"):
            await _probe(report, "vapi", provider.get_assistant, "assistant reachable")
        elif hasattr(provider, "get_account"):
            await _probe(report, "twilio", provider.get_account, "account reachable")
        else:
            report.add("voice calls", SKIP, "set VAPI_* (with imported Twilio number) or TWILIO_*")
        report.add(
            "live calls",
            WARN if settings.voice_calls_enabled else OK,
            "VOICE_CALLS_ENABLED=true (real calls)" if settings.voice_calls_enabled else "disabled",
        )
        from jarvis.calls.service import enabled as calls_enabled

        if calls_enabled(settings):  # the session itself lives only in the `caller` service: see process:caller
            at = settings.telegram_call_at_repeat
            report.add(
                "telegram calls",
                OK,
                f"on: rings after repeat {at} without «Понял» (≤{settings.max_calls_per_day}/day, not in sleep hours)"
                if at
                else "on: only «позвони мне …»",
            )
        else:
            report.add("telegram calls", SKIP, "off (pilot: `uv run jarvis caller-login` on the Mac)")

        # --- maps ---
        maps_provider = maps_provider_name(settings)
        mode = settings.maps_travel_mode.upper()
        maps_row = {GOOGLE: "google maps", OPENROUTESERVICE: "openrouteservice"}.get(maps_provider or "", "maps")
        if maps_provider is None:
            report.add(maps_row, SKIP, f"{missing_key_hint(settings)} (reminders fall back to start-time)")
        elif mode not in container.routes.travel_modes:
            report.add(
                maps_row, WARN, f"MAPS_TRAVEL_MODE={mode} is not supported by {container.routes.label} → no travel time"
            )
        elif live_maps and settings.default_origin:
            from datetime import UTC, datetime

            async def route() -> str:
                # One real request (Google: billable; OpenRouteService: 1 geocode + 1 route from the free quota):
                # DEFAULT_ORIGIN → the weather point (the city centre), a real route.
                result = await container.routes.compute(
                    settings.default_origin or "",
                    format_coordinates(settings.weather_latitude, settings.weather_longitude),
                    datetime.now(UTC) + timedelta(minutes=5),
                    settings.maps_travel_mode,
                )
                if result.status != RouteStatus.OK or result.duration_seconds is None:
                    raise RuntimeError(f"routes status={result.status}: {result.error}")
                return f"routes OK: {settings.weather_place} centre ~{-(-result.duration_seconds // 60)} min from home"

            await _probe(report, maps_row, route)
        elif maps_provider == GOOGLE:
            sku = billing_sku(settings.maps_travel_mode, settings.maps_traffic_aware)
            report.add(
                maps_row,
                OK,
                f"key present; {mode}, Compute Routes {sku}, "
                f"≤{settings.maps_max_requests_per_day}/day (--live-maps: a billable probe)",
            )
        else:
            report.add(
                maps_row,
                OK,
                f"key present; {mode} → {ors_profile(mode)}, no live traffic, ≤{settings.maps_max_requests_per_day}"
                f"/day (free plan: {DAILY_DIRECTIONS_QUOTA}) (--live-maps: one real request)",
            )
        if container.geocoder is not None:
            report.add(
                "2gis route button",
                OK,
                f"addresses → coordinates via OpenRouteService (≤{settings.maps_geocode_max_requests_per_day}/day, "
                f"free plan: {DAILY_GEOCODE_QUOTA}; cached 30 days)",
            )
        else:
            report.add("2gis route button", SKIP, "needs OPENROUTESERVICE_API_KEY (addresses → coordinates)")
        report.add(
            "default origin",
            OK if settings.default_origin else WARN,
            (settings.default_origin and "set")
            or "DEFAULT_ORIGIN empty → travel time only from a location shared in Telegram",
        )
        async with container.db.session() as s:
            shared = await load_origin(s)  # no coordinates printed
        report.add(
            "current origin",
            OK,
            origin_status(
                shared,
                container.clock.now(),
                timedelta(minutes=settings.location_origin_ttl_minutes),
                settings.tz,
            ),
        )

        # --- STT / TTS / embeddings ---
        stt_status = container.status.get("stt", "")
        report.add("speech-to-text", OK if stt_status.startswith("CONFIGURED") else SKIP, stt_status)
        tts = container.tts
        probe_tts = getattr(tts, "probe", None)
        if probe_tts is not None:
            await _probe(report, "text-to-speech", probe_tts, f"{tts.model}, voice {tts.voice} (no billable call)")
        else:
            report.add("text-to-speech", SKIP, container.status.get("tts", ""))
        report.add(
            "voice replies",
            OK,
            "on by default (chat: /voice off)" if settings.voice_replies_enabled else "off by default (/voice on)",
        )
        embeddings_status = container.status.get("embeddings", "")
        probe_embeddings = getattr(container.embeddings, "probe", None)
        if probe_embeddings is not None:
            await _probe(report, "embeddings", probe_embeddings)
        else:
            report.add("embeddings", OK if embeddings_status.startswith("CONFIGURED") else SKIP, embeddings_status)
        if container.search.semantic:
            from jarvis.core.budget import used_today

            async with container.db.session() as s:
                coverage = await container.search.coverage(s)
                calls = await used_today(s, "embeddings", container.clock.now())
            vectors = ", ".join(f"{kind} {n}" for kind, n in sorted(coverage.items()))
            report.add(
                "search by meaning",
                OK if coverage else WARN,
                f"vectors: {vectors or 'none yet (uv run jarvis run-job embeddings.backfill)'}; "
                f"calls today {calls}/{settings.embeddings_max_calls_per_day}",
            )
        else:
            report.add("search by meaning", SKIP, "search by words only (OPENROUTER_API_KEY or VOYAGE_API_KEY)")
        if container.health.configured():
            from jarvis.db.models import HealthSample

            async with container.db.session() as s:
                last = await container.health.last_received(s)
                rows = (await s.execute(select(HealthSample.kind, func.count()).group_by(HealthSample.kind))).all()
            kinds: dict[str, int] = {str(kind): int(n) for kind, n in rows}
            if last is None:
                report.add("watch & scale", WARN, "nothing from the phone yet (pair it: jarvis health-pair)")
            else:
                ago = int((container.clock.now() - last).total_seconds() // 60)
                listed = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "only test pings"
                report.add("watch & scale", OK if ago < 24 * 60 else WARN, f"last data {ago} min ago; {listed}")
        else:
            report.add("watch & scale", SKIP, "HEALTH_WEBHOOK_SECRET not set (phone: Life Dashboard Companion)")
        weather_status = container.status.get("weather", "")
        if weather_status.startswith("CONFIGURED"):

            async def weather_probe() -> str:
                forecast = await container.weather.forecast()
                if forecast is None:
                    raise RuntimeError("forecast unavailable")
                return f"{settings.weather_place}: {forecast.current_temperature:+.0f}° (open-meteo, free)"

            await _probe(report, "weather", weather_probe)
        else:
            report.add("weather", SKIP, weather_status)
        lmf_status = container.status.get("lmf", "")
        auth_status = getattr(container.lmf_api, "auth_status", None)
        if lmf_status.startswith("CONFIGURED") and auth_status is not None:
            from jarvis.core.errors import NotFoundError

            try:
                answer: dict[str, Any] | None = await auth_status()
            except NotFoundError:
                answer = None
            except Exception as exc:
                report.add("lmf access", FAIL, f"{type(exc).__name__}: {exc}")
            else:
                report.add("lmf access", *lmf_access_row(answer, token_set=bool(settings.lmf_api_token)))
        if lmf_status.startswith("CONFIGURED"):

            async def lmf_probe() -> str:
                d = await container.tracker.dictionary()
                spheres = len({i.category for i in d.items})
                return f"TLT {len(d.tags)} tags, TPL {spheres} spheres / {len(d.items)} items ({settings.lmf_base_url})"

            await _probe(report, "life tracker (LMF)", lmf_probe)
        else:
            report.add("life tracker (LMF)", SKIP, lmf_status)
        sites_status = container.status.get("localsites", "")
        if sites_status.startswith("CONFIGURED"):

            async def sites_probe() -> str:
                state = await container.sites.state()
                running = sum(1 for x in state.sites if x.running)
                return f"{len(state.sites)} sites, {running} running ({settings.localsites_url}, «Пульт»)"

            await _probe(report, "localsites", sites_probe)
        else:
            report.add("localsites", SKIP, sites_status)
        fx_status = container.status.get("fx", "")
        if fx_status.startswith("CONFIGURED"):

            async def fx_probe() -> str:
                rates, _ = await container.fx.fiat()
                usd_rub = rates.per_usd.get("RUB")
                return f"{len(rates.per_usd)} currencies, 1 USD = {usd_rub} RUB (open.er-api.com, free)"

            await _probe(report, "currency rates", fx_probe)
        else:
            report.add("currency rates", SKIP, fx_status)

        # --- processes & dev worker ---
        async with container.db.session() as s:
            roles = ("worker", "scheduler", "bot") + (("caller",) if settings.telegram_calls_enabled else ())
            for role in roles:
                state = await s.scalar(
                    select(IntegrationSyncState).where(
                        IntegrationSyncState.provider == "process", IntegrationSyncState.resource == role
                    )
                )
                if state is None or state.last_success_at is None:
                    report.add(f"process:{role}", WARN, "no heartbeat yet")
                else:
                    age = int((db_now - state.last_success_at).total_seconds())
                    mode = str((state.state or {}).get("mode") or "") if role == "caller" else ""
                    healthy = age < 180 and not mode.startswith("error")
                    report.add(f"process:{role}", OK if healthy else FAIL, f"heartbeat {age}s ago {mode}".strip())
            workers = (await s.scalars(select(DevWorkerHeartbeat))).all()
            dead = await s.scalar(text("SELECT count(*) FROM scheduled_jobs WHERE status = 'dead'"))
        if workers:
            for w in workers:
                age = int((db_now - w.last_seen_at).total_seconds())
                report.add(f"dev worker:{w.worker_id}", OK if age < 600 else WARN, f"last seen {age}s ago")
        else:
            report.add("dev worker", SKIP if not settings.dev_worker_token else WARN, "never connected")
        report.add("dead jobs", OK if not dead else WARN, f"{dead}")
        flags = {
            "ALLOW_LIVE_CALL_TESTS": settings.allow_live_call_tests,
            "ALLOW_LIVE_CLAUDE_TESTS": settings.allow_live_claude_tests,
            "ALLOW_LIVE_PURCHASES": settings.allow_live_purchases,
        }
        report.add(
            "safety flags",
            WARN if any(flags.values()) else OK,
            ", ".join(f"{k}={'true' if v else 'false'}" for k, v in flags.items()),
        )
        report.add("shopping providers", OK, "wolt/bolt: AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE; manual import: OK")
    finally:
        await container.aclose()
    print(report.render())
    return 1 if report.failed else 0
