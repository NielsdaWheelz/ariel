from __future__ import annotations

from typing import Any

import httpx
import pytest

from ariel.model_adapter import OpenAIResponsesAdapter


def _adapter() -> OpenAIResponsesAdapter:
    return OpenAIResponsesAdapter(
        provider="openai",
        model="gpt-test",
        api_key="sk-test",
    )


def _install_post(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr("ariel.model_adapter.httpx.post", fake_post)


def _call() -> dict[str, Any]:
    return _adapter().create_response(
        input_items=[],
        tools=[],
        user_message="",
        history=[],
        context_bundle={},
    )


def test_usage_nested_details_are_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(
        monkeypatch,
        {
            "id": "resp_1",
            "output": [],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "input_tokens_details": {"cached_tokens": 7},
                "output_tokens_details": {"reasoning_tokens": 42},
            },
        },
    )

    result = _call()

    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 7,
        "reasoning_tokens": 42,
    }


def test_usage_only_input_details_lifts_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(
        monkeypatch,
        {
            "id": "resp_2",
            "output": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 3},
            },
        },
    )

    usage = _call()["usage"]

    assert usage["cached_tokens"] == 3
    assert "reasoning_tokens" not in usage


def test_usage_only_output_details_lifts_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(
        monkeypatch,
        {
            "id": "resp_3",
            "output": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 11},
            },
        },
    )

    usage = _call()["usage"]

    assert usage["reasoning_tokens"] == 11
    assert "cached_tokens" not in usage


def test_usage_none_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(monkeypatch, {"id": "resp_4", "output": [], "usage": None})

    assert _call()["usage"] is None


def test_usage_malformed_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(monkeypatch, {"id": "resp_5", "output": [], "usage": "not-a-dict"})

    assert _call()["usage"] is None


def test_usage_empty_dict_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(monkeypatch, {"id": "resp_6", "output": [], "usage": {}})

    assert _call()["usage"] is None


def test_usage_unknown_extra_fields_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_post(
        monkeypatch,
        {
            "id": "resp_7",
            "output": [],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "audio_tokens": 99,
                "text_tokens": 88,
            },
        },
    )

    assert _call()["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
    }
