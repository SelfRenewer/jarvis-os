"""LLM layer: schema generation, validation, bounded repair, refusal handling, real SDK request shape."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from jarvis.core.errors import LLMOutputError
from jarvis.ideas.schemas import DevSpecification, IdeaAnalysis
from jarvis.integrations.llm.client import FakeLLM, RawOutput, strict_json_schema


def test_strict_schema_keeps_fields_named_like_keywords() -> None:
    """R12 (found live with Gemini): the field `title` was stripped together with schema metadata `title`."""
    schema = strict_json_schema(IdeaAnalysis)
    assert "title" in schema["properties"]
    assert "title" in schema["required"]
    assert set(schema["required"]) == set(IdeaAnalysis.model_fields)
    assert "title" not in schema["properties"]["summary"]  # metadata still stripped inside field schemas


def test_strict_schema_is_closed_and_inlined() -> None:
    schema = strict_json_schema(DevSpecification)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "$defs" not in json.dumps(schema)
    assert "title" not in schema


async def test_malformed_output_gets_one_repair_then_succeeds() -> None:
    responses = iter(["{not json", {"title": "X", "summary": "Y"}])
    llm = FakeLLM(responders={"idea_analyzer": lambda u, m: next(responses)})
    result = await llm.structured(capability="idea_analyzer", system="s", user="u", schema=IdeaAnalysis)
    assert result.title == "X"
    assert len(llm.calls) == 2


async def test_invalid_output_twice_raises_typed_error() -> None:
    llm = FakeLLM(responders={"idea_analyzer": lambda u, m: {"title": "", "summary": ""}})
    with pytest.raises(LLMOutputError):
        await llm.structured(capability="idea_analyzer", system="s", user="u", schema=IdeaAnalysis)
    assert len(llm.calls) == 2  # bounded: exactly one repair attempt


async def test_refusal_is_not_retried() -> None:
    llm = FakeLLM(responders={"x": lambda u, m: RawOutput("", "refusal")})
    with pytest.raises(LLMOutputError, match="declined"):
        await llm.structured(capability="x", system="s", user="u", schema=IdeaAnalysis)
    assert len(llm.calls) == 1


async def test_hallucinated_fields_rejected() -> None:
    # Extra keys (e.g. an invented task id) do not leak into the validated object.
    llm = FakeLLM(responders={"i": lambda u, m: {"title": "T", "summary": "S", "task_id": "123"}})
    result = await llm.structured(capability="i", system="s", user="u", schema=IdeaAnalysis)
    assert not hasattr(result, "task_id")


async def test_anthropic_adapter_request_shape() -> None:
    """Exercise the real `anthropic` SDK against an in-process mock transport (no network, no cost)."""
    import anthropic
    import httpx2

    from jarvis.integrations.llm.client import AnthropicLLM

    captured: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": json.dumps({"title": "AI Inbox", "summary": "S"})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    llm = AnthropicLLM("sk-test", "claude-opus-5", effort="low")
    llm._client = anthropic.AsyncAnthropic(  # type: ignore[attr-defined]
        api_key="sk-test",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    result = await llm.structured(capability="idea_analyzer", system="sys", user="text", schema=IdeaAnalysis)
    assert result.title == "AI Inbox"
    body = captured["body"]
    assert body["model"] == "claude-opus-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"
    assert body["fallbacks"] == "default"
    assert captured["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    await llm.aclose()


# --- OpenRouter ------------------------------------------------------------------------------------

OR = "https://openrouter.ai/api/v1/"


def _completion(content: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "gen-1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


@respx.mock
async def test_openrouter_request_shape_and_per_task_model() -> None:
    from jarvis.integrations.llm.client import OpenRouterLLM

    route = respx.post(OR + "chat/completions").mock(
        return_value=httpx.Response(200, json=_completion('```json\n{"title": "AI Inbox", "summary": "S"}\n```'))
    )
    llm = OpenRouterLLM("or-key", "anthropic/claude-opus-5", models={"intent_router": "google/gemini-3.8-flash"})
    result = await llm.structured(capability="idea_analyzer", system="sys", user="text", schema=IdeaAnalysis)
    assert result.title == "AI Inbox"  # code fences stripped, then Pydantic-validated
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "anthropic/claude-opus-5"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"effort": "low"}
    assert route.calls[0].request.headers["authorization"] == "Bearer or-key"
    assert llm.model_for("intent_router") == "google/gemini-3.8-flash"
    await llm.aclose()


@respx.mock
async def test_openrouter_truncation_and_content_filter() -> None:
    from jarvis.integrations.llm.client import OpenRouterLLM

    respx.post(OR + "chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=_completion('{"title": "x"', "length")),
            httpx.Response(200, json=_completion('{"title": "T", "summary": "S"}')),
        ]
    )
    llm = OpenRouterLLM("k", "m/1")
    assert (await llm.structured(capability="c", system="s", user="u", schema=IdeaAnalysis)).title == "T"
    respx.post(OR + "chat/completions").mock(return_value=httpx.Response(200, json=_completion("", "content_filter")))
    with pytest.raises(LLMOutputError, match="declined"):
        await llm.structured(capability="c", system="s", user="u", schema=IdeaAnalysis)
    await llm.aclose()


@respx.mock
async def test_openrouter_errors_are_typed() -> None:
    from jarvis.core.errors import AuthError, PermanentError, TransientError
    from jarvis.integrations.llm.client import OpenRouterLLM

    llm = OpenRouterLLM("k", "m/1")
    respx.post(OR + "chat/completions").mock(return_value=httpx.Response(402, json={"error": {"code": 402}}))
    with pytest.raises(PermanentError):  # out of credits: never retried
        await llm.structured(capability="c", system="s", user="u", schema=IdeaAnalysis)
    respx.post(OR + "chat/completions").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await llm.structured(capability="c", system="s", user="u", schema=IdeaAnalysis)
    respx.post(OR + "chat/completions").mock(
        return_value=httpx.Response(200, json={"error": {"code": 502, "message": "upstream down"}})
    )
    with pytest.raises(TransientError):
        await llm.structured(capability="c", system="s", user="u", schema=IdeaAnalysis)
    await llm.aclose()


@respx.mock
async def test_openrouter_probe_checks_structured_output_support() -> None:
    from jarvis.core.errors import PermanentError
    from jarvis.integrations.llm.client import OpenRouterLLM

    respx.get(OR + "key").mock(return_value=httpx.Response(200, json={"data": {"limit_remaining": 5}}))
    respx.get(OR + "models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "anthropic/claude-opus-5", "supported_parameters": ["structured_outputs", "tools"]},
                    {"id": "some/old-model", "supported_parameters": ["tools"]},
                ]
            },
        )
    )
    ok = OpenRouterLLM("k", "anthropic/claude-opus-5")
    assert (await ok.probe())["id"] == "anthropic/claude-opus-5"
    bad = OpenRouterLLM("k", "anthropic/claude-opus-5", models={"intent_router": "some/old-model"})
    with pytest.raises(PermanentError, match="some/old-model"):
        await bad.probe()
    await ok.aclose()
    await bad.aclose()


def test_provider_selection() -> None:
    from jarvis.integrations.llm.client import AnthropicLLM, OpenRouterLLM
    from jarvis.services.container import build_container
    from tests.conftest import make_settings

    def llm_for(**overrides: object) -> object:
        return build_container(make_settings(jarvis_env="dev", **overrides)).llm

    assert isinstance(llm_for(openrouter_api_key="or", anthropic_api_key="an"), OpenRouterLLM)
    assert isinstance(llm_for(anthropic_api_key="an"), AnthropicLLM)
    assert isinstance(llm_for(openrouter_api_key="or", anthropic_api_key="an", llm_provider="anthropic"), AnthropicLLM)
    assert llm_for(llm_provider="openrouter") is None
