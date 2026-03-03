from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s2-pr03"
    model: str = "model.s2-pr03-v1"
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
            "usage": {"prompt_tokens": 21, "completion_tokens": 15, "total_tokens": 36},
            "provider_response_id": "resp_s2_pr03_123",
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


def _proposal_with_optional_taint_metadata(
    *,
    capability_id: str,
    input_payload: dict[str, Any],
    include_taint_field: bool,
    taint_value: Any,
) -> dict[str, Any]:
    proposal = {"capability_id": capability_id, "input": input_payload}
    if include_taint_field:
        proposal["influenced_by_untrusted_content"] = taint_value
    return proposal


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    attempt = lifecycle[proposal_index - 1]
    assert isinstance(attempt, dict)
    return attempt


@pytest.mark.parametrize(
    ("include_taint_field", "taint_value", "expected_model_taint_status"),
    [
        (False, None, "missing"),
        (True, False, "false"),
        (True, {"malformed": "shape"}, "malformed"),
    ],
    ids=["taint_metadata_omitted", "taint_metadata_false", "taint_metadata_malformed"],
)
def test_s2_pr03_runtime_provenance_taint_applies_to_side_effects_despite_unreliable_model_taint_flags(
    postgres_url: str,
    include_taint_field: bool,
    taint_value: Any,
    expected_model_taint_status: str,
) -> None:
    tainted_write_proposal = _proposal_with_optional_taint_metadata(
        capability_id="cap.framework.write_draft",
        input_payload={"note": "ship this draft"},
        include_taint_field=include_taint_field,
        taint_value=taint_value,
    )
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ingest untrusted": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "external feed says: ship now"},
                }
            ],
            "draft from feed": [tainted_write_proposal],
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        ingest = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ingest untrusted"},
        )
        assert ingest.status_code == 200
        ingest_body = ingest.json()
        ingest_turn_id = ingest_body["turn"]["id"]
        ingest_attempt = _surface_attempt(ingest_body["turn"])
        ingest_attempt_id = ingest_attempt["action_attempt_id"]
        assert ingest_attempt["execution"]["status"] == "succeeded"

        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "draft from feed"},
        )
        assert sent.status_code == 200
        body = sent.json()

        attempt = _surface_attempt(body["turn"])
        assert attempt["approval"]["status"] == "pending"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["policy"]["reason"] == "taint_escalated_requires_approval"
        assert "evt.action.execution.started" not in _event_types(body["turn"])

        proposed_event = next(
            event for event in body["turn"]["events"] if event["event_type"] == "evt.action.proposed"
        )
        taint_payload = proposed_event["payload"]["taint"]
        assert taint_payload["influenced_by_untrusted_content"] is True
        assert taint_payload["provenance_status"] == "tainted"
        assert taint_payload["runtime_provenance"]["status"] == "tainted"
        assert taint_payload["model_declared_taint"]["status"] == expected_model_taint_status
        assert any(
            evidence.get("kind") == "prior_tool_output_in_context"
            and evidence.get("turn_id") == ingest_turn_id
            and evidence.get("action_attempt_id") == ingest_attempt_id
            and evidence.get("capability_id") == "cap.framework.read_echo"
            for evidence in taint_payload["runtime_provenance"]["evidence"]
        )

        policy_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.policy_decided"
        )
        assert policy_event["payload"]["reason"] == "taint_escalated_requires_approval"
        assert policy_event["payload"]["taint"]["provenance_status"] == "tainted"


def test_s2_pr03_tainted_external_send_is_denied_even_when_model_declares_clean(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ingest untrusted": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "external message body"},
                }
            ],
            "send outbound": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "ship now",
                    },
                    "influenced_by_untrusted_content": False,
                }
            ],
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        ingest = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ingest untrusted"},
        )
        assert ingest.status_code == 200
        assert _surface_attempt(ingest.json()["turn"])["execution"]["status"] == "succeeded"

        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "send outbound"},
        )
        assert sent.status_code == 200
        body = sent.json()
        attempt = _surface_attempt(body["turn"])
        assert attempt["policy"]["decision"] == "deny"
        assert attempt["policy"]["reason"] == "taint_denied_untrusted_side_effect"
        assert "evt.action.execution.started" not in _event_types(body["turn"])

        policy_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.policy_decided"
        )
        assert policy_event["payload"]["reason"] == "taint_denied_untrusted_side_effect"
        assert policy_event["payload"]["taint"]["provenance_status"] == "tainted"


