from __future__ import annotations

import copy
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest
from testcontainers.postgres import PostgresContainer

import ariel.action_runtime as action_runtime_module
import ariel.capability_registry as capability_registry_module
import ariel.policy_engine as policy_engine_module
from ariel.app import ModelAdapter, create_app
from ariel.capability_registry import CapabilityDefinition
from ariel.google_connector import ConnectorTokenCipher


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s6-pr02"
    model: str = "model.s6-pr02-v1"
    proposals_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history, context_bundle
        proposals = self.proposals_by_message.get(user_message, [])
        return {
            "assistant_text": f"assistant::{user_message}",
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 41, "completion_tokens": 26, "total_tokens": 67},
            "provider_response_id": "resp_s6_pr02_123",
            "action_proposals": copy.deepcopy(proposals),
        }


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


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
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


def _set_valid_maps_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_SECRET", "dev-local-connector-secret")
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION", "v1")
    monkeypatch.delenv("ARIEL_CONNECTOR_ENCRYPTION_KEYS", raising=False)
    cipher = ConnectorTokenCipher.from_config(
        active_key_version="v1",
        configured_keys=None,
        fallback_secret="dev-local-connector-secret",
    )
    monkeypatch.setenv("ARIEL_MAPS_PROVIDER_API_KEY_ENC", cipher.encrypt("maps-test-key"))


