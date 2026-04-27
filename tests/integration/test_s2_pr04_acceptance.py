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


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr04"
    model: str = "model.s2-pr04-v1"
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
            "usage": {"prompt_tokens": 19, "completion_tokens": 13, "total_tokens": 32},
            "provider_response_id": "resp_s2_pr04_123",
            "action_proposals": copy.deepcopy(proposals),
        }


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
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


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


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
    assert set(item["approval"].keys()) == {"status", "reference", "reason", "expires_at", "decided_at"}
    assert set(item["execution"].keys()) == {"status", "output", "error"}


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    _assert_allowlisted_lifecycle_item(item)
    return item


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    item = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval = item["approval"]
    assert approval["status"] == "pending"
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


def _assert_redacted(value: Any) -> None:
    text = str(value)
    assert "sk-live" not in text
    assert "api_key=" not in text


def test_s2_pr04_inline_read_success_is_surface_inspectable_redacted_and_allowlisted(
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
        surface_payload = surface.json()
        assert surface_payload["ok"] is True
        assert surface_payload["surface"] == "discord"
        assert surface_payload["api"]["session_events"] == "/v1/sessions/{session_id}/events"


def test_s2_pr04_approval_denied_is_surface_inspectable_with_redacted_reason(
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
                "actor_id": "user.local",
                "reason": "deny because api_key=sk-live-deny-secret",
            },
        )
        assert denied.status_code == 200

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


def test_s2_pr04_approval_expired_is_surface_inspectable_as_terminal_not_executed(
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
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
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


def test_s2_pr04_approval_approved_execution_success_is_surface_inspectable_and_redacted(
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
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "approve success"})
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


def test_s2_pr04_approval_approved_execution_failure_is_surface_inspectable(
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
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
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
