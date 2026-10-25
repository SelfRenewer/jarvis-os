"""Typed errors shared by integrations and the job runner.

The job runner only looks at the class: `TransientError` → retry with backoff, `PermanentError`
→ dead-letter immediately. Adapters must translate provider failures into these classes.
"""

from __future__ import annotations


class JarvisError(Exception):
    """Base class."""


class TransientError(JarvisError):
    """Temporary failure (network, 5xx, rate limit). Safe to retry later."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RateLimitedError(TransientError):
    pass


class PermanentError(JarvisError):
    """Retrying will not help (validation, 4xx, business rule)."""


class AuthError(PermanentError):
    """Credentials missing, invalid or revoked."""


class NotFoundError(PermanentError):
    pass


class NotConfiguredError(PermanentError):
    """Integration has no credentials configured."""


class CapabilityUnavailableError(PermanentError):
    """The provider does not offer this capability through a permitted mechanism."""

    def __init__(self, capability: str, message: str) -> None:
        super().__init__(f"{capability}: {message}")
        self.capability = capability


class LiveActionBlockedError(PermanentError):
    """A billable / externally visible action was blocked by a safety flag."""


class CircuitOpenError(TransientError):
    pass


class LLMOutputError(PermanentError):
    """LLM output failed validation after bounded repair attempts."""


class BudgetExceededError(PermanentError):
    """A per-day cost budget was exhausted."""
