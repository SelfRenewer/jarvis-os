"""Deterministic bounded exponential backoff."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 5.0
    cap_seconds: float = 900.0
    max_attempts: int = 5
    jitter_ratio: float = 0.2  # 0 disables jitter

    def delay(self, attempt: int, seed: str = "") -> float:
        """Delay before the retry that follows failed attempt number `attempt` (1-based).

        Jitter is derived from a hash of `seed` + attempt, so it is reproducible (tests) while still
        spreading retries of different jobs.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = min(self.base_seconds * (2 ** (attempt - 1)), self.cap_seconds)
        if self.jitter_ratio <= 0:
            return raw
        digest = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
        jitter = raw * self.jitter_ratio * (fraction * 2 - 1)
        return max(0.0, min(self.cap_seconds, raw + jitter))

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts


HTTP_RETRY = RetryPolicy(base_seconds=0.5, cap_seconds=8.0, max_attempts=3, jitter_ratio=0.2)
JOB_RETRY = RetryPolicy(base_seconds=10.0, cap_seconds=1800.0, max_attempts=5, jitter_ratio=0.2)
