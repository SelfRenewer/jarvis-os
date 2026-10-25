"""JARVIS Dev Worker — runs on the trusted development machine and PULLS approved jobs.

Usage:
  jarvis-dev-worker check-config
  jarvis-dev-worker once        # claim and run at most one job
  jarvis-dev-worker run         # loop forever
Configuration: env (JARVIS_SERVER_URL, DEV_WORKER_ID, DEV_WORKER_TOKEN[_FILE], DEV_WORKER_SIGNING_KEY[_FILE],
DEV_WORKER_ADAPTER=sdk|cli|fake, DEV_WORKER_REPOS=path/to/repos.yaml, DEV_WORKER_WORKTREES=dir).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from dev_worker import git_ops
from dev_worker.checks import run_check
from dev_worker.claude import (
    ClaudeCodeAdapter,
    ClaudeLimits,
    CliClaudeCodeAdapter,
    FakeClaudeCodeAdapter,
    SdkClaudeCodeAdapter,
)
from dev_worker.policy import REPO_ID_RE, PolicyViolation, RepoConfig, validate_job
from jarvis.core.security import verify_payload_signature

log = logging.getLogger("dev_worker")
VERSION = "0.1.0"
MANUAL_REVIEW_PATTERNS = {
    "database migration": ("migrations/", "alembic", "schema.sql", "prisma/migrations"),
    "dependency changes": ("pyproject.toml", "package.json", "requirements", "uv.lock", "package-lock.json"),
    "CI/deploy config": (".github/", "Dockerfile", "docker-compose", "deploy/"),
    "security-sensitive code": ("auth", "security", "crypto", "permission"),
}


def _secret_from_env(name: str) -> str | None:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text().strip() or None
    return os.environ.get(name) or None


@dataclass
class WorkerConfig:
    server_url: str
    worker_id: str
    token: str
    signing_key: str
    repos: dict[str, RepoConfig]
    worktrees_dir: Path
    adapter: str
    limits: ClaudeLimits

    @classmethod
    def load(cls) -> WorkerConfig:
        repos_file = Path(os.environ.get("DEV_WORKER_REPOS", "dev_worker/repos.yaml")).expanduser()
        data: dict[str, Any] = yaml.safe_load(repos_file.read_text()) if repos_file.exists() else {}
        token = _secret_from_env("DEV_WORKER_TOKEN")
        key = _secret_from_env("DEV_WORKER_SIGNING_KEY")
        if not token or not key:
            raise SystemExit("DEV_WORKER_TOKEN and DEV_WORKER_SIGNING_KEY (or *_FILE) are required")
        return cls(
            server_url=os.environ.get("JARVIS_SERVER_URL", data.get("server_url", "http://127.0.0.1:8080")).rstrip("/"),
            worker_id=os.environ.get("DEV_WORKER_ID", data.get("worker_id", "dev-mac")),
            token=token,
            signing_key=key,
            repos=parse_repos(data.get("repos") or {}),
            worktrees_dir=Path(
                os.environ.get("DEV_WORKER_WORKTREES", data.get("worktrees_dir", "~/.jarvis-dev-worker/worktrees"))
            ).expanduser(),
            adapter=os.environ.get("DEV_WORKER_ADAPTER", data.get("adapter", "sdk")),
            limits=ClaudeLimits(
                max_turns=int(data.get("max_turns", 60)),
                max_budget_usd=float(data.get("max_budget_usd", 5.0)),
                model=data.get("model"),
                timeout_seconds=int(data.get("timeout_seconds", 3600)),
            ),
        )


def parse_repos(raw: dict[str, Any]) -> dict[str, RepoConfig]:
    repos: dict[str, RepoConfig] = {}
    for repo_id, cfg in raw.items():
        if not REPO_ID_RE.match(repo_id):
            raise ValueError(f"invalid repo id {repo_id!r}")
        path = Path(str(cfg["path"])).expanduser().resolve()
        for key in ("test", "lint", "typecheck"):
            value = cfg.get(key) or []
            if isinstance(value, str) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{repo_id}.{key} must be an argv list, not a shell string")
        repos[repo_id] = RepoConfig(
            repo_id=repo_id,
            path=path,
            base_branch=cfg.get("base_branch", "main"),
            remote=cfg.get("remote", "origin"),
            test=list(cfg.get("test") or []),
            lint=list(cfg.get("lint") or []),
            typecheck=list(cfg.get("typecheck") or []),
            allowed_bash=list(cfg.get("allowed_bash") or []),
            env_passthrough=list(cfg.get("env_passthrough") or []),
            check_timeout_seconds=int(cfg.get("check_timeout_seconds", 900)),
            push=bool(cfg.get("push", False)),
        )
    return repos


def make_adapter(name: str) -> ClaudeCodeAdapter:
    if name == "fake":
        return FakeClaudeCodeAdapter()
    if name == "cli":
        return CliClaudeCodeAdapter()
    if name == "sdk":
        return SdkClaudeCodeAdapter()
    raise SystemExit(f"unknown adapter {name!r}")


def manual_review_flags(paths: list[str]) -> list[str]:
    flags = []
    for label, needles in MANUAL_REVIEW_PATTERNS.items():
        if any(any(n in p for n in needles) for p in paths):
            flags.append(label)
    return flags


async def execute_job(payload: dict[str, Any], cfg: WorkerConfig, adapter: ClaudeCodeAdapter) -> dict[str, Any]:
    """Run one validated job. Never raises for job-level failures: returns a failed result instead."""
    base: dict[str, Any] = {
        "job_id": payload.get("job_id"),
        "branch": payload.get("branch_name", ""),
        "status": "failed",
        "changed_files": 0,
        "claude_status": "not_run",
        "worker_version": VERSION,
        "tests": {"status": "not_configured"},
        "lint": {"status": "not_configured"},
        "typecheck": {"status": "not_configured"},
    }
    try:
        repo = validate_job(payload, cfg.repos)
    except PolicyViolation as exc:
        return {**base, "status": "rejected", "error": f"policy: {exc}"}
    try:
        ws = await asyncio.to_thread(
            git_ops.prepare_worktree,
            repo.path,
            repo.base_branch,
            repo.remote,
            payload["branch_name"],
            cfg.worktrees_dir,
        )
    except (git_ops.GitError, PolicyViolation, OSError) as exc:
        return {**base, "error": f"workspace: {exc}"}
    base["base_commit"] = ws.base_commit
    (ws.worktree / "DEV_TASK.md").write_text(payload["dev_task_markdown"])
    claude = await adapter.run(ws.worktree, payload["dev_task_markdown"], repo, cfg.limits)
    base.update(claude_status=claude.status, claude_cost_usd=claude.cost_usd, claude_turns=claude.turns)
    try:
        (ws.worktree / "DEV_TASK.md").unlink(missing_ok=True)  # keep the task out of the diff; stored server-side
        commit = await asyncio.to_thread(
            git_ops.commit_all,
            ws,
            f"JARVIS: {payload['branch_name']}\n\nJob {payload['job_id']} (approved {payload['approval_id']}).",
        )
        changed, stat = await asyncio.to_thread(git_ops.diff_stat, ws)
        paths = await asyncio.to_thread(git_ops.changed_paths, ws)
    except git_ops.GitError as exc:
        return {**base, "error": f"commit: {exc}"}
    base.update(commit=commit, changed_files=changed, diff_stat=stat, requires_manual_review=manual_review_flags(paths))
    for name, argv in (("tests", repo.test), ("lint", repo.lint), ("typecheck", repo.typecheck)):
        base[name] = await asyncio.to_thread(
            run_check, argv, ws.worktree, repo.check_timeout_seconds, repo.env_passthrough
        )
    ok = (
        claude.status == "success"
        and bool(commit)
        and all(base[n]["status"] in ("passed", "not_configured") for n in ("tests", "lint", "typecheck"))
    )
    if claude.error:
        base["error"] = claude.error
    elif not commit:
        base["error"] = "Claude produced no changes"
    base["status"] = "succeeded" if ok else "failed"
    if ok and repo.push and repo.remote:
        try:
            await asyncio.to_thread(git_ops.push_branch, ws, repo.remote)
        except git_ops.GitError as exc:  # L7 (review 17.10): a push that failed was still «succeeded»
            base["status"] = "failed"
            base["error"] = f"push failed (branch kept locally): {exc}"
    return base


class ServerClient:
    def __init__(self, cfg: WorkerConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            base_url=cfg.server_url,
            timeout=30.0,
            transport=transport,
            headers={"Authorization": f"Bearer {cfg.token}", "X-Worker-Id": cfg.worker_id},
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def heartbeat(self) -> None:
        await self.http.post("/api/dev-worker/v1/heartbeat", json={"version": VERSION, "repos": sorted(self.cfg.repos)})

    async def claim(self) -> dict[str, Any] | None:
        response = await self.http.post("/api/dev-worker/v1/claim")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        data = response.json()
        job, signature = data.get("job") or {}, str(data.get("signature") or "")
        if not verify_payload_signature(self.cfg.signing_key, job, signature):
            raise PolicyViolation("job signature invalid — refusing to execute")
        return dict(job)

    async def report(self, job_id: str, result: dict[str, Any]) -> None:
        response = await self.http.post(f"/api/dev-worker/v1/jobs/{job_id}/result", json=result)
        response.raise_for_status()

    async def keepalive(self, job_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.http.post(f"/api/dev-worker/v1/jobs/{job_id}/heartbeat")
            except httpx.HTTPError:
                log.warning("job heartbeat failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=300)


async def run_once(cfg: WorkerConfig, client: ServerClient, adapter: ClaudeCodeAdapter) -> str:
    await client.heartbeat()
    try:
        job = await client.claim()
    except PolicyViolation as exc:
        log.error("%s", exc)
        return "rejected"
    if job is None:
        return "idle"
    log.info("claimed job %s for repo %s", job.get("job_id"), job.get("repo_id"))
    stop = asyncio.Event()
    keepalive = asyncio.create_task(client.keepalive(str(job["job_id"]), stop))
    try:
        result = await execute_job(job, cfg, adapter)
    finally:
        stop.set()
        await keepalive
    await client.report(str(job["job_id"]), result)
    return str(result["status"])


async def run_forever(cfg: WorkerConfig, poll_seconds: float = 20.0) -> None:
    adapter = make_adapter(cfg.adapter)
    client = ServerClient(cfg)
    try:
        while True:
            try:
                status = await run_once(cfg, client, adapter)
            except httpx.HTTPError as exc:
                log.warning("server unreachable: %s", type(exc).__name__)
                status = "error"
            if status in ("idle", "error"):
                await asyncio.sleep(poll_seconds)
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="jarvis-dev-worker")
    parser.add_argument("command", choices=["run", "once", "check-config"])
    args = parser.parse_args(argv)
    cfg = WorkerConfig.load()
    if args.command == "check-config":
        for repo in cfg.repos.values():
            ok = (repo.path / ".git").exists()
            print(
                f"{repo.repo_id}: {repo.path} {'OK' if ok else 'NOT A GIT REPO'} base={repo.base_branch} "
                f"push={'on' if repo.push else 'off'}"
            )
        print(f"server={cfg.server_url} adapter={cfg.adapter} worktrees={cfg.worktrees_dir}")
        return 0
    if args.command == "once":

        async def once() -> str:
            client = ServerClient(cfg)
            try:
                return await run_once(cfg, client, make_adapter(cfg.adapter))
            finally:
                await client.aclose()

        print(asyncio.run(once()))
        return 0
    asyncio.run(run_forever(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
