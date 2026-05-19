from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast
from urllib.parse import urlparse

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

import ariel.action_runtime as action_runtime_module
import ariel.capability_registry as capability_registry_module
import ariel.policy_engine as policy_engine_module
from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import (
    post_message_and_drain,
    responses_message,
    responses_with_run_calls,
)
from ariel.capability_registry import CapabilityDefinition
from ariel.persistence import ArtifactRecord
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


def _turn_data(client: TestClient, session_id: str) -> dict[str, Any]:
    """Fetch the latest turn from the events timeline."""
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    turns = resp.json()["turns"]
    assert turns, "no turns in timeline"
    return turns[-1]


def _surface_attempt(turn_data: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_data.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    return item


def _event_types(turn_data: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_data["events"]]


def _assert_source_contract(source: dict[str, Any]) -> None:
    assert set(source.keys()) == {"artifact_id", "title", "source", "retrieved_at", "published_at"}
    assert isinstance(source["artifact_id"], str)
    assert source["artifact_id"].startswith("art_")
    assert isinstance(source["title"], str)
    assert isinstance(source["source"], str)
    assert isinstance(source["retrieved_at"], str)
    assert source["published_at"] is None or isinstance(source["published_at"], str)


def _turn_sources(client: TestClient, turn_id: str) -> list[dict[str, Any]]:
    """Return retrieval-provenance sources for a turn by querying the DB directly.

    The async turn path does not embed sources in evt.assistant.emitted; they
    live as ArtifactRecord rows and are read back here for source assertions.
    """
    from ariel.persistence import to_rfc3339

    session_factory = cast(Any, client.app).state.session_factory
    with session_factory() as db:
        artifacts = db.scalars(
            select(ArtifactRecord)
            .where(
                ArtifactRecord.turn_id == turn_id,
                ArtifactRecord.artifact_type == "retrieval_provenance",
            )
            .order_by(ArtifactRecord.created_at.asc(), ArtifactRecord.id.asc())
        ).all()
    return [
        {
            "artifact_id": artifact.id,
            "title": artifact.title,
            "source": artifact.source,
            "retrieved_at": to_rfc3339(artifact.retrieved_at),
            "published_at": (
                to_rfc3339(artifact.published_at) if artifact.published_at is not None else None
            ),
        }
        for artifact in artifacts
    ]


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
                        "staticDuration": "1080s",
                        "description": "I-5 N",
                        "legs": [
                            {
                                "distanceMeters": 17200,
                                "duration": "1320s",
                                "staticDuration": "1080s",
                            }
                        ],
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
        post_message_and_drain(client, session_id, message="route to airport")
        turn_data = _turn_data(client, session_id)

        attempt = _surface_attempt(turn_data)
        assert attempt["proposal"]["capability_id"] == "cap.maps.directions"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["approval"]["status"] == "not_requested"
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert output["origin"] == "Pike Place Market, Seattle, WA"
        assert output["destination"] == "SEA Airport, Seattle, WA"
        assert output["waypoints"] == []
        assert output["travel_mode"] == "driving"
        assert output["uncertainty"] is None
        assert len(output["routes"]) == 1
        route = output["routes"][0]
        assert route["distance_meters"] == 17200
        assert route["duration_seconds"] == 1320
        assert route["static_duration_seconds"] == 1080
        assert route["description"] == "I-5 N"
        assert route["stops"] == [
            "Pike Place Market, Seattle, WA",
            "SEA Airport, Seattle, WA",
        ]
        assert route["legs"] == [
            {"distance_meters": 17200, "duration_seconds": 1320, "static_duration_seconds": 1080}
        ]
        assert route["source"].startswith("https://www.google.com/maps/dir/?api=1")
        assert output["results"][0]["source"] == route["source"]

        assert calls[0]["url"] == "https://routes.googleapis.com/directions/v2:computeRoutes"
        assert calls[0]["method"] == "POST"
        routes_body = calls[0]["json"]
        assert routes_body["origin"] == {"address": "Pike Place Market, Seattle, WA"}
        assert routes_body["destination"] == {"address": "SEA Airport, Seattle, WA"}
        assert routes_body["travelMode"] == "DRIVE"
        assert routes_body["routingPreference"] == "TRAFFIC_AWARE"
        assert routes_body["computeAlternativeRoutes"] is True
        assert "intermediates" not in routes_body
        assert "optimizeWaypointOrder" not in routes_body
        routes_headers = calls[0]["headers"]
        assert routes_headers["x-goog-api-key"] == "test-maps-key"
        assert routes_headers["x-goog-fieldmask"] == (
            "routes.distanceMeters,routes.duration,routes.staticDuration,routes.description,"
            "routes.legs.distanceMeters,routes.legs.duration,routes.legs.staticDuration,"
            "routes.optimizedIntermediateWaypointIndex"
        )

        assistant_message = turn_data["assistant_message"]
        assert "[1]" in assistant_message

        # Sources are retrieval_provenance artifacts in the DB.
        sources = _turn_sources(client, turn_data["id"])
        assert len(sources) == 1
        _assert_source_contract(sources[0])

        event_type_list = _event_types(turn_data)
        assert "evt.action.execution.started" in event_type_list
        assert "evt.action.execution.succeeded" in event_type_list


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
        post_message_and_drain(client, session_id, message="find nearby coffee")
        turn_data = _turn_data(client, session_id)

        attempt = _surface_attempt(turn_data)
        assert attempt["proposal"]["capability_id"] == "cap.maps.search_places"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert len(output["results"]) == 2
        first = output["results"][0]
        assert first["title"] == "Blue Bottle Coffee"
        assert first["source"] == "https://maps.google.com/?cid=1"
        assert first["snippet"] == "300 1st Ave, Seattle, WA"
        assert first["address"] == "300 1st Ave, Seattle, WA"
        assert first["rating"] == 4.6
        assert first["rating_count"] == 320
        assert first["open_now"] is True
        assert first["business_status"] == "OPERATIONAL"
        assert isinstance(first["distance_meters"], int)
        assert output["results"][1]["open_now"] is False

        assert calls[0]["url"] == "https://maps.googleapis.com/maps/api/geocode/json"
        assert calls[0]["method"] == "GET"
        assert calls[0]["params"]["address"] == "Downtown Seattle, WA"
        assert calls[0]["params"]["key"] == "test-maps-key"
        assert calls[1]["url"] == "https://places.googleapis.com/v1/places:searchText"
        assert calls[1]["method"] == "POST"
        places_body = calls[1]["json"]
        assert places_body["textQuery"] == "coffee near Downtown Seattle, WA"
        assert places_body["pageSize"] == 5
        places_circle = places_body["locationBias"]["circle"]
        assert places_circle["center"] == {"latitude": 47.6097, "longitude": -122.3331}
        assert places_circle["radius"] == 2000.0
        places_headers = calls[1]["headers"]
        assert places_headers["x-goog-api-key"] == "test-maps-key"
        assert "places.displayName" in places_headers["x-goog-fieldmask"]
        assert "places.rating" in places_headers["x-goog-fieldmask"]

        assert "[1]" in turn_data["assistant_message"]
        # Sources are retrieval_provenance artifacts in the DB.
        assert len(_turn_sources(client, turn_data["id"])) >= 1


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
        post_message_and_drain(client, session_id, message="radius filtered coffee")
        turn_data = _turn_data(client, session_id)

        attempt = _surface_attempt(turn_data)
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
        post_message_and_drain(client, session_id, message="missing route field")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        message = turn_data["assistant_message"].lower()
        assert expected_hint in message
        assert "infer" in message
        assert "location" in message
        assert _turn_sources(client, turn_data["id"]) == []


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
        post_message_and_drain(client, session_id, message="missing places location")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "maps_location_context_required"
        message = turn_data["assistant_message"].lower()
        assert "location" in message
        assert "nearby" in message or "context" in message
        assert "infer" in message
        assert _turn_sources(client, turn_data["id"]) == []


def test_s6_pr02_maps_capability_not_offered_when_api_key_missing(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maps capabilities are exposed only when ARIEL_MAPS_API_KEY is configured.
    With no key the maps vertical is gated off before execution: no maps action
    is surfaced and no maps execution lifecycle is recorded."""
    monkeypatch.delenv("ARIEL_MAPS_API_KEY", raising=False)
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "maps without key": [
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
        assistant_text_by_message={"maps without key": "Maps is not configured."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="maps without key")
        turn_data = _turn_data(client, session_id)
        assert turn_data["surface_action_lifecycle"] == []
        assert all(
            event["event_type"] != "evt.action.execution.started" for event in turn_data["events"]
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
        post_message_and_drain(client, session_id, message="maps runtime failure")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        rendered_message = turn_data["assistant_message"].lower()
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
        post_message_and_drain(client, session_id, message="maps egress deny")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (attempt["execution"]["error"] or "")
        message = turn_data["assistant_message"].lower()
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
        post_message_and_drain(client, session_id, message="maps while google disconnected")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        assert "google connector auth failure" not in turn_data["assistant_message"].lower()
        # Sources are retrieval_provenance artifacts in the DB.
        assert len(_turn_sources(client, turn_data["id"])) == 1


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
        post_message_and_drain(client, session_id, message="mixed retrieval")
        turn_data = _turn_data(client, session_id)
        message = turn_data["assistant_message"]
        assert "[1]" in message
        assert "[2]" in message
        # Sources are retrieval_provenance artifacts in the DB.
        assert len(_turn_sources(client, turn_data["id"])) == 2

        first_attempt = _surface_attempt(turn_data, proposal_index=1)
        second_attempt = _surface_attempt(turn_data, proposal_index=2)
        assert first_attempt["proposal"]["capability_id"] == "cap.maps.directions"
        assert second_attempt["proposal"]["capability_id"] == "cap.search.web"
        assert first_attempt["execution"]["status"] == "succeeded"
        assert second_attempt["execution"]["status"] == "succeeded"


def test_s6_pr02_maps_directions_walking_mode_omits_traffic_routing_preference(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-driving travel mode maps to the right Routes enum and omits the
    DRIVE-only traffic routing preference; a route with no provider description
    yields a null `description` and a concise travel-mode citation snippet, and a
    route with no `staticDuration` yields a null `static_duration_seconds`."""
    calls = _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={"routes": [{"distanceMeters": 1400, "duration": "1100s"}]},
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "walk there": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "Seattle Art Museum, Seattle, WA",
                        "travel_mode": "walking",
                    },
                }
            ]
        },
        assistant_text_by_message={"walk there": "The walking route is available [1]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="walk there")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        routes_body = calls[0]["json"]
        assert routes_body["travelMode"] == "WALK"
        assert "routingPreference" not in routes_body
        output = attempt["execution"]["output"]
        route = output["routes"][0]
        assert route["description"] is None
        assert route["static_duration_seconds"] is None
        assert route["legs"] == []
        assert output["results"][0]["snippet"] == "walking route"


def test_s6_pr02_maps_directions_reports_uncertainty_when_no_route(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Routes API returns no route, the output declares explicit
    uncertainty and empty `routes`/`results` rather than fabricating a route."""
    _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(status_code=200, payload={"routes": []}),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "no route": [
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
        assistant_text_by_message={"no route": "I could not find a route."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="no route")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["uncertainty"] == "insufficient_evidence"
        assert output["routes"] == []
        assert output["results"] == []


def test_s6_pr02_maps_directions_multi_stop_routes_a_single_legged_trip(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waypoint request carries `intermediates` and no alternatives flag, returns
    exactly one route whose `stops` are origin → waypoints → destination, with one
    leg per stop pair and a `source` deep link that includes the waypoints."""
    calls = _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [
                    {
                        "distanceMeters": 14300,
                        "duration": "2100s",
                        "staticDuration": "1980s",
                        "description": "Errand loop",
                        "legs": [
                            {"distanceMeters": 4100, "duration": "660s", "staticDuration": "600s"},
                            {"distanceMeters": 5200, "duration": "720s", "staticDuration": "690s"},
                            {"distanceMeters": 5000, "duration": "720s", "staticDuration": "690s"},
                        ],
                    }
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "plan my errands": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Home, Seattle, WA",
                        "destination": "Office, Seattle, WA",
                        "travel_mode": "driving",
                        "waypoints": ["Cleaner, Seattle, WA", "Grocery, Seattle, WA"],
                    },
                }
            ]
        },
        assistant_text_by_message={"plan my errands": "Your errand route is planned [1]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="plan my errands")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["waypoints"] == ["Cleaner, Seattle, WA", "Grocery, Seattle, WA"]
        assert len(output["routes"]) == 1
        route = output["routes"][0]
        assert route["stops"] == [
            "Home, Seattle, WA",
            "Cleaner, Seattle, WA",
            "Grocery, Seattle, WA",
            "Office, Seattle, WA",
        ]
        assert len(route["legs"]) == len(route["stops"]) - 1
        assert route["legs"][0] == {
            "distance_meters": 4100,
            "duration_seconds": 660,
            "static_duration_seconds": 600,
        }
        assert "&waypoints=" in route["source"]

        routes_body = calls[0]["json"]
        assert routes_body["intermediates"] == [
            {"address": "Cleaner, Seattle, WA"},
            {"address": "Grocery, Seattle, WA"},
        ]
        assert "computeAlternativeRoutes" not in routes_body
        assert "optimizeWaypointOrder" not in routes_body


def test_s6_pr02_maps_directions_optimize_order_reports_googles_chosen_stop_order(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `optimize_order`, the request sets `optimizeWaypointOrder` and the
    output `stops` reflect Google's `optimizedIntermediateWaypointIndex` reorder."""
    calls = _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [
                    {
                        "distanceMeters": 13100,
                        "duration": "1860s",
                        "staticDuration": "1800s",
                        "description": "Optimized loop",
                        "optimizedIntermediateWaypointIndex": [1, 0],
                        "legs": [
                            {"distanceMeters": 3000, "duration": "500s", "staticDuration": "480s"},
                            {"distanceMeters": 5100, "duration": "700s", "staticDuration": "680s"},
                            {"distanceMeters": 5000, "duration": "660s", "staticDuration": "640s"},
                        ],
                    }
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "best errand order": [
                {
                    "name": "maps.directions",
                    "input": {
                        "origin": "Home, Seattle, WA",
                        "destination": "Office, Seattle, WA",
                        "travel_mode": "driving",
                        "waypoints": ["Cleaner, Seattle, WA", "Grocery, Seattle, WA"],
                        "optimize_order": True,
                    },
                }
            ]
        },
        assistant_text_by_message={"best errand order": "Best errand order found [1]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="best errand order")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        route = attempt["execution"]["output"]["routes"][0]
        assert route["stops"] == [
            "Home, Seattle, WA",
            "Grocery, Seattle, WA",
            "Cleaner, Seattle, WA",
            "Office, Seattle, WA",
        ]
        routes_body = calls[0]["json"]
        assert routes_body["optimizeWaypointOrder"] is True
        assert routes_body["intermediates"] == [
            {"address": "Cleaner, Seattle, WA"},
            {"address": "Grocery, Seattle, WA"},
        ]


def test_s6_pr02_maps_directions_returns_alternative_routes_for_plain_query(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-waypoint query returns up to three routes; `routes[0]` is the
    recommended route and the rest are alternatives, each with its own legs."""
    calls = _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [
                    {
                        "distanceMeters": 17200,
                        "duration": "1200s",
                        "staticDuration": "1100s",
                        "description": "I-5 N",
                        "legs": [
                            {
                                "distanceMeters": 17200,
                                "duration": "1200s",
                                "staticDuration": "1100s",
                            }
                        ],
                    },
                    {
                        "distanceMeters": 19400,
                        "duration": "1440s",
                        "staticDuration": "1380s",
                        "description": "I-405 N",
                        "legs": [
                            {
                                "distanceMeters": 19400,
                                "duration": "1440s",
                                "staticDuration": "1380s",
                            }
                        ],
                    },
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "route options": [
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
        assistant_text_by_message={"route options": "Two routes are available [1][2]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="route options")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert len(output["routes"]) == 2
        assert output["routes"][0]["description"] == "I-5 N"
        assert output["routes"][0]["duration_seconds"] == 1200
        assert output["routes"][1]["description"] == "I-405 N"
        assert output["routes"][1]["duration_seconds"] == 1440
        assert len(output["results"]) == 2
        assert calls[0]["json"]["computeAlternativeRoutes"] is True


def test_s6_pr02_maps_directions_caps_alternatives_at_three_routes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Routes API can return more than three routes; the directions output
    caps `routes`/`results` at the three-route alternative bound."""
    _install_maps_responses(
        monkeypatch,
        routes=_FakeHTTPResponse(
            status_code=200,
            payload={
                "routes": [
                    {"distanceMeters": 1000 * n, "duration": f"{600 * n}s", "description": f"R{n}"}
                    for n in range(1, 6)
                ]
            },
        ),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "many routes": [
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
        assistant_text_by_message={"many routes": "Routes are available [1]."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="many routes")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert len(output["routes"]) == 3
        assert len(output["results"]) == 3
        assert [route["description"] for route in output["routes"]] == ["R1", "R2", "R3"]


def test_s6_pr02_maps_search_places_reports_uncertainty_when_no_places(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Places API returns no candidates, the output declares explicit
    uncertainty and an empty result set."""
    _install_maps_responses(
        monkeypatch,
        geocode=_seattle_geocode_response(),
        places=_FakeHTTPResponse(status_code=200, payload={"places": []}),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "no places": [
                {
                    "name": "maps.search_places",
                    "input": {
                        "query": "coffee",
                        "location_context": "Downtown Seattle, WA",
                        "radius_meters": 2000,
                    },
                }
            ]
        },
        assistant_text_by_message={"no places": "I could not find nearby places."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="no places")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["uncertainty"] == "insufficient_evidence"
        assert output["results"] == []


def test_s6_pr02_maps_search_places_unresolvable_location_asks_clarification(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A location string that geocodes to ZERO_RESULTS is a clarification
    condition, not a provider fault: it surfaces the typed `maps_location_not_found`
    outcome so the assistant asks for a clearer location instead of retrying."""
    _install_maps_responses(
        monkeypatch,
        geocode=_FakeHTTPResponse(status_code=200, payload={"status": "ZERO_RESULTS"}),
    )
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "vague place": [
                {
                    "name": "maps.search_places",
                    "input": {"query": "coffee", "location_context": "near my place"},
                }
            ]
        },
        assistant_text_by_message={
            "vague place": "I could not resolve that location; please give a clearer one."
        },
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="vague place")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "maps_location_not_found"


@pytest.mark.parametrize(
    ("geocode_response", "places_response", "expected_error"),
    [
        (
            _FakeHTTPResponse(status_code=200, payload={"status": "REQUEST_DENIED"}),
            None,
            "provider_permission_denied",
        ),
        (
            _FakeHTTPResponse(status_code=200, payload={"status": "OVER_QUERY_LIMIT"}),
            None,
            "provider_rate_limited",
        ),
        (
            _seattle_geocode_response(),
            _FakeHTTPResponse(status_code=503, payload={"error": {"status": "UNAVAILABLE"}}),
            "provider_upstream_failure",
        ),
    ],
)
def test_s6_pr02_maps_search_places_geocoding_and_places_leg_failures_are_typed(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    geocode_response: _FakeHTTPResponse,
    places_response: _FakeHTTPResponse | None,
    expected_error: str,
) -> None:
    """`search_places` has two wire legs. The Geocoding API signals failure as an
    HTTP-200 body with a `status` field; the Places API signals it with an HTTP
    status code. Both legs map to stable typed outcomes."""
    _install_maps_responses(monkeypatch, geocode=geocode_response, places=places_response)
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "geocode failure": [
                {
                    "name": "maps.search_places",
                    "input": {"query": "coffee", "location_context": "Downtown Seattle, WA"},
                }
            ]
        },
        assistant_text_by_message={"geocode failure": f"maps failure: {expected_error}."},
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="geocode failure")
        turn_data = _turn_data(client, session_id)
        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error


def test_s6_pr02_maps_declared_egress_destinations_are_allowlisted() -> None:
    """Every destination the maps capabilities declare for egress must be a host
    on the capability's static allowlist. This is the real fail-closed contract;
    the deny path is exercised separately with an injected hostile destination."""
    samples: dict[str, dict[str, Any]] = {
        "cap.maps.directions": {
            "origin": "Pike Place Market, Seattle, WA",
            "destination": "SEA Airport, Seattle, WA",
            "travel_mode": "driving",
        },
        "cap.maps.search_places": {
            "query": "coffee",
            "location_context": "Downtown Seattle, WA",
            "radius_meters": 2000,
        },
    }
    for capability_id, sample_input in samples.items():
        capability = capability_registry_module.get_capability(capability_id)
        assert capability is not None
        assert capability.declare_egress_intent is not None
        declared = capability.declare_egress_intent(sample_input)
        assert declared is not None and len(declared) >= 1
        for request in declared:
            host = urlparse(request["destination"]).hostname
            assert host in capability.allowed_egress_destinations, (
                f"{capability_id} declares un-allowlisted egress host {host!r}"
            )


@pytest.mark.parametrize(
    ("capability_id", "raw_input"),
    [
        ("cap.maps.directions", {"origin": "A", "destination": "B", "unexpected": "x"}),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "travel_mode": "flying"}),
        ("cap.maps.directions", {"origin": "x" * 321, "destination": "B"}),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "waypoints": "C"}),
        (
            "cap.maps.directions",
            {"origin": "A", "destination": "B", "waypoints": [f"w{n}" for n in range(11)]},
        ),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "waypoints": ["C", ""]}),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "waypoints": ["C", 7]}),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "waypoints": ["x" * 321]}),
        ("cap.maps.directions", {"origin": "A", "destination": "B", "optimize_order": "yes"}),
        ("cap.maps.search_places", {"query": "coffee", "radius_meters": 50}),
        ("cap.maps.search_places", {"query": "coffee", "radius_meters": 99999}),
        ("cap.maps.search_places", {"query": "", "location_context": "Seattle, WA"}),
        ("cap.maps.search_places", {"query": "x" * 201, "location_context": "Seattle, WA"}),
        ("cap.maps.search_places", {"query": "coffee", "unexpected": "x"}),
    ],
)
def test_s6_pr02_maps_input_validation_rejects_malformed_payloads(
    capability_id: str,
    raw_input: dict[str, Any],
) -> None:
    """Maps input validators reject structurally invalid payloads at the boundary
    with a `schema_invalid` typed error — before any provider call."""
    capability = capability_registry_module.get_capability(capability_id)
    assert capability is not None
    normalized, error = capability.validate_input(raw_input)
    assert normalized is None
    assert error == "schema_invalid"


def test_s6_pr02_maps_search_places_radius_defaults_when_omitted() -> None:
    """radius_meters has a single owner — the input validator — which defaults it
    to 2000 m when the caller omits it; execution trusts that normalized value."""
    capability = capability_registry_module.get_capability("cap.maps.search_places")
    assert capability is not None
    normalized, error = capability.validate_input(
        {"query": "coffee", "location_context": "Seattle, WA"}
    )
    assert error is None
    assert normalized is not None
    assert normalized["radius_meters"] == 2000
