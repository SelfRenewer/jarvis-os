"""LLM provider layer.

One client, many capabilities (intent routing, idea analysis, spec writing, product matching…).
Machine-readable output is always:
  1. constrained with a JSON schema (Anthropic structured outputs), and
  2. validated with Pydantic by Jarvis;
malformed output gets at most one repair attempt, then a typed `LLMOutputError`.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from jarvis.core.errors import (
    AuthError,
    LLMOutputError,
    PermanentError,
    RateLimitedError,
    TransientError,
)
from jarvis.core.http import ResilientHttp

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_STRIP_KEYS = {
    "title",
    "default",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "examples",
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → structured-output-friendly schema (inlined refs, closed objects)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[name]))
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    # Keys here are FIELD NAMES (a field may be called "title") — never strip them.
                    out[key] = {name: resolve(sub) for name, sub in value.items()}
                elif key not in _STRIP_KEYS:
                    out[key] = resolve(value)
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return dict(resolve(raw))


@dataclass
class RawOutput:
    text: str
    stop_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class WebAnswer:
    text: str
    sources: list[tuple[str, str]]  # (title, url) from the answer's url_citation annotations
    cost_usd: Decimal | None  # usage.cost of the request (search + tokens)
    model: str


@dataclass
class LLMRunRecord:
    capability: str
    model: str
    status: str
    attempts: int
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    error: str | None = None


Image = tuple[str, bytes]  # (mime type, bytes) — e.g. a Telegram photo of a food label
RunHook = Callable[[LLMRunRecord], Awaitable[None]]
BudgetHook = Callable[[], Awaitable[None]]


class LLMClient(Protocol):
    model: str

    async def structured(
        self,
        *,
        capability: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4000,
        images: list[Image] | None = None,
    ) -> T: ...

    async def chat(
        self,
        *,
        capability: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        model: str | None = None,
    ) -> dict[str, Any]: ...


class StructuredLLMBase:
    model: str = "unknown"
    max_attempts: int = 2

    def __init__(self, *, on_run: RunHook | None = None, budget: BudgetHook | None = None) -> None:
        self._on_run = on_run
        self._budget = budget

    def model_for(self, capability: str) -> str:
        return self.model

    async def _raw_call(
        self,
        capability: str,
        system: str,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any],
        max_tokens: int,
        images: list[Image] | None = None,
    ) -> RawOutput:
        """`images` belong to the first user message (repair turns keep them)."""
        raise NotImplementedError

    async def structured(
        self,
        *,
        capability: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4000,
        images: list[Image] | None = None,
    ) -> T:
        json_schema = strict_json_schema(schema)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        started = time.monotonic()
        tokens_in = tokens_out = 0
        last_error = "no attempt"
        status = "invalid_output"
        for attempt in range(1, self.max_attempts + 1):
            if self._budget is not None:
                await self._budget()
            try:
                raw = await self._raw_call(capability, system, messages, json_schema, max_tokens, images)
            except Exception as exc:
                await self._record(capability, "error", attempt, started, tokens_in, tokens_out, str(exc))
                raise
            tokens_in += raw.input_tokens or 0
            tokens_out += raw.output_tokens or 0
            if raw.stop_reason == "refusal":
                status, last_error = "refusal", "model declined the request"
                break
            if raw.stop_reason == "max_tokens":
                last_error = "output truncated (max_tokens)"
                max_tokens = min(max_tokens * 2, 16000)
                continue
            try:
                result = schema.model_validate(json.loads(raw.text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                messages = [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": raw.text[:8000] or "{}"},
                    {
                        "role": "user",
                        "content": "The previous JSON was invalid: "
                        f"{last_error}. Return corrected JSON that satisfies the schema.",
                    },
                ]
                continue
            await self._record(capability, "ok", attempt, started, tokens_in, tokens_out, None)
            return result
        await self._record(capability, status, self.max_attempts, started, tokens_in, tokens_out, last_error)
        raise LLMOutputError(f"{capability}: {last_error}")

    async def chat(
        self,
        *,
        capability: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        model: str | None = None,
    ) -> dict[str, Any]:
        """A chat turn with optional tool calling (OpenAI format): the assistant message (`content`,
        `tool_calls`). Only OpenRouter implements it; elsewhere the caller falls back."""
        from jarvis.core.errors import NotConfiguredError

        raise NotConfiguredError(f"{type(self).__name__}: chat with tools is not supported")

    async def _record(
        self, capability: str, status: str, attempts: int, started: float, tin: int, tout: int, error: str | None
    ) -> None:
        if self._on_run is None:
            return
        try:
            await self._on_run(
                LLMRunRecord(
                    capability,
                    self.model_for(capability),
                    status,
                    attempts,
                    int((time.monotonic() - started) * 1000),
                    tin or None,
                    tout or None,
                    error,
                )
            )
        except Exception:  # pragma: no cover
            log.exception("failed to record LLM run")


class AnthropicLLM(StructuredLLMBase):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: str = "low",
        enable_fallbacks: bool = True,
        timeout: float = 60.0,
        on_run: RunHook | None = None,
        budget: BudgetHook | None = None,
    ) -> None:
        super().__init__(on_run=on_run, budget=budget)
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self._effort = effort
        self._fallbacks = enable_fallbacks

    async def aclose(self) -> None:
        await self._client.close()

    async def _raw_call(
        self,
        capability: str,
        system: str,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any],
        max_tokens: int,
        images: list[Image] | None = None,
    ) -> RawOutput:
        a = self._anthropic
        if images:
            blocks: list[dict[str, Any]] = [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}
                for mime, data in images
            ]
            messages = [{**messages[0], "content": [*blocks, {"type": "text", "text": messages[0]["content"]}]},
                        *messages[1:]]  # fmt: skip
        extra_headers: dict[str, str] = {}
        extra_body: dict[str, Any] = {}
        if self._fallbacks:
            extra_headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            extra_body["fallbacks"] = "default"
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"format": {"type": "json_schema", "schema": json_schema}, "effort": self._effort},
            "extra_headers": extra_headers or None,
            "extra_body": extra_body or None,
        }
        try:
            response = await self._client.messages.create(**request)
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise AuthError(f"anthropic: {type(exc).__name__}") from exc
        except a.NotFoundError as exc:
            raise PermanentError(f"anthropic: model {self.model!r} not found") from exc
        except a.RateLimitError as exc:
            raise RateLimitedError("anthropic: rate limited") from exc
        except a.BadRequestError as exc:
            raise PermanentError(f"anthropic: bad request: {exc.message[:300]}") from exc
        except (a.APITimeoutError, a.APIConnectionError) as exc:
            raise TransientError(f"anthropic: {type(exc).__name__}") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientError(f"anthropic: HTTP {exc.status_code}") from exc
            raise PermanentError(f"anthropic: HTTP {exc.status_code}") from exc
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = response.usage
        return RawOutput(
            text=text,
            stop_reason=response.stop_reason,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

    async def probe(self) -> dict[str, Any]:
        model = await self._client.models.retrieve(self.model)
        return {"id": model.id, "display_name": getattr(model, "display_name", None)}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


OPENROUTER_URL = "https://openrouter.ai/api/v1/"
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class OpenRouterLLM(StructuredLLMBase):
    """OpenRouter (OpenAI-compatible) with JSON-schema structured outputs and a model per capability.

    Only endpoints that honour `response_format` are used (`provider.require_parameters=true`); the
    result is still validated with Pydantic by the shared base class.
    """

    def __init__(
        self,
        api_key: str,
        default_model: str,
        *,
        models: dict[str, str] | None = None,
        reasoning_effort: str | None = "low",
        timeout: float = 60.0,
        on_run: RunHook | None = None,
        budget: BudgetHook | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        no_reasoning: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(on_run=on_run, budget=budget)
        self.model = default_model
        self.models = dict(models or {})
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.no_reasoning = no_reasoning  # capabilities that must answer fast (a voice call): no reasoning tokens
        self._http = ResilientHttp(
            "openrouter",
            base_url=OPENROUTER_URL,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "JARVIS"},
            transport=transport,
        )

    def model_for(self, capability: str) -> str:
        return self.models.get(capability, self.model)

    def configured_models(self) -> set[str]:
        return {self.model, *self.models.values()}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _raw_call(
        self,
        capability: str,
        system: str,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any],
        max_tokens: int,
        images: list[Image] | None = None,
    ) -> RawOutput:
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": messages[0]["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{_b64(data)}"}}
                      for mime, data in images]  # fmt: skip
            messages = [{**messages[0], "content": parts}, *messages[1:]]
        body: dict[str, Any] = {
            "model": self.model_for(capability),
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": capability, "strict": True, "schema": json_schema},
            },
            "provider": {"require_parameters": True},
        }
        if self.reasoning_effort and capability not in self.no_reasoning:
            body["reasoning"] = {"effort": self.reasoning_effort}
        # Generation is side-effect free, so a timeout may be retried; 4xx (auth, credits, bad model) are not.
        data = (await self._http.post("chat/completions", json=body, retry_non_idempotent=True)).json()
        if data.get("error"):
            code = int((data["error"] or {}).get("code") or 0)
            message = str((data["error"] or {}).get("message") or "error")[:300]
            if code in (401, 403):
                raise AuthError(f"openrouter: {message}")
            if code in (408, 429, 502, 503) or code >= 500:
                raise TransientError(f"openrouter: {message}")
            raise PermanentError(f"openrouter: {message}")
        choices = data.get("choices") or []
        if not choices:
            raise TransientError("openrouter: empty response")
        choice = choices[0]
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if match := _FENCE.match(content):
            content = match.group(1)
        finish = choice.get("finish_reason")
        stop_reason = {"length": "max_tokens", "content_filter": "refusal"}.get(str(finish), "end_turn")
        usage = data.get("usage") or {}
        return RawOutput(content, stop_reason, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    async def chat(
        self,
        *,
        capability: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        model: str | None = None,
    ) -> dict[str, Any]:
        if self._budget is not None:
            await self._budget()
        started = time.monotonic()
        body: dict[str, Any] = {
            "model": model or self.model_for(capability),
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.reasoning_effort and capability not in self.no_reasoning:
            body["reasoning"] = {"effort": self.reasoning_effort}
        try:
            data = (await self._http.post("chat/completions", json=body, retry_non_idempotent=True)).json()
        except Exception as exc:
            await self._record(capability, "error", 1, started, 0, 0, str(exc))
            raise
        if data.get("error"):
            message = str((data["error"] or {}).get("message") or "error")[:300]
            await self._record(capability, "error", 1, started, 0, 0, message)
            raise TransientError(f"openrouter: {message}")
        choices = data.get("choices") or []
        if not choices:
            raise TransientError("openrouter: empty response")
        usage = data.get("usage") or {}
        await self._record(
            capability, "ok", 1, started, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0, None
        )
        return dict(choices[0].get("message") or {})

    async def web_answer(
        self,
        *,
        system: str,
        user: str,
        max_results: int = 5,
        max_tokens: int = 1800,
        engine: str = "exa",
        model: str | None = None,
        timeout: float = 90.0,
    ) -> WebAnswer:
        """A text answer grounded in a web search (the `web` plugin; Exa costs a fixed price per request).

        Paid per request, so it is never retried automatically (a timeout may still have been billed); sources come
        from the message `annotations` (url_citation); the real price from `usage.cost`."""
        if self._budget is not None:
            await self._budget()
        chosen = model or self.model_for("research")
        body: dict[str, Any] = {
            "model": chosen,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "plugins": [{"id": "web", "engine": engine, "max_results": max_results}],
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        started = time.monotonic()
        try:
            data = (await self._http.post("chat/completions", json=body, timeout=timeout)).json()
            if data.get("error"):
                code = int((data["error"] or {}).get("code") or 0)
                message = str((data["error"] or {}).get("message") or "error")[:300]
                if code in (401, 403):
                    raise AuthError(f"openrouter: {message}")
                kind = TransientError if code in (408, 429) or code >= 500 else PermanentError
                raise kind(f"openrouter: {message}")
            choice = (data.get("choices") or [{}])[0]
            message_body = choice.get("message") or {}
            content = message_body.get("content") or ""
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if not str(content).strip():
                raise TransientError("openrouter: empty research answer")
        except Exception as exc:
            await self._record("research", "error", 1, started, 0, 0, str(exc))
            raise
        sources: list[tuple[str, str]] = []
        for note in message_body.get("annotations") or []:
            cite = note.get("url_citation") or {} if isinstance(note, dict) else {}
            url = str(cite.get("url") or "")
            if url.startswith(("https://", "http://")) and url not in [u for _, u in sources]:
                sources.append((str(cite.get("title") or "").strip(), url))
        usage = data.get("usage") or {}
        try:
            cost = Decimal(str(usage["cost"])) if usage.get("cost") is not None else None
        except InvalidOperation:
            cost = None
        tokens_in, tokens_out = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        await self._record("research", "ok", 1, started, tokens_in, tokens_out, None)
        return WebAnswer(str(content), sources, cost, chosen)

    async def usage(self) -> dict[str, Any]:
        """Spend of this key in USD (usage_daily/weekly/monthly, limit, limit_remaining). Free call."""
        return dict((await self._http.get("key")).json().get("data") or {})

    async def credits(self) -> dict[str, Any]:
        """Account balance in USD (total_credits, total_usage — all keys of the account). Free call."""
        return dict((await self._http.get("credits")).json().get("data") or {})

    async def probe(self) -> dict[str, Any]:
        """Key validity + every configured model supports structured outputs (no generation, no cost)."""
        key = (await self._http.get("key")).json().get("data") or {}
        listing = (await self._http.get("models")).json().get("data") or []
        supported = {m["id"] for m in listing if "structured_outputs" in (m.get("supported_parameters") or [])}
        missing = sorted(self.configured_models() - supported)
        if missing:
            raise PermanentError(f"models without structured-output support or unknown: {', '.join(missing)}")
        return {"id": ", ".join(sorted(self.configured_models())), "limit_remaining": key.get("limit_remaining")}


FakeResponder = Callable[[str, list[dict[str, Any]]], "str | dict[str, Any] | BaseModel | RawOutput"]


@dataclass
class FakeLLM(StructuredLLMBase):
    """Scripted LLM. `responders[capability](user_text, messages)` returns JSON text / dict / model."""

    responders: dict[str, FakeResponder] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    # chat with tools: `chat_responder(messages, tools)` returns the assistant message dict
    chat_responder: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]] | None = None
    chats: list[list[dict[str, Any]]] = field(default_factory=list)
    images: list[tuple[str, list[Image]]] = field(default_factory=list)  # (capability, images) per call with images
    model: str = "fake-llm"

    def __post_init__(self) -> None:
        StructuredLLMBase.__init__(self)

    async def _raw_call(
        self,
        capability: str,
        system: str,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any],
        max_tokens: int,
        images: list[Image] | None = None,
    ) -> RawOutput:
        user_text = str(messages[0]["content"])
        self.calls.append((capability, user_text))
        if images:
            self.images.append((capability, list(images)))
        responder = self.responders.get(capability)
        if responder is None:
            from jarvis.core.errors import NotConfiguredError

            raise NotConfiguredError(f"fake llm: no responder for {capability}")
        value = responder(user_text, messages)
        if isinstance(value, RawOutput):
            return value
        if isinstance(value, BaseModel):
            return RawOutput(value.model_dump_json(), "end_turn")
        if isinstance(value, dict):
            return RawOutput(json.dumps(value, ensure_ascii=False), "end_turn")
        return RawOutput(str(value), "end_turn")

    async def chat(
        self,
        *,
        capability: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        model: str | None = None,
    ) -> dict[str, Any]:
        self.chats.append(list(messages))
        if self.chat_responder is None:
            from jarvis.core.errors import NotConfiguredError

            raise NotConfiguredError("fake llm: no chat_responder")
        return self.chat_responder(messages, tools)
