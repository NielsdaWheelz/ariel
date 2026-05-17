from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

import ariel.action_runtime as action_runtime_module
import ariel.capability_registry as capability_registry_module
import ariel.policy_engine as policy_engine_module
from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_message, responses_with_run_calls
from ariel.capability_registry import CapabilityDefinition
from tests.fake_sandbox import FakeSandboxRuntime


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s6-pr02"
    model: str = "model.s6-pr02-v1"
    run_calls_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    assistant_text_by_message: dict[str, str] = field(default_factory=dict)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history
        if context_bundle.get("origin") == "tool_result_interpretation":
            interpreter_input = context_bundle.get("tool_result_interpreter_input")
            if not isinstance(interpreter_input, dict):
                interpreter_input = {}
            audited_outputs = interpreter_input.get("audited_tool_outputs")
            selected_output_refs = []
            if isinstance(audited_outputs, list):
                selected_output_refs = [
                    output["output_ref"]
                    for output in audited_outputs
                    if isinstance(output, dict) and isinstance(output.get("output_ref"), str)
                ]
            return responses_message(
                assistant_text=json.dumps(
                    {
                        "findings": ["maps evidence inspected"],
                        "contradictions": [],
                        "uncertainty": [],
                        "selected_output_refs": selected_output_refs,
                        "omitted_output_refs": [],
                        "citation_refs": interpreter_input.get("citation_refs", []),
                        "artifact_refs": interpreter_input.get("artifact_refs", []),
                        "recommended_next_evidence": [],
                        "confidence": 0.9,
                    },
                    sort_keys=True,
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_s6_pr02_interpreter",
                input_tokens=41,
                output_tokens=26,
            )
        run_calls = copy.deepcopy(self.run_calls_by_message.get(user_message, []))
        assistant_text = self.assistant_text_by_message.get(
            user_message,
            {
                "route to airport": "The driving route to SEA is available [1].",
                "find nearby coffee": "Nearby coffee options are available [1][2].",
                "missing places location": "Please provide nearby location context; I cannot infer the location.",
                "maps egress deny": "maps runtime failure: allowlist blocked this request.",
                "maps while google disconnected": "Maps route is available [1].",
                "mixed retrieval": "Route and construction updates are available [1][2].",
            }.get(user_message, f"assistant::{user_message}"),
        )
        if any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        if not run_calls:
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        return responses_with_run_calls(
            assistant_text=assistant_text,
            calls=run_calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s6_pr02_123",
            input_tokens=41,
            output_tokens=26,
        )


@dataclass(slots=True)
class _FakeHTTPResponse:
    status_code: int
    payload: Any = field(default_factory=dict)
    text: str = ""
    json_raises: bool = False

    def json(self) -> Any:
        if self.json_raises:
            raise ValueError("invalid json")
        return self.payload


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    return item


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _assert_source_contract(source: dict[str, Any]) -> None:
    assert set(source.keys()) == {"artifact_id", "title", "source", "retrieved_at", "published_at"}
    assert isinstance(source["artifact_id"], str)
    assert source["artifact_id"].startswith("art_")
    assert isinstance(source["title"], str)
    assert isinstance(source["source"], str)
    assert isinstance(source["retrieved_at"], str)
    assert source["published_at"] is None or isinstance(source["published_at"], str)


def _patch_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capability_id: str,
    mutate: Callable[[CapabilityDefinition], CapabilityDefinition],
) -> None:
    prior_policy_lookup = policy_engine_module.get_capability
    prior_runtime_lookup = action_runtime_module.get_capability

    def patched_policy_get_capability(candidate_id: str) -> CapabilityDefinition | None:
        capability = prior_policy_lookup(candidate_id)
        if candidate_id != capability_id or capability is None:
            return capability
        return mutate(capability)

    def patched_runtime_get_capability(candidate_id: str) -> CapabilityDefinition | None:
        capability = prior_runtime_lookup(candidate_id)
        if candidate_id != capability_id or capability is None:
            return capability
        return mutate(capability)

    monkeypatch.setattr(policy_engine_module, "get_capability", patched_policy_get_capability)
    monkeypatch.setattr(action_runtime_module, "get_capability", patched_runtime_get_capability)


