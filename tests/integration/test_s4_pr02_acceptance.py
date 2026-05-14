from __future__ import annotations

import copy
import json
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.persistence import ProviderWriteReceiptRecord
from tests.integration.responses_helpers import responses_with_function_calls


GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s4-pr02"
    model: str = "model.s4-pr02-v1"
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
        del tools, history
        if context_bundle.get("origin") == "tool_strategy":
            strategy_input = json.loads(str(input_items[1]["content"]))
            available_ids = {
                capability_id
                for family in strategy_input.get("available_capability_families", [])
                if isinstance(family, dict)
                for capability_id in family.get("capability_ids", [])
                if isinstance(capability_id, str)
            }
            selected_capability_ids = [
                proposal["capability_id"]
                for proposal in self.proposals_by_message.get(user_message, [])
                if proposal.get("capability_id") in available_ids
            ]
            return responses_with_function_calls(
                input_items=input_items,
                assistant_text=json.dumps(
                    {
                        "decision": "selected_tools" if selected_capability_ids else "no_tools",
                        "selected_capability_ids": selected_capability_ids,
                        "rationale": "test strategy",
                        "unavailable_reason": None,
                        "confidence": 1.0,
                    },
                    sort_keys=True,
                ),
                proposals=[],
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_s4_pr02_strategy",
                input_tokens=3,
                output_tokens=2,
            )
        proposals = copy.deepcopy(self.proposals_by_message.get(user_message, []))
        current_turn_ref = None
        for item in input_items:
            content = item.get("content")
            if not isinstance(content, str):
                continue
            for line in content.splitlines():
                if line.startswith("- current user instruction: "):
                    current_turn_ref = line.removeprefix("- current user instruction: ").strip()
        for proposal in proposals:
            input_payload = proposal.get("input")
            if (
                current_turn_ref is not None
                and isinstance(input_payload, dict)
                and input_payload.get("user_instruction_ref") == "turn:current"
            ):
                input_payload["user_instruction_ref"] = current_turn_ref
        assistant_text = self.assistant_text_by_message.get(
            user_message,
            f"assistant::{user_message}",
        )
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text=assistant_text,
            proposals=proposals,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s4_pr02_123",
            input_tokens=31,
            output_tokens=20,
        )


@dataclass(slots=True)
class FakeTokenBundle:
    account_subject: str
    account_email: str
    granted_scopes: list[str]
    access_token: str
    refresh_token: str
    expires_in_seconds: int = 3600


@dataclass
class FakeGoogleOAuthClient:
    tokens_by_code: dict[str, FakeTokenBundle] = field(default_factory=dict)
    refresh_mode: str = "ok"

    def build_authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        scopes: list[str],
        redirect_uri: str,
        prompt_consent: bool,
    ) -> str:
        scope_value = "+".join(sorted(scopes))
        prompt = "consent" if prompt_consent else "none"
        return (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id=test-client"
            f"&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&scope={scope_value}"
            f"&state={state}"
            f"&code_challenge={code_challenge}"
            f"&code_challenge_method=S256"
            f"&prompt={prompt}"
        )

    def exchange_code_for_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        state: str,
    ) -> dict[str, Any]:
        assert isinstance(code_verifier, str)
        assert len(code_verifier) >= 43
        assert redirect_uri
        assert state
        token_bundle = self.tokens_by_code.get(code)
        if token_bundle is None:
            msg = f"unexpected_code:{code}"
            raise RuntimeError(msg)
        return {
            "account_subject": token_bundle.account_subject,
            "account_email": token_bundle.account_email,
            "granted_scopes": list(token_bundle.granted_scopes),
            "access_token": token_bundle.access_token,
            "refresh_token": token_bundle.refresh_token,
            "expires_in_seconds": token_bundle.expires_in_seconds,
        }

    def refresh_access_token(self, *, refresh_token: str) -> dict[str, Any]:
        if self.refresh_mode == "invalid_grant":
            raise RuntimeError("invalid_grant")
        if self.refresh_mode == "transient_failure":
            raise RuntimeError("upstream_timeout")
        return {
            "access_token": f"refreshed::{refresh_token}",
            "refresh_token": refresh_token,
            "expires_in_seconds": 3600,
        }

    def revoke_token(self, *, token: str) -> None:
        del token


