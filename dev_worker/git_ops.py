"""Safe git operations (argv only, no shell, hooks disabled)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_worker.policy import BRANCH_RE, PolicyViolation, subprocess_env

# Repository-provided hooks must not run on the worker (`-c core.hooksPath=/dev/null`).
GIT_BASE = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


class GitError(Exception):
    pass


def git(repo: Path, *args: str, timeout: int = 120, check: bool = True) -> str:
    result = subprocess.run(
        [*GIT_BASE, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(),
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


@dataclass
class Workspace:
    worktree: Path
    branch: str
    base_ref: str
    base_commit: str


def prepare_worktree(
    repo_path: Path, base_branch: str, remote: str | None, branch: str, worktrees_dir: Path
) -> Workspace:
    if not BRANCH_RE.match(branch):
        raise PolicyViolation("invalid branch name")
    if not (repo_path / ".git").exists():
        raise GitError("allowlisted path is not a git repository")
    if git(repo_path, "status", "--porcelain"):
        raise GitError("repository checkout is not clean; refusing to start")
    existing = git(repo_path, "branch", "--list", branch)
    if existing:
        raise GitError(f"branch {branch} already exists; refusing to reuse it")
    base_ref = base_branch
    if remote:
        remotes = git(repo_path, "remote").split()
        if remote in remotes:
            git(repo_path, "fetch", "--prune", remote, base_branch, timeout=300)
            base_ref = f"{remote}/{base_branch}"
    base_commit = git(repo_path, "rev-parse", base_ref)
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    worktree = worktrees_dir / branch.replace("/", "__")
    if worktree.exists():
        raise GitError("worktree directory already exists")
    git(repo_path, "worktree", "add", "-b", branch, str(worktree), base_ref, timeout=300)
    return Workspace(worktree=worktree, branch=branch, base_ref=base_ref, base_commit=base_commit)


def commit_all(ws: Workspace, message: str) -> str | None:
    if git(ws.worktree, "rev-parse", "--abbrev-ref", "HEAD") != ws.branch:
        raise GitError("worktree is not on the job branch")
    git(ws.worktree, "add", "-A")
    if not git(ws.worktree, "status", "--porcelain"):
        return None
    git(
        ws.worktree,
        "-c",
        "user.name=JARVIS Dev Worker",
        "-c",
        "user.email=jarvis-dev-worker@localhost",
        "commit",
        "--no-verify",
        "-m",
        message,
    )
    return git(ws.worktree, "rev-parse", "HEAD")


def diff_stat(ws: Workspace) -> tuple[int, str]:
    stat = git(ws.worktree, "diff", "--stat", f"{ws.base_commit}...HEAD")
    names = git(ws.worktree, "diff", "--name-only", f"{ws.base_commit}...HEAD")
    changed = len([n for n in names.splitlines() if n.strip()])
    return changed, stat[:20000]


def changed_paths(ws: Workspace) -> list[str]:
    return [n for n in git(ws.worktree, "diff", "--name-only", f"{ws.base_commit}...HEAD").splitlines() if n]


def push_branch(ws: Workspace, remote: str) -> None:
    """Explicitly opt-in per repo. Never force."""
    git(ws.worktree, "push", "--set-upstream", remote, ws.branch, timeout=300)
