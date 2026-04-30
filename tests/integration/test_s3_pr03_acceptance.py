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
from ariel.capability_registry import (
    CapabilityDefinition,
    get_capability as registry_get_capability,
)


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s3-pr03"
    model: str = "model.s3-pr03-v1"
    proposals_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
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
        del tools, history, context_bundle
        proposals = self.proposals_by_message.get(user_message, [])
        assistant_text = self.assistant_text_by_message.get(
            user_message, f"assistant::{user_message}"
        )
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text=assistant_text,
            proposals=copy.deepcopy(proposals),
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s3_pr03_123",
            input_tokens=41,
            output_tokens=24,
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


def _patch_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capability_id: str,
    mutate: Callable[[CapabilityDefinition], CapabilityDefinition],
) -> None:
    def patched_get_capability(candidate_id: str) -> CapabilityDefinition | None:
        capability = registry_get_capability(candidate_id)
        if candidate_id != capability_id or capability is None:
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


def test_s3_pr03_conflicting_evidence_returns_uncertainty_with_recovery(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "capital of freedonia",
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [
                    {
                        "title": "freedonia almanac",
                        "source": "https://example.com/almanac",
                        "snippet": "The capital of Freedonia is Belltown.",
                        "published_at": "2026-03-03T10:00:00Z",
                    },
                    {
                        "title": "freedonia census",
                        "source": "https://example.com/census",
                        "snippet": "The capital of Freedonia is Northport.",
                        "published_at": "2026-03-03T10:05:00Z",
                    },
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "conflicting evidence": [
                {"capability_id": "cap.search.web", "input": {"query": "capital of freedonia"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "conflicting evidence"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()

        assert "uncertain" in message
        assert "conflict" in message or "disagree" in message
        assert "retry" in message or "narrower" in message or "source" in message
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


def test_s3_pr03_distinct_facts_about_same_entity_do_not_trigger_conflict_mode(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": "paris facts",
                "retrieved_at": "2026-03-03T12:00:00Z",
                "results": [
                    {
                        "title": "paris profile",
                        "source": "https://example.com/paris-profile",
                        "snippet": "Paris is the capital of France.",
                        "published_at": "2026-03-03T10:00:00Z",
                    },
                    {
                        "title": "paris demographics",
                        "source": "https://example.com/paris-demographics",
                        "snippet": "Paris is the most populous city in France.",
                        "published_at": "2026-03-03T10:05:00Z",
                    },
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "distinct paris facts": [
                {"capability_id": "cap.search.web", "input": {"query": "paris facts"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "distinct paris facts"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()

        assert "uncertain" not in message
        assert "conflict" not in message
        assert "[1]" in payload["assistant"]["message"]
        assert "[2]" in payload["assistant"]["message"]
        assert isinstance(payload["assistant"]["sources"], list)
        assert len(payload["assistant"]["sources"]) == 2


def _make_search_output(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "retrieved_at": "2026-03-03T12:00:00Z",
        "results": [
            {
                "title": f"source for {query}",
                "source": "https://example.com/search",
                "snippet": "Retrieved evidence confirms this statement from the cited source.",
                "published_at": "2026-03-03T11:30:00Z",
            }
        ],
    }


def _make_news_output(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "retrieved_at": "2026-03-03T12:00:00Z",
        "results": [
            {
                "title": f"news for {query}",
                "source": "https://example.com/news",
                "snippet": "News item reports the latest update for this topic.",
                "published_at": "2026-03-03T11:45:00Z",
            }
        ],
    }


def _make_weather_output(location: str, timeframe: str) -> dict[str, Any]:
    return {
        "location": location,
        "timeframe": timeframe,
        "forecast_timestamp": "2026-03-03T13:00:00Z",
        "retrieved_at": "2026-03-03T12:59:30Z",
        "results": [
            {
                "title": f"forecast for {location}",
                "source": "https://weather.example/forecast",
                "snippet": "Light rain expected with temperatures near 14C.",
                "published_at": "2026-03-03T12:58:00Z",
            }
        ],
    }


@pytest.mark.parametrize(
    ("capability_id", "proposal_input"),
    [
        ("cap.search.web", {"query": "capital of france"}),
        ("cap.search.news", {"query": "ai regulation europe"}),
        ("cap.weather.forecast", {"location": "Tokyo, JP", "timeframe": "today"}),
    ],
)
def test_s3_pr03_mixed_turns_keep_grounded_citations_and_sources_for_retrieval_caps(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capability_id: str,
    proposal_input: dict[str, Any],
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            if capability_id == "cap.search.news":
                query = input_payload.get("query", "query")
                assert isinstance(query, str)
                return _make_news_output(query)
            if capability_id == "cap.weather.forecast":
                location = input_payload.get("location", "unknown")
                timeframe = input_payload.get("timeframe", "today")
                assert isinstance(location, str)
                assert isinstance(timeframe, str)
                return _make_weather_output(location, timeframe)
            query = input_payload.get("query", "query")
            assert isinstance(query, str)
            return _make_search_output(query)

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id=capability_id, mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed retrieval": [
                {"capability_id": capability_id, "input": proposal_input},
                {"capability_id": "cap.framework.read_echo", "input": {"text": "alpha"}},
            ]
        },
        assistant_text_by_message={
            "mixed retrieval": "the capital of france is definitely lyon and i am fully certain.",
        },
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
        assert "action result (" not in message
        assert "lyon" not in message.lower()

        sources = payload["assistant"]["sources"]
        assert isinstance(sources, list)
        assert len(sources) == 1
        source = sources[0]
        _assert_source_contract(source)

        artifact = client.get(f"/v1/artifacts/{source['artifact_id']}")
        assert artifact.status_code == 200

        lifecycle = payload["turn"]["surface_action_lifecycle"]
        assert len(lifecycle) == 2
        lifecycle_by_capability = {
            item["proposal"]["capability_id"]: item for item in lifecycle if isinstance(item, dict)
        }
        assert capability_id in lifecycle_by_capability
        assert "cap.framework.read_echo" in lifecycle_by_capability
        assert (
            lifecycle_by_capability["cap.framework.read_echo"]["execution"]["status"] == "succeeded"
        )
        assert lifecycle_by_capability["cap.framework.read_echo"]["execution"]["output"] == {
            "text": "alpha"
        }


def test_s3_pr03_mixed_turn_partial_retrieval_failure_is_disclosed_and_recoverable(
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
                        "title": "britannica - paris",
                        "source": "https://www.britannica.com/place/Paris",
                        "snippet": "Paris is the capital of France.",
                        "published_at": "2026-03-03T10:00:00Z",
                    }
                ],
            }

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed partial retrieval": [
                {"capability_id": "cap.search.web", "input": {"query": "capital of france"}},
                {"capability_id": "cap.search.web", "input": {"query": "population timeout"}},
                {"capability_id": "cap.framework.read_echo", "input": {"text": "alpha"}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "mixed partial retrieval"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()

        assert "[1]" in payload["assistant"]["message"]
        assert "partial" in message
        assert "timeout" in message
        assert "retry" in message
        assert "action result (" not in message
        assert isinstance(payload["assistant"]["sources"], list)
        assert len(payload["assistant"]["sources"]) == 1

        event_types = _event_types(payload["turn"])
        assert event_types.count("evt.action.execution.succeeded") == 2
        assert event_types.count("evt.action.execution.failed") == 1

        lifecycle = payload["turn"]["surface_action_lifecycle"]
        assert len(lifecycle) == 3
        retrieval_failures = [
            item
            for item in lifecycle
            if item["proposal"]["capability_id"] == "cap.search.web"
            and item["execution"]["status"] == "failed"
        ]
        assert len(retrieval_failures) == 1


def test_s3_pr03_mixed_turn_non_retrieval_denial_remains_inspectable_without_appendix(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            return _make_search_output(input_payload["query"])

        return replace(capability, execute=execute)

    _patch_capability_lookup(monkeypatch, capability_id="cap.search.web", mutate=mutate)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed denial inspectable": [
                {"capability_id": "cap.search.web", "input": {"query": "capital of france"}},
                {"capability_id": "cap.framework.read_echo", "input": {}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "mixed denial inspectable"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"]

        assert "[1]" in message
        assert "action result (" not in message
        assert isinstance(payload["assistant"]["sources"], list)
        assert len(payload["assistant"]["sources"]) == 1

        lifecycle = payload["turn"]["surface_action_lifecycle"]
        assert len(lifecycle) == 2
        lifecycle_by_capability = {
            item["proposal"]["capability_id"]: item for item in lifecycle if isinstance(item, dict)
        }
        assert lifecycle_by_capability["cap.search.web"]["execution"]["status"] == "succeeded"
        assert lifecycle_by_capability["cap.framework.read_echo"]["policy"]["decision"] == "deny"
        assert (
            lifecycle_by_capability["cap.framework.read_echo"]["policy"]["reason"]
            == "schema_invalid"
        )
        assert (
            lifecycle_by_capability["cap.framework.read_echo"]["execution"]["status"]
            == "not_executed"
        )
