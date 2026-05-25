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


class _WeatherResponse:
    def __init__(self, *, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


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

    capability = registry.get_capability("cap.search.web")
    assert capability is not None
    assert capability.execute is not None
    output = capability.execute({"query": "example query"})

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
        "status": "succeeded",
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


def test_search_provider_errors_map_to_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_search_tool(monkeypatch)
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "test-key")
    _FakeProvider.error = registry.WebSearchError(
        registry.WebSearchErrorCode.RATE_LIMITED,
        "rate limited",
        provider="test",
    )

    with pytest.raises(registry.CapabilityExecutionError, match="search provider rate limited"):
        capability = registry.get_capability("cap.search.web")
        assert capability is not None
        assert capability.execute is not None
        capability.execute({"query": "example query"})


def test_search_web_validates_query_only_inputs() -> None:
    capability = registry.get_capability("cap.search.web")
    assert capability is not None

    normalized, error = capability.validate_input({"query": "  launch plan  "})

    assert error is None
    assert normalized == {"query": "launch plan"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"q": "x"},
        {"query": "x", "limit": 5},
        {"query": "x", "topn": 10},
        {"query": ""},
        {"query": "   "},
        {"query": 123},
        {"query": "x" * 1001},
    ],
)
def test_search_web_rejects_non_query_or_malformed_inputs(
    payload: dict[str, Any],
) -> None:
    capability_id = "cap.search.web"
    capability = registry.get_capability(capability_id)
    assert capability is not None

    normalized, error = capability.validate_input(payload)

    assert normalized is None
    assert error == "schema_invalid"


def test_web_extract_calls_jina_reader_with_bearer_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "code": 200,
                "data": {
                    "url": "https://example.com/article",
                    "title": "Example",
                    "content": "Extracted article body.",
                },
            }

    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _Response:
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return _Response()

    monkeypatch.setenv("ARIEL_JINA_API_KEY", "jina-test-key")
    monkeypatch.setattr(registry.httpx, "get", fake_get)

    capability = registry.get_capability("cap.web.extract")
    assert capability is not None
    assert capability.execute is not None
    output = capability.execute({"url": "https://example.com/article"})

    assert output["status"] == "succeeded"
    assert seen["url"] == "https://r.jina.ai/https://example.com/article"
    assert seen["headers"]["authorization"] == "Bearer jina-test-key"
    assert seen["headers"]["accept"] == "application/json"


def test_web_extract_validator_normalizes_public_urls_at_input_boundary() -> None:
    capability = registry.get_capability("cap.web.extract")
    assert capability is not None

    normalized, error = capability.validate_input({"url": "  HTTPS://Example.COM/path#frag  "})

    assert error is None
    assert normalized == {"url": "https://example.com/path"}


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"url": "definitely-not-a-url"}, "url_invalid"),
        ({"url": "ftp://example.com/resource"}, "url_scheme_unsupported"),
        ({"url": "http://127.0.0.1/private"}, "url_destination_unsafe"),
        ({"url": "https://example.com:invalid-port/path"}, "url_invalid"),
    ],
)
def test_web_extract_validator_rejects_unsafe_urls_with_typed_errors(
    payload: dict[str, Any],
    expected_error: str,
) -> None:
    capability = registry.get_capability("cap.web.extract")
    assert capability is not None

    normalized, error = capability.validate_input(payload)

    assert normalized is None
    assert error == expected_error


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"q": "https://example.com"},
        {"url": "https://example.com", "limit": 5},
        {"url": ""},
        {"url": "   "},
        {"url": 123},
        {"url": "https://example.com/" + ("x" * 2049)},
    ],
)
def test_web_extract_validator_keeps_shape_failures_schema_invalid(
    payload: dict[str, Any],
) -> None:
    capability = registry.get_capability("cap.web.extract")
    assert capability is not None

    normalized, error = capability.validate_input(payload)

    assert normalized is None
    assert error == "schema_invalid"


def test_weather_dev_adapter_parses_wttr_payload_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _WeatherResponse:
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _WeatherResponse(
            status_code=200,
            payload={
                "current_condition": [
                    {
                        "weatherDesc": [{"value": "Light rain"}],
                        "temp_C": "14",
                    }
                ],
                "weather": [{"date": "2026-05-24"}],
            },
        )

    monkeypatch.setenv("ARIEL_WEATHER_PROVIDER_MODE", "dev")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_ENDPOINT", "https://wttr.example.test")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_TIMEOUT_SECONDS", "4.5")
    monkeypatch.delenv("ARIEL_WEATHER_PRODUCTION_API_KEY", raising=False)
    monkeypatch.setattr(registry.httpx, "get", fake_get)

    capability = registry.get_capability("cap.weather.forecast")
    assert capability is not None
    assert capability.execute is not None
    output = capability.execute({"location": "Portland, OR", "timeframe": "today"})

    assert seen == {
        "url": "https://wttr.example.test/Portland%2C%20OR",
        "kwargs": {"params": {"format": "j1"}, "timeout": 4.5},
    }
    assert output["location"] == "Portland, OR"
    assert output["timeframe"] == "today"
    assert output["forecast"] == {
        "summary": "Light rain, 14C",
        "source": "https://wttr.example.test",
        "timestamp": "2026-05-24T00:00:00Z",
    }
    assert output["status"] == "succeeded"


