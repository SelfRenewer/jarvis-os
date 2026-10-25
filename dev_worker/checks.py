"""Run repository checks (tests / lint / typecheck) as fixed argv lists with a scrubbed environment."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from dev_worker.policy import subprocess_env

_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
_ERRORS = re.compile(r"(\d+) errors?")


def run_check(argv: list[str], cwd: Path, timeout: int, env_names: list[str]) -> dict[str, Any]:
    if not argv:
        return {"status": "not_configured", "summary": ""}
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(env_names),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "summary": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"status": "error", "summary": f"{type(exc).__name__}: {exc}"[:500]}
    output = (result.stdout + "\n" + result.stderr).strip()
    tail = output[-1500:]
    passed = int(m.group(1)) if (m := _PASSED.search(output)) else None
    failed = int(m.group(1)) if (m := _FAILED.search(output)) else None
    if failed is None and (m := _ERRORS.search(tail)) and result.returncode != 0:
        failed = int(m.group(1))
    status = "passed" if result.returncode == 0 else "failed"
    if passed is not None and failed is None:
        failed = 0
    return {"status": status, "passed": passed, "failed": failed, "summary": tail}