def _install_maps_responses(
    monkeypatch: pytest.MonkeyPatch,
    *,
    routes: _FakeHTTPResponse | None = None,
    geocode: _FakeHTTPResponse | None = None,
    places: _FakeHTTPResponse | None = None,
) -> list[dict[str, Any]]:
    """Route mocked ``httpx.request`` calls to the Google Maps Platform endpoint
    the maps wire layer targets (Routes API, Geocoding API, Places API New)."""
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        calls.append({"method": method, "url": url, **kwargs})
        if "routes.googleapis.com" in url and routes is not None:
            return routes
        if "maps.googleapis.com/maps/api/geocode" in url and geocode is not None:
            return geocode
        if "places.googleapis.com" in url and places is not None:
            return places
        raise AssertionError(f"unexpected maps request: {method} {url}")

    monkeypatch.setattr(capability_registry_module.httpx, "request", fake_request)
    return calls


def _seattle_geocode_response() -> _FakeHTTPResponse:
    return _FakeHTTPResponse(
        status_code=200,
        payload={
            "status": "OK",
            "results": [{"geometry": {"location": {"lat": 47.6097, "lng": -122.3331}}}],
        },
    )


@pytest.fixture(autouse=True)
def _maps_provider_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAPS_API_KEY", "test-maps-key")


def test_s6_pr02_maps_directions_executes_against_routes_api_with_citations(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [
                    {
                        "distanceMeters": 17200,
                        "duration": "1320s",
                        "description": "I-5 N",
                    }
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "route to airport": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "route to airport"}
        )
        assert sent.status_code == 200
        payload = sent.json()

        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.maps.directions"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["approval"]["status"] == "not_requested"
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert output["distance_meters"] == 17200
        assert output["duration_seconds"] == 1320
        assert output["uncertainty"] is None
        assert output["results"][0]["source"].startswith("https://www.google.com/maps/dir/?api=1")

        assert calls[0]["url"] == "https://routes.googleapis.com/directions/v2:computeRoutes"
        assert calls[0]["json"]["travelMode"] == "DRIVE"

        assert "[1]" in payload["assistant"]["message"]
        assert len(payload["assistant"]["sources"]) == 1
        _assert_source_contract(payload["assistant"]["sources"][0])

        event_types = _event_types(payload["turn"])
        assert "evt.action.execution.started" in event_types
        assert "evt.action.execution.succeeded" in event_types


