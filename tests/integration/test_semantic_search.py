"""Search by meaning: OpenRouter embeddings adapter, backfill (per model, idempotent), hybrid «найди …», fallbacks.

No network: the adapter is tested with respx, everything else with `FakeEmbeddings` (synonyms are close).
Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from jarvis.bot.service import BotService
from jarvis.core.errors import AuthError, BudgetExceededError, PermanentError, TransientError
from jarvis.db.models import MemoryEmbedding, ScheduledJob, TodoistTask
from jarvis.integrations.embeddings.client import (
    FakeEmbeddings,
    HashingEmbeddings,
    OpenRouterEmbeddings,
    VoyageEmbeddings,
    embeddings_provider_name,
)
from jarvis.jobs.handlers import periodic_tasks
from jarvis.scheduler.queue import SKIPPED, enqueue
from jarvis.search.service import fuse
from jarvis.services.container import choose_embeddings
from tests.conftest import make_settings

URL = "https://openrouter.ai/api/v1/embeddings"
USER = 1001


def vec(first: float, dims: int = 1024) -> list[float]:
    return [first] + [0.0] * (dims - 1)


def ok(body: dict[str, Any], *, reverse: bool = False) -> httpx.Response:
    """OpenRouter-shaped response; `reverse` returns the items out of order (they carry `index`)."""
    indices = list(range(len(body["input"])))
    data = [{"object": "embedding", "index": i, "embedding": vec(float(i + 1))} for i in indices]
    return httpx.Response(
        200,
        json={
            "id": "embd-1",
            "object": "list",
            "data": list(reversed(data)) if reverse else data,
            "model": body["model"],
            "usage": {"prompt_tokens": 4, "total_tokens": 4, "cost": 0.00000004},
        },
    )


# --- OpenRouter adapter (respx) -----------------------------------------------------------------------------


@respx.mock
async def test_openrouter_embeddings_batches_budget_and_order() -> None:
    budget_calls: list[int] = []

    async def budget() -> None:
        budget_calls.append(1)

    emb = OpenRouterEmbeddings("or-key", "baai/bge-m3", batch_size=2, budget=budget)
    route = respx.post(URL).mock(side_effect=lambda request: ok(json.loads(request.content), reverse=True))
    vectors = await emb.embed(["стрижка", "барбер", "машина"], kind="document")
    assert len(route.calls) == 2 and len(budget_calls) == 2  # one budget unit per call, not per text
    first = json.loads(route.calls[0].request.content)
    assert first == {"model": "baai/bge-m3", "input": ["стрижка", "барбер"], "encoding_format": "float"}
    assert json.loads(route.calls[1].request.content)["input"] == ["машина"]
    assert route.calls[0].request.headers["authorization"] == "Bearer or-key"
    assert [v[0] for v in vectors] == [1.0, 2.0, 1.0]  # sorted by `index`, not by arrival
    assert all(len(v) == 1024 for v in vectors)
    assert emb.semantic and emb.dims == 1024
    await emb.aclose()


@respx.mock
async def test_openrouter_embeddings_errors() -> None:
    emb = OpenRouterEmbeddings("or-key", "baai/bge-m3")
    route = respx.post(URL)
    route.mock(return_value=httpx.Response(401, json={"error": {"code": 401, "message": "No auth"}}))
    with pytest.raises(AuthError):
        await emb.embed(["x"], kind="query")
    assert len(route.calls) == 1  # auth is never retried
    route.mock(return_value=httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}))
    with pytest.raises(PermanentError):
        await emb.embed(["x"], kind="query")
    route.mock(return_value=httpx.Response(200, json={"error": {"code": 503, "message": "No provider"}}))
    with pytest.raises(TransientError):
        await emb.embed(["x"], kind="query")
    route.mock(return_value=httpx.Response(200, json={"error": {"code": 400, "message": "bad model"}}))
    with pytest.raises(PermanentError):
        await emb.embed(["x"], kind="query")
    wrong_dims = {"object": "list", "model": "m", "data": [{"index": 0, "embedding": [0.1] * 768}]}
    route.mock(return_value=httpx.Response(200, json=wrong_dims))
    with pytest.raises(PermanentError, match="768-d"):
        await emb.embed(["x"], kind="query")
    count = {"object": "list", "model": "m", "data": []}
    route.mock(return_value=httpx.Response(200, json=count))
    with pytest.raises(PermanentError, match="count mismatch"):
        await emb.embed(["x"], kind="query")
    with pytest.raises(PermanentError, match="empty text"):
        await emb.embed(["  "], kind="document")
    await emb.aclose()


@respx.mock
async def test_openrouter_embeddings_retry_and_budget_stop() -> None:
    async def exhausted() -> None:
        raise BudgetExceededError("embeddings: daily budget 300 exhausted")

    route = respx.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "0"}), ok({"input": ["x"], "model": "m"})]
    )
    emb = OpenRouterEmbeddings("or-key", "baai/bge-m3")
    [vector] = await emb.embed(["x"], kind="query")
    assert vector[0] == 1.0 and len(route.calls) == 2  # rate limit → retried (embedding has no side effects)
    blocked = OpenRouterEmbeddings("or-key", "baai/bge-m3", budget=exhausted)
    with pytest.raises(BudgetExceededError):
        await blocked.embed(["y"], kind="query")
    assert len(route.calls) == 2  # no HTTP call over budget
    await emb.aclose()
    await blocked.aclose()


@respx.mock
async def test_e5_prefixes_and_probe() -> None:
    route = respx.post(URL).mock(side_effect=lambda request: ok(json.loads(request.content)))
    e5 = OpenRouterEmbeddings("or-key", "intfloat/multilingual-e5-large")
    await e5.embed(["стрижка"], kind="query")
    await e5.embed(["Записаться к барберу"], kind="document")
    assert json.loads(route.calls[0].request.content)["input"] == ["query: стрижка"]
    assert json.loads(route.calls[1].request.content)["input"] == ["passage: Записаться к барберу"]
    listing = {"data": [{"id": "baai/bge-m3", "context_length": 8192}, {"id": "openai/text-embedding-3-small"}]}
    respx.get("https://openrouter.ai/api/v1/embeddings/models").mock(return_value=httpx.Response(200, json=listing))
    assert "baai/bge-m3" in await OpenRouterEmbeddings("k", "baai/bge-m3").probe()
    with pytest.raises(PermanentError, match="not offered"):
        await e5.probe()
    await e5.aclose()


async def test_provider_selection() -> None:
    assert embeddings_provider_name(make_settings()) == "hashing"
    assert embeddings_provider_name(make_settings(openrouter_api_key="or")) == "openrouter"
    assert embeddings_provider_name(make_settings(openrouter_api_key="or", voyage_api_key="vo")) == "voyage"
    both = {"openrouter_api_key": "or", "voyage_api_key": "vo"}
    assert embeddings_provider_name(make_settings(embeddings_provider="openrouter", **both)) == "openrouter"
    assert embeddings_provider_name(make_settings(embeddings_provider="hashing", **both)) == "hashing"
    assert embeddings_provider_name(make_settings(embeddings_provider="voyage", openrouter_api_key="or")) == "hashing"
    with pytest.raises(ValueError, match="EMBEDDINGS_PROVIDER"):
        embeddings_provider_name(make_settings(embeddings_provider="cohere"))

    openrouter, status = choose_embeddings(make_settings(openrouter_api_key="or"))
    assert isinstance(openrouter, OpenRouterEmbeddings) and openrouter.model == "baai/bge-m3"
    assert status == "CONFIGURED (openrouter: baai/bge-m3)"  # /status: «поиск по смыслу» counts as connected
    await openrouter.aclose()
    voyage, status = choose_embeddings(make_settings(openrouter_api_key="or", voyage_api_key="vo"))
    assert isinstance(voyage, VoyageEmbeddings) and status == "CONFIGURED (voyage: voyage-4-lite)"
    await voyage.aclose()
    provider, status = choose_embeddings(make_settings())
    assert isinstance(provider, HashingEmbeddings) and not provider.semantic
    assert status.startswith("LOCAL_FALLBACK") and "OPENROUTER_API_KEY" in status

    assert "embeddings.backfill" in {t.name for t in periodic_tasks(make_settings(openrouter_api_key="or"))}
    assert "embeddings.backfill" not in {t.name for t in periodic_tasks(make_settings())}


def test_fuse_is_deterministic_and_lexical_first() -> None:
    items = {k: k for k in ("a", "b", "c", "d")}
    merged = fuse(["b"], [("c", 0.9), ("b", 0.8), ("d", 0.8)], key=str, resolve=items, tiebreak=lambda _x: "")
    assert merged == ["b", "c", "d"]  # in both lists > lexical only > vector only; equal similarity → by id
    assert fuse([], [("zz", 0.9)], key=str, resolve=items, tiebreak=lambda _x: "") == []  # unknown id dropped


# --- search over the database (FakeEmbeddings) ----------------------------------------------------------------


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


async def seed(c: Any) -> None:
    now = c.clock.now()
    c.todoist.add(id="BRB", content="Записаться к барберу")
    c.todoist.add(id="CIG", content="Купить сигару")
    c.todoist.add(id="DOC", content="Позвонить Анне", description="терапевт")
    c.calendar.add_event(
        "primary",
        summary="Шиномонтаж: сменить резину",
        start={"dateTime": iso(now + timedelta(days=1))},
        end={"dateTime": iso(now + timedelta(days=1, hours=1))},
    )
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, now)
        await c.calendar_sync.reconcile(s, now)
        await c.notes.save(s, text_value="Пароль от wifi", source="test")
        await c.shopping.add_items(s, "кефир", now)
        await c.ideas.capture(s, raw_input="Сменить парикмахера", source="api", now=now)


def semantic(c: Any, model: str = "fake-semantic-v1") -> FakeEmbeddings:
    fake = FakeEmbeddings(model=model)
    c.search.embeddings = fake
    return fake


async def find(c: Any, query: str) -> Any:
    async with c.db.session() as s:
        return await c.search.find_all(s, query, c.clock.now())


async def vectors(c: Any) -> list[tuple[str, str, str]]:
    async with c.db.session() as s:
        rows = await s.execute(select(MemoryEmbedding.object_type, MemoryEmbedding.object_id, MemoryEmbedding.model))
        return sorted((r.object_type, r.object_id, r.model) for r in rows.all())


@pytest.mark.db
async def test_meaning_finds_what_words_miss(container: Any) -> None:
    c = container
    await seed(c)
    words_only = await find(c, "стрижка")  # hashing provider: lexical, as before
    assert (words_only.tasks, words_only.ideas, words_only.by_meaning) == ([], [], 0)

    fake = semantic(c)
    stats = await c.search.backfill(c.db, c.clock.now())
    assert (stats.documents, stats.embedded, stats.written, stats.stopped) == (7, 7, 7, None)
    assert len(fake.calls) == 1  # 7 texts, one batched call

    found = await find(c, "стрижка")
    assert [t.content for t in found.tasks] == ["Записаться к барберу"]
    assert [i.raw_input for i in found.ideas] == ["Сменить парикмахера"]
    assert found.by_meaning == 2 and not found.events and not found.notes and not found.shopping
    assert [e.summary for e in (await find(c, "машина")).events] == ["Шиномонтаж: сменить резину"]
    assert [i.requested_name for i in (await find(c, "продукты")).shopping] == ["кефир"]
    assert [n.text for n in (await find(c, "код")).notes] == ["Пароль от wifi"]
    assert [t.content for t in (await find(c, "доктор")).tasks] == ["Позвонить Анне"]  # the description counts too
    nothing = await find(c, "билеты в Рим")
    assert not (nothing.tasks or nothing.events or nothing.ideas or nothing.notes or nothing.shopping)

    both = await find(c, "барбер")  # the task is found by words and by meaning: shown once
    assert [t.content for t in both.tasks] == ["Записаться к барберу"]
    assert [i.raw_input for i in both.ideas] == ["Сменить парикмахера"] and both.by_meaning == 1


@pytest.mark.db
async def test_bot_search_by_meaning(container: Any) -> None:
    c = container
    await seed(c)
    semantic(c)
    await c.search.backfill(c.db, c.clock.now())
    await BotService(c).handle_update(msg("найди стрижку", 1))
    answer = c.telegram.texts()[-1]
    assert answer.startswith("🔎 «стрижку»")
    assert "📋 Задачи\n• Записаться к барберу" in answer
    assert "💡 Идеи\n• Сменить парикмахера" in answer
    assert c.llm.calls == []  # «найди …» is routed by keywords; meaning comes from the vectors


@pytest.mark.db
async def test_backfill_is_idempotent_and_follows_changes(container: Any) -> None:
    c = container
    await seed(c)
    fake = semantic(c)
    await c.search.backfill(c.db, c.clock.now())
    again = await c.search.backfill(c.db, c.clock.now())
    assert (again.embedded, again.written, again.deleted, again.unchanged) == (0, 0, 0, 7)
    assert len(fake.calls) == 1  # nothing changed → no call

    async with c.db.session() as s:
        await s.execute(update(TodoistTask).where(TodoistTask.id == "CIG").values(content="Купить сигару и спички"))
        await s.execute(update(TodoistTask).where(TodoistTask.id == "DOC").values(checked=True))
    changed = await c.search.backfill(c.db, c.clock.now())
    assert (changed.embedded, changed.written, changed.deleted) == (1, 1, 1)
    assert fake.calls[-1] == ("document", ["Купить сигару и спички"])
    assert ("task", "DOC", "fake-semantic-v1") not in await vectors(c)

    # The same text in two objects (instances of a recurring event) is embedded once.
    now = c.clock.now()
    for day in (2, 3):
        c.calendar.add_event(
            "primary",
            summary="Йога",
            start={"dateTime": iso(now + timedelta(days=day))},
            end={"dateTime": iso(now + timedelta(days=day, hours=1))},
        )
    async with c.db.session() as s:
        await c.calendar_sync.reconcile(s, now)
    recurring = await c.search.backfill(c.db, now)
    assert (recurring.embedded, recurring.written) == (1, 2)


@pytest.mark.db
async def test_model_change_reembeds_and_never_mixes_models(container: Any) -> None:
    c = container
    await seed(c)
    semantic(c, "fake-semantic-v1")
    await c.search.backfill(c.db, c.clock.now())
    v2 = semantic(c, "fake-semantic-v2")
    before = await find(c, "стрижка")
    assert before.tasks == [] and v2.calls == []  # no v2 vectors yet: lexical only, no query call
    stats = await c.search.backfill(c.db, c.clock.now())
    assert (stats.deleted, stats.embedded) == (7, 7)
    assert {model for _, _, model in await vectors(c)} == {"fake-semantic-v2"}
    assert [t.content for t in (await find(c, "стрижка")).tasks] == ["Записаться к барберу"]


@dataclass
class SlowEmbeddings(FakeEmbeddings):
    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        if kind == "query":
            await asyncio.sleep(5)
        return await super().embed(texts, kind=kind)


@pytest.mark.db
async def test_search_falls_back_to_words_silently(container: Any) -> None:
    c = container
    await seed(c)
    fake = semantic(c)
    await c.search.backfill(c.db, c.clock.now())
    fake.fail_with = TransientError("openrouter-embeddings: HTTP 503")
    down = await find(c, "стрижка")
    assert down.tasks == [] and down.by_meaning == 0  # outage → words only, no exception
    assert [t.content for t in (await find(c, "барбер")).tasks] == ["Записаться к барберу"]
    fake.fail_with = BudgetExceededError("embeddings: daily budget 300 exhausted")
    assert (await find(c, "машина")).events == []

    slow = SlowEmbeddings(model="fake-semantic-v1")
    c.search.embeddings = slow
    c.settings.search_embed_timeout_seconds = 0.05
    started = asyncio.get_running_loop().time()
    assert (await find(c, "стрижка")).tasks == []
    assert asyncio.get_running_loop().time() - started < 2  # a slow provider never holds the reply


@pytest.mark.db
async def test_query_vectors_are_cached_briefly(container: Any) -> None:
    c = container
    await seed(c)
    fake = semantic(c)
    await c.search.backfill(c.db, c.clock.now())
    await find(c, "стрижка")
    await find(c, " Стрижка ")
    assert [kind for kind, _ in fake.calls].count("query") == 1
    c.search._monotonic = lambda: time.monotonic() + 3600  # past the cache lifetime
    await find(c, "стрижка")
    assert [kind for kind, _ in fake.calls].count("query") == 2


@dataclass
class BudgetedEmbeddings(FakeEmbeddings):
    allowed: int = 1

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        if kind == "document":
            if self.allowed <= 0:
                raise BudgetExceededError("embeddings: daily budget 1 exhausted")
            self.allowed -= 1
        return await super().embed(texts, kind=kind)


@pytest.mark.db
async def test_backfill_stops_on_budget_and_resumes(container: Any) -> None:
    c = container
    await seed(c)
    c.settings.embeddings_batch_size = 3
    capped = BudgetedEmbeddings(allowed=1)
    c.search.embeddings = capped
    first = await c.search.backfill(c.db, c.clock.now())
    assert (first.embedded, first.written, first.stopped) == (3, 3, "BudgetExceededError")
    capped.allowed = 10
    rest = await c.search.backfill(c.db, c.clock.now())
    assert (rest.unchanged, rest.embedded, rest.stopped) == (3, 4, None)
    assert len(await vectors(c)) == 7


@pytest.mark.db
async def test_backfill_job_and_idea_trigger(container: Any, worker: Any) -> None:
    c = container
    async with c.db.session() as s:
        await enqueue(s, job_type="embeddings.backfill", run_at=c.clock.now(), dedupe_key="bf-lexical")
    await worker.drain()
    async with c.db.session() as s:
        status = await s.scalar(select(ScheduledJob.status).where(ScheduledJob.dedupe_key == "bf-lexical"))
    assert status == SKIPPED  # hashing provider: nothing to embed

    semantic(c)
    async with c.db.session() as s:
        await c.ideas.capture(s, raw_input="Сменить парикмахера", source="api", now=c.clock.now())
    await worker.drain()  # idea.analyze → embeddings.backfill (coalesced)
    assert [(t, m) for t, _, m in await vectors(c)] == [("idea", "fake-semantic-v1")]
    async with c.db.session() as s:
        hits = await c.ideas.search(s, "стрижка")  # no shared word: found by meaning
        jobs = await s.scalar(select(func.count()).where(ScheduledJob.job_type == "embeddings.backfill"))
    assert [h.title for h in hits] == ["Сменить парикмахера"]
    assert jobs == 2


@pytest.mark.db
async def test_idea_search_without_vectors_stays_lexical(container: Any) -> None:
    c = container
    async with c.db.session() as s:
        for text in ("Сменить парикмахера", "Купить велосипед летом"):
            idea, _ = await c.ideas.capture(s, raw_input=text, source="api", now=c.clock.now())
            await c.ideas.analyze(s, idea.id, c.clock.now())
    async with c.db.session() as s:
        assert await c.ideas.search(s, "стрижка") == []  # hashing: no noise from unrelated «nearest» ideas
        assert [h.snippet for h in await c.ideas.search(s, "велосипед")] == ["Купить велосипед летом"]
    assert await vectors(c) == []
