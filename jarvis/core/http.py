"""Resilient outbound HTTP for provider adapters.

* explicit timeouts;
* bounded retries only for transient failures (network, 408/425/429/5xx) and only for idempotent
  requests unless the caller opts in;
* `Retry-After` respected (bounded);
* 4xx auth/validation errors raised immediately as permanent errors;
* a small circuit breaker per provider.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from jarvis.core.errors import (
    AuthError,
    CircuitOpenError,
    NotFoundError,
    PermanentError,
    RateLimitedError,
    TransientError,
)
from jarvis.core.retry import HTTP_RETRY, RetryPolicy

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None
    _monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)

    def before_call(self, provider: str) -> None:
        if self.opened_at is None:
            return
        if self._monotonic() - self.opened_at >= self.reset_after_seconds:
            self.opened_at = None  # half-open: allow one trial call
            self.failures = self.failure_threshold - 1
            return
        raise CircuitOpenError(f"{provider}: circuit open after repeated failures")

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self._monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            return None
    return None


def classify_response(provider: str, response: httpx.Response) -> None:
    """Raise a typed error for a non-2xx response."""
    status = response.status_code
    if status < 400:
        return
    snippet = response.text[:200] if response.content else ""
    message = f"{provider}: HTTP {status}"
    if status in (401, 403):
        raise AuthError(f"{message} (authentication/authorization failed)")
    if status == 404:
        raise NotFoundError(message)
    if status == 429:
        raise RateLimitedError(f"{message} rate limited", retry_after_seconds=_retry_after(response))
    if status in TRANSIENT_STATUS:
        raise TransientError(message, retry_after_seconds=_retry_after(response))
    raise PermanentError(f"{message}: {snippet}")


class ResilientHttp:
    def __init__(
        self,
        provider: str,
        *,
        base_url: str = "",
        timeout: float = 15.0,
        retry: RetryPolicy = HTTP_RETRY,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.retry = retry
        self.breaker = CircuitBreaker()
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            headers=headers or {},
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        retry_non_idempotent: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        method = method.upper()
        may_retry = method in IDEMPOTENT_METHODS or retry_non_idempotent
        attempt = 0
        while True:
            attempt += 1
            self.breaker.before_call(self.provider)
            try:
                response = await self._client.request(method, url, **kwargs)
                classify_response(self.provider, response)
            except httpx.ConnectError as exc:
                # Nothing was sent: safe to retry even for POST.
                error: TransientError = TransientError(f"{self.provider}: connect error {type(exc).__name__}")
                self.breaker.record_failure()
                if attempt < self.retry.max_attempts:
                    await self._sleep(self.retry.delay(attempt, url))
                    continue
                raise error from exc
            except httpx.TimeoutException as exc:
                self.breaker.record_failure()
                if may_retry and attempt < self.retry.max_attempts:
                    await self._sleep(self.retry.delay(attempt, url))
                    continue
                raise TransientError(f"{self.provider}: timeout ({type(exc).__name__})") from exc
            except httpx.TransportError as exc:
                self.breaker.record_failure()
                if may_retry and attempt < self.retry.max_attempts:
                    await self._sleep(self.retry.delay(attempt, url))
                    continue
                raise TransientError(f"{self.provider}: transport error {type(exc).__name__}") from exc
            except TransientError as exc:
                self.breaker.record_failure()
                if may_retry and attempt < self.retry.max_attempts:
                    delay = exc.retry_after_seconds or self.retry.delay(attempt, url)
                    await self._sleep(delay)
                    continue
                raise
            except PermanentError:
                # A permanent error means the service is reachable: don't trip the breaker.
                self.breaker.record_success()
                raise
            self.breaker.record_success()
            return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)
