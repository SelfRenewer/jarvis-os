"""Shared fixtures.

* Normal tests never reach the network: an autouse guard blocks non-loopback sockets.
* DB tests run against a real PostgreSQL (`TEST_DATABASE_URL`, default: compose Postgres on :55432,
  database `jarvis_test`). The schema is built once per session with Alembic (validating migrations)
  and every DB test starts from truncated tables.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jarvis.core.clock import FrozenClock
from jarvis.core.config import Settings
from jarvis.db.session import Database

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://jarvis_test:jarvis_test@localhost:55432/jarvis_test"
)
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}  # noqa: S104


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> object:
        if isinstance(address, tuple) and address and address[0] not in _ALLOWED_HOSTS:
            raise RuntimeError(f"external network access blocked in tests: {address[0]}")
        return original_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if isinstance(host, str) and host not in _ALLOWED_HOSTS:
            raise RuntimeError(f"DNS lookup blocked in tests: {host}")
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "jarvis_env": "test",
        "database_url": TEST_DATABASE_URL,
        "log_json": False,
        "telegram_allowed_user_ids": "1001",
        "default_origin": "Rustaveli Ave 1, Tbilisi",
        "user_phone_number": "+15555550111",
        "voice_calls_enabled": True,
        "dev_worker_token": "test-worker-token-123",
        "dev_worker_signing_key": "test-signing-key-456",
        "dev_repo_ids": "golden-app",
        "todoist_client_secret": "todoist-client-secret",
        "telegram_webhook_secret": "tg-secret-token",
        "vapi_webhook_secret": "vapi-secret-123",  # gitleaks:allow (test value)
        "google_channel_token": "gcal-channel-token",
        "jarvis_api_token": "admin-token-123456",
        "voice_replies_enabled": False,  # voice replies have their own tests (tests/integration/test_voice.py)
        "persona_enabled": False,  # exact reply texts in older tests; the manner has its own tests
        "menu_keyboard_enabled": False,  # the one-time keyboard message would change message counts
        "system_site_url": "",  # Jarvis OS (wakes TLT) is not called in tests unless a test mocks it
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def clock() -> FrozenClock:
    # 2026-09-23 09:00 Asia/Tbilisi == 05:00 UTC
    return FrozenClock(datetime(2026, 9, 23, 5, 0, tzinfo=UTC))


_schema_ready = False


async def _prepare_schema(db: Database) -> None:
    global _schema_ready
    if _schema_ready:
        return
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    async with db.engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    def _upgrade(sync_conn: object) -> None:
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "head")

    async with db.engine.begin() as conn:
        await conn.run_sync(_upgrade)
    _schema_ready = True


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database.from_url(TEST_DATABASE_URL, pool_size=10)
    try:
        await _prepare_schema(database)
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL: {exc}")
    async with database.engine.begin() as conn:
        tables = (
            (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")
                )
            )
            .scalars()
            .all()
        )
        if tables:
            await conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    yield database
    await database.dispose()


@pytest.fixture
async def container(db: Database, clock: FrozenClock, settings: Settings) -> object:
    from jarvis.services.container import build_container

    return build_container(settings, db=db, clock=clock)


@pytest.fixture
def worker(container: object, db: Database, clock: FrozenClock) -> object:
    from jarvis.core.retry import RetryPolicy
    from jarvis.jobs.handlers import build_registry
    from jarvis.scheduler.queue import JobQueue
    from jarvis.scheduler.runner import Worker

    queue = JobQueue(db, clock, worker_id="test-worker", retry=RetryPolicy(10, 60, 3, 0))
    return Worker(queue, build_registry(), container, concurrency=4)  # type: ignore[arg-type]
