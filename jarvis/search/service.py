"""Hybrid search: the lexical matching «найди …» always had, plus vector similarity (embeddings).

Indexing — `backfill()` (job `embeddings.backfill`: periodic, and right after an idea is analyzed):
* the searchable objects are exactly what «найди …» shows: open Todoist tasks (title + description), calendar events
  from yesterday on, ideas (title + summary + original text), notes, open shopping items;
* every object's text is hashed; only new or changed texts are embedded, several per call (batching), and a text the
  current model already embedded (a recurring event) reuses that vector — a run with no changes makes no call;
* vectors of other models and of objects that left the set (done tasks, past events, bought items) are deleted, so
  vectors of two models are never compared.

Retrieval — one small call per search embeds the query (identical queries are cached for 10 minutes); the nearest
vectors of the *current* model are found by an exact scan (a personal index is small, and an approximate HNSW scan
filtered by type could miss hits) and kept only above `search_min_similarity` and within `search_similarity_margin`
of the best one. Lexical and vector hits are merged by reciprocal rank (lexical weighs more), ties broken
deterministically. No provider, no vectors yet, budget exhausted, an outage or a slow answer → the lexical result
alone, silently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.config import Settings
from jarvis.core.errors import JarvisError
from jarvis.db.models import (
    CalendarEvent,
    Idea,
    IdeaVersion,
    MemoryEmbedding,
    Note,
    ShoppingItem,
    ShoppingList,
    TodoistTask,
)
from jarvis.db.session import Database
from jarvis.inbox.service import NoteService
from jarvis.integrations.embeddings.client import EmbeddingProvider
from jarvis.tasks.service import TaskOpsService, match_tasks

log = logging.getLogger(__name__)

TASK, EVENT, IDEA, NOTE, SHOPPING = "task", "event", "idea", "note", "shopping"
OBJECT_TYPES = (TASK, EVENT, IDEA, NOTE, SHOPPING)
RRF_K = 60
LEXICAL_WEIGHT = 1.5  # an exact word is the strongest signal
VECTOR_WEIGHT = 1.0
QUERY_CACHE_SECONDS = 600.0
QUERY_CACHE_SIZE = 256
MAX_DOC_CHARS = 2000
EVENTS_FROM = timedelta(days=1)  # the calendar part of «найди …» looks from yesterday on
NEAR_PER_TYPE = 5


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _join(*parts: str | None) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())[:MAX_DOC_CHARS]


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _uuids(ids: Iterable[str]) -> list[uuid.UUID]:
    out = []
    for value in ids:
        try:
            out.append(uuid.UUID(value))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class Doc:
    """One searchable object as the embedding model sees it."""

    object_type: str
    object_id: str
    text: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.object_type, self.object_id)

    @property
    def digest(self) -> str:
        return content_hash(self.text)


@dataclass
class BackfillStats:
    model: str
    documents: int = 0  # searchable objects now
    unchanged: int = 0  # already had a vector of this model for the same text
    embedded: int = 0  # texts sent to the provider
    written: int = 0  # vectors stored (a text shared by several objects is embedded once)
    deleted: int = 0  # vectors of other models / of objects that are no longer searchable
    stopped: str | None = None  # why the run ended early (budget, outage); the next run continues

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Found:
    """What «найди …» shows, per section, in the order to show it (same object types as before)."""

    tasks: list[TodoistTask] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    ideas: list[Idea] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    shopping: list[ShoppingItem] = field(default_factory=list)
    by_meaning: int = 0  # hits that only the vectors found


def fuse[T](
    lexical: Sequence[T],
    near: Sequence[tuple[str, float]],
    *,
    key: Callable[[T], str],
    resolve: dict[str, T],
    tiebreak: Callable[[T], Any],
    limit: int | None = None,
) -> list[T]:
    """Reciprocal-rank merge of lexical hits (in their order) and vector hits (dense rank by similarity, so the
    instances of one recurring event score the same). Vector ids that do not resolve (a task completed since the
    last backfill) are dropped. Deterministic: score, then `tiebreak`, then the id."""
    scores: dict[str, float] = {}
    items: dict[str, T] = {}
    for rank, item in enumerate(lexical):
        k = key(item)
        if k not in items:
            items[k] = item
            scores[k] = LEXICAL_WEIGHT / (RRF_K + rank)
    rank, level = -1, None
    for object_id, similarity in near:
        if round(similarity, 6) != level:
            rank, level = rank + 1, round(similarity, 6)
        found = items.get(object_id) or resolve.get(object_id)
        if found is None:
            continue
        items[object_id] = found
        scores[object_id] = scores.get(object_id, 0.0) + VECTOR_WEIGHT / (RRF_K + rank)
    order = sorted(items, key=lambda k: (-round(scores[k], 9), tiebreak(items[k]), k))
    ranked = [items[k] for k in order]
    return ranked[:limit] if limit is not None else ranked


class SearchService:
    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingProvider,
        *,
        task_ops: TaskOpsService,
        notes: NoteService,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.task_ops = task_ops
        self.notes = notes
        self._monotonic = monotonic
        self._query_cache: dict[tuple[str, str], tuple[float, list[float]]] = {}

    @property
    def semantic(self) -> bool:
        """True when the provider understands meaning (hashing only reflects shared words)."""
        return bool(getattr(self.embeddings, "semantic", False))

    # --- what is searchable ---------------------------------------------------------------------------

    @staticmethod
    async def open_items(session: AsyncSession) -> list[ShoppingItem]:
        """Open items of the active shopping list (read-only: unlike `ShoppingService.active_list`, creates nothing)."""
        return list(
            (
                await session.scalars(
                    select(ShoppingItem)
                    .join(ShoppingList, ShoppingList.id == ShoppingItem.list_id)
                    .where(ShoppingList.status == "active", ShoppingItem.status == "open")
                    .order_by(ShoppingItem.created_at)
                )
            ).all()
        )

    async def documents(self, session: AsyncSession, now: datetime) -> list[Doc]:
        docs = [
            Doc(TASK, t.id, _join(t.content, (t.description or "")[:1000])) for t in await self.task_ops.active(session)
        ]
        events = await session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.status != "cancelled",
                CalendarEvent.start_at >= now - EVENTS_FROM,
                CalendarEvent.summary.is_not(None),
            )
        )
        docs += [Doc(EVENT, str(e.id), _join(e.summary)) for e in events]
        summaries: dict[uuid.UUID, str] = {
            row.idea_id: row.summary
            for row in (
                await session.execute(
                    select(IdeaVersion.idea_id, IdeaVersion.summary).join(
                        Idea, (Idea.id == IdeaVersion.idea_id) & (Idea.current_version == IdeaVersion.version)
                    )
                )
            ).all()
        }
        docs += [
            Doc(IDEA, str(i.id), _join(i.title, summaries.get(i.id), i.transcript or i.raw_input))
            for i in await session.scalars(select(Idea))
        ]
        docs += [Doc(NOTE, str(n.id), _join(n.text)) for n in await session.scalars(select(Note))]
        docs += [Doc(SHOPPING, str(i.id), _join(i.requested_name)) for i in await self.open_items(session)]
        return [d for d in docs if d.text]

    # --- indexing -------------------------------------------------------------------------------------

    async def backfill(self, db: Database, now: datetime) -> BackfillStats:
        """Embed what is missing or changed for the current model; idempotent (a second run makes no call)."""
        model = self.embeddings.model
        stats = BackfillStats(model=model)
        if not self.semantic:
            stats.stopped = "lexical provider (no search by meaning)"
            return stats
        async with db.session() as session:
            docs = await self.documents(session, now)
            stats.documents = len(docs)
            removed = await session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.model != model))
            stats.deleted += int(getattr(removed, "rowcount", 0) or 0)
            stored = {
                (row.object_type, row.object_id): row.content_hash
                for row in (
                    await session.execute(
                        select(
                            MemoryEmbedding.object_type, MemoryEmbedding.object_id, MemoryEmbedding.content_hash
                        ).where(MemoryEmbedding.model == model)
                    )
                ).all()
            }
            wanted = {d.key for d in docs}
            gone = sorted(k for k in stored if k not in wanted)
            for chunk in _chunks(gone, 500):
                removed = await session.execute(
                    delete(MemoryEmbedding).where(
                        MemoryEmbedding.model == model,
                        tuple_(MemoryEmbedding.object_type, MemoryEmbedding.object_id).in_(list(chunk)),
                    )
                )
                stats.deleted += int(getattr(removed, "rowcount", 0) or 0)
        todo = [d for d in docs if stored.get(d.key) != d.digest]
        stats.unchanged = len(docs) - len(todo)
        if not todo:
            return stats
        by_digest: dict[str, list[Doc]] = {}
        for doc in todo:
            by_digest.setdefault(doc.digest, []).append(doc)
        async with db.session() as session:  # the same text already embedded by this model: no call needed
            known = await self._vectors_by_digest(session, model, list(by_digest))
            reused = [(doc, known[digest]) for digest in known for doc in by_digest[digest]]
            await self._upsert(session, model, reused)
            stats.written += len(reused)
        missing = [digest for digest in by_digest if digest not in known]
        for digests in _chunks(missing, max(1, self.settings.embeddings_batch_size)):
            try:
                vectors = await self.embeddings.embed([by_digest[d][0].text for d in digests], kind="document")
            except JarvisError as exc:  # budget, outage, auth: keep what is done; the next run continues
                stats.stopped = type(exc).__name__
                log.warning("embeddings backfill stopped after %d texts: %s", stats.embedded, type(exc).__name__)
                break
            rows = [(doc, vector) for d, vector in zip(digests, vectors, strict=True) for doc in by_digest[d]]
            async with db.session() as session:
                await self._upsert(session, model, rows)
            stats.embedded += len(digests)
            stats.written += len(rows)
        return stats

    @staticmethod
    async def _vectors_by_digest(session: AsyncSession, model: str, digests: list[str]) -> dict[str, Any]:
        known: dict[str, Any] = {}
        for chunk in _chunks(digests, 500):
            rows = await session.execute(
                select(MemoryEmbedding.content_hash, MemoryEmbedding.embedding)
                .where(MemoryEmbedding.model == model, MemoryEmbedding.content_hash.in_(list(chunk)))
                .distinct(MemoryEmbedding.content_hash)
            )
            known.update({row.content_hash: row.embedding for row in rows.all()})
        return known

    @staticmethod
    async def _upsert(session: AsyncSession, model: str, rows: Sequence[tuple[Doc, Any]]) -> None:
        for chunk in _chunks(rows, 100):
            stmt = insert(MemoryEmbedding).values(
                [
                    {
                        "id": uuid.uuid4(),
                        "object_type": doc.object_type,
                        "object_id": doc.object_id,
                        "model": model,
                        "content_hash": doc.digest,
                        "embedding": vector,
                    }
                    for doc, vector in chunk
                ]
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[MemoryEmbedding.object_type, MemoryEmbedding.object_id, MemoryEmbedding.model],
                    set_={"content_hash": stmt.excluded.content_hash, "embedding": stmt.excluded.embedding},
                )
            )

    async def coverage(self, session: AsyncSession) -> dict[str, int]:
        """Vectors of the current model per object type (doctor)."""
        rows = await session.execute(
            select(MemoryEmbedding.object_type, func.count())
            .where(MemoryEmbedding.model == self.embeddings.model)
            .group_by(MemoryEmbedding.object_type)
        )
        return {object_type: int(count) for object_type, count in rows.all()}

    # --- retrieval ------------------------------------------------------------------------------------

    async def query_vector(self, query: str) -> list[float] | None:
        """The query's vector (cached briefly), or None when it cannot be had quickly — never raises."""
        cache_key = (self.embeddings.model, " ".join(query.lower().split()))
        now = self._monotonic()
        cached = self._query_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        timeout = self.settings.search_embed_timeout_seconds or None
        try:
            [vector] = await asyncio.wait_for(self.embeddings.embed([query.strip()], kind="query"), timeout=timeout)
        except Exception as exc:  # any provider failure → search by words only
            log.warning("search by meaning unavailable, words only: %s", type(exc).__name__)
            return None
        if len(self._query_cache) >= QUERY_CACHE_SIZE:
            self._query_cache.pop(next(iter(self._query_cache)))
        self._query_cache[cache_key] = (now + QUERY_CACHE_SECONDS, vector)
        return vector

    async def similar(
        self, session: AsyncSession, query: str, object_types: Sequence[str], *, per_type: int = NEAR_PER_TYPE
    ) -> dict[str, list[tuple[str, float]]]:
        """Nearest objects by meaning per type: [(object_id, cosine similarity)], best first. {} when unavailable."""
        if not self.semantic or not query.strip():
            return {}
        model = self.embeddings.model
        scope = (MemoryEmbedding.model == model, MemoryEmbedding.object_type.in_(list(object_types)))
        if await session.scalar(select(MemoryEmbedding.id).where(*scope).limit(1)) is None:
            return {}  # nothing indexed for this model yet: no call
        qvec = await self.query_vector(query)
        if qvec is None:
            return {}
        # MATERIALIZED: an exact scan of this model's vectors (an HNSW index scan would return ~40 rows before the
        # type filter and could miss hits).
        candidates = (
            select(MemoryEmbedding.object_type, MemoryEmbedding.object_id, MemoryEmbedding.embedding)
            .where(*scope)
            .cte("candidates")
            .prefix_with("MATERIALIZED")
        )
        distance = candidates.c.embedding.cosine_distance(qvec)
        ranked = select(
            candidates.c.object_type,
            candidates.c.object_id,
            (1 - distance).label("similarity"),
            func.row_number()
            .over(partition_by=candidates.c.object_type, order_by=(distance, candidates.c.object_id))
            .label("rn"),
        ).subquery()
        floor = self.settings.search_min_similarity
        rows = (
            await session.execute(
                select(ranked.c.object_type, ranked.c.object_id, ranked.c.similarity).where(
                    ranked.c.rn <= per_type, ranked.c.similarity >= floor
                )
            )
        ).all()
        log.debug("search by meaning: %s", sorted((r.object_type, round(float(r.similarity), 3)) for r in rows))
        if not rows:
            return {}
        best = max(float(r.similarity) for r in rows)
        cutoff = max(floor, best - self.settings.search_similarity_margin)
        near: dict[str, list[tuple[str, float]]] = {}
        for row in sorted(rows, key=lambda r: (-float(r.similarity), r.object_type, r.object_id)):
            if float(row.similarity) >= cutoff:
                near.setdefault(row.object_type, []).append((row.object_id, float(row.similarity)))
        return near

    async def find_all(self, session: AsyncSession, query: str, now: datetime) -> Found:
        """«Найди …» across tasks, calendar, ideas, notes and shopping: the lexical hits as before + hits by meaning."""
        low = query.lower()
        pattern = "%" + query.replace("%", "").replace("_", "") + "%"
        active = list(await self.task_ops.active(session))
        lex_tasks = match_tasks(query, active) or [t for t in active if low in t.content.lower()]
        window = (CalendarEvent.status != "cancelled", CalendarEvent.start_at >= now - EVENTS_FROM)
        lex_events = (
            await session.scalars(
                select(CalendarEvent)
                .where(*window, CalendarEvent.summary.ilike(pattern))
                .order_by(CalendarEvent.start_at)
                .limit(5)
            )
        ).all()
        lex_notes = await self.notes.search(session, query, limit=3)
        lex_ideas = (
            await session.scalars(
                select(Idea)
                .where(Idea.raw_input.ilike(pattern) | Idea.title.ilike(pattern))
                .order_by(Idea.created_at.desc())
                .limit(3)
            )
        ).all()
        open_items = await self.open_items(session)
        lex_shopping = [i for i in open_items if low in i.requested_name.lower()]

        near = await self.similar(session, query, OBJECT_TYPES)
        events_by_id: dict[str, CalendarEvent] = {}
        if ids := _uuids(oid for oid, _ in near.get(EVENT, [])):
            found_events = await session.scalars(select(CalendarEvent).where(*window, CalendarEvent.id.in_(ids)))
            events_by_id = {str(e.id): e for e in found_events}
        ideas_by_id: dict[str, Idea] = {}
        if ids := _uuids(oid for oid, _ in near.get(IDEA, [])):
            ideas_by_id = {str(i.id): i for i in await session.scalars(select(Idea).where(Idea.id.in_(ids)))}
        notes_by_id: dict[str, Note] = {}
        if ids := _uuids(oid for oid, _ in near.get(NOTE, [])):
            notes_by_id = {str(n.id): n for n in await session.scalars(select(Note).where(Note.id.in_(ids)))}

        tasks = fuse(
            lex_tasks, near.get(TASK, []), key=lambda t: t.id, resolve={t.id: t for t in active}, tiebreak=lambda t: ""
        )
        events = fuse(
            lex_events,
            near.get(EVENT, []),
            key=lambda e: str(e.id),
            resolve=events_by_id,
            tiebreak=lambda e: e.start_at,
            limit=5,
        )
        events.sort(key=lambda e: (e.start_at, str(e.id)))  # the calendar reads in time order
        ideas = fuse(
            lex_ideas,
            near.get(IDEA, []),
            key=lambda i: str(i.id),
            resolve=ideas_by_id,
            tiebreak=lambda i: -i.created_at.timestamp(),
            limit=3,
        )
        notes = fuse(
            lex_notes,
            near.get(NOTE, []),
            key=lambda n: str(n.id),
            resolve=notes_by_id,
            tiebreak=lambda n: -n.created_at.timestamp(),
            limit=3,
        )
        shopping = fuse(
            lex_shopping,
            near.get(SHOPPING, []),
            key=lambda i: str(i.id),
            resolve={str(i.id): i for i in open_items},
            tiebreak=lambda i: i.created_at,
        )

        def keys(*groups: Sequence[CalendarEvent | Idea | Note | ShoppingItem]) -> set[str]:
            return {str(x.id) for group in groups for x in group}

        lexical = {t.id for t in lex_tasks} | keys(lex_events, lex_ideas, lex_notes, lex_shopping)
        shown = {t.id for t in tasks} | keys(events, ideas, notes, shopping)
        by_meaning = sum(1 for k in shown if k not in lexical)
        return Found(tasks, events, ideas, notes, shopping, by_meaning)
