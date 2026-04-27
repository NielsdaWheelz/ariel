from __future__ import annotations

from collections.abc import Generator
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr01"
    model: str = "model.s2-pr01-v1"
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
            provider_response_id="resp_s2_pr01_123",
            input_tokens=13,
            output_tokens=11,
        )


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance_seconds(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


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


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _surface_attempts(turn_payload: dict[str, Any]) -> list[dict[str, Any]]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    return lifecycle


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    attempts = _surface_attempts(turn_payload)
    assert len(attempts) >= proposal_index
    attempt = attempts[proposal_index - 1]
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


def test_s2_pr01_allowlisted_read_executes_inline_with_redacted_output_and_audit_chain(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "run read": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "my token is sk-live-super-secret"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "run read"})
        assert sent.status_code == 200
        body = sent.json()
        assert body["ok"] is True
        assert "[REDACTED]" in body["assistant"]["message"]
        assert "sk-live-super-secret" not in body["assistant"]["message"]

        turn = body["turn"]
        action_attempts = _surface_attempts(turn)
        assert len(action_attempts) == 1
        action_attempt = action_attempts[0]
        assert action_attempt["policy"]["decision"] == "allow_inline"
        assert action_attempt["execution"]["status"] == "succeeded"
        assert "[REDACTED]" in str(action_attempt["execution"]["output"])
        assert "sk-live-super-secret" not in str(action_attempt["execution"]["output"])

        event_types = _event_types(turn)
        assert event_types.index("evt.action.proposed") < event_types.index("evt.action.policy_decided")
        assert event_types.index("evt.action.policy_decided") < event_types.index(
            "evt.action.execution.started"
        )
        assert event_types.index("evt.action.execution.started") < event_types.index(
            "evt.action.execution.succeeded"
        )


def test_s2_pr01_approval_required_action_is_persisted_pending_without_preapproval_execution(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "queue write": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "ship pr-01"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "queue write"})
        assert sent.status_code == 200
        turn = sent.json()["turn"]

        attempt = _surface_attempt(turn)
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["approval"]["status"] == "pending"
        assert isinstance(attempt["approval"]["reference"], str)
        assert attempt["execution"]["status"] == "not_executed"
        assert "approval required" in sent.json()["assistant"]["message"].lower()
        assert "evt.action.execution.started" not in _event_types(turn)


