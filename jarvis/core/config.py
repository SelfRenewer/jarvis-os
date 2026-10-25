"""Application settings.

All configuration comes from environment variables (optionally a local `.env`). Secrets are `SecretStr`
so they never appear in `repr()`/logs. Feature availability is derived from which credentials exist.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Env(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class CalendarStrategy(StrEnum):
    NATIVE = "native"  # Strategy A: Todoist's own calendar integration renders tasks
    MIRROR = "mirror"  # Strategy B: Jarvis mirrors Todoist -> Google (one-way)


class AutoApplyPolicy(StrEnum):
    NEVER = "never"
    SMALL = "small"
    ALWAYS = "always"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- runtime -------------------------------------------------------------------------------
    jarvis_env: Env = Env.DEV
    log_level: str = "INFO"
    log_json: bool = True
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:55432/jarvis"
    jarvis_api_token: SecretStr | None = None  # bearer token for /api/* admin endpoints
    jarvis_public_base_url: str | None = None  # https://jarvis.example.com (enables webhooks)
    jarvis_encryption_key: SecretStr | None = None  # Fernet key for OAuth tokens at rest
    worker_id: str = "worker-1"
    worker_concurrency: int = 4
    worker_poll_interval_seconds: float = 2.0
    job_lease_seconds: int = 300

    # --- user ----------------------------------------------------------------------------------
    default_user_timezone: str = "Asia/Tbilisi"
    default_user_name: str = "Алекс"
    default_origin: str | None = None
    working_hours_start: str = "09:00"
    working_hours_end: str = "21:00"
    working_days: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    sleep_start: str = "23:30"
    sleep_end: str = "07:30"

    # --- Todoist -------------------------------------------------------------------------------
    todoist_api_token: SecretStr | None = None
    todoist_client_id: str | None = None
    todoist_client_secret: SecretStr | None = None
    # No public webhook yet → polling. Every 2 min keeps completions made in the Todoist app / ChatGPT visible
    # quickly (1 API request per run; Todoist allows ~1000 per 15 min).
    todoist_reconcile_minutes: int = 2

    # --- Google --------------------------------------------------------------------------------
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Secret iCal address of the primary calendar: read-only, no OAuth / Cloud project / 7-day expiry. Wins over OAuth.
    google_calendar_ics_url: SecretStr | None = None
    google_calendar_ids: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["primary"])
    google_todoist_calendar_id: str | None = None
    google_mirror_calendar_id: str | None = None
    google_channel_token: SecretStr | None = None
    calendar_strategy: CalendarStrategy = CalendarStrategy.NATIVE
    calendar_sync_days: int = 14
    calendar_reconcile_minutes: int = 5

    # --- Maps ----------------------------------------------------------------------------------
    # auto: Google if GOOGLE_MAPS_API_KEY (live traffic, needs a billing account), else OpenRouteService if
    # OPENROUTESERVICE_API_KEY (free, OpenStreetMap data, no live traffic), else no travel time.
    maps_provider: str = "auto"  # auto | google | openrouteservice
    openrouteservice_api_key: SecretStr | None = None
    # Addresses → coordinates through OpenRouteService (Pelias): routing there and the 2GIS route button.
    maps_geocode_country: str = "GE"  # ISO 3166 code the search is limited to; empty = anywhere
    maps_geocode_max_requests_per_day: int = 100  # OpenRouteService free: 1,000/day; results are cached for 30 days
    # «🧭 Маршрут в 2ГИС» opens https://2gis.ge/<city>/directions/points/… (the 2GIS app picks it up on the phone).
    maps_2gis_base_url: str = "https://2gis.ge/tbilisi"
    google_maps_api_key: SecretStr | None = None
    maps_travel_mode: str = "DRIVE"  # DRIVE | WALK | BICYCLE | TRANSIT | TWO_WHEELER (two-wheeler: Enterprise SKU)
    # DRIVE/TWO_WHEELER with live traffic = Compute Routes Pro (5,000 free requests a month); off = Essentials (10,000).
    maps_traffic_aware: bool = True
    # Routing requests per day, for the active provider. Google: 31 × 150 < 5,000 — a month of the cap stays inside
    # the free Pro usage. OpenRouteService (free): 2,000/day.
    maps_max_requests_per_day: int = 150
    use_previous_event_location_as_origin: bool = True
    # A location shared in Telegram is the starting point for this long; a live location while Telegram keeps
    # updating it (and at most this long after its last update). «Я дома» → DEFAULT_ORIGIN again.
    location_origin_ttl_minutes: int = 120

    # --- Telegram ------------------------------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_webhook_secret: SecretStr | None = None
    telegram_mode: str = "polling"  # polling | webhook

    # --- Anthropic -----------------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "low"
    anthropic_enable_fallbacks: bool = True
    anthropic_timeout_seconds: float = 60.0
    llm_max_calls_per_day: int = 500

    # --- LLM provider selection ------------------------------------------------------------------
    llm_provider: str = "auto"  # auto | anthropic | openrouter  (auto: OpenRouter if its key is set)
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "anthropic/claude-opus-5"
    # Reasoning depth sent as `reasoning.effort` (none|minimal|low|medium|high; empty = provider default).
    # Low keeps chat replies fast; Gemini 3 maps minimal/low → thinkingLevel "low".
    openrouter_reasoning_effort: str = "low"
    # Per-task models, e.g. "intent_router=google/gemini-3.8-flash,spec_writer=anthropic/claude-opus-5"
    openrouter_models: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # A chat message waits at most this long for the LLM router, then is answered without it (R27; 0 = no limit).
    llm_router_timeout_seconds: float = 8.0
    # The intent router (chat and calls) runs on a faster model without reasoning — the user's choice 14.10, measured
    # on the real router prompt: gemini-3.5-flash-lite 1.5 s median / 1.7 s max, same intents; gemini-3.8-flash 4 s
    # median, up to 40 s. Other LLM tasks keep OPENROUTER_MODEL; OPENROUTER_MODELS still overrides per task.
    router_model: str = "google/gemini-3.5-flash-lite"
    call_router_timeout_seconds: float = 6.0
    agent_model: str = "google/gemini-3.5-flash-lite"  # questions (intent ask); calls use router_model
    system_site_port: int = 39110  # Jarvis OS (graph + lab) served by desktop/system-site/server.mjs on this Mac
    # Jarvis OS as seen from the containers: it keeps TLT running (16.10) and starts it when Jarvis finds it down.
    system_site_url: str = "http://host.docker.internal:39110"
    # The CRM («Воронка») on its own server since 24.10: «открой CRM» opens it, Jarvis OS's menu goes there too.
    crm_url: str = "https://crm.example.com/"

    # --- Embeddings / STT ----------------------------------------------------------------------
    # Search by meaning: auto = Voyage if its key, else OpenRouter if its key, else local hashing (words only).
    embeddings_provider: str = "auto"  # auto | openrouter | voyage | hashing
    # Must return 1024-d vectors (the `vector(1024)` column): baai/bge-m3 (default), intfloat/multilingual-e5-large…
    openrouter_embedding_model: str = "baai/bge-m3"
    embeddings_max_calls_per_day: int = 300  # one call embeds a batch of texts (backfill) or one search query
    embeddings_batch_size: int = 64
    embeddings_backfill_minutes: int = 10  # missing/changed vectors are embedded by the periodic backfill
    search_min_similarity: float = 0.55  # cosine floor for a hit found only by meaning (model-specific)
    search_similarity_margin: float = 0.12  # …and at most this far below the best hit by meaning
    search_embed_timeout_seconds: float = 4.0  # a search waits at most this long for the query vector
    voyage_api_key: SecretStr | None = None
    voyage_model: str = "voyage-4-lite"
    deepgram_api_key: SecretStr | None = None
    deepgram_model: str = "nova-3"

    # --- Weather (Open-Meteo: free, no key) ------------------------------------------------------
    weather_enabled: bool = True
    weather_place: str = "Тбилиси"
    weather_latitude: float = 41.7151
    weather_longitude: float = 44.8271
    weather_alerts_enabled: bool = True  # "rain starts in ~2 h" style messages (not during sleep hours)
    weather_alerts_per_day: int = 4

    # --- TLT / TPL tracker (LMF app on the Mac: ~/Sites/TLT; from Docker via host.docker.internal) ----
    lmf_enabled: bool = True
    lmf_base_url: str = "http://host.docker.internal:4321"
    # LMF's machine token (LMF writes it to app/data/api-token; `jarvis lmf-setup` copies it here). Sent as
    # `Authorization: Bearer`; empty = no header (an LMF without a password still answers).
    lmf_api_token: SecretStr | None = None
    tracker_reminders_enabled: bool = True  # 08:00 «как спалось?», 21:00 what is missing, 23:00 empty-day nudge
    tracker_nudge_at: str = "23:00"
    tracker_day_starts_at: str = "04:00"  # a report sent before this is about yesterday

    # --- LocalSites (its «Пульт» API on the Mac; loopback needs no password) ---------------------------------
    localsites_enabled: bool = True
    localsites_url: str = "http://host.docker.internal:7788"
    sites_tunnel_hours: float = 3.0  # a tunnel opened from chat closes by itself after this
    mac_notify_reminders: bool = True  # reminders also pop up on the Mac (its small window) while it is online
    # --- Researcher (web search through OpenRouter; every search only after a price button) ---------------------
    research_enabled: bool = True
    research_model: str = ""  # empty = OPENROUTER_MODEL
    research_max_results: int = 5
    research_max_per_day: int = 10
    research_price_hint: str = "≈ $0.01–0.03"
    finance_default_currency: str = "GEL"  # «потратил 45 на такси» without a currency; totals are shown in it
    lmf_site_name: str = "TLT"  # the LocalSites site that runs LMF: started by Jarvis when LMF does not answer

    # --- Watch and scale (Health Connect on the phone → Life Dashboard Companion → POST /webhooks/health) -----
    # The `health` service listens on the home network only for this endpoint; requests are HMAC-signed with the
    # secret the phone got from the pairing QR code (`jarvis health-pair`). Without the secret it refuses all.
    health_webhook_secret: SecretStr | None = None
    health_notify_sleep: bool = True  # a watch sleep session → «🛌 … ✅ Записать в TLT» once a day
    health_notify_weight: bool = True  # a new weighing → «⚖️ … ✅ Записать в TLT»
    health_min_sleep_hours: float = 3.0  # shorter sessions are naps: kept, never offered to TLT
    # Weighings are offered to TLT, compared and shown only from these Health Connect sources (the scale through
    # openScale sync); Google Fit also writes the weight of its profile. Everything is still stored. Empty = any.
    health_weight_sources: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["com.health.openscale.sync"])
    health_height_cm: float | None = None  # BMI when Health Connect has no height (openScale does not export it)

    # --- Currency rates (open.er-api.com + CoinGecko: free, no key; the same as currency-tracker) --------
    fx_enabled: bool = True
    # Shown when the target is not named («сколько 100 долларов?», «курс лари») — the currency-tracker set.
    fx_default_currencies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RUB", "USD", "AMD", "EUR", "GEL", "TRY", "QRL"]
    )

    # --- Chat manner and menu ---------------------------------------------------------------------
    persona_enabled: bool = True  # butler manner: «Да, сэр», greetings by time of day
    persona_address: str = "сэр"
    menu_keyboard_enabled: bool = True  # persistent reply keyboard with the main commands

    # --- Voice replies (text-to-speech via OpenRouter, same key as the LLM) ---------------------
    # Jarvis answers the user's messages with a voice note + the same text (toggle in chat: /voice on|off).
    voice_replies_enabled: bool = True  # Telegram: conversation/idea replies as voice notes (/voice on|off)
    mac_voice_replies_enabled: bool = True  # Jarvis for Mac: EVERY reply spoken (own toggle, separate from Telegram)
    # The user's pick 14.10 from six samples: Gemini Charon in an «AI butler» manner (was Grok Rex). Calls use the
    # same voice on the lite model: 1.4–2 s per short sentence instead of 2.3–3.9 s (measured).
    tts_model: str = "google/gemini-3.8-flash-tts"
    tts_voice: str = "Charon"
    call_tts_model: str = "google/gemini-3.8-flash-lite-tts"
    # Delivery note, used only by models that understand inline style tags (Gemini TTS), e.g.
    # "[calm, composed, refined British butler manner, dry subtle irony]".
    # In Russian: the English manner gave Charon an American accent (the user, 16.10); this one sounds native.
    tts_style: str = (
        "Говори как носитель русского языка: чистое московское произношение, без иностранного акцента. "
        "Спокойный, точный, вежливый ИИ-дворецкий, как Джарвис. Размеренный темп."
    )
    tts_max_chars: int = 700  # spoken part of one reply; the full text is always sent as well
    tts_max_calls_per_day: int = 300
    # Speech-to-text for incoming voice notes: Deepgram if its key is set, otherwise OpenRouter (Whisper).
    openrouter_stt_model: str = "openai/whisper-large-v3-turbo"
    stt_max_calls_per_day: int = 300

    # --- Voice ---------------------------------------------------------------------------------
    voice_calls_enabled: bool = False
    user_phone_number: str | None = None
    vapi_api_key: SecretStr | None = None
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    vapi_webhook_secret: SecretStr | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_phone_number: str | None = None
    max_calls_per_day: int = 6
    # Telegram calls (2026-10-14 pilot): a second Telegram account (the user's Georgian number) rings the user's own
    # account through py-tgcalls in the `caller` service; credentials come from `jarvis caller-login`.
    telegram_calls_enabled: bool = False
    telegram_caller_api_id: int | None = None
    telegram_caller_api_hash: SecretStr | None = None
    telegram_caller_session: SecretStr | None = None  # Telethon StringSession: full access to that account
    telegram_call_at_repeat: int = 2  # the persistent-reminder repeat that rings (1 = 3 min, 2 = 8 min); 0 = never
    telegram_call_ring_seconds: int = 45

    # --- Reminders -----------------------------------------------------------------------------
    reminder_offsets_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [30, 8, 0])
    # Persistent reminders (2026-10-14, instead of calls): repeat until «👌 Понял», minutes after the first message
    reminder_escalation_enabled: bool = True
    reminder_repeat_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [3, 8, 15])
    reminder_escalation_voice: bool = True  # the first repeat also comes as a voice note
    reminder_call_offsets_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [0])
    reminder_task_offsets_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [0])
    reminder_travel_buffer_minutes: int = 10
    reminder_horizon_hours: int = 36
    call_grace_seconds: int = 300
    telegram_grace_seconds: int = 1800
    travel_refresh_lead_minutes: int = 40

    # --- Planner -------------------------------------------------------------------------------
    planner_auto_apply: AutoApplyPolicy = AutoApplyPolicy.NEVER
    planner_small_change_max_tasks: int = 2
    planner_default_duration_minutes: int = 30
    planner_min_break_minutes: int = 10
    planner_horizon_days: int = 3
    planner_slot_granularity_minutes: int = 15
    planner_unknown_travel_minutes: int = 30  # blocked before events with an address if Maps is unknown

    # --- Briefings -----------------------------------------------------------------------------
    # Morning briefing (includes the daily plan proposal) and evening review, local wall-clock time.
    # A briefing whose slot is older than the grace window (Mac asleep, restart) is skipped, not sent late.
    briefing_morning_enabled: bool = True
    briefing_morning_at: str = "08:00"
    briefing_evening_enabled: bool = True
    briefing_evening_at: str = "21:00"
    briefing_grace_minutes: int = 180

    # --- Dev worker ----------------------------------------------------------------------------
    dev_worker_token: SecretStr | None = None
    dev_worker_signing_key: SecretStr | None = None
    dev_repo_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Shopping ------------------------------------------------------------------------------
    shopping_currency: str = "GEL"
    shopping_extra_delivery_penalty: str = "3.00"
    shopping_substitution_penalty: str = "1.50"
    shopping_min_match_confidence: float = 0.8

    # --- Safety flags --------------------------------------------------------------------------
    allow_live_call_tests: bool = False
    allow_live_claude_tests: bool = False
    allow_live_purchases: bool = False

    @field_validator(
        "working_days",
        "google_calendar_ids",
        "telegram_allowed_user_ids",
        "reminder_offsets_minutes",
        "reminder_repeat_minutes",
        "reminder_call_offsets_minutes",
        "reminder_task_offsets_minutes",
        "dev_repo_ids",
        "health_weight_sources",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("health_height_cm", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value  # `HEALTH_HEIGHT_CM=` in .env

    @field_validator("crm_url")
    @classmethod
    def _https_crm(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("CRM_URL must start with https://")
        return value

    @field_validator("fx_default_currencies", mode="before")
    @classmethod
    def _currency_codes(cls, value: object) -> object:
        value = _split_csv(value)
        return [str(code).upper() for code in value] if isinstance(value, list) else value

    @field_validator("openrouter_models", mode="before")
    @classmethod
    def _model_map(cls, value: object) -> object:
        if isinstance(value, str):
            mapping: dict[str, str] = {}
            for part in value.split(","):
                if not part.strip():
                    continue
                capability, sep, model = part.partition("=")
                if not sep or not capability.strip() or not model.strip():
                    raise ValueError(f"OPENROUTER_MODELS entry must be capability=model, got {part!r}")
                mapping[capability.strip()] = model.strip()
            return mapping
        return value

    @field_validator("maps_provider")
    @classmethod
    def _maps_provider(cls, value: str) -> str:
        choice = value.strip().lower()
        if choice not in ("auto", "google", "openrouteservice"):
            raise ValueError("MAPS_PROVIDER must be auto, google or openrouteservice")
        return choice

    @field_validator("default_user_timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.default_user_timezone)

    @property
    def is_test(self) -> bool:
        return self.jarvis_env == Env.TEST

    def secret_values(self) -> list[str]:
        """All configured secret strings (used by the log redaction filter)."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw and len(raw) >= 6:
                    values.append(raw)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
