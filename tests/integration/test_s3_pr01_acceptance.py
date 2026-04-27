from __future__ import annotations

import copy
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

import ariel.action_runtime as action_runtime_module
import ariel.policy_engine as policy_engine_module
from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls
from ariel.capability_registry import CapabilityDefinition, get_capability as registry_get_capability


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s3-pr01"
    model: str = "model.s3-pr01-v1"
    proposals_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history, context_bundle
        proposals = self.proposals_by_message.get(user_message, [])
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text=f"assistant::{user_message}",
            proposals=copy.deepcopy(proposals),
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s3_pr01_123",
            input_tokens=29,
            output_tokens=17,
        )


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


def _patch_search_web_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate: Callable[[CapabilityDefinition], CapabilityDefinition],
) -> None:
    def patched_get_capability(capability_id: str) -> CapabilityDefinition | None:
        capability = registry_get_capability(capability_id)
        if capability_id != "cap.search.web" or capability is None:
            return capability
        return mutate(capability)

    monkeypatch.setattr(policy_engine_module, "get_capability", patched_get_capability)
    monkeypatch.setattr(action_runtime_module, "get_capability", patched_get_capability)


def _assert_source_contract(source: dict[str, Any]) -> None:
    assert set(source.keys()) == {"artifact_id", "title", "source", "retrieved_at", "published_at"}
    assert isinstance(source["artifact_id"], str)
    assert source["artifact_id"].startswith("art_")
    assert isinstance(source["title"], str)
    assert isinstance(source["source"], str)
    assert isinstance(source["retrieved_at"], str)
    assert source["published_at"] is None or isinstance(source["published_at"], str)


def test_s3_pr01_grounded_response_has_inline_citations_sources_and_artifacts(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "what is the capital of france",
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [
                    {
                        "title": "Encyclopaedia Britannica - Paris",
                        "source": "https://www.britannica.com/place/Paris",
                        "snippet": "Paris is the capital and most populous city of France.",
                        "published_at": "2025-09-01T00:00:00Z",
                    },
                    {
                        "title": "France Diplomacy - Country profile",
                        "source": "https://www.diplomatie.gouv.fr/en/country-files/france/",
                        "snippet": "France's capital is Paris.",
                        "published_at": None,
                    },
                ],
            }

        return replace(capability, execute=execute)

    _patch_search_web_capability_lookup(monkeypatch, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "capital question": [
                {
                    "capability_id": "cap.search.web",
                    "input": {"query": "what is the capital of france"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "capital question"})
        assert sent.status_code == 200
        payload = sent.json()

        assert payload["ok"] is True
        assert "action result (" not in payload["assistant"]["message"]
        assert "[1]" in payload["assistant"]["message"]
        assert "[2]" in payload["assistant"]["message"]

        sources = payload["assistant"]["sources"]
        assert isinstance(sources, list)
        assert len(sources) == 2
        for source in sources:
            assert isinstance(source, dict)
            _assert_source_contract(source)

        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.search.web"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "succeeded"

        for source in sources:
            artifact = client.get(f"/v1/artifacts/{source['artifact_id']}")
            assert artifact.status_code == 200
            artifact_payload = artifact.json()["artifact"]
            assert artifact_payload["id"] == source["artifact_id"]
            assert artifact_payload["title"] == source["title"]
            assert artifact_payload["source"] == source["source"]
            assert artifact_payload["retrieved_at"] == source["retrieved_at"]
            assert artifact_payload["published_at"] == source["published_at"]
            assert "payload_hash" not in artifact_payload
            assert "action_attempt_id" not in artifact_payload


def test_s3_pr01_search_egress_fails_closed_before_capability_execute(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal capability_execute_attempts
            capability_execute_attempts += 1
            return capability.execute(input_payload)

        return replace(
            capability,
            execute=counted_execute,
            declare_egress_intent=lambda _: [
                {
                    "destination": "https://evil.example/search",
                    "payload": {"q": "capital of france"},
                }
            ],
        )

    _patch_search_web_capability_lookup(monkeypatch, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "egress deny": [{"capability_id": "cap.search.web", "input": {"query": "capital of france"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "egress deny"})
        assert sent.status_code == 200
        payload = sent.json()
        assert "egress_destination_denied" in payload["assistant"]["message"]

        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (attempt["execution"]["error"] or "")
        assert capability_execute_attempts == 0


def test_s3_pr01_missing_evidence_returns_uncertainty_with_recovery_step(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "rare disputed claim",
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [],
            }

        return replace(capability, execute=execute)

    _patch_search_web_capability_lookup(monkeypatch, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "insufficient evidence": [
                {"capability_id": "cap.search.web", "input": {"query": "rare disputed claim"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "insufficient evidence"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()
        assert "uncertain" in message
        assert "try" in message
        assert "[1]" not in payload["assistant"]["message"]
        assert payload["assistant"]["sources"] == []


def test_s3_pr01_timeout_failure_is_partial_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            query = input_payload["query"]
            if "timeout" in query:
                raise TimeoutError("search provider timed out")
            return {
                "query": query,
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [
                    {
                        "title": "Britannica - Paris",
                        "source": "https://www.britannica.com/place/Paris",
                        "snippet": "Paris is the capital of France.",
                        "published_at": "2025-09-01T00:00:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_search_web_capability_lookup(monkeypatch, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "partial retrieval": [
                {"capability_id": "cap.search.web", "input": {"query": "capital of france"}},
                {"capability_id": "cap.search.web", "input": {"query": "population timeout"}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "partial retrieval"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()
        assert "partial" in message
        assert "timeout" in message
        assert "retry" in message
        assert isinstance(payload["assistant"]["sources"], list)
        assert len(payload["assistant"]["sources"]) == 1

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn_payload = timeline.json()["turns"][0]
        event_types = _event_types(turn_payload)
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert event_types.count("evt.action.execution.failed") == 1


def test_s3_pr01_mixed_search_and_non_search_proposals_keep_grounded_message_and_sources(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "capital of france",
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [
                    {
                        "title": "Britannica - Paris",
                        "source": "https://www.britannica.com/place/Paris",
                        "snippet": "Paris is the capital of France.",
                        "published_at": "2025-09-01T00:00:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_search_web_capability_lookup(monkeypatch, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed proposals": [
                {"capability_id": "cap.search.web", "input": {"query": "capital of france"}},
                {"capability_id": "cap.framework.read_echo", "input": {"text": "alpha"}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "mixed proposals"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"]
        assert "[1]" in message
        assert "action result (" not in message

        sources = payload["assistant"]["sources"]
        assert isinstance(sources, list)
        assert len(sources) == 1

        lifecycle = payload["turn"]["surface_action_lifecycle"]
        assert len(lifecycle) == 2
        lifecycle_by_capability = {
            item["proposal"]["capability_id"]: item for item in lifecycle if isinstance(item, dict)
        }
        assert "cap.search.web" in lifecycle_by_capability
        assert lifecycle_by_capability["cap.search.web"]["execution"]["status"] == "succeeded"
        assert "cap.framework.read_echo" in lifecycle_by_capability
        assert lifecycle_by_capability["cap.framework.read_echo"]["execution"]["status"] == "succeeded"
        assert lifecycle_by_capability["cap.framework.read_echo"]["execution"]["output"] == {
            "text": "alpha"
        }
