"""Router eval: accuracy, latency and cost on `evals/router_cases.py` (live OpenRouter calls for phrases the keyword
router does not settle — about $0.002 each with the default router model).

    uv run python scripts/eval_router.py                    # the configured router model
    uv run python scripts/eval_router.py --model google/gemini-3.8-flash --only free,general
    uv run python scripts/eval_router.py --keyword-only     # free: what code alone understands

Prints one line per miss and a summary per kind: accuracy, share routed without the LLM, p50/p90 latency.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.router_cases import CASES, Case
from jarvis.bot.intents import route_message

TASKS = ["Постричься", "Купить хлеб", "Позвонить маме", "Встреча с Ниной", "Купить сигару", "Записаться к стоматологу"]


def matches(case: Case, got: list[str]) -> bool:
    """Every expected action (in order) is one of its alternatives; «a+b» alternatives match several actions."""
    if len(case.expect) == 1:
        options = case.expect[0].split("|")
        return any(option.split("+") == got for option in options) or (len(got) == 1 and got[0] in options)
    if len(got) != len(case.expect):
        return False
    return all(g in e.split("|") for g, e in zip(got, case.expect, strict=True))


async def run(args: argparse.Namespace) -> int:
    llm = None
    if not args.keyword_only:
        from jarvis.core.config import get_settings
        from jarvis.integrations.llm.client import OpenRouterLLM

        s = get_settings()
        if s.openrouter_api_key is None:
            print("OPENROUTER_API_KEY is not set")
            return 2
        model = args.model or s.router_model
        llm = OpenRouterLLM(s.openrouter_api_key.get_secret_value(), model, reasoning_effort=args.reasoning or None)
        print(f"router model: {model}, reasoning: {args.reasoning or 'off'}")
    kinds = set(args.only.split(",")) if args.only else None
    rows: dict[str, list[tuple[bool, bool, float]]] = {}
    for case in CASES:
        if kinds and case.kind not in kinds:
            continue
        started = time.monotonic()
        try:
            routed = await route_message(case.text, llm, TASKS, None, 20)
            got = [r.intent.value for r in routed]
            source = routed[0].source if routed else ""
        except Exception as exc:  # a provider error counts as a miss
            got, source = [f"error:{type(exc).__name__}"], "error"
        elapsed = time.monotonic() - started
        ok = matches(case, got)
        rows.setdefault(case.kind, []).append((ok, source == "keyword", elapsed))
        if not ok:
            print(f"✗ [{case.kind}] {case.text!r}: got {'+'.join(got)} ({source}, {elapsed:.1f}s), want {case.expect}")
    print()
    total = [r for v in rows.values() for r in v]
    for kind, items in [*sorted(rows.items()), ("ALL", total)]:
        times = sorted(t for _, by_code, t in items if not by_code) or [0.0]
        p90 = times[min(len(times) - 1, int(len(times) * 0.9))]
        print(
            f"{kind:8} {sum(ok for ok, _, _ in items)}/{len(items)} correct · "
            f"{sum(code for _, code, _ in items)} by code · LLM p50 {statistics.median(times):.1f}s p90 {p90:.1f}s"
        )
    if llm is not None:
        await llm.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--reasoning", default="", help="low | minimal | empty = off")
    parser.add_argument("--only", help="comma-separated kinds: command,multi,free,general")
    parser.add_argument("--keyword-only", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
