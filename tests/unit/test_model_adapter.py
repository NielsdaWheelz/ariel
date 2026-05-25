from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_ai.embeddings.base import EmbeddingModel
from pydantic_ai.embeddings.test import TestEmbeddingModel
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse as PydAIModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from ariel.config import AppSettings
from ariel.model_adapter import ModelAdapter, ModelCall, ToolSpec
from ariel.models import MAIN, ModelRef
from ariel.response_contracts import ResponseContractViolation


class _Out(BaseModel):
    answer: str
    count: int


def _settings() -> AppSettings:
    # The autouse hermetic-AppSettings fixture in tests/conftest.py already
    # neutralizes env_file; a bare AppSettings() resolves from defaults.
    return AppSettings()


def _msgs(prompt: str = "hi") -> list[ModelMessage]:
    return [ModelRequest(parts=[UserPromptPart(content=prompt)])]


class _ProbeAdapter(ModelAdapter):
    """Test adapter: returns canned substrate from the per-provider build hooks.

    Production caching, dispatch, and response shaping run unchanged; only
    ``_build_model``/``_build_embedder`` are overridden to skip the real
    provider construction (which would require API credentials).
    """

    def __init__(
        self,
        *,
        substrate: Model | None = None,
        embedder: EmbeddingModel | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        super().__init__(settings or _settings())
        self._probe_substrate = substrate
        self._probe_embedder = embedder

    def _build_model(self, ref: ModelRef) -> Model:
        if self._probe_substrate is not None:
            return self._probe_substrate
        return super()._build_model(ref)

    def _build_embedder(self, ref: ModelRef) -> EmbeddingModel:
        if self._probe_embedder is not None:
            return self._probe_embedder
        return super()._build_embedder(ref)


def _adapter_with(substrate: Model) -> ModelAdapter:
    return _ProbeAdapter(substrate=substrate)


def test_call_routes_to_ref() -> None:
    adapter = _adapter_with(TestModel(custom_output_text="hello"))

    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))

    assert response.text == "hello"
    assert response.provider == MAIN.provider
    assert response.model == MAIN.model
    assert response.tool_calls == []
    assert response.structured_output is None
    assert response.usage.input_tokens > 0


def test_call_extracts_tool_calls() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[ToolCallPart(tool_name="lookup", args={"q": "x"}, tool_call_id="c1")]
        )

    adapter = _adapter_with(FunctionModel(fn))
    tools = [ToolSpec(name="lookup", description="d", parameters={"type": "object"})]
    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs(), tools=tools)))

    assert response.text is None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.tool_calls[0].call_id == "c1"


def test_call_marks_function_tools_strict_for_provider_schema() -> None:
    def fn(_msgs: list[ModelMessage], info: AgentInfo) -> PydAIModelResponse:
        assert len(info.function_tools) == 1
        assert info.function_tools[0].strict is True
        return PydAIModelResponse(
            parts=[ToolCallPart(tool_name="lookup", args={}, tool_call_id="c1")]
        )

    adapter = _adapter_with(FunctionModel(fn))
    tools = [
        ToolSpec(
            name="lookup",
            description="d",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
    ]

    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs(), tools=tools)))

    assert response.tool_calls[0].name == "lookup"


def test_call_parses_string_tool_call_arguments() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[ToolCallPart(tool_name="lookup", args='{"q": "x"}', tool_call_id="c1")]
        )

    adapter = _adapter_with(FunctionModel(fn))
    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))

    assert response.tool_calls[0].arguments == {"q": "x"}


def test_call_raises_contract_violation_on_malformed_tool_args() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[ToolCallPart(tool_name="lookup", args="{not-json", tool_call_id="c1")]
        )

    adapter = _adapter_with(FunctionModel(fn))
    with pytest.raises(ResponseContractViolation):
        asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))