def test_s6_pr02_maps_search_places_executes_against_places_api_with_metadata(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_maps_responses(
        monkeypatch,
        geocode=_seattle_geocode_response(),
        places=_FakeHTTPResponse(
            status_code=200,
            payload={
                "places": [
                    {
                        "displayName": {"text": "Blue Bottle Coffee"},
                        "formattedAddress": "300 1st Ave, Seattle, WA",
                        "location": {"latitude": 47.6099, "longitude": -122.3335},
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                        "rating": 4.6,
                        "userRatingCount": 320,
                        "regularOpeningHours": {"openNow": True},
                        "businessStatus": "OPERATIONAL",
                    },
                    {
                        "displayName": {"text": "Anchorhead Coffee"},
                        "formattedAddress": "1600 7th Ave, Seattle, WA",
                        "location": {"latitude": 47.6110, "longitude": -122.3340},
                        "googleMapsUri": "https://maps.google.com/?cid=2",
                        "rating": 4.5,
                        "userRatingCount": 210,
                        "regularOpeningHours": {"openNow": False},
                        "businessStatus": "OPERATIONAL",
                    },
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "find nearby coffee": [
                {
                    "name": "maps.search_places",
                    "input": {
                        "query": "coffee",
                        "location_context": "Downtown Seattle, WA",
                        "radius_meters": 2000,
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "find nearby coffee"}
        )
        assert sent.status_code == 200
        payload = sent.json()

        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.maps.search_places"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert len(output["results"]) == 2
        first = output["results"][0]
        assert first["title"] == "Blue Bottle Coffee"
        assert first["source"] == "https://maps.google.com/?cid=1"
        assert "distance_meters=" in first["snippet"]
        assert "rating=4.6" in first["snippet"]
        assert "open_now=true" in first["snippet"]

        assert calls[0]["url"] == "https://maps.googleapis.com/maps/api/geocode/json"
        assert calls[1]["url"] == "https://places.googleapis.com/v1/places:searchText"

        assert "[1]" in payload["assistant"]["message"]
        assert len(payload["assistant"]["sources"]) >= 1


def test_s6_pr02_maps_search_places_enforces_radius_with_haversine_filter(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_maps_responses(
        monkeypatch,
        geocode=_seattle_geocode_response(),
        places=_FakeHTTPResponse(
            status_code=200,
            payload={
                "places": [
                    {
                        "displayName": {"text": "Near Cafe"},
                        "formattedAddress": "1 Near St, Seattle, WA",
                        "location": {"latitude": 47.6099, "longitude": -122.3335},
                        "googleMapsUri": "https://maps.google.com/?cid=near",
                    },
                    {
                        "displayName": {"text": "Far Cafe"},
                        "formattedAddress": "1 Far Rd, Tacoma, WA",
                        "location": {"latitude": 47.7510, "longitude": -122.4400},
                        "googleMapsUri": "https://maps.google.com/?cid=far",
                    },
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "radius filtered coffee": [
                {
                    "name": "maps.search_places",
                    "input": {
                        "query": "coffee",
                        "location_context": "Downtown Seattle, WA",
                        "radius_meters": 1000,
                    },
                }
            ]
        },
        assistant_text_by_message={"radius filtered coffee": "Nearby coffee is available [1]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "radius filtered coffee"}
        )
        assert sent.status_code == 200
        payload = sent.json()

        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert [result["title"] for result in output["results"]] == ["Near Cafe"]


@pytest.mark.parametrize(
    ("input_payload", "expected_error", "expected_hint"),
    [
        (
            {"destination": "SEA Airport, Seattle, WA", "travel_mode": "driving"},
            "maps_origin_required",
            "origin",
        ),
        (
            {"origin": "Pike Place Market, Seattle, WA", "travel_mode": "driving"},
            "maps_destination_required",
            "destination",
        ),
    ],
)
def test_s6_pr02_maps_directions_missing_required_route_fields_asks_explicit_clarification(
    postgres_url: str,
    input_payload: dict[str, Any],
    expected_error: str,
    expected_hint: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "missing route field": [
                {"name": "maps.directions", "input": input_payload},
            ]
        },
        assistant_text_by_message={
            "missing route field": f"Please provide {expected_hint}; I cannot infer location."
        },
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "missing route field"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        message = payload["assistant"]["message"].lower()
        assert expected_hint in message
        assert "infer" in message
        assert "location" in message
        assert payload["assistant"]["sources"] == []


def test_s6_pr02_maps_search_places_missing_location_context_asks_explicit_clarification(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "missing places location": [
                {"name": "maps.search_places", "input": {"query": "coffee"}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "missing places location"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "maps_location_context_required"
        message = payload["assistant"]["message"].lower()
        assert "location" in message
        assert "nearby" in message or "context" in message
        assert "infer" in message
        assert payload["assistant"]["sources"] == []


def test_s6_pr02_maps_credentials_missing_is_typed_and_recoverable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARIEL_MAPS_API_KEY", raising=False)
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "maps credentials failure": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                }
            ]
        },
        assistant_text_by_message={
            "maps credentials failure": "provider_credentials_missing: contact the operator."
        },
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "maps credentials failure"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        assert payload["turn"]["surface_action_lifecycle"] == []
        assert "provider_credentials_missing" in payload["assistant"]["message"].lower()
        assert all(
            event["event_type"] != "evt.action.execution.started"
            for event in payload["turn"]["events"]
        )


@pytest.mark.parametrize(
    ("failure_mode", "expected_error", "expected_hint"),
    [
        ("timeout", "provider_timeout", "retry"),
        ("network", "provider_network_failure", "retry"),
        ("rate_limited", "provider_rate_limited", "wait"),
        ("upstream_failure", "provider_upstream_failure", "retry"),
        ("permission_denied", "provider_permission_denied", "permission"),
        ("request_rejected", "provider_request_rejected", "verify"),
        ("invalid_payload", "provider_invalid_payload", "retry"),
    ],
)
def test_s6_pr02_maps_provider_failures_are_typed_and_recoverable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_error: str,
    expected_hint: str,
) -> None:
    if failure_mode == "timeout":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            raise httpx.TimeoutException("maps timeout")
    elif failure_mode == "network":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            raise httpx.HTTPError("maps network failure")
    elif failure_mode == "rate_limited":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            return _FakeHTTPResponse(
                status_code=429, payload={"error": {"status": "RESOURCE_EXHAUSTED"}}
            )
    elif failure_mode == "upstream_failure":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            return _FakeHTTPResponse(status_code=503, payload={"error": {"status": "UNAVAILABLE"}})
    elif failure_mode == "permission_denied":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            return _FakeHTTPResponse(
                status_code=403, payload={"error": {"status": "PERMISSION_DENIED"}}
            )
    elif failure_mode == "request_rejected":

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            return _FakeHTTPResponse(
                status_code=400, payload={"error": {"status": "INVALID_ARGUMENT"}}
            )
    else:  # invalid_payload

        def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
            del method, url, kwargs
            return _FakeHTTPResponse(status_code=200, json_raises=True)

    monkeypatch.setattr(capability_registry_module.httpx, "request", fake_request)

    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "maps runtime failure": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                }
            ]
        },
        assistant_text_by_message={"maps runtime failure": f"{expected_error}: {expected_hint}."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "maps runtime failure"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        rendered_message = payload["assistant"]["message"].lower()
        assert expected_error in rendered_message
        assert expected_hint in rendered_message


def test_s6_pr02_maps_egress_preflight_remains_fail_closed_before_execution(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        assert capability.execute is not None
        original_execute = capability.execute

        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal execute_attempts
            execute_attempts += 1
            return original_execute(input_payload)

        return replace(
            capability,
            execute=counted_execute,
            declare_egress_intent=lambda _: [
                {
                    "destination": "https://evil.example/maps",
                    "payload": {"intent": "directions"},
                }
            ],
        )

    _patch_capability_lookup(monkeypatch, capability_id="cap.maps.directions", mutate=mutate)
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "maps egress deny": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "maps egress deny"}
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (attempt["execution"]["error"] or "")
        message = payload["assistant"]["message"].lower()
        assert "maps runtime failure" in message
        assert "allowlist" in message
        assert execute_attempts == 0


def test_s6_pr02_maps_retrieval_isolation_from_google_connector_readiness(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [{"distanceMeters": 17200, "duration": "1260s", "description": "I-5 N"}]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "maps while google disconnected": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        connector_status = client.get("/v1/connectors/google")
        assert connector_status.status_code == 200
        assert connector_status.json()["connector"]["readiness"] == "not_connected"

        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "maps while google disconnected"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        assert "google connector auth failure" not in payload["assistant"]["message"].lower()
        assert len(payload["assistant"]["sources"]) == 1


def test_s6_pr02_maps_outputs_remain_normalized_for_mixed_retrieval_turns(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "fixture-search-key")
    _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [{"distanceMeters": 17200, "duration": "1260s", "description": "I-5 N"}]
            },
        ),
    )

    def mutate_web(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "airport construction updates",
                "retrieved_at": "2026-03-06T12:30:15Z",
                "results": [
                    {
                        "title": "Terminal lane closures",
                        "source": "https://example.com/terminal-closures",
                        "snippet": "Airport authority reports temporary lane closures near departures.",
                        "published_at": "2026-03-06T11:00:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate_web)
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "mixed retrieval": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                },
                {
                    "name": "search.web",
                    "input": {"query": "airport construction updates"},
                },
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "mixed retrieval"}
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"]
        assert "[1]" in message
        assert "[2]" in message
        assert len(payload["assistant"]["sources"]) == 2

        first_attempt = _surface_attempt(payload["turn"], proposal_index=1)
        second_attempt = _surface_attempt(payload["turn"], proposal_index=2)
        assert first_attempt["proposal"]["capability_id"] == "cap.maps.directions"
        assert second_attempt["proposal"]["capability_id"] == "cap.search.web"
        assert first_attempt["execution"]["status"] == "succeeded"
        assert second_attempt["execution"]["status"] == "succeeded"
