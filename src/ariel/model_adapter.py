from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.embeddings.base import EmbeddingModel
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.messages import (
    ModelMessage as ModelMessage,
    ModelResponse as _PydAIModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .config import AppSettings
from .model_tiers import ModelTier, TierBinding, resolve_tier
from .response_contracts import ResponseContractViolation


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ReasoningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effort: Literal["minimal", "low", "medium", "high"] | None = None
    max_thinking_tokens: int | None = None


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cached_tokens: int = 0


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    arguments: dict[str, Any]


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    tier: ModelTier
    messages: list[ModelMessage]
    tools: list[ToolSpec] = []
    tool_choice: Literal["auto", "required", "none"] = "auto"
    response_format: type[BaseModel] | None = None
    reasoning: ReasoningConfig | None = None
    max_output_tokens: int | None = None
    metadata: dict[str, Any] = {}


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    text: str | None
    tool_calls: list[ToolCall]
    structured_output: BaseModel | None
    reasoning_summary: str | None
    usage: TokenUsage
    provider: str
    model: str
    tier: ModelTier
    duration_ms: int
    provider_response_id: str | None


class ModelAdapter:
    """Thin tier-routed adapter over Pydantic AI per-provider Models.

    Construction resolves every tier to a single concrete substrate Model and
    caches it. ``call`` translates an ``ModelCall`` into a pydantic-ai
    ``Model.request``; ``embed`` dispatches to the EMBEDDING tier's
    ``EmbeddingModel``. Callers wrap calls in their own ``evt.model.*``
    telemetry — the adapter is pure (input → output or raised exception).
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._tiers: dict[ModelTier, TierBinding] = {
            tier: resolve_tier(tier, self._tier_override(settings, tier)) for tier in ModelTier
        }
        # Lazy: building a Pydantic AI provider eagerly validates creds, which
        # would block startup when (say) ANTHROPIC_API_KEY is unset even though
        # the user only ever calls BULK/STRUCTURED tiers. Defer to first use.
        self._models: dict[ModelTier, Model] = {}
        self._embedder: EmbeddingModel | None = None

    @staticmethod
    def _tier_override(settings: AppSettings, tier: ModelTier) -> str | None:
        # Each tier maps 1:1 to a config field; missing creds are validated at
        # the per-call boundary (the provider client raises if asked to dispatch
        # without an API key).
        return {
            ModelTier.MAIN: settings.model_tier_main,
            ModelTier.BULK: settings.model_tier_bulk,
            ModelTier.STRUCTURED: settings.model_tier_structured,
            ModelTier.CODING: settings.model_tier_coding,
            ModelTier.VISION: settings.model_tier_vision,
            ModelTier.EMBEDDING: settings.model_tier_embedding,
        }[tier]

    def _build_model(self, binding: TierBinding) -> Model:
        s = self._settings
        if binding.provider == "openai":
            return OpenAIResponsesModel(
                binding.model,
                provider=OpenAIProvider(api_key=s.openai_api_key, base_url=s.openai_base_url),
            )
        if binding.provider == "openrouter":
            return OpenAIResponsesModel(
                binding.model,
                provider=OpenAIProvider(
                    api_key=s.openrouter_api_key,
                    base_url=s.openai_base_url or "https://openrouter.ai/api/v1",
                ),
            )
        if binding.provider == "anthropic":
            return AnthropicModel(
                binding.model, provider=AnthropicProvider(api_key=s.anthropic_api_key)
            )
        if binding.provider == "google":
            return GoogleModel(binding.model, provider=GoogleProvider(api_key=s.google_api_key))
        raise ValueError(f"unsupported provider {binding.provider!r} for tier model")

    def _build_embedder(self, binding: TierBinding) -> EmbeddingModel:
        s = self._settings
        if binding.provider == "openai":
            return OpenAIEmbeddingModel(
                binding.model,
                provider=OpenAIProvider(api_key=s.openai_api_key, base_url=s.openai_base_url),
            )
        raise ValueError(
            f"EMBEDDING tier provider {binding.provider!r} not yet supported; only openai"
        )

    # Test-injection seam: substitute a substrate model under unit test.
    def _override_model(self, tier: ModelTier, model: Model) -> None:
        self._models[tier] = model

    def _override_embedder(self, embedder: EmbeddingModel) -> None:
        self._embedder = embedder

    def _get_model(self, tier: ModelTier) -> Model:
        model = self._models.get(tier)
        if model is None:
            model = self._build_model(self._tiers[tier])
            self._models[tier] = model
        return model

    def _get_embedder(self) -> EmbeddingModel:
        if self._embedder is None:
            self._embedder = self._build_embedder(self._tiers[ModelTier.EMBEDDING])
        return self._embedder

    async def call(self, request: ModelCall) -> ModelResponse:
        if request.tier is ModelTier.EMBEDDING:
            raise ValueError("EMBEDDING tier is dispatched via .embed(), not .call()")
        binding = self._tiers[request.tier]
        model = self._get_model(request.tier)

        function_tools = [
            ToolDefinition(
                name=t.name, description=t.description, parameters_json_schema=t.parameters
            )
            for t in request.tools
        ]
        if request.response_format is not None:
            output_object = OutputObjectDefinition(
                json_schema=request.response_format.model_json_schema(),
                name=request.response_format.__name__,
                strict=True,
            )
            params = ModelRequestParameters(
                function_tools=function_tools,
                output_mode="native",
                output_object=output_object,
            )
        else:
            params = ModelRequestParameters(function_tools=function_tools)

        model_settings: ModelSettings = {"tool_choice": request.tool_choice}
        if request.max_output_tokens is not None:
            model_settings["max_tokens"] = request.max_output_tokens
        if request.reasoning is not None and request.reasoning.effort is not None:
            model_settings["thinking"] = request.reasoning.effort

        started_at = time.perf_counter()
        raw = await model.request(request.messages, model_settings, params)
        duration_ms = int((time.perf_counter() - started_at) * 1000)

        return self._build_response(
            raw=raw,
            request=request,
            binding=binding,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _build_response(
        *,
        raw: _PydAIModelResponse,
        request: ModelCall,
        binding: TierBinding,
        duration_ms: int,
    ) -> ModelResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in raw.parts:
            if isinstance(part, TextPart):
                text_parts.append(part.content)
            elif isinstance(part, ToolCallPart):
                args = part.args
                if isinstance(args, str):
                    try:
                        parsed_args: dict[str, Any] = json.loads(args)
                    except json.JSONDecodeError as exc:
                        raise ResponseContractViolation(
                            contract="model_tool_call_arguments",
                            errors=[{"tool": part.tool_name, "error": str(exc)}],
                        ) from exc
                elif args is None:
                    parsed_args = {}
                else:
                    parsed_args = dict(args)
                tool_calls.append(
                    ToolCall(call_id=part.tool_call_id, name=part.tool_name, arguments=parsed_args)
                )
            elif isinstance(part, ThinkingPart):
                thinking_parts.append(part.content)

        text = "".join(text_parts) if text_parts else None
        structured_output: BaseModel | None = None
        if request.response_format is not None:
            if text is None:
                raise ResponseContractViolation(
                    contract=request.response_format.__name__,
                    errors=[{"msg": "structured-output requested but model returned no text"}],
                )
            try:
                structured_output = request.response_format.model_validate_json(text)
            except ValidationError as exc:
                raise ResponseContractViolation(
                    contract=request.response_format.__name__, errors=exc.errors()
                ) from exc

        usage = raw.usage
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            structured_output=structured_output,
            reasoning_summary="\n".join(thinking_parts) if thinking_parts else None,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.details.get("reasoning_tokens", 0),
                cached_tokens=usage.cache_read_tokens,
            ),
            provider=binding.provider,
            model=binding.model,
            tier=request.tier,
            duration_ms=duration_ms,
            provider_response_id=raw.provider_response_id,
        )

    async def embed(
        self, texts: list[str], tier: ModelTier = ModelTier.EMBEDDING
    ) -> list[list[float]]:
        if tier is not ModelTier.EMBEDDING:
            raise ValueError(f"embed() requires EMBEDDING tier (got {tier!r})")
        result = await self._get_embedder().embed(texts, input_type="document")
        return [list(vec) for vec in result.embeddings]