@dataclass
class FakeGoogleWorkspaceProvider:
    fail_scope_missing_for: set[str] = field(default_factory=set)
    calendar_create_calls: list[dict[str, Any]] = field(default_factory=list)
    email_draft_calls: list[dict[str, Any]] = field(default_factory=list)
    email_send_calls: list[dict[str, Any]] = field(default_factory=list)

    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token
        return {
            "schema_version": "google.calendar.events.v1",
            "events": [
                {
                    "provider_account_id": "google",
                    "calendar_id": "primary",
                    "event_id": "evt-team-sync",
                    "status": "confirmed",
                    "summary": "team sync",
                    "description_blocks": [],
                    "attendees": [],
                    "start": {
                        "value": "2026-03-04T10:00:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "end": {
                        "value": "2026-03-04T10:30:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "all_day": False,
                    "recurrence": [],
                    "updated": "2026-03-03T09:00:00Z",
                    "provider_url": "https://calendar.google.com/event?eid=evt-team-sync",
                    "raw_payload_digest": "c" * 64,
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
            "window_start": normalized_input["window_start"],
            "window_end": normalized_input["window_end"],
        }

    def calendar_propose_slots(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        del access_token, attendee_intersection_enabled
        return {
            "schema_version": "google.calendar.slot_options.v1",
            "slots": [
                {
                    "slot_id": "slot_1",
                    "start": {
                        "value": "2026-03-04T10:30:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "end": {
                        "value": "2026-03-04T11:00:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "availability_scope": "all_attendees",
                    "partial": False,
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
            "window_start": normalized_input["window_start"],
            "window_end": normalized_input["window_end"],
            "duration_minutes": normalized_input["duration_minutes"],
            "attendees_considered": normalized_input.get("attendees", []),
            "availability_scope": "all_attendees",
            "partial": False,
            "partial_reason": None,
            "timezone": "UTC",
            "source_evidence_refs": [],
            "constraints_used": {},
            "freebusy_diagnostics": [],
            "no_slots_reason": None,
        }

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        if "cap.calendar.create_event" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        self.calendar_create_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        return {
            "schema_version": "google.calendar.create_result.v1",
            "status": "created",
            "event_id": f"evt_{len(self.calendar_create_calls)}",
            "calendar_id": normalized_input.get("calendar_id", "primary"),
            "title": normalized_input["title"],
            "start_time": normalized_input["start_time"],
            "end_time": normalized_input["end_time"],
            "provider_event_ref": f"calendar://evt_{len(self.calendar_create_calls)}",
            "etag": f"etag_{len(self.calendar_create_calls)}",
            "updated": "2026-03-03T12:00:00Z",
            "ical_uid": f"ical_{len(self.calendar_create_calls)}@google.com",
            "provider_status": "confirmed",
            "executed_at": "2026-03-03T12:00:01Z",
        }

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {
            "schema_version": "google.gmail.message_refs.v1",
            "messages": [],
            "retrieved_at": "2026-03-03T12:00:00Z",
        }

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {
            "schema_version": "google.gmail.message_evidence.v1",
            "message": {"message_id": "msg-1"},
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg-1",
                "body_digest": "f" * 64,
                "blocks": [
                    {
                        "block_id": "gmail:msg-1:body:0",
                        "kind": "body",
                        "text": "message evidence",
                        "digest": "a" * 64,
                    }
                ],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
        }

    def email_create_draft(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        if "cap.email.draft" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        self.email_draft_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        return {
            "provider_draft_ref": f"gmail-draft-{len(self.email_draft_calls)}",
        }

    def email_send(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        if "cap.email.send" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        self.email_send_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        return {
            "status": "sent",
            "message_id": f"msg_sent_{len(self.email_send_calls)}",
            "provider_message_ref": f"gmail://sent/{len(self.email_send_calls)}",
            "to": normalized_input["to"],
            "subject": normalized_input["subject"],
        }


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
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    return item


def _approval_ref(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> str:
    attempt = _surface_attempt(turn_payload, proposal_index=proposal_index)
    approval = attempt.get("approval")
    assert isinstance(approval, dict)
    ref = approval.get("reference")
    assert isinstance(ref, str)
    return ref


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _bind_google_fakes(
    client: TestClient,
    *,
    oauth_client: FakeGoogleOAuthClient,
    workspace_provider: FakeGoogleWorkspaceProvider,
) -> None:
    app_state = cast(Any, client.app).state
    app_state.google_oauth_client = oauth_client
    app_state.google_workspace_provider = workspace_provider


def _connect_google(client: TestClient, *, code: str) -> dict[str, Any]:
    started = client.post("/v1/connectors/google/start")
    assert started.status_code == 200
    state = started.json()["oauth"]["state"]
    callback = client.get(
        "/v1/connectors/google/callback",
        params={"state": state, "code": code},
    )
    assert callback.status_code == 200
    return callback.json()


def test_s4_pr02_write_scope_remediation_reconnect_is_capability_intent_driven_and_least_privilege(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "create kickoff event": [
                {
                    "capability_id": "cap.calendar.create_event",
                    "input": {
                        "title": "kickoff",
                        "start_time": "2026-03-04T10:00:00Z",
                        "end_time": "2026-03-04T10:30:00Z",
                        "idempotency_key": "calendar-kickoff-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ]
        },
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-read-only": FakeTokenBundle(
                account_subject="sub_pr02",
                account_email="pr02@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_read_only",
                refresh_token="tok_refresh_read_only",
            ),
            "reconnect-calendar-write": FakeTokenBundle(
                account_subject="sub_pr02",
                account_email="pr02@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_CALENDAR_WRITE_SCOPE,
                ],
                access_token="tok_access_calendar_write",
                refresh_token="tok_refresh_calendar_write",
            ),
        },
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-read-only")

        session_id = _session_id(client)
        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "create kickoff event"},
        )
        assert first.status_code == 200
        first_turn = first.json()["turn"]
        assert first_turn["surface_action_lifecycle"] == []
        assert "evt.action.execution.started" not in _event_types(first_turn)

        reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.calendar.create_event"},
        )
        assert reconnect.status_code == 200
        requested_scopes = set(reconnect.json()["oauth"]["requested_scopes"])
        assert GOOGLE_CALENDAR_READ_SCOPE in requested_scopes
        assert GOOGLE_GMAIL_READ_SCOPE in requested_scopes
        assert GOOGLE_CALENDAR_WRITE_SCOPE in requested_scopes
        assert GOOGLE_GMAIL_COMPOSE_SCOPE not in requested_scopes
        assert GOOGLE_GMAIL_SEND_SCOPE not in requested_scopes

        reconnect_state = reconnect.json()["oauth"]["state"]
        reconnect_callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": reconnect_state, "code": "reconnect-calendar-write"},
        )
        assert reconnect_callback.status_code == 200

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "create kickoff event"},
        )
        assert second.status_code == 200
        second_turn = second.json()["turn"]
        second_approval_ref = _approval_ref(second_turn)
        approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": second_approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert approved.status_code == 200

        timeline_after_success = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline_after_success.status_code == 200
        succeeded_attempt = _surface_attempt(timeline_after_success.json()["turns"][-1])
        assert succeeded_attempt["execution"]["status"] == "succeeded"
        assert succeeded_attempt["execution"]["output"]["status"] == "created"


def test_s4_pr02_calendar_create_requires_approval_and_executes_exactly_once(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "create launch review": [
                {
                    "capability_id": "cap.calendar.create_event",
                    "input": {
                        "title": "launch review",
                        "start_time": "2026-03-04T15:00:00Z",
                        "end_time": "2026-03-04T15:30:00Z",
                        "attendees": ["team@example.com"],
                        "idempotency_key": "calendar-launch-review-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ]
        },
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-calendar-write": FakeTokenBundle(
                account_subject="sub_create",
                account_email="create@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_CALENDAR_WRITE_SCOPE,
                ],
                access_token="tok_access_create",
                refresh_token="tok_refresh_create",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-calendar-write")
        session_id = _session_id(client)

        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "create launch review"},
        )
        assert sent.status_code == 200
        turn = sent.json()["turn"]
        attempt = _surface_attempt(turn)
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["approval"]["status"] == "pending"
        assert "evt.action.execution.started" not in _event_types(turn)

        approval_ref = _approval_ref(turn)
        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200

        replay = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["execution"]["status"] == "succeeded"
        assert latest_attempt["execution"]["output"]["status"] == "created"

        event_types = _event_types(latest_turn)
        assert event_types.count("evt.action.execution.started") == 1
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert event_types.index("evt.action.approval.approved") < event_types.index(
            "evt.action.execution.started"
        )
        assert len(workspace_provider.calendar_create_calls) == 1
        with cast(Any, client.app).state.session_factory() as db:
            receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
            assert receipt is not None
            assert receipt.capability_id == "cap.calendar.create_event"
            assert receipt.status == "succeeded"
            assert "act_" not in receipt.idempotency_key
            assert receipt.provider_object_ids["event_id"] == "evt_1"
            assert receipt.provider_object_ids["calendar_id"] == "primary"
            assert receipt.provider_object_ids["etag"] == "etag_1"
            assert receipt.response_payload["schema_version"] == "google.calendar.create_result.v1"
            assert receipt.response_payload["updated"] == "2026-03-03T12:00:00Z"


def test_s4_pr02_email_draft_queues_then_executes_as_draft_only_without_send_side_effect(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "draft follow-up": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["Teammate@Example.com"],
                        "subject": "Follow up",
                        "body": "Can we sync tomorrow at 10am?",
                        "idempotency_key": "draft-follow-up-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ]
        },
        assistant_text_by_message={
            "draft follow-up": "Approval is required before I create that draft.",
        },
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-compose": FakeTokenBundle(
                account_subject="sub_draft",
                account_email="draft@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_draft",
                refresh_token="tok_refresh_draft",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-compose")
        session_id = _session_id(client)

        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "draft follow-up"}
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.email.draft"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["approval"]["status"] == "pending"
        assert attempt["execution"]["status"] == "not_executed"
        assert attempt["execution"]["output"] is None
        assert len(workspace_provider.email_draft_calls) == 0
        assert len(workspace_provider.email_send_calls) == 0

        approval_ref = _approval_ref(payload["turn"])
        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert isinstance(output, dict)
        assert output["status"] == "drafted_not_sent"
        assert output["delivery_state"] == "draft_only"
        assert output["sent"] is False
        assert output["draft"]["to"] == ["teammate@example.com"]
        assert output["draft"]["subject"] == "Follow up"
        assert output["draft"]["body_redacted"]["redacted"] is True

        assistant_message = payload["assistant"]["message"].lower()
        assert "approval" in assistant_message
        assert len(workspace_provider.email_draft_calls) == 1
        assert len(workspace_provider.email_send_calls) == 0


def test_s4_pr02_user_instruction_authority_requires_real_turn_id(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "draft with slug authority": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                        "idempotency_key": "draft-slug-authority-1",
                        "user_instruction_ref": "turn:draft-with-slug-authority",
                    },
                }
            ]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-compose": FakeTokenBundle(
                account_subject="sub_slug_ref",
                account_email="slug-ref@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_slug_ref",
                refresh_token="tok_refresh_slug_ref",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-compose")
        session_id = _session_id(client)

        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "draft with slug authority"},
        )
        assert sent.status_code == 200
        approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": _approval_ref(sent.json()["turn"]),
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert approved.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "provider_user_instruction_not_found"
        assert len(workspace_provider.email_draft_calls) == 0


