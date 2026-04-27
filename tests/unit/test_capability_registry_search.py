from __future__ import annotations

from typing import Any

import pytest

import ariel.capability_registry as registry
from web_search_tool.types import WebSearchRequest, WebSearchResponse, WebSearchResultItem


class _FakeProvider:
    last_init: dict[str, Any] | None = None
    last_request: WebSearchRequest | None = None
    response: WebSearchResponse | None = None
    error: registry.WebSearchError | None = None

    def __init__(
        self,
        client: object,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        del client
        self.__class__.last_init = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
        }

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        self.__class__.last_request = request
        if self.__class__.error is not None:
            raise self.__class__.error
        if self.__class__.response is None:
            raise AssertionError("test did not configure provider response")
        return self.__class__.response


def _install_fake_search_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeProvider.last_init = None
    _FakeProvider.last_request = None
    _FakeProvider.response = None
    _FakeProvider.error = None
    monkeypatch.setattr(registry, "BraveSearchProvider", _FakeProvider)


def _result(
    *,
    title: str,
    url: str,
    snippet: str,
    published_at: str | None,
) -> WebSearchResultItem:
    return WebSearchResultItem(
        result_ref=f"test:{url}",
        title=title,
        url=url,
        display_url=url,
        snippet=snippet,
        extra_snippets=(),
        published_at=published_at,
        source_name=None,
        rank=1,
        provider="test",
        provider_request_id="req_test",
    )


def test_search_web_maps_provider_results_to_search_results_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_search_tool(monkeypatch)
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "test-key")
    monkeypatch.setenv("ARIEL_SEARCH_BRAVE_BASE_URL", "https://search.example.test/res/v1")
    monkeypatch.setenv("ARIEL_SEARCH_WEB_TIMEOUT_SECONDS", "3.5")
    _FakeProvider.response = WebSearchResponse(
        provider="brave",
        provider_request_id="req_test",
        retrieved_at="2026-04-27T12:00:00Z",
        results=(
            _result(
                title=" Example ",
                url=" https://example.test ",
                snippet=" Result snippet ",
                published_at="2026-04-26T10:00:00+00:00",
            ),
        ),
    )

    output = registry.get_capability("cap.search.web").execute({"query": "example query"})  # type: ignore[union-attr]

    assert output == {
        "query": "example query",
        "retrieved_at": "2026-04-27T12:00:00Z",
        "results": [
            {
                "title": "Example",
                "source": "https://example.test",
                "snippet": "Result snippet",
                "published_at": "2026-04-26T10:00:00Z",
            }
        ],
    }
    assert _FakeProvider.last_init == {
        "api_key": "test-key",
        "base_url": "https://search.example.test/res/v1",
        "timeout_seconds": 3.5,
    }
    assert _FakeProvider.last_request is not None
    assert _FakeProvider.last_request.query == "example query"
    assert _FakeProvider.last_request.result_type == registry.WebSearchResultType.WEB
    assert _FakeProvider.last_request.limit == 5


def test_search_news_uses_news_result_type_and_preserves_egress_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_search_tool(monkeypatch)
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "shared-key")
    monkeypatch.setenv("ARIEL_SEARCH_BRAVE_BASE_URL", "https://search.example.test/res/v1")
    _FakeProvider.response = WebSearchResponse(
        provider="brave",
        provider_request_id="req_test",
        retrieved_at="2026-04-27T13:00:00Z",
        results=(
            _result(
                title="News",
                url="https://publisher.test/story",
                snippet="Story",
                published_at="2026-04-27T12:30:00Z",
            ),
        ),
    )

    capability = registry.get_capability("cap.search.news")
    assert capability is not None
    assert capability.declare_egress_intent is not None
    output = capability.execute({"query": "news query"})

    assert capability.allowed_egress_destinations == ("search.example.test",)
    assert capability.declare_egress_intent({"query": "news query"}) == [
        {
            "destination": "https://search.example.test/res/v1/news/search",
            "payload": {"query": "news query"},
        }
    ]
    assert output["results"] == [
        {
            "title": "News",
            "source": "https://publisher.test/story",
            "snippet": "Story",
            "published_at": "2026-04-27T12:30:00Z",
        }
    ]
    assert _FakeProvider.last_request is not None
    assert _FakeProvider.last_request.query == "news query"
    assert _FakeProvider.last_request.result_type == registry.WebSearchResultType.NEWS
    assert _FakeProvider.last_request.limit == 5


def test_search_provider_errors_map_to_existing_runtime_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_search_tool(monkeypatch)
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "test-key")
    _FakeProvider.error = registry.WebSearchError(
        registry.WebSearchErrorCode.RATE_LIMITED,
        "rate limited",
        provider="test",
    )

    with pytest.raises(RuntimeError, match="search provider rate limited"):
        registry.get_capability("cap.search.web").execute({"query": "example query"})  # type: ignore[union-attr]
