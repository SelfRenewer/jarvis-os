"""Claude Code adapters.

* `FakeClaudeCodeAdapter` — default for tests; never spawns Claude, costs nothing.
* `SdkClaudeCodeAdapter`  — Claude Agent SDK (`claude_agent_sdk.query`), locked down.
* `CliClaudeCodeAdapter`  — `claude -p` headless fallback with the same restrictions.
Never uses bypassPermissions / --dangerously-skip-permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dev_worker.policy import (
    DISALLOWED_TOOLS,
    SYSTEM_POLICY,
    RepoConfig,
    allowed_tools_for,
    check_tool_call,
    claude_env_overrides,
)


@dataclass
class ClaudeRunResult:
    status: str  # success|error
    summary: str
    cost_usd: float | None = None
    turns: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClaudeLimits:
    max_turns: int = 60
    max_budget_usd: float = 5.0
    model: str | None = None
    timeout_seconds: int = 3600


class ClaudeCodeAdapter(Protocol):
    name: str

    async def run(
        self, worktree: Path, task_markdown: str, repo: RepoConfig, limits: ClaudeLimits
    ) -> ClaudeRunResult: ...


def build_prompt(task_markdown: str) -> str:
    return (
        "Implement the development task described in DEV_TASK.md (also included below). Follow the working "
        "rules. Run the project's tests with the allowed commands before finishing.\n\n"
        "<dev_task>\n" + task_markdown + "\n</dev_task>"
    )


class FakeClaudeCodeAdapter:
    """Deterministic stand-in: writes one file so the rest of the pipeline (commit, diff, tests) is exercised."""

    name = "fake"

    def __init__(self, files: dict[str, str] | None = None, fail: bool = False) -> None:
        self.files = files or {"JARVIS_CHANGE.md": "Change produced by FakeClaudeCodeAdapter.\n"}
        self.fail = fail
        self.calls: list[tuple[Path, str]] = []

    async def run(self, worktree: Path, task_markdown: str, repo: RepoConfig, limits: ClaudeLimits) -> ClaudeRunResult:
        self.calls.append((worktree, task_markdown))
        if self.fail:
            return ClaudeRunResult("error", "", error="fake failure")
        for rel, content in self.files.items():
            if check_tool_call("Write", {"file_path": rel}, worktree) is not None:
                return ClaudeRunResult("error", "", error=f"fake adapter refused path {rel}")
            target = worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ClaudeRunResult("success", "fake change applied", cost_usd=0.0, turns=1)


def _guard_hook(worktree: Path) -> Any:
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check_tool_call(str(input_data.get("tool_name")), dict(input_data.get("tool_input") or {}), worktree)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


class SdkClaudeCodeAdapter:
    name = "sdk"

    async def run(self, worktree: Path, task_markdown: str, repo: RepoConfig, limits: ClaudeLimits) -> ClaudeRunResult:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

        options = ClaudeAgentOptions(
            cwd=str(worktree),
            allowed_tools=allowed_tools_for(repo),
            disallowed_tools=list(DISALLOWED_TOOLS),
            permission_mode="dontAsk",
            setting_sources=[],  # ignore repository/user settings: repo content is untrusted
            strict_mcp_config=True,
            mcp_servers={},
            max_turns=limits.max_turns,
            max_budget_usd=limits.max_budget_usd,
            model=limits.model,
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_POLICY},
            env=claude_env_overrides(),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_guard_hook(worktree)])]},
        )
        result: ClaudeRunResult | None = None

        async def consume() -> None:
            nonlocal result
            async for message in query(prompt=build_prompt(task_markdown), options=options):
                if isinstance(message, ResultMessage):
                    result = ClaudeRunResult(
                        status="error" if message.is_error else "success",
                        summary=(message.result or "")[:4000],
                        cost_usd=message.total_cost_usd,
                        turns=message.num_turns,
                        error=f"subtype={message.subtype}" if message.is_error else None,
                    )

        try:
            await asyncio.wait_for(consume(), timeout=limits.timeout_seconds)
        except TimeoutError:
            return ClaudeRunResult("error", "", error="claude run timed out")
        except Exception as exc:
            return ClaudeRunResult("error", "", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        return result or ClaudeRunResult("error", "", error="no result message")


class CliClaudeCodeAdapter:
    """`claude -p` fallback. Path/tool checks rely on dontAsk + allow/deny rules (no Python hook)."""

    name = "cli"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    async def run(self, worktree: Path, task_markdown: str, repo: RepoConfig, limits: ClaudeLimits) -> ClaudeRunResult:
        args = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-budget-usd",
            str(limits.max_budget_usd),
            "--max-turns",
            str(limits.max_turns),
            "--append-system-prompt",
            SYSTEM_POLICY,
            "--allowedTools",
            *allowed_tools_for(repo),
            "--disallowedTools",
            *DISALLOWED_TOOLS,
        ]
        if limits.model:
            args += ["--model", limits.model]
        env = {**os.environ, **claude_env_overrides()}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(worktree),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(build_prompt(task_markdown).encode()), timeout=limits.timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            return ClaudeRunResult("error", "", error="claude cli timed out")
        try:
            data = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            return ClaudeRunResult("error", "", error=f"unparseable CLI output (exit {proc.returncode})")
        is_error = bool(data.get("is_error")) or proc.returncode != 0
        return ClaudeRunResult(
            "error" if is_error else "success",
            str(data.get("result") or "")[:4000],
            data.get("total_cost_usd"),
            data.get("num_turns"),
            err.decode()[:500] if is_error else None,
        )
