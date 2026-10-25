"""Seed an approved development job (used by scripts/smoke_dev_worker.sh against a scratch database)."""

from __future__ import annotations

import asyncio

from jarvis.approvals.service import APPROVE, decide
from jarvis.core.config import get_settings
from jarvis.services.container import build_container


async def main() -> None:
    settings = get_settings()
    container = build_container(settings)
    now = container.clock.now()
    try:
        async with container.db.session() as s:
            idea, _ = await container.ideas.capture(
                s,
                raw_input="Smoke test: AI Inbox, который понимает, задача это, идея или проект.",
                source="api",
                now=now,
            )
        async with container.db.session() as s:
            spec = await container.ideas.create_spec(s, idea.id, now, "smoke-repo")
        async with container.db.session() as s:
            job, approval = await container.development.request_job(s, spec.id, "smoke-repo", now)
        async with container.db.session() as s:
            decision = await decide(s, nonce=approval.nonce, decision=APPROVE, actor="smoke", now=now)
            assert decision is not None
            await container.development.on_decision(s, decision.approval, now)
        print(f"{job.id} {job.branch_name}")
    finally:
        await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
