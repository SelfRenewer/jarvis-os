"""Dependency wiring.

* `JARVIS_ENV=test` → every external adapter is a fake (no network, no cost), regardless of keys.
* otherwise → a real adapter only when its credentials exist; missing credentials yield an explicit
  "not configured" adapter so the rest of the system keeps working.
Live phone calls additionally require VOICE_CALLS_ENABLED=true (and never happen in test mode).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from jarvis.agenda.service import AgendaService
from jarvis.calc.currency import FxService
from jarvis.core.budget import consume
from jarvis.core.clock import Clock, SystemClock
from jarvis.core.config import Settings
from jarvis.core.logging import get_correlation_id
from jarvis.core.security import CallbackSigner, TokenCipher
from jarvis.db.models import AgentRun, IntegrationAccount
from jarvis.db.session import Database
from jarvis.desktop.service import DesktopService
from jarvis.development.service import DevelopmentService
from jarvis.finance.service import FinanceService
from jarvis.health.service import HealthService
from jarvis.ideas.service import IdeaService
from jarvis.inbox.service import NoteService
from jarvis.integrations.embeddings.client import (
    EmbeddingProvider,
    HashingEmbeddings,
    OpenRouterEmbeddings,
    VoyageEmbeddings,
    embeddings_provider_name,
)
from jarvis.integrations.fx.client import FakeRates, HttpRates, RatesApi
from jarvis.integrations.google_calendar.client import CalendarApi, FakeCalendarApi, HttpCalendarApi
from jarvis.integrations.google_calendar.ics import IcsCalendarApi
from jarvis.integrations.llm.client import AnthropicLLM, FakeLLM, LLMClient, LLMRunRecord, OpenRouterLLM
from jarvis.integrations.lmf.client import FakeLifeTracker, HttpLifeTracker, LifeTrackerApi
from jarvis.integrations.localsites.client import FakeSites, HttpSites, SitesApi
from jarvis.integrations.maps.provider import build_maps
from jarvis.integrations.maps.routes import FakeGeocoder, FakeRoutesApi, Geocoder, RoutesApi
from jarvis.integrations.stt.client import DeepgramSTT, FakeSTT, OpenRouterSTT, SpeechToText, UnavailableSTT
from jarvis.integrations.telegram.client import FakeTelegramApi, HttpTelegramApi, TelegramApi
from jarvis.integrations.todoist.client import FakeTodoistApi, HttpTodoistApi, TodoistApi
from jarvis.integrations.tts.client import FakeTTS, OpenRouterTTS, TextToSpeech, UnavailableTTS
from jarvis.integrations.voice.providers import (
    CallProvider,
    DisabledCallProvider,
    FakeCallProvider,
    TwilioCallProvider,
    VapiCallProvider,
)
from jarvis.integrations.weather.client import FakeWeather, OpenMeteoWeather, WeatherApi
from jarvis.meds.service import MedService
from jarvis.notify.service import Notifier
from jarvis.planner.service import PlannerService
from jarvis.reminders.service import ReminderService
from jarvis.reminders.travel import TravelService
from jarvis.research.service import FakeWebSearcher, ResearchService, WebSearcher
from jarvis.search.service import SearchService
from jarvis.shopping.service import ShoppingService
from jarvis.sites.service import SitesService
from jarvis.sync.calendar_sync import CalendarSync
from jarvis.sync.todoist_sync import TodoistSync
from jarvis.tasks.service import TaskOpsService
from jarvis.tracker.service import TrackerService
from jarvis.voice.service import CallService
from jarvis.weather.service import WeatherService

log = logging.getLogger(__name__)


class DbTokenStore:
    """Google refresh token, encrypted at rest with JARVIS_ENCRYPTION_KEY."""

    def __init__(self, db: Database, cipher: TokenCipher | None, clock: Clock) -> None:
        self.db = db
        self.cipher = cipher
        self.clock = clock

    async def load_refresh_token(self) -> str | None:
        if self.cipher is None:
            return None
        async with self.db.session() as session:
            account = await session.scalar(select(IntegrationAccount).where(IntegrationAccount.provider == "google"))
            if account is None or account.status != "active" or not account.encrypted_credentials:
                return None
            return self.cipher.decrypt(account.encrypted_credentials)

    async def save_refresh_token(self, token: str, scopes: str) -> None:
        if self.cipher is None:
            raise RuntimeError("JARVIS_ENCRYPTION_KEY is required to store OAuth tokens")
        async with self.db.session() as session:
            account = await session.scalar(select(IntegrationAccount).where(IntegrationAccount.provider == "google"))
            if account is None:
                account = IntegrationAccount(provider="google")
                session.add(account)
            account.encrypted_credentials = self.cipher.encrypt(token)
            account.scopes = scopes
            account.status = "active"
            account.meta = {"authorized_at": self.clock.now().isoformat()}

    async def mark_needs_reauth(self, reason: str) -> None:
        async with self.db.session() as session:
            account = await session.scalar(select(IntegrationAccount).where(IntegrationAccount.provider == "google"))
            if account is not None:
                account.status = "needs_reauth"
                account.meta = {**(account.meta or {}), "reason": reason}


@dataclass
class Container:
    settings: Settings
    db: Database
    clock: Clock
    telegram: TelegramApi | None
    todoist: TodoistApi | None
    calendar: CalendarApi | None
    routes: RoutesApi
    llm: LLMClient | None
    stt: SpeechToText
    embeddings: EmbeddingProvider
    call_provider: CallProvider
    status: dict[str, str] = field(default_factory=dict)
    tts: TextToSpeech = field(default_factory=UnavailableTTS)
    weather_api: WeatherApi | None = None
    fx_api: RatesApi | None = None
    lmf_api: LifeTrackerApi | None = None
    sites_api: SitesApi | None = None
    web: WebSearcher | None = None
    callback_signer: CallbackSigner | None = None
    token_store: DbTokenStore | None = None
    geocoder: Geocoder | None = None  # addresses → coordinates (OpenRouteService): routing there + the 2GIS button
    _closables: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        chat_id = self.settings.telegram_allowed_user_ids[0] if self.settings.telegram_allowed_user_ids else None
        self.notifier = Notifier(self.db, self.telegram, self.clock, chat_id)
        self.call_service = CallService(self.db, self.call_provider, self.notifier, self.settings, self.clock)
        self.todoist_sync = TodoistSync(self.todoist, self.settings.tz) if self.todoist else None
        self.calendar_sync = CalendarSync(self.calendar, self.settings) if self.calendar else None
        self.planner = PlannerService(self.settings)
        self.travel = TravelService(self.routes, self.settings, self.geocoder)
        self.reminders = ReminderService(self.settings, self.travel)
        signing = self.settings.dev_worker_signing_key
        self.development = DevelopmentService(
            self.settings.dev_repo_ids, signing.get_secret_value() if signing else None
        )
        self.shopping = ShoppingService(self.settings, self.llm)
        self.agenda = AgendaService(self.settings)
        self.task_ops = TaskOpsService(self.settings)
        self.notes = NoteService()
        self.search = SearchService(self.settings, self.embeddings, task_ops=self.task_ops, notes=self.notes)
        self.ideas = IdeaService(self.llm, self.search)
        self.weather = WeatherService(self.settings, self.weather_api)
        self.fx = FxService(self.settings, self.fx_api)
        self.tracker = TrackerService(self.settings, self.lmf_api, self.llm)
        self.finance = FinanceService(self.settings, self.fx)
        self.health = HealthService(self.settings, self.clock)
        self.research = ResearchService(self.settings, self.web)
        self.sites = SitesService(self.settings, self.sites_api)
        self.meds = MedService(self.settings)
        self.desktop = DesktopService()

    async def aclose(self) -> None:
        for item in self._closables:
            try:
                await item.aclose()
            except Exception:  # pragma: no cover
                log.exception("close failed")
        await self.db.dispose()


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    raw = value.get_secret_value()
    return raw or None


def choose_embeddings(settings: Settings, budget: Any = None) -> tuple[EmbeddingProvider, str]:
    """EMBEDDINGS_PROVIDER → (provider, status). Hashing is lexical: search then works by words only."""
    name = embeddings_provider_name(settings)
    batch = settings.embeddings_batch_size
    if name == "voyage":
        key = _secret(settings.voyage_api_key)
        assert key is not None
        return (
            VoyageEmbeddings(key, settings.voyage_model, batch_size=batch, budget=budget),
            f"CONFIGURED (voyage: {settings.voyage_model})",
        )
    if name == "openrouter":
        key = _secret(settings.openrouter_api_key)
        assert key is not None
        model = settings.openrouter_embedding_model
        return (
            OpenRouterEmbeddings(key, model, batch_size=batch, budget=budget),
            f"CONFIGURED (openrouter: {model})",
        )
    if settings.embeddings_provider.strip().lower() == "hashing":
        return HashingEmbeddings(), "LOCAL_FALLBACK (EMBEDDINGS_PROVIDER=hashing: search by words only)"
    wanted = {"voyage": "VOYAGE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(
        settings.embeddings_provider.strip().lower(), "OPENROUTER_API_KEY or VOYAGE_API_KEY"
    )
    return HashingEmbeddings(), f"LOCAL_FALLBACK (search by words only; set {wanted} for search by meaning)"


def build_container(settings: Settings, *, db: Database | None = None, clock: Clock | None = None) -> Container:
    database = db or Database.from_url(settings.database_url)
    clk = clock or SystemClock()
    if settings.is_test:
        return Container(
            settings=settings,
            db=database,
            clock=clk,
            telegram=FakeTelegramApi(),
            todoist=FakeTodoistApi(),
            calendar=FakeCalendarApi(),
            routes=FakeRoutesApi(),
            geocoder=FakeGeocoder(),
            llm=FakeLLM(),
            stt=FakeSTT(),
            embeddings=HashingEmbeddings(),
            call_provider=FakeCallProvider(),
            tts=FakeTTS(),
            weather_api=FakeWeather(),
            fx_api=FakeRates(),
            lmf_api=FakeLifeTracker(),
            sites_api=FakeSites(),
            web=FakeWebSearcher(),
            status={
                name: "IMPLEMENTED_MOCK_TESTED"
                for name in ("telegram", "todoist", "google_calendar", "maps", "llm", "stt", "tts", "voice")
            },
        )
    status: dict[str, str] = {}
    closables: list[Any] = []

    telegram: TelegramApi | None = None
    callback_signer: CallbackSigner | None = None
    if token := _secret(settings.telegram_bot_token):
        # Inline buttons are signed with a key derived from the bot token (secret, always present for the bot).
        callback_signer = CallbackSigner(token)
        telegram = HttpTelegramApi(token, signer=callback_signer)
        closables.append(telegram)
        status["telegram"] = "CONFIGURED"
    else:
        status["telegram"] = "IMPLEMENTED_NEEDS_CREDENTIALS"

    todoist: TodoistApi | None = None
    if token := _secret(settings.todoist_api_token):
        todoist = HttpTodoistApi(token)
        closables.append(todoist)
        status["todoist"] = "CONFIGURED"
    else:
        status["todoist"] = "IMPLEMENTED_NEEDS_CREDENTIALS"

    cipher = TokenCipher(key) if (key := _secret(settings.jarvis_encryption_key)) else None
    token_store = DbTokenStore(database, cipher, clk)
    calendar: CalendarApi | None = None
    if ics_url := _secret(settings.google_calendar_ics_url):
        calendar = IcsCalendarApi(ics_url, settings.tz)
        closables.append(calendar)
        status["google_calendar"] = "CONFIGURED (iCal secret address, read-only)"
    elif settings.google_client_id and (secret := _secret(settings.google_client_secret)) and cipher:
        calendar = HttpCalendarApi(settings.google_client_id, secret, token_store)
        closables.append(calendar)
        status["google_calendar"] = "CONFIGURED (authorization checked by doctor)"
    else:
        status["google_calendar"] = "IMPLEMENTED_NEEDS_CREDENTIALS"

    maps = build_maps(settings)  # MAPS_PROVIDER: Google / OpenRouteService / none
    routes: RoutesApi = maps.routes
    closables.extend(maps.closables)
    status["maps"] = maps.status

    async def record_run(record: LLMRunRecord) -> None:
        async with database.session() as session:
            session.add(
                AgentRun(
                    capability=record.capability,
                    model=record.model,
                    status=record.status,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    latency_ms=record.latency_ms,
                    attempts=record.attempts,
                    error=record.error,
                    correlation_id=get_correlation_id(),
                )
            )

    async def llm_budget() -> None:
        async with database.session() as session:
            await consume(session, "llm", clk.now(), settings.llm_max_calls_per_day)

    llm: LLMClient | None = None
    web: WebSearcher | None = None
    provider = settings.llm_provider.strip().lower()
    openrouter_key = _secret(settings.openrouter_api_key)
    anthropic_key = _secret(settings.anthropic_api_key)
    if provider not in ("auto", "anthropic", "openrouter"):
        raise ValueError("LLM_PROVIDER must be auto, anthropic or openrouter")
    use_openrouter = bool(openrouter_key) and provider in ("auto", "openrouter")
    if use_openrouter:
        assert openrouter_key is not None
        openrouter_llm = OpenRouterLLM(
            openrouter_key,
            settings.openrouter_model,
            models={
                "intent_router": settings.router_model,
                "intent_router_call": settings.router_model,
                "agent": settings.agent_model,
                **settings.openrouter_models,
            },
            reasoning_effort=settings.openrouter_reasoning_effort,
            no_reasoning=frozenset({"intent_router", "intent_router_call", "agent"}),
            timeout=settings.anthropic_timeout_seconds,
            on_run=record_run,
            budget=llm_budget,
        )
        closables.append(openrouter_llm)
        llm = openrouter_llm
        web = openrouter_llm
        overrides = f", per-task: {len(settings.openrouter_models)}" if settings.openrouter_models else ""
        status["llm"] = f"CONFIGURED (openrouter: {settings.openrouter_model}{overrides})"
    elif provider == "openrouter":
        status["llm"] = "IMPLEMENTED_NEEDS_CREDENTIALS (OPENROUTER_API_KEY; deterministic fallbacks active)"
    elif key := anthropic_key:
        anthropic_llm = AnthropicLLM(
            key,
            settings.anthropic_model,
            effort=settings.anthropic_effort,
            enable_fallbacks=settings.anthropic_enable_fallbacks,
            timeout=settings.anthropic_timeout_seconds,
            on_run=record_run,
            budget=llm_budget,
        )
        closables.append(anthropic_llm)
        llm = anthropic_llm
        status["llm"] = f"CONFIGURED (anthropic: {settings.anthropic_model})"
    else:
        status["llm"] = "IMPLEMENTED_NEEDS_CREDENTIALS (OPENROUTER_API_KEY or ANTHROPIC_API_KEY; fallbacks active)"

    def daily_budget(provider: str, limit: int) -> Any:
        async def hook() -> None:
            async with database.session() as session:
                await consume(session, provider, clk.now(), limit)

        return hook

    stt: SpeechToText
    if key := _secret(settings.deepgram_api_key):
        stt = DeepgramSTT(key, settings.deepgram_model)
        closables.append(stt)
        status["stt"] = "CONFIGURED (deepgram)"
    elif openrouter_key:
        stt = OpenRouterSTT(
            openrouter_key, settings.openrouter_stt_model, budget=daily_budget("stt", settings.stt_max_calls_per_day)
        )
        closables.append(stt)
        status["stt"] = f"CONFIGURED (openrouter: {settings.openrouter_stt_model})"
    else:
        stt = UnavailableSTT()
        status["stt"] = "IMPLEMENTED_NEEDS_CREDENTIALS"

    weather_api: WeatherApi | None = None
    if settings.weather_enabled:
        weather_api = OpenMeteoWeather()
        closables.append(weather_api)
        status["weather"] = f"CONFIGURED (open-meteo: {settings.weather_place})"
    else:
        status["weather"] = "DISABLED"

    sites_api: SitesApi | None = None
    if settings.localsites_enabled:
        sites_api = HttpSites(settings.localsites_url)
        closables.append(sites_api)
        status["localsites"] = f"CONFIGURED ({settings.localsites_url})"
    else:
        status["localsites"] = "DISABLED"

    lmf_api: LifeTrackerApi | None = None
    if settings.lmf_enabled:
        lmf_token = settings.lmf_api_token.get_secret_value() if settings.lmf_api_token else None
        lmf_api = HttpLifeTracker(settings.lmf_base_url, token=lmf_token)
        closables.append(lmf_api)
        status["lmf"] = f"CONFIGURED ({settings.lmf_base_url})"
    else:
        status["lmf"] = "DISABLED"

    fx_api: RatesApi | None = None
    if settings.fx_enabled:
        fx_api = HttpRates()
        closables.append(fx_api)
        status["fx"] = "CONFIGURED (open.er-api.com + CoinGecko)"
    else:
        status["fx"] = "DISABLED"

    tts: TextToSpeech
    if openrouter_key:
        tts = OpenRouterTTS(
            openrouter_key,
            settings.tts_model,
            settings.tts_voice,
            style=settings.tts_style,
            budget=daily_budget("tts", settings.tts_max_calls_per_day),
        )
        closables.append(tts)
        status["tts"] = f"CONFIGURED (openrouter: {settings.tts_model}, voice {settings.tts_voice})"
    else:
        tts = UnavailableTTS()
        status["tts"] = "IMPLEMENTED_NEEDS_CREDENTIALS (OPENROUTER_API_KEY)"

    embeddings, status["embeddings"] = choose_embeddings(
        settings, budget=daily_budget("embeddings", settings.embeddings_max_calls_per_day)
    )
    if hasattr(embeddings, "aclose"):
        closables.append(embeddings)

    live_calls = settings.voice_calls_enabled and not settings.is_test
    call_provider: CallProvider
    vapi_key = _secret(settings.vapi_api_key)
    if vapi_key and settings.vapi_assistant_id and settings.vapi_phone_number_id:
        call_provider = VapiCallProvider(
            vapi_key, settings.vapi_assistant_id, settings.vapi_phone_number_id, live_enabled=live_calls
        )
        closables.append(call_provider)
        status["voice"] = "CONFIGURED (vapi)" + ("" if live_calls else ", live calls disabled")
    elif settings.twilio_account_sid and (tok := _secret(settings.twilio_auth_token)) and settings.twilio_phone_number:
        callback = (
            settings.jarvis_public_base_url.rstrip("/") + "/webhooks/twilio/status"
            if settings.jarvis_public_base_url
            else None
        )
        call_provider = TwilioCallProvider(
            settings.twilio_account_sid,
            tok,
            settings.twilio_phone_number,
            live_enabled=live_calls,
            status_callback_url=callback,
        )
        closables.append(call_provider)
        status["voice"] = "CONFIGURED (twilio fallback)" + ("" if live_calls else ", live calls disabled")
    else:
        call_provider = DisabledCallProvider()
        status["voice"] = "IMPLEMENTED_NEEDS_CREDENTIALS"

    return Container(
        settings=settings,
        db=database,
        clock=clk,
        telegram=telegram,
        todoist=todoist,
        calendar=calendar,
        routes=routes,
        geocoder=maps.geocoder,
        llm=llm,
        stt=stt,
        embeddings=embeddings,
        call_provider=call_provider,
        status=status,
        tts=tts,
        weather_api=weather_api,
        fx_api=fx_api,
        lmf_api=lmf_api,
        sites_api=sites_api,
        web=web,
        callback_signer=callback_signer,
        token_store=token_store,
        _closables=closables,
    )