def test_s6_pr02_maps_directions_execute_inline_with_citations_and_auditable_lifecycle(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "origin": input_payload["origin"],
                "destination": input_payload["destination"],
                "travel_mode": input_payload["travel_mode"],
                "retrieved_at": "2026-03-06T12:00:00Z",
                "results": [
                    {
                        "title": "Fastest route to destination",
                        "source": "https://maps.example.test/directions/primary-route",
                        "snippet": "distance_meters=17200 duration_seconds=1260 via i-5 northbound",
                        "published_at": "2026-03-06T11:59:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.maps.directions", mutate=mutate)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "route to airport": [
                {
                    "capability_id": "cap.maps.directions",
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "route to airport"})
        assert sent.status_code == 200
        payload = sent.json()

        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.maps.directions"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["approval"]["status"] == "not_requested"
        assert attempt["execution"]["status"] == "succeeded"
        assert "[1]" in payload["assistant"]["message"]
        assert len(payload["assistant"]["sources"]) == 1
        _assert_source_contract(payload["assistant"]["sources"][0])
        assert "maps.example.test" in payload["assistant"]["sources"][0]["source"]

        event_types = _event_types(payload["turn"])
        assert "evt.action.execution.started" in event_types
        assert "evt.action.execution.succeeded" in event_types


def test_s6_pr02_maps_search_places_execute_inline_with_disambiguating_metadata_and_citations(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": input_payload["query"],
                "location_context": input_payload["location_context"],
                "retrieved_at": "2026-03-06T12:10:00Z",
                "results": [
                    {
                        "title": "Blue Bottle Coffee",
                        "source": "https://maps.example.test/place/blue-bottle",
                        "snippet": (
                            "address=300 1st Ave, Seattle WA distance_meters=260 "
                            "rating=4.6 open_now=true"
                        ),
                        "published_at": "2026-03-06T12:09:00Z",
                    },
                    {
                        "title": "Anchorhead Coffee",
                        "source": "https://maps.example.test/place/anchorhead",
                        "snippet": (
                            "address=1600 7th Ave, Seattle WA distance_meters=420 "
                            "rating=4.5 open_now=false"
                        ),
                        "published_at": "2026-03-06T12:08:00Z",
                    },
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.maps.search_places", mutate=mutate)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "find nearby coffee": [
                {
                    "capability_id": "cap.maps.search_places",
                    "input": {
                        "query": "coffee",
                        "location_context": "Downtown Seattle, WA",
                        "radius_meters": 1200,
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "find nearby coffee"})
        assert sent.status_code == 200
        payload = sent.json()

        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.maps.search_places"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["approval"]["status"] == "not_requested"
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert isinstance(output["results"], list)
        assert len(output["results"]) >= 2
        assert "distance_meters=" in output["results"][0]["snippet"]
        assert "[1]" in payload["assistant"]["message"]
        assert len(payload["assistant"]["sources"]) >= 1


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
        proposals_by_message={
            "missing route field": [
                {"capability_id": "cap.maps.directions", "input": input_payload},
            ]
        }
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
        proposals_by_message={
            "missing places location": [
                {"capability_id": "cap.maps.search_places", "input": {"query": "coffee"}},
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


@pytest.mark.parametrize(
    ("credential_mode", "expected_error", "expected_hint"),
    [
        ("missing", "provider_credentials_missing", "operator"),
        ("invalid", "provider_credentials_invalid", "operator"),
    ],
)
def test_s6_pr02_maps_credentials_failures_are_typed_and_recoverable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    credential_mode: str,
    expected_error: str,
    expected_hint: str,
) -> None:
    if credential_mode == "missing":
        monkeypatch.delenv("ARIEL_MAPS_PROVIDER_API_KEY_ENC", raising=False)
    else:
        monkeypatch.setenv("ARIEL_MAPS_PROVIDER_API_KEY_ENC", "aeadv1:v1:bad_nonce:bad_payload")

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "maps credentials failure": [
                {
                    "capability_id": "cap.maps.directions",
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
            f"/v1/sessions/{session_id}/message",
            json={"message": "maps credentials failure"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        rendered_message = payload["assistant"]["message"].lower()
        assert expected_error in rendered_message
        assert expected_hint in rendered_message


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
        ("unreachable", "provider_unreachable", "operator"),
    ],
)
def test_s6_pr02_maps_provider_failures_are_typed_and_recoverable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_error: str,
    expected_hint: str,
) -> None:
    _set_valid_maps_credentials(monkeypatch)
    monkeypatch.setenv("ARIEL_MAPS_PROVIDER_TIMEOUT_SECONDS", "2.0")

    if failure_mode == "timeout":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            raise httpx.TimeoutException("maps timeout")
    elif failure_mode == "network":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            raise httpx.HTTPError("maps network failure")
    elif failure_mode == "rate_limited":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            return _FakeHTTPResponse(status_code=429, payload={"error": "rate_limited"})
    elif failure_mode == "upstream_failure":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            return _FakeHTTPResponse(status_code=503, payload={"error": "upstream"})
    elif failure_mode == "permission_denied":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            return _FakeHTTPResponse(status_code=403, payload={"error": "forbidden"})
    elif failure_mode == "request_rejected":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            return _FakeHTTPResponse(status_code=400, payload={"error": "bad_request"})
    elif failure_mode == "invalid_payload":
        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            del args, kwargs
            return _FakeHTTPResponse(status_code=200, json_raises=True)
    else:
        monkeypatch.setenv("ARIEL_MAPS_PROVIDER_ENDPOINT", "ftp://maps.googleapis.com/maps/api")

        def fake_get(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
            msg = "unreachable should fail before httpx call"
            raise AssertionError(msg)

    monkeypatch.setattr(capability_registry_module.httpx, "get", fake_get)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "maps runtime failure": [
                {
                    "capability_id": "cap.maps.search_places",
                    "input": {
                        "query": "coffee",
                        "location_context": "Downtown Seattle, WA",
                        "radius_meters": 1200,
                    },
                }
            ]
        }
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
        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal execute_attempts
            execute_attempts += 1
            return capability.execute(input_payload)

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
        proposals_by_message={
            "maps egress deny": [
                {
                    "capability_id": "cap.maps.directions",
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "maps egress deny"})
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
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "origin": input_payload["origin"],
                "destination": input_payload["destination"],
                "travel_mode": input_payload["travel_mode"],
                "retrieved_at": "2026-03-06T12:20:00Z",
                "results": [
                    {
                        "title": "Default route",
                        "source": "https://maps.example.test/directions/default",
                        "snippet": "distance_meters=17200 duration_seconds=1260",
                        "published_at": "2026-03-06T12:19:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.maps.directions", mutate=mutate)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "maps while google disconnected": [
                {
                    "capability_id": "cap.maps.directions",
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
    def mutate_maps(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "origin": input_payload["origin"],
                "destination": input_payload["destination"],
                "travel_mode": input_payload["travel_mode"],
                "retrieved_at": "2026-03-06T12:30:00Z",
                "results": [
                    {
                        "title": "Route option A",
                        "source": "https://maps.example.test/directions/route-a",
                        "snippet": "distance_meters=17200 duration_seconds=1260 tolls=false",
                        "published_at": "2026-03-06T12:29:30Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

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

    _patch_capability_lookup(monkeypatch, capability_id="cap.maps.directions", mutate=mutate_maps)
    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate_web)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed retrieval": [
                {
                    "capability_id": "cap.maps.directions",
                    "input": {
                        "origin": "Pike Place Market, Seattle, WA",
                        "destination": "SEA Airport, Seattle, WA",
                        "travel_mode": "driving",
                    },
                },
                {
                    "capability_id": "cap.search.web",
                    "input": {"query": "airport construction updates"},
                },
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "mixed retrieval"})
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