def test_s4_pr02_email_send_requires_approval_and_executes_exactly_once(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "send follow-up": [
                {
                    "capability_id": "cap.email.send",
                    "input": {
                        "to": ["client@example.com"],
                        "subject": "Status update",
                        "body": "All milestones are on track.",
                        "idempotency_key": "send-follow-up-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-send": FakeTokenBundle(
                account_subject="sub_send",
                account_email="send@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_SEND_SCOPE,
                ],
                access_token="tok_access_send",
                refresh_token="tok_refresh_send",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-send")
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "send follow-up"})
        assert sent.status_code == 200
        turn = sent.json()["turn"]
        attempt = _surface_attempt(turn)
        assert attempt["proposal"]["capability_id"] == "cap.email.send"
        assert attempt["policy"]["decision"] == "requires_approval"
        assert attempt["approval"]["status"] == "pending"
        assert "evt.action.execution.started" not in _event_types(turn)

        approval_ref = _approval_ref(turn)
        approved = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert approved.status_code == 200

        replay = client.post(
            "/v1/approvals",
            json={"approval_ref": approval_ref, "decision": "approve", "actor_id": "user.local"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["execution"]["status"] == "succeeded"
        assert latest_attempt["execution"]["output"]["status"] == "sent"
        assert latest_attempt["execution"]["output"]["message_id"].startswith("msg_sent_")

        event_types = _event_types(latest_turn)
        assert event_types.count("evt.action.execution.started") == 1
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert len(workspace_provider.email_send_calls) == 1


def test_s4_pr02_draft_and_send_are_distinct_lifecycle_units_with_independent_histories(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "draft note": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["client@example.com"],
                        "subject": "Proposal draft",
                        "body": "Draft body.",
                        "idempotency_key": "draft-note-1",
                        "user_instruction_ref": "turn:current",
                    },
                    "influenced_by_untrusted_content": False,
                }
            ],
            "send note": [
                {
                    "capability_id": "cap.email.send",
                    "input": {
                        "to": ["client@example.com"],
                        "subject": "Proposal draft",
                        "body": "Draft body.",
                        "idempotency_key": "send-note-1",
                        "user_instruction_ref": "turn:current",
                    },
                    "influenced_by_untrusted_content": False,
                }
            ],
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-compose-send": FakeTokenBundle(
                account_subject="sub_draft_send",
                account_email="draft-send@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                    GOOGLE_GMAIL_SEND_SCOPE,
                ],
                access_token="tok_access_draft_send",
                refresh_token="tok_refresh_draft_send",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-compose-send")
        session_id = _session_id(client)

        drafted = client.post(f"/v1/sessions/{session_id}/message", json={"message": "draft note"})
        assert drafted.status_code == 200
        draft_turn = drafted.json()["turn"]
        draft_attempt = _surface_attempt(draft_turn)
        assert draft_attempt["proposal"]["capability_id"] == "cap.email.draft"
        assert draft_attempt["approval"]["status"] == "pending"
        draft_approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": _approval_ref(draft_turn),
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert draft_approved.status_code == 200

        send_proposed = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "send note"}
        )
        assert send_proposed.status_code == 200
        send_turn = send_proposed.json()["turn"]
        send_attempt_pending = _surface_attempt(send_turn)
        assert send_attempt_pending["proposal"]["capability_id"] == "cap.email.send"
        assert send_attempt_pending["approval"]["status"] == "pending"
        send_approval_ref = _approval_ref(send_turn)
        send_approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": send_approval_ref,
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert send_approved.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        draft_turn = next(turn for turn in turns if turn["user_message"] == "draft note")
        send_turn_final = next(turn for turn in turns if turn["user_message"] == "send note")

        draft_attempt_final = _surface_attempt(draft_turn)
        send_attempt_final = _surface_attempt(send_turn_final)

        assert draft_attempt_final["action_attempt_id"] != send_attempt_final["action_attempt_id"]
        assert draft_attempt_final["approval"]["status"] == "approved"
        assert send_attempt_final["approval"]["status"] == "approved"
        assert draft_attempt_final["execution"]["status"] == "succeeded"
        assert send_attempt_final["execution"]["status"] == "succeeded"