def test_call_validates_structured_output() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[TextPart(content=json.dumps({"answer": "yes", "count": 5}))]
        )

    adapter = _adapter_with(FunctionModel(fn))
    response = asyncio.run(
        adapter.call(ModelCall(model=MAIN, messages=_msgs(), response_format=_Out))
    )

    assert isinstance(response.structured_output, _Out)
    assert response.structured_output.answer == "yes"
    assert response.structured_output.count == 5


def test_call_raises_contract_violation_on_structured_output_mismatch() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(parts=[TextPart(content='{"answer": "yes"}')])

    adapter = _adapter_with(FunctionModel(fn))
    with pytest.raises(ResponseContractViolation) as info:
        asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs(), response_format=_Out)))

    assert info.value.contract == "_Out"


def test_call_captures_thinking_parts_as_reasoning_summary() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[ThinkingPart(content="reasoning"), TextPart(content="answer")]
        )

    adapter = _adapter_with(FunctionModel(fn))
    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))

    assert response.text == "answer"
    assert response.reasoning_summary == "reasoning"


def test_call_lifts_provider_response_id_and_usage_details() -> None:
    def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        return PydAIModelResponse(
            parts=[TextPart(content="ok")],
            usage=RequestUsage(
                input_tokens=12,
                output_tokens=3,
                cache_read_tokens=4,
                details={"reasoning_tokens": 7},
            ),
            provider_response_id="resp_abc",
        )

    adapter = _adapter_with(FunctionModel(fn))
    response = asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))

    assert response.provider_response_id == "resp_abc"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.usage.cached_tokens == 4
    assert response.usage.reasoning_tokens == 7


def test_call_uses_configured_timeout() -> None:
    async def fn(_msgs: list[ModelMessage], _info: AgentInfo) -> PydAIModelResponse:
        await asyncio.sleep(0.2)
        return PydAIModelResponse(parts=[TextPart(content="late")])

    settings = _settings().model_copy(update={"model_timeout_seconds": 0.01})
    adapter = _ProbeAdapter(substrate=FunctionModel(fn), settings=settings)

    with pytest.raises(TimeoutError):
        asyncio.run(adapter.call(ModelCall(model=MAIN, messages=_msgs())))


def test_openrouter_uses_its_own_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeProvider:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            calls.append({"kind": "provider", "api_key": api_key, "base_url": base_url})

    class FakeChatModel:
        def __init__(self, model: str, *, provider: object) -> None:
            calls.append({"kind": "model", "model": model, "provider": provider})

    monkeypatch.setattr("ariel.model_adapter.OpenAIProvider", FakeProvider)
    monkeypatch.setattr("ariel.model_adapter.OpenAIChatModel", FakeChatModel)
    settings = AppSettings(
        openai_base_url="https://openai.example.test/v1",
        openrouter_api_key="openrouter-key",
        openrouter_base_url="https://openrouter.example.test/api/v1",
    )

    ModelAdapter(settings)._build_model(ModelRef(provider="openrouter", model="router-model"))

    assert calls[0] == {
        "kind": "provider",
        "api_key": "openrouter-key",
        "base_url": "https://openrouter.example.test/api/v1",
    }
    assert calls[1]["kind"] == "model"
    assert calls[1]["model"] == "router-model"


def test_embed_returns_vector_per_input() -> None:
    adapter = _ProbeAdapter(embedder=TestEmbeddingModel(dimensions=4))

    vectors = asyncio.run(adapter.embed(["a", "b", "c"]))

    assert len(vectors) == 3
    assert all(len(v) == 4 for v in vectors)


def test_model_call_is_frozen() -> None:
    call = ModelCall(model=MAIN, messages=_msgs())
    with pytest.raises(ValidationError):
        call.model = ModelRef(provider="openai", model="gpt-5")  # type: ignore[misc]  # justify-test-invariant


def test_model_call_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ModelCall(
            model=MAIN,
            messages=_msgs(),
            unknown="x",  # type: ignore[call-arg]  # justify-test-invariant
        )


def test_model_ref_rejects_unsupported_provider() -> None:
    with pytest.raises(ValidationError):
        ModelRef(provider="bogus-provider", model="x")  # type: ignore[arg-type]  # justify-test-invariant
