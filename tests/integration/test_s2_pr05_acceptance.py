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


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr05"
    model: str = "model.s2-pr05-v1"
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
            provider_response_id="resp_s2_pr05_123",
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


def _assert_redacted(value: Any) -> None:
    text = str(value)
    assert "sk-live" not in text
    assert "api_key=" not in text


def _assert_allowlisted_lifecycle_item(item: dict[str, Any]) -> None:
    assert set(item.keys()) == {
        "action_attempt_id",
        "proposal_index",
        "proposal",
        "policy",
        "approval",
        "execution",
    }
    assert set(item["proposal"].keys()) == {"capability_id", "input_summary"}
    assert set(item["policy"].keys()) == {"decision", "reason"}
    assert set(item["approval"].keys()) == {
        "status",
        "reference",
        "reason",
        "expires_at",
        "decided_at",
    }
    assert set(item["execution"].keys()) == {"status", "output", "error"}
    raw_internal_keys = {
        "capability_contract_hash",
        "payload_hash",
        "impact_level",
        "approval_required",
    }
    assert raw_internal_keys.isdisjoint(item.keys())


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    assert "action_attempts" not in turn_payload
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    _assert_allowlisted_lifecycle_item(item)
    return item


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    item = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval_payload = item["approval"]
    assert approval_payload["status"] == "pending"
    approval_ref = approval_payload.get("reference")
    assert isinstance(approval_ref, str)
    assert approval_ref.startswith("apr_")
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


def test_s2_pr05_turn_timeline_uses_surface_only_lifecycle_contract(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "run read": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "my credential is sk-live-inline-secret"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "run read"})
        assert sent.status_code == 200
        body = sent.json()
        item = _surface_attempt(body["turn"])

        assert item["proposal"]["capability_id"] == "cap.framework.read_echo"
        assert item["proposal"]["input_summary"]["text"] == "my credential is [REDACTED]"
        assert item["policy"]["decision"] == "allow_inline"
        assert item["policy"]["reason"] == "allowlisted_read"
        assert item["approval"]["status"] == "not_requested"
        assert item["approval"]["reference"] is None
        assert item["execution"]["status"] == "succeeded"
        assert item["execution"]["output"]["text"] == "my credential is [REDACTED]"
        _assert_redacted(item)

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        timeline_item = _surface_attempt(timeline.json()["turns"][-1])
        assert timeline_item["execution"]["status"] == "succeeded"
        _assert_redacted(timeline_item)

        surface = client.get("/")
        assert surface.status_code == 200
        assert surface.json()["surface"] == "discord"
        assert surface.json()["api"]["approval_decisions"] == "/v1/approvals"
        assert "chat-form" not in surface.text
        assert "lifecycleItem.approval.reference" not in surface.text
        assert "turn.action_attempts" not in surface.text


def test_s2_pr05_denied_flow_uses_surface_approval_ref_only(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "deny me": [{"capability_id": "cap.framework.write_note", "input": {"note": "nope"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "deny me"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        denied = client.post(
            "/v1/approvals",
            json={
                "approval_ref": approval_ref,
                "decision": "deny",
                "reason": "deny because api_key=sk-live-deny-secret",
            },
        )
        assert denied.status_code == 200
        denied_payload = denied.json()
        _assert_surface_approval_response(denied_payload, expected_status="denied")
        denied_reason = denied_payload["approval"]["reason"]
        assert isinstance(denied_reason, str)
        assert "[REDACTED]" in denied_reason
        assert "sk-live-deny-secret" not in denied_reason

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        item = _surface_attempt(timeline.json()["turns"][-1])
        assert item["policy"]["decision"] == "requires_approval"
        assert item["policy"]["reason"] == "approval_required"
        assert item["approval"]["status"] == "denied"
        assert isinstance(item["approval"]["reason"], str)
        assert "[REDACTED]" in item["approval"]["reason"]
        assert item["execution"]["status"] == "not_executed"
        assert item["execution"]["output"] is None
        assert item["execution"]["error"] is None
        _assert_redacted(item)


def test_s2_pr05_expired_flow_uses_surface_approval_ref_only(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FrozenClock(datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
    monkeypatch.setattr("ariel.app._utcnow", clock.now)
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "5")

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "expire me": [{"capability_id": "cap.framework.write_note", "input": {"note": "late"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "expire me"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        clock.advance(seconds=10)
        expired = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "E_APPROVAL_EXPIRED"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        item = _surface_attempt(timeline.json()["turns"][-1])
        assert item["policy"]["decision"] == "requires_approval"
        assert item["policy"]["reason"] == "approval_required"
        assert item["approval"]["status"] == "expired"
        assert item["approval"]["reason"] == "approval_expired"
        assert item["execution"]["status"] == "not_executed"
        assert item["execution"]["output"] is None
        assert item["execution"]["error"] is None


def test_s2_pr05_approval_approved_execution_success_is_surface_only_and_redacted(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "approve success": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "notify sk-live-approved-secret",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "approve success"}
        )
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200
        _assert_surface_approval_response(approved.json(), expected_status="approved")

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        item = _surface_attempt(timeline.json()["turns"][-1])
        assert item["policy"]["decision"] == "requires_approval"
        assert item["policy"]["reason"] == "approval_required"
        assert item["approval"]["status"] == "approved"
        assert item["execution"]["status"] == "succeeded"
        assert item["execution"]["output"]["status"] == "sent"
        assert item["execution"]["output"]["destination"] == "https://api.framework.local/notify"
        assert item["execution"]["output"]["message"] == "notify [REDACTED]"
        assert "__egress__" not in str(item)
        _assert_redacted(item)


def test_s2_pr05_approval_approved_execution_failure_is_surface_only(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "approve fail": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://evil.example/exfil",
                        "message": "should fail",
                    },
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "approve fail"})
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert approved.status_code == 200
        _assert_surface_approval_response(approved.json(), expected_status="approved")

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        item = _surface_attempt(timeline.json()["turns"][-1])
        assert item["policy"]["decision"] == "requires_approval"
        assert item["policy"]["reason"] == "approval_required"
        assert item["approval"]["status"] == "approved"
        assert item["execution"]["status"] == "failed"
        assert isinstance(item["execution"]["error"], str)
        assert "egress_destination_denied" in item["execution"]["error"]


def test_s2_pr05_approval_endpoint_rejects_legacy_approval_id_field(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "legacy approval key": [
                {"capability_id": "cap.framework.write_note", "input": {"note": "modern only"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "legacy approval key"},
        )
        assert sent.status_code == 200
        approval_ref = _approval_ref(sent.json()["turn"])

        legacy = client.post(
            "/v1/approvals",
            json={"approval_id": approval_ref, "decision": "approve"},
        )
        assert legacy.status_code == 422
        assert legacy.json()["error"]["code"] == "E_VALIDATION"
