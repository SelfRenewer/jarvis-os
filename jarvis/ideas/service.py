"""Idea capture, structuring, versioning, semantic retrieval and spec generation."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.service import A, audit
from jarvis.core.errors import LLMOutputError, NotConfiguredError, TransientError
from jarvis.db.models import DevelopmentArtifact, DevSpec, Idea, IdeaVersion
from jarvis.ideas.schemas import IDEA_SYSTEM, SPEC_SYSTEM, DevSpecification, IdeaAnalysis
from jarvis.ideas.spec import fallback_spec, render_dev_task
from jarvis.integrations.llm.client import LLMClient
from jarvis.scheduler.queue import enqueue, enqueue_coalesced

if TYPE_CHECKING:
    from jarvis.search.service import SearchService

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    idea_id: uuid.UUID
    title: str | None
    snippet: str
    created_at: datetime
    score: float
    version: int


def _fallback_analysis(raw: str) -> IdeaAnalysis:
    first = raw.strip().split("\n")[0]
    title = first[:80] + ("…" if len(first) > 80 else "")
    return IdeaAnalysis(
        title=title or "Идея",
        summary=raw.strip()[:1000] or "(пусто)",
        open_questions=["Идея сохранена без LLM-анализа"],
    )


class IdeaService:
    def __init__(self, llm: LLMClient | None, search: SearchService | None = None) -> None:
        self.llm = llm
        self.search_service = search  # vectors (search by meaning); None → words only

    async def capture(
        self,
        session: AsyncSession,
        *,
        raw_input: str,
        source: str,
        now: datetime,
        chat_id: int | None = None,
        message_id: int | None = None,
        transcript: str | None = None,
        raw_media_id: uuid.UUID | None = None,
    ) -> tuple[Idea, bool]:
        """Store the original thought verbatim. Returns (idea, created)."""
        dedupe = f"tg:{chat_id}:{message_id}" if chat_id is not None and message_id is not None else None
        values = dict(
            source=source,
            raw_input=raw_input,
            transcript=transcript,
            raw_media_id=raw_media_id,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
            dedupe_key=dedupe,
            analysis_status="pending",
        )
        if dedupe:
            result = await session.execute(
                insert(Idea)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[Idea.dedupe_key])
                .returning(Idea.id)
            )
            row = result.first()
            if row is None:
                existing = await session.scalar(select(Idea).where(Idea.dedupe_key == dedupe))
                assert existing is not None
                return existing, False
            idea = await session.get(Idea, row[0])
            assert idea is not None
        else:
            idea = Idea(**values)
            session.add(idea)
            await session.flush()
        await audit(
            session,
            now=now,
            actor="user",
            action=A.IDEA_CREATED,
            target_type="idea",
            target_id=str(idea.id),
            details={"source": source, "chars": len(raw_input)},
        )
        await enqueue(
            session,
            job_type="idea.analyze",
            run_at=now,
            payload={"idea_id": str(idea.id)},
            dedupe_key=f"idea.analyze:{idea.id}:v1",
            max_attempts=4,
        )
        return idea, True

    async def analyze(self, session: AsyncSession, idea_id: uuid.UUID, now: datetime) -> IdeaVersion:
        idea = await session.get(Idea, idea_id)
        if idea is None:
            raise LookupError("idea not found")
        source_text = idea.transcript or idea.raw_input
        produced_by = "fallback"
        if self.llm is not None:
            try:
                analysis = await self.llm.structured(
                    capability="idea_analyzer",
                    system=IDEA_SYSTEM,
                    user=f"<raw_thought>\n{source_text}\n</raw_thought>",
                    schema=IdeaAnalysis,
                    max_tokens=3000,
                )
                produced_by = self.llm.model
            except (LLMOutputError, NotConfiguredError) as exc:
                log.warning("idea analysis fell back: %s", exc)
                analysis = _fallback_analysis(source_text)
            except TransientError:
                raise  # retried by the job runner
        else:
            analysis = _fallback_analysis(source_text)
        version_no = idea.current_version + 1
        version = IdeaVersion(
            idea_id=idea.id, version=version_no, produced_by=produced_by[:60], **analysis.model_dump()
        )
        session.add(version)
        idea.current_version = version_no
        idea.title = analysis.title
        idea.analysis_status = "analyzed" if produced_by != "fallback" else "fallback"
        await session.flush()
        if self.search_service is not None and self.search_service.semantic:
            # The vector comes from the shared backfill (same text format, budget, batching); soon, not inline, so
            # an embedding outage never fails the analysis.
            await enqueue_coalesced(
                session, job_type="embeddings.backfill", run_at=now, coalesce_key="ideas", priority=120
            )
        await audit(
            session,
            now=now,
            actor="idea_agent",
            action=A.IDEA_ANALYZED,
            target_type="idea",
            target_id=str(idea.id),
            details={"version": version_no, "by": produced_by},
        )
        return version

    async def search(self, session: AsyncSession, query: str, limit: int = 5) -> list[SearchHit]:
        """Hybrid retrieval: vector similarity (search by meaning, when available) + Russian full-text search,
        fused by reciprocal rank (lexical hits weigh a bit more)."""
        scores: dict[uuid.UUID, float] = {}
        near = await self.search_service.similar(session, query, ("idea",), per_type=20) if self.search_service else {}
        for rank, (object_id, _similarity) in enumerate(near.get("idea", [])):
            idea_id = uuid.UUID(object_id)
            scores[idea_id] = scores.get(idea_id, 0) + 1 / (60 + rank)
        tsq = func.websearch_to_tsquery(text("'russian'"), query)
        fts_rows = (
            await session.execute(
                select(
                    Idea.id,
                    func.greatest(
                        func.ts_rank(Idea.raw_tsv, tsq),
                        func.coalesce(func.max(func.ts_rank(IdeaVersion.search_tsv, tsq)), 0),
                    ).label("rank"),
                )
                .outerjoin(IdeaVersion, IdeaVersion.idea_id == Idea.id)
                .where((Idea.raw_tsv.op("@@")(tsq)) | (IdeaVersion.search_tsv.op("@@")(tsq)))
                .group_by(Idea.id)
                .order_by(text("rank DESC"))
                .limit(20)
            )
        ).all()
        for rank, row in enumerate(fts_rows):
            scores[row.id] = scores.get(row.id, 0) + 1.5 / (60 + rank)  # lexical hits weigh a bit more
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]
        hits: list[SearchHit] = []
        for idea_id, score in ranked:
            idea = await session.get(Idea, idea_id)
            if idea is None:
                continue
            raw = idea.transcript or idea.raw_input
            hits.append(
                SearchHit(idea.id, idea.title, raw[:200], idea.created_at, round(score, 5), idea.current_version)
            )
        return hits

    async def latest_version(self, session: AsyncSession, idea_id: uuid.UUID) -> IdeaVersion | None:
        return await session.scalar(
            select(IdeaVersion).where(IdeaVersion.idea_id == idea_id).order_by(IdeaVersion.version.desc()).limit(1)
        )

    async def create_spec(
        self, session: AsyncSession, idea_id: uuid.UUID, now: datetime, repo_id: str | None
    ) -> DevSpec:
        idea = await session.get(Idea, idea_id)
        if idea is None:
            raise LookupError("idea not found")
        version = await self.latest_version(session, idea_id)
        if version is None:
            version = await self.analyze(session, idea_id, now)
        structured = {
            "title": version.title,
            "summary": version.summary,
            "problem": version.problem,
            "proposed_solution": version.proposed_solution,
            "user_value": version.user_value,
            "assumptions": version.assumptions,
            "constraints": version.constraints,
            "open_questions": version.open_questions,
            "related_projects": version.related_projects,
        }
        spec_model: DevSpecification
        if self.llm is not None:
            try:
                spec_model = await self.llm.structured(
                    capability="spec_writer",
                    system=SPEC_SYSTEM,
                    user="<structured_idea>\n"
                    + json.dumps(structured, ensure_ascii=False)
                    + "\n</structured_idea>\n<original_thought>\n"
                    + (idea.transcript or idea.raw_input)
                    + "\n</original_thought>",
                    schema=DevSpecification,
                    max_tokens=6000,
                )
            except (LLMOutputError, NotConfiguredError) as exc:
                log.warning("spec generation fell back: %s", exc)
                spec_model = fallback_spec(idea.transcript or idea.raw_input, version.title)
        else:
            spec_model = fallback_spec(idea.transcript or idea.raw_input, version.title)
        spec = DevSpec(
            idea_id=idea.id,
            idea_version=version.version,
            repo_id=repo_id,
            spec=spec_model.model_dump(),
            dev_task_md="",
            status="draft",
        )
        session.add(spec)
        await session.flush()
        spec.dev_task_md = render_dev_task(
            spec_model,
            idea_title=version.title,
            idea_id=str(idea.id),
            spec_id=str(spec.id),
            repo_id=repo_id,
            created_at=now,
        )
        session.add(DevelopmentArtifact(spec_id=spec.id, kind="DEV_TASK.md", content=spec.dev_task_md))
        await audit(
            session,
            now=now,
            actor="idea_agent",
            action=A.SPEC_CREATED,
            target_type="dev_spec",
            target_id=str(spec.id),
            details={"idea": str(idea.id)},
        )
        return spec