@pytest.mark.parametrize("payload", [{"timeframe": "today"}, {"location": ""}])
def test_weather_validator_requires_resolved_location(payload: dict[str, Any]) -> None:
    capability = registry.get_capability("cap.weather.forecast")
    assert capability is not None

    normalized, error = capability.validate_input(payload)

    assert normalized is None
    assert error == "weather_location_required"


def test_weather_production_adapter_parses_tomorrow_io_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _WeatherResponse:
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _WeatherResponse(
            status_code=200,
            payload={
                "timelines": {
                    "daily": [
                        {
                            "time": "2026-05-24T00:00:00Z",
                            "values": {
                                "temperatureMin": 9,
                                "temperatureMax": 15,
                                "weatherCode": 1000,
                            },
                        },
                        {
                            "time": "2026-05-25T00:00:00Z",
                            "values": {
                                "temperatureMin": 11,
                                "temperatureMax": 17,
                                "weatherCode": 1001,
                                "windSpeed": 4,
                            },
                        },
                    ]
                }
            },
        )

    monkeypatch.setenv("ARIEL_WEATHER_PROVIDER_MODE", "production")
    monkeypatch.setenv(
        "ARIEL_WEATHER_PRODUCTION_ENDPOINT",
        "https://weather.example.test/v4/weather/forecast",
    )
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_API_KEY", "weather-key")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS", "6.5")
    monkeypatch.setattr(registry.httpx, "get", fake_get)

    capability = registry.get_capability("cap.weather.forecast")
    assert capability is not None
    assert capability.execute is not None
    output = capability.execute({"location": "Tokyo, JP", "timeframe": "tomorrow"})

    assert seen == {
        "url": "https://weather.example.test/v4/weather/forecast",
        "kwargs": {
            "params": {
                "location": "Tokyo JP",
                "timesteps": "1d",
                "apikey": "weather-key",
            },
            "timeout": 6.5,
        },
    }
    assert output["location"] == "Tokyo, JP"
    assert output["timeframe"] == "tomorrow"
    assert output["forecast"] == {
        "summary": "temperature 11.0-17.0C, code 1001, wind 4.0 m/s",
        "source": "https://weather.example.test/v4/weather/forecast",
        "timestamp": "2026-05-25T00:00:00Z",
    }
    assert output["status"] == "succeeded"


def test_weather_production_adapter_preserves_lat_lon_location_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_params: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _WeatherResponse:
        del url
        seen_params.update(kwargs["params"])
        return _WeatherResponse(
            status_code=200,
            payload={
                "timelines": {
                    "daily": [
                        {
                            "time": "2026-05-24T00:00:00Z",
                            "values": {"temperatureMin": 9, "temperatureMax": 15},
                        }
                    ]
                }
            },
        )

    monkeypatch.setenv("ARIEL_WEATHER_PROVIDER_MODE", "production")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_API_KEY", "weather-key")
    monkeypatch.setattr(registry.httpx, "get", fake_get)

    capability = registry.get_capability("cap.weather.forecast")
    assert capability is not None
    assert capability.execute is not None
    output = capability.execute({"location": "47.6062,-122.3321", "timeframe": "today"})

    assert seen_params["location"] == "47.6062,-122.3321"
    assert output["status"] == "succeeded"


def test_memory_search_validator_normalizes_boundary_filters() -> None:
    capability = registry.get_capability("cap.memory.search")
    assert capability is not None

    normalized, error = capability.validate_input(
        {
            "query": "  offsite plan  ",
            "limit": 12,
            "since": "2026-05-20T10:15:00-07:00",
            "kinds": ["user_message", "tool_observation"],
        }
    )

    assert error is None
    assert normalized == {
        "query": "offsite plan",
        "limit": 12,
        "since": "2026-05-20T17:15:00Z",
        "kinds": ["user_message", "tool_observation"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "offsite", "limit": 0},
        {"query": "offsite", "limit": 101},
        {"query": "offsite", "limit": True},
        {"query": "offsite", "limit": None},
        {"query": "offsite", "since": "not-a-date"},
        {"query": "offsite", "since": ""},
        {"query": "offsite", "since": 123},
        {"query": "offsite", "kinds": []},
        {"query": "offsite", "kinds": ["assistant_message", "unknown_kind"]},
        {"query": "offsite", "kinds": ["user_message", 7]},
        {"query": "offsite", "kinds": ["log"]},
        {"query": "offsite", "kinds": ["note"]},
        {"query": "offsite", "kinds": ["memory_notes"]},
        {"query": "offsite", "kinds": "assistant_message"},
        {"query": "offsite", "layer": "note"},
    ],
)
def test_memory_search_validator_rejects_malformed_boundary_filters(
    payload: dict[str, Any],
) -> None:
    capability = registry.get_capability("cap.memory.search")
    assert capability is not None

    normalized, error = capability.validate_input(payload)

    assert normalized is None
    assert error == "schema_invalid"