def test_s2_pr01_post_approvals_approve_executes_once_with_actor_binding_and_replay_protection(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "do write": [{"capability_id": "cap.framework.write_note", "input": {"note": "frozen-a"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "do write"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        wrong_actor = client.post(
            "/v1/approvals",
            json={
                "approval_ref": approval_ref,
                "decision": "approve",
                "actor_id": "intruder.user",
            },
        )
        assert wrong_actor.status_code == 403
        assert wrong_actor.json()["error"]["code"] == "E_APPROVAL_ACTOR_MISMATCH"

        approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        _assert_surface_approval_response(approved_body, expected_status="approved")

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert latest_attempt["approval"]["status"] == "approved"
        assert latest_attempt["execution"]["status"] == "succeeded"
        assert latest_attempt["execution"]["output"]["note"] == "frozen-a"

        replay = client.post(
            "/v1/approvals",
            json={
                "approval_ref": approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"


def test_s2_pr01_approval_executes_canonical_frozen_payload_identity(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "do write canonical": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "  frozen-a  "}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "do write canonical"},
        )
        assert sent.status_code == 200
        attempt = _surface_attempt(sent.json()["turn"])
        assert attempt["proposal"]["input_summary"]["note"] == "frozen-a"

        approval_ref = attempt["approval"]["reference"]
        approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert approved.status_code == 200
        _assert_surface_approval_response(approved.json(), expected_status="approved")
        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_attempt = _surface_attempt(timeline.json()["turns"][-1])
        output = latest_attempt["execution"]["output"]
        assert isinstance(output, dict)
        assert output["note"] == "frozen-a"


def test_s2_pr01_deny_and_expire_are_terminal_and_non_executing(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FrozenClock(datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
    monkeypatch.setattr("ariel.app._utcnow", clock.now)
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "5")

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "deny me": [{"capability_id": "cap.framework.write_note", "input": {"note": "nope"}}],
            "expire me": [{"capability_id": "cap.framework.write_note", "input": {"note": "late"}}],
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)

        deny_turn = client.post(f"/v1/sessions/{session_id}/message", json={"message": "deny me"})
        assert deny_turn.status_code == 200
        deny_approval_ref = _approval_ref(deny_turn.json()["turn"])

        denied = client.post(
            "/v1/approvals",
            json={"approval_ref": deny_approval_ref, "decision": "deny", "actor_id": "user.local"},
        )
        assert denied.status_code == 200
        _assert_surface_approval_response(denied.json(), expected_status="denied")
        deny_timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert deny_timeline.status_code == 200
        denied_attempt = _surface_attempt(deny_timeline.json()["turns"][-1])
        assert denied_attempt["approval"]["status"] == "denied"
        assert denied_attempt["execution"]["status"] == "not_executed"

        expire_turn = client.post(f"/v1/sessions/{session_id}/message", json={"message": "expire me"})
        assert expire_turn.status_code == 200
        expire_approval_ref = _approval_ref(expire_turn.json()["turn"])
        clock.advance_seconds(10)

        expired = client.post(
            "/v1/approvals",
            json={
                "approval_ref": expire_approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "E_APPROVAL_EXPIRED"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["approval"]["status"] == "expired"
        assert latest_attempt["execution"]["status"] == "not_executed"
        assert "evt.action.approval.expired" in _event_types(latest_turn)


@pytest.mark.parametrize(
    ("proposal", "expected_reason"),
    [
        (
            {"capability_id": "cap.framework.unknown", "input": {}},
            "unknown_capability",
        ),
        (
            {"capability_id": "cap.framework.read_echo", "input": {"invalid": "shape"}},
            "schema_invalid",
        ),
        (
            {"capability_id": "cap.framework.read_private", "input": {"text": "sensitive"}},
            "policy_denied",
        ),
    ],
)
def test_s2_pr01_invalid_or_denied_proposals_are_blocked_with_explicit_reason_and_safe_fallback(
    postgres_url: str,
    proposal: dict[str, Any],
    expected_reason: str,
) -> None:
    adapter = ActionProposalAdapter(proposals_by_message={"unsafe action": [proposal]})
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "unsafe action"})
        assert sent.status_code == 200
        body = sent.json()

        attempt = _surface_attempt(body["turn"])
        assert attempt["policy"]["decision"] == "deny"
        assert expected_reason in (attempt["policy"]["reason"] or "")
        assert "blocked" in body["assistant"]["message"].lower()
        assert "evt.action.execution.started" not in _event_types(body["turn"])


def test_s2_pr01_turn_allows_multiple_inline_reads_and_only_one_pending_approval(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mix actions": [
                {"capability_id": "cap.framework.read_echo", "input": {"text": "r1"}},
                {"capability_id": "cap.framework.read_echo", "input": {"text": "r2"}},
                {"capability_id": "cap.framework.write_note", "input": {"note": "w1"}},
                {"capability_id": "cap.framework.write_note", "input": {"note": "w2"}},
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "mix actions"})
        assert sent.status_code == 200

        attempts = _surface_attempts(sent.json()["turn"])
        assert len(attempts) == 4
        assert attempts[0]["execution"]["status"] == "succeeded"
        assert attempts[1]["execution"]["status"] == "succeeded"
        assert attempts[2]["approval"]["status"] == "pending"
        assert attempts[2]["execution"]["status"] == "not_executed"
        assert attempts[3]["policy"]["decision"] == "deny"
        assert attempts[3]["policy"]["reason"] == "pending_approval_limit_reached"
        assert sum(attempt["approval"]["status"] == "pending" for attempt in attempts) == 1

