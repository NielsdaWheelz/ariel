from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls

FORBIDDEN_SURFACE_KEYS = {
    "action_attempts",
    "action_attempt",
    "capability_contract_hash",
    "payload_hash",
    "impact_level",
    "approval_required",
}


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr07"
    model: str = "model.s2-pr07-v1"
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
            provider_response_id="resp_s2_pr07_123",
            input_tokens=23,
            output_tokens=17,
        )


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
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
    lifecycle = turn_payload["surface_action_lifecycle"]
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    assert set(item.keys()) == {
        "action_attempt_id",
        "proposal_index",
        "proposal",
        "policy",
        "approval",
        "execution",
    }
    return item


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    attempt = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval = attempt["approval"]
    assert approval["status"] == "pending"
    approval_ref = approval["reference"]
    assert isinstance(approval_ref, str)
    return approval_ref


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        keys = set(value.keys())
        assert keys.isdisjoint(FORBIDDEN_SURFACE_KEYS)
        for nested in value.values():
            _assert_no_forbidden_keys(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_keys(nested)


def test_s2_pr07_expired_pending_reconciles_on_timeline_read_without_decision_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FrozenClock(datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
    monkeypatch.setattr("ariel.app._utcnow", clock.now)
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "5")

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "expire on read": [
                {
                    "capability_id": "cap.framework.write_note",
                    "input": {"note": "contains sk-live-pr07-secret"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "expire on read"})
        assert sent.status_code == 200
        created_turn = sent.json()["turn"]

        approval_ref = _approval_ref(created_turn)
        created_attempt = _surface_attempt(created_turn)
        action_attempt_id = created_attempt["action_attempt_id"]
        assert created_attempt["approval"]["status"] == "pending"

        clock.advance(seconds=10)

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        reconciled_turn = timeline.json()["turns"][-1]
        reconciled_attempt = _surface_attempt(reconciled_turn)

        assert reconciled_attempt["action_attempt_id"] == action_attempt_id
        assert reconciled_attempt["approval"]["status"] == "expired"
        assert reconciled_attempt["approval"]["reason"] == "approval_expired"
        assert reconciled_attempt["execution"]["status"] == "not_executed"
        assert reconciled_attempt["approval"]["reference"] == approval_ref
        assert "sk-live-pr07-secret" not in str(reconciled_attempt)
        _assert_no_forbidden_keys(reconciled_attempt)

        expired_events = [
            event
            for event in reconciled_turn["events"]
            if event["event_type"] == "evt.action.approval.expired"
        ]
        assert len(expired_events) == 1
        expired_payload = expired_events[0]["payload"]
        assert set(expired_payload.keys()) == {"action_attempt_id", "approval_ref", "reason"}
        assert expired_payload["action_attempt_id"] == action_attempt_id
        assert expired_payload["approval_ref"] == approval_ref
        assert expired_payload["reason"] == "approval_expired"


def test_s2_pr07_reconciled_expiry_is_idempotent_for_reads_and_decisions(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FrozenClock(datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
    monkeypatch.setattr("ariel.app._utcnow", clock.now)
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "5")

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "expire idempotently": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "late"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "expire idempotently"}
        )
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        clock.advance(seconds=10)

        first_timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert first_timeline.status_code == 200
        first_turn = first_timeline.json()["turns"][-1]
        assert _event_types(first_turn).count("evt.action.approval.expired") == 1
        assert _surface_attempt(first_turn)["approval"]["status"] == "expired"

        second_timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert second_timeline.status_code == 200
        second_turn = second_timeline.json()["turns"][-1]
        assert _event_types(second_turn).count("evt.action.approval.expired") == 1
        assert _surface_attempt(second_turn)["approval"]["status"] == "expired"

        replay = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert replay.status_code == 409
        replay_payload = replay.json()
        assert replay_payload["error"]["code"] == "E_APPROVAL_NOT_PENDING"
        details = replay_payload["error"]["details"]
        assert isinstance(details, dict)
        assert details["approval_ref"] == approval_ref
        assert details["status"] == "expired"

        final_timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert final_timeline.status_code == 200
        final_turn = final_timeline.json()["turns"][-1]
        event_types = _event_types(final_turn)
        assert event_types.count("evt.action.approval.expired") == 1
        assert "evt.action.approval.approved" not in event_types
        assert "evt.action.execution.started" not in event_types
        assert "evt.action.execution.succeeded" not in event_types
        assert "evt.action.execution.failed" not in event_types
        final_attempt = _surface_attempt(final_turn)
        assert final_attempt["approval"]["status"] == "expired"
        assert final_attempt["execution"]["status"] == "not_executed"