@pytest.mark.parametrize(
    (
        "case_name",
        "capability_id",
        "connect_code",
        "refresh_mode",
        "fail_scope_missing_for",
        "expected_class",
        "requires_approval",
    ),
    [
        (
            "not_connected_send",
            "cap.email.send",
            None,
            "ok",
            None,
            "not_connected",
            False,
        ),
        (
            "scope_missing_send",
            "cap.email.send",
            "connect-send",
            "ok",
            "cap.email.send",
            "scope_missing",
            True,
        ),
        (
            "token_expired_send",
            "cap.email.send",
            "connect-send-expired",
            "transient_failure",
            None,
            "token_expired",
            True,
        ),
        (
            "access_revoked_send",
            "cap.email.send",
            "connect-send-expired",
            "invalid_grant",
            None,
            "access_revoked",
            True,
        ),
    ],
)
def test_s4_pr02_write_paths_return_typed_auth_failures_with_recovery_guidance(
    postgres_url: str,
    case_name: str,
    capability_id: str,
    connect_code: str | None,
    refresh_mode: str,
    fail_scope_missing_for: str | None,
    expected_class: str,
    requires_approval: bool,
) -> None:
    del case_name
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "perform write": [
                {
                    "capability_id": capability_id,
                    "input": (
                        {
                            "title": "Risk review",
                            "start_time": "2026-03-05T10:00:00Z",
                            "end_time": "2026-03-05T10:30:00Z",
                            "idempotency_key": "calendar-risk-review-1",
                            "user_instruction_ref": "turn:current",
                        }
                        if capability_id == "cap.calendar.create_event"
                        else {
                            "to": ["ops@example.com"],
                            "subject": "status",
                            "body": "hello",
                            "idempotency_key": "email-write-1",
                            "user_instruction_ref": "turn:current",
                        }
                    ),
                }
            ]
        },
        assistant_text_by_message={
            "perform write": f"{expected_class}: connect, reconnect, then retry.",
        },
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-read-only": FakeTokenBundle(
                account_subject="sub_read_only",
                account_email="read-only@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_read_only",
                refresh_token="tok_refresh_read_only",
            ),
            "connect-send": FakeTokenBundle(
                account_subject="sub_send_scope",
                account_email="send-scope@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_SEND_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_send_scope",
                refresh_token="tok_refresh_send_scope",
            ),
            "connect-send-expired": FakeTokenBundle(
                account_subject="sub_send_expired",
                account_email="send-expired@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_SEND_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_send_expired",
                refresh_token="tok_refresh_send_expired",
                expires_in_seconds=-5,
            ),
        },
        refresh_mode=refresh_mode,
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        fail_scope_missing_for={fail_scope_missing_for} if fail_scope_missing_for else set()
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        if connect_code is not None:
            _connect_google(client, code=connect_code)

        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "perform write"})
        assert sent.status_code == 200

        if requires_approval:
            approval_ref = _approval_ref(sent.json()["turn"])
            decided = client.post(
                "/v1/approvals",
                json={
                    "approval_ref": approval_ref,
                    "decision": "approve",
                    "actor_id": "user.local",
                },
            )
            assert decided.status_code == 200
            rendered_message = decided.json()["assistant"]["message"].lower()
        else:
            rendered_message = sent.json()["assistant"]["message"].lower()

        assert expected_class in rendered_message
        if expected_class == "not_connected":
            assert "connect" in rendered_message
        if expected_class in {"consent_required", "scope_missing", "access_revoked"}:
            assert "reconnect" in rendered_message
        if expected_class == "token_expired":
            assert "retry" in rendered_message
            assert "reconnect" in rendered_message

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        latest_turn = timeline.json()["turns"][-1]
        if expected_class == "not_connected":
            assert latest_turn["surface_action_lifecycle"] == []
            assert all(
                event["event_type"] != "evt.action.execution.failed"
                for event in latest_turn["events"]
            )
            return

        latest_attempt = _surface_attempt(latest_turn)
        assert latest_attempt["execution"]["status"] == "failed"
        assert latest_attempt["execution"]["error"] == expected_class
