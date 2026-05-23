from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ModelAdapter(Protocol):
    provider: str
    model: str

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]: ...

    # Expected provider/model failures must raise ModelAdapterError. Other
    # exceptions are defects in the adapter implementation.


class ModelAdapterError(Exception):
    def __init__(
        self,
        *,
        safe_reason: str,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        provider: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        provider_response_id: str | None = None,
        parse_status: str | None = None,
        validation_status: str | None = None,
        raw_output_shape: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.provider = provider
        self.model = model
        self.usage = usage
        self.provider_response_id = provider_response_id
        self.parse_status = parse_status
        self.validation_status = validation_status
        self.raw_output_shape = raw_output_shape


@dataclass(slots=True)
class OpenAIResponsesAdapter:
    provider: str
    model: str
    api_key: str | None
    timeout_seconds: float = 30.0
    reasoning_effort: str = "medium"
    verbosity: str = "low"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, history, context_bundle
        if not self.api_key:
            raise ModelAdapterError(
                safe_reason="model credentials are not configured",
                status_code=503,
                code="E_MODEL_CREDENTIALS",
                message="model credentials are not configured",
                retryable=False,
            )

        try:
            response = httpx.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": input_items,
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "store": False,
                    "reasoning": {"effort": self.reasoning_effort},
                    "text": {"verbosity": self.verbosity},
                },
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelAdapterError(
                safe_reason="model provider request timed out",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelAdapterError(
                safe_reason="model provider network request failed",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc

        if response.status_code in {401, 403}:
            raise ModelAdapterError(
                safe_reason="model credentials were rejected by provider",
                status_code=502,
                code="E_MODEL_CREDENTIALS",
                message="model credentials were rejected by provider",
                retryable=False,
            )

        if response.status_code >= 400:
            raise ModelAdapterError(
                safe_reason=f"model provider returned HTTP {response.status_code}",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelAdapterError(
                safe_reason="model provider returned invalid JSON",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc

        raw_usage = payload.get("usage")
        usage: dict[str, int] | None = None
        if isinstance(raw_usage, dict):
            normalized: dict[str, int] = {}
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = raw_usage.get(key)
                if isinstance(value, int):
                    normalized[key] = value
            input_details = raw_usage.get("input_tokens_details")
            if isinstance(input_details, dict):
                cached = input_details.get("cached_tokens")
                if isinstance(cached, int):
                    normalized["cached_tokens"] = cached
            output_details = raw_usage.get("output_tokens_details")
            if isinstance(output_details, dict):
                reasoning = output_details.get("reasoning_tokens")
                if isinstance(reasoning, int):
                    normalized["reasoning_tokens"] = reasoning
            usage = normalized or None
        provider_response_id = payload.get("id")

        return {
            "output": payload.get("output"),
            "provider": self.provider,
            "model": self.model,
            "usage": usage,
            "provider_response_id": provider_response_id,
        }
