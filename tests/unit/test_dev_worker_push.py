"""Dev Worker: a push that failed is a failed job (L7, review 17.10 — it was reported as «succeeded»)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dev_worker import git_ops, main
from dev_worker.claude import ClaudeRunResult
from dev_worker.policy import RepoConfig


class _Claude:
    async def run(self, *args: Any) -> ClaudeRunResult:
        return ClaudeRunResult(status="success", summary="done")


@pytest.mark.parametrize(("push_fails", "status"), [(False, "succeeded"), (True, "failed")])
async def test_a_failed_push_fails_the_job(tmp_path: Path, monkeypatch: Any, push_fails: bool, status: str) -> None:
    repo = RepoConfig(repo_id="r", path=tmp_path, remote="origin", push=True)
    ws = git_ops.Workspace(worktree=tmp_path, branch="jarvis/x", base_ref="main", base_commit="abc")

    def push(*args: Any) -> None:
        if push_fails:
            raise git_ops.GitError("rejected")

    monkeypatch.setattr(main, "validate_job", lambda payload, repos: repo)
    monkeypatch.setattr(git_ops, "prepare_worktree", lambda *a: ws)
    monkeypatch.setattr(git_ops, "commit_all", lambda *a: "def")
    monkeypatch.setattr(git_ops, "diff_stat", lambda *a: (1, "1 file"))
    monkeypatch.setattr(git_ops, "changed_paths", lambda *a: ["a.py"])
    monkeypatch.setattr(git_ops, "push_branch", push)
    cfg: Any = type("Cfg", (), {"repos": {}, "worktrees_dir": tmp_path, "limits": None})()
    payload = {"job_id": "j", "branch_name": "jarvis/x", "dev_task_markdown": "task", "approval_id": "a"}
    result = await main.execute_job(payload, cfg, _Claude())  # type: ignore[arg-type]
    assert result["status"] == status
    assert ("push failed" in (result.get("error") or "")) is push_fails