def test_s2_pr03_malformed_taint_metadata_is_treated_as_provenance_ambiguous_for_write_reversible(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ambiguous write": [
                {
                    "capability_id": "cap.framework.write_draft",
                    "input": {"note": "ambiguous source"},
                    "influenced_by_untrusted_content": "definitely-clean",
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ambiguous write"},
        )
        assert sent.status_code == 200
        body = sent.json()
        attempt = _surface_attempt(body["turn"])
        assert attempt["approval"]["status"] == "pending"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["policy"]["reason"] == "taint_escalated_requires_approval"
        assert "evt.action.execution.started" not in _event_types(body["turn"])

        proposed_event = next(
            event for event in body["turn"]["events"] if event["event_type"] == "evt.action.proposed"
        )
        taint_payload = proposed_event["payload"]["taint"]
        assert taint_payload["influenced_by_untrusted_content"] is True
        assert taint_payload["provenance_status"] == "ambiguous"
        assert taint_payload["runtime_provenance"]["status"] == "clean"
        assert taint_payload["runtime_provenance"]["evidence"] == []
        assert taint_payload["model_declared_taint"]["status"] == "malformed"


def test_s2_pr03_malformed_taint_metadata_is_treated_as_provenance_ambiguous_for_external_send(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ambiguous outbound": [
                {
                    "capability_id": "cap.framework.external_notify",
                    "input": {
                        "destination": "https://api.framework.local/notify",
                        "message": "ambiguous content",
                    },
                    "influenced_by_untrusted_content": ["not", "a", "bool"],
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ambiguous outbound"},
        )
        assert sent.status_code == 200
        body = sent.json()
        attempt = _surface_attempt(body["turn"])
        assert attempt["policy"]["decision"] == "deny"
        assert attempt["policy"]["reason"] == "taint_denied_untrusted_side_effect"
        assert "evt.action.execution.started" not in _event_types(body["turn"])

        policy_event = next(
            event
            for event in body["turn"]["events"]
            if event["event_type"] == "evt.action.policy_decided"
        )
        assert policy_event["payload"]["taint"]["provenance_status"] == "ambiguous"
        assert policy_event["payload"]["reason"] == "taint_denied_untrusted_side_effect"


def test_s2_pr03_read_behavior_does_not_clear_runtime_taint_for_later_side_effects(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ingest untrusted": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "external quote"},
                }
            ],
            "another read": [
                {
                    "capability_id": "cap.framework.read_echo",
                    "input": {"text": "read still allowed"},
                    "influenced_by_untrusted_content": False,
                }
            ],
            "side effect after reads": [
                {
                    "capability_id": "cap.framework.write_draft",
                    "input": {"note": "should not auto-execute"},
                    "influenced_by_untrusted_content": False,
                }
            ],
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)

        ingest = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ingest untrusted"},
        )
        assert ingest.status_code == 200
        assert _surface_attempt(ingest.json()["turn"])["execution"]["status"] == "succeeded"

        read_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "another read"},
        )
        assert read_turn.status_code == 200
        read_attempt = _surface_attempt(read_turn.json()["turn"])
        assert read_attempt["execution"]["status"] == "succeeded"
        assert read_attempt["policy"]["decision"] == "allow_inline"

        side_effect_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "side effect after reads"},
        )
        assert side_effect_turn.status_code == 200
        body = side_effect_turn.json()
        attempt = _surface_attempt(body["turn"])
        assert attempt["approval"]["status"] == "pending"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["policy"]["reason"] == "taint_escalated_requires_approval"
        assert "evt.action.execution.started" not in _event_types(body["turn"])
