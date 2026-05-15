from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from ariel.app import ModelAdapter, create_app
from ariel.google_connector import GOOGLE_CONNECTOR_ID
from ariel.persistence import GoogleConnectorRecord
from tests.integration.responses_helpers import (
    process_queued_action_execution,
    responses_with_run_calls,
)


GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s4-pr03"
    model: str = "model.s4-pr03-v1"
    run_calls_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
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
        run_calls = copy.deepcopy(self.run_calls_by_message.get(user_message, []))
        current_turn_ref = None
        for item in input_items:
            content = item.get("content")
            if not isinstance(content, str):
                continue
            for line in content.splitlines():
                if line.startswith("- current user instruction: "):
                    current_turn_ref = line.removeprefix("- current user instruction: ").strip()
        for run_call in run_calls:
            input_payload = run_call.get("input")
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
        if any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        if not run_calls:
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        return responses_with_run_calls(
            assistant_text=assistant_text,
            calls=run_calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s4_pr03_123",
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
        del access_token
        attendees = normalized_input.get("attendees", [])
        if attendee_intersection_enabled:
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
                "attendees_considered": attendees,
                "availability_scope": "all_attendees",
                "partial": False,
                "partial_reason": None,
                "timezone": "UTC",
                "source_evidence_refs": [],
                "constraints_used": {},
                "freebusy_diagnostics": [],
                "no_slots_reason": None,
            }
        return {
            "schema_version": "google.calendar.slot_options.v1",
            "slots": [
                {
                    "slot_id": "slot_1",
                    "start": {
                        "value": "2026-03-04T09:30:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "end": {
                        "value": "2026-03-04T10:00:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "availability_scope": "primary_calendar_only",
                    "partial": True,
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
            "window_start": normalized_input["window_start"],
            "window_end": normalized_input["window_end"],
            "duration_minutes": normalized_input["duration_minutes"],
            "attendees_considered": attendees,
            "availability_scope": "primary_calendar_only",
            "partial": True,
            "partial_reason": "attendee_freebusy_scope_missing",
            "timezone": "UTC",
            "source_evidence_refs": [],
            "constraints_used": {},
            "freebusy_diagnostics": [
                {"calendar_id": "attendees", "reason_code": "freebusy_scope_missing"}
            ],
            "no_slots_reason": None,
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
        del access_token, normalized_input
        if "cap.email.draft" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        return {"provider_draft_ref": "gmail://draft/1"}

    def email_send(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        if "cap.email.send" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        return {
            "status": "sent",
            "message_id": "msg_1",
            "provider_message_ref": "gmail://sent/msg_1",
            "to": [],
            "subject": "subject",
        }

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        if "cap.calendar.create_event" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        return {
            "schema_version": "google.calendar.create_result.v1",
            "status": "created",
            "event_id": "evt_1",
            "calendar_id": "primary",
            "title": "event",
            "start_time": "2026-03-04T10:00:00Z",
            "end_time": "2026-03-04T10:30:00Z",
            "provider_event_ref": "calendar://evt_1",
            "etag": "etag_evt_1",
            "updated": "2026-03-03T12:00:00Z",
            "ical_uid": "evt_1@google.com",
            "provider_status": "confirmed",
            "executed_at": "2026-03-03T12:00:01Z",
        }


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _bind_google_fakes(
    client: TestClient,
    *,
    oauth_client: FakeGoogleOAuthClient,
    workspace_provider: FakeGoogleWorkspaceProvider,
) -> None:
    app_state = cast(Any, client.app).state
    app_state.google_oauth_client = oauth_client
    app_state.google_workspace_provider = workspace_provider


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


@pytest.mark.parametrize(
    ("case_name", "connect_code", "refresh_mode", "scope_missing", "expected_failure"),
    [
        ("consent_required", "connect-read-only", "ok", False, "consent_required"),
        ("scope_missing", "connect-compose", "ok", True, "scope_missing"),
        ("access_revoked", "connect-compose-expired", "invalid_grant", False, "access_revoked"),
    ],
)
def test_s4_pr03_blocking_auth_failures_remap_readiness_to_reconnect_required(
    postgres_url: str,
    case_name: str,
    connect_code: str,
    refresh_mode: str,
    scope_missing: bool,
    expected_failure: str,
) -> None:
    del case_name
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "draft follow-up": [
                {
                    "name": "email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                        "idempotency_key": "draft-follow-up-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ]
        }
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
            "connect-compose": FakeTokenBundle(
                account_subject="sub_compose",
                account_email="compose@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_compose",
                refresh_token="tok_refresh_compose",
            ),
            "connect-compose-expired": FakeTokenBundle(
                account_subject="sub_compose_expired",
                account_email="compose-expired@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_compose_expired",
                refresh_token="tok_refresh_compose_expired",
                expires_in_seconds=-5,
            ),
        },
        refresh_mode=refresh_mode,
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        fail_scope_missing_for={"cap.email.draft"} if scope_missing else set()
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code=connect_code)
        session_id = _session_id(client)

        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "draft follow-up"}
        )
        assert sent.status_code == 200
        turn = sent.json()["turn"]
        if expected_failure == "consent_required" and turn["surface_action_lifecycle"] == []:
            assert all(
                event["event_type"] != "evt.action.execution.started" for event in turn["events"]
            )
            connector = client.get("/v1/connectors/google")
            assert connector.status_code == 200
            assert connector.json()["connector"]["readiness"] == "connected"
            return

        attempt = _surface_attempt(turn)
        assert attempt["approval"]["status"] == "pending"
        approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": _approval_ref(turn),
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert approved.status_code == 200
        assert process_queued_action_execution(client, approved.json()) is True

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_failure

        connector = client.get("/v1/connectors/google")
        assert connector.status_code == 200
        connector_payload = connector.json()["connector"]
        assert connector_payload["readiness"] == "reconnect_required"
        assert connector_payload["last_error_code"] == expected_failure


def test_s4_pr03_transient_auth_failures_do_not_remap_connected_readiness(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "search inbox": [{"name": "email.search", "input": {"query": "invoice"}}]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-read-expired": FakeTokenBundle(
                account_subject="sub_read_expired",
                account_email="read-expired@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_read_expired",
                refresh_token="tok_refresh_read_expired",
                expires_in_seconds=-5,
            )
        },
        refresh_mode="transient_failure",
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-read-expired")
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "search inbox"})
        assert sent.status_code == 200
        attempt = _surface_attempt(sent.json()["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "token_expired"

        connector = client.get("/v1/connectors/google")
        assert connector.status_code == 200
        connector_payload = connector.json()["connector"]
        assert connector_payload["status"] == "connected"
        assert connector_payload["readiness"] == "connected"
        assert connector_payload["last_error_code"] == "token_expired"


def test_s4_pr03_reconnect_required_persists_until_successful_reconnect(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "draft follow-up": [
                {
                    "name": "email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                        "idempotency_key": "draft-follow-up-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ],
            "show schedule": [
                {
                    "name": "calendar.list",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                    },
                }
            ],
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-compose": FakeTokenBundle(
                account_subject="sub_sticky",
                account_email="sticky@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_sticky",
                refresh_token="tok_refresh_sticky",
            ),
            "reconnect-compose": FakeTokenBundle(
                account_subject="sub_sticky",
                account_email="sticky@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_sticky_reconnect",
                refresh_token="tok_refresh_sticky_reconnect",
            ),
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider(fail_scope_missing_for={"cap.email.draft"})
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-compose")
        session_id = _session_id(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "draft follow-up"}
        )
        assert first.status_code == 200
        first_turn = first.json()["turn"]
        first_attempt = _surface_attempt(first_turn)
        assert first_attempt["approval"]["status"] == "pending"
        first_approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": _approval_ref(first_turn),
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert first_approved.status_code == 200
        assert process_queued_action_execution(client, first_approved.json()) is True

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        first_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert first_attempt["execution"]["error"] == "scope_missing"

        blocked_status = client.get("/v1/connectors/google")
        assert blocked_status.status_code == 200
        assert blocked_status.json()["connector"]["readiness"] == "reconnect_required"

        successful_read = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "show schedule"},
        )
        assert successful_read.status_code == 200
        successful_attempt = _surface_attempt(successful_read.json()["turn"])
        assert successful_attempt["execution"]["status"] == "succeeded"

        still_blocked_status = client.get("/v1/connectors/google")
        assert still_blocked_status.status_code == 200
        assert still_blocked_status.json()["connector"]["readiness"] == "reconnect_required"

        reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.email.draft"},
        )
        assert reconnect.status_code == 200
        reconnect_state = reconnect.json()["oauth"]["state"]
        reconnect_callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": reconnect_state, "code": "reconnect-compose"},
        )
        assert reconnect_callback.status_code == 200

        healed_status = client.get("/v1/connectors/google")
        assert healed_status.status_code == 200
        connector_payload = healed_status.json()["connector"]
        assert connector_payload["status"] == "connected"
        assert connector_payload["readiness"] == "connected"
        assert connector_payload["last_error_code"] is None


def test_s4_pr03_blocking_readiness_state_is_not_downgraded_by_later_transient_failure(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "draft follow-up": [
                {
                    "name": "email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                        "idempotency_key": "draft-follow-up-1",
                        "user_instruction_ref": "turn:current",
                    },
                }
            ],
            "show schedule": [
                {
                    "name": "calendar.list",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                    },
                }
            ],
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-compose": FakeTokenBundle(
                account_subject="sub_sticky_transient",
                account_email="sticky-transient@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_GMAIL_COMPOSE_SCOPE,
                ],
                access_token="tok_access_sticky_transient",
                refresh_token="tok_refresh_sticky_transient",
            )
        },
        refresh_mode="transient_failure",
    )
    workspace_provider = FakeGoogleWorkspaceProvider(fail_scope_missing_for={"cap.email.draft"})
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-compose")
        session_id = _session_id(client)

        blocking_failure = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "draft follow-up"},
        )
        assert blocking_failure.status_code == 200
        blocking_turn = blocking_failure.json()["turn"]
        blocking_attempt = _surface_attempt(blocking_turn)
        assert blocking_attempt["approval"]["status"] == "pending"
        blocking_approved = client.post(
            "/v1/approvals",
            json={
                "approval_ref": _approval_ref(blocking_turn),
                "decision": "approve",
                "actor_id": "user.local",
            },
        )
        assert blocking_approved.status_code == 200
        assert process_queued_action_execution(client, blocking_approved.json()) is True

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        blocking_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert blocking_attempt["execution"]["error"] == "scope_missing"

        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                connector = db.get(GoogleConnectorRecord, GOOGLE_CONNECTOR_ID)
                assert connector is not None
                connector.access_token_expires_at = datetime.now(tz=UTC) - timedelta(minutes=1)

        transient_failure = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "show schedule"},
        )
        assert transient_failure.status_code == 200
        transient_attempt = _surface_attempt(transient_failure.json()["turn"])
        assert transient_attempt["execution"]["error"] == "token_expired"

        connector = client.get("/v1/connectors/google")
        assert connector.status_code == 200
        connector_payload = connector.json()["connector"]
        assert connector_payload["status"] == "connected"
        assert connector_payload["readiness"] == "reconnect_required"
        assert connector_payload["last_error_code"] == "scope_missing"


def test_s4_pr03_attendee_reconnect_intent_requests_freebusy_and_closes_fallback_path(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "plan team sync": [
                {
                    "name": "calendar.propose_slots",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                        "duration_minutes": 30,
                        "attendees": ["a@example.com", "b@example.com"],
                        "timezone": "UTC",
                        "source_evidence_ids": [],
                        "quoted_content_caveat": False,
                        "participants": ["a@example.com", "b@example.com"],
                        "proposed_windows": [],
                        "timezone_evidence": {
                            "source": None,
                            "rationale": None,
                            "confidence": None,
                        },
                        "constraints": {"hard": [], "soft": [], "attendee_notes": []},
                    },
                }
            ]
        },
        assistant_text_by_message={
            "plan team sync": (
                "Your calendar only is available right now; reconnect calendar free/busy access."
            )
        },
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-no-freebusy": FakeTokenBundle(
                account_subject="sub_slots",
                account_email="slots@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_slots",
                refresh_token="tok_refresh_slots",
            ),
            "reconnect-freebusy": FakeTokenBundle(
                account_subject="sub_slots",
                account_email="slots@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_CALENDAR_FREEBUSY_SCOPE,
                ],
                access_token="tok_access_slots_freebusy",
                refresh_token="tok_refresh_slots_freebusy",
            ),
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-no-freebusy")
        session_id = _session_id(client)

        before_reconnect = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "plan team sync"},
        )
        assert before_reconnect.status_code == 200
        before_payload = before_reconnect.json()
        before_attempt = _surface_attempt(before_payload["turn"])
        assert before_attempt["execution"]["status"] == "succeeded"
        assert before_attempt["execution"]["output"]["partial"] is True
        before_message = before_payload["assistant"]["message"].lower()
        assert "user-calendar-only" in before_message or "your calendar only" in before_message
        assert "reconnect" in before_message

        reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.calendar.propose_slots"},
        )
        assert reconnect.status_code == 200
        requested_scopes = set(reconnect.json()["oauth"]["requested_scopes"])
        assert GOOGLE_CALENDAR_READ_SCOPE in requested_scopes
        assert GOOGLE_GMAIL_READ_SCOPE in requested_scopes
        assert GOOGLE_CALENDAR_FREEBUSY_SCOPE in requested_scopes
        assert GOOGLE_CALENDAR_WRITE_SCOPE not in requested_scopes
        assert GOOGLE_GMAIL_COMPOSE_SCOPE not in requested_scopes
        assert GOOGLE_GMAIL_SEND_SCOPE not in requested_scopes

        reconnect_events = client.get("/v1/connectors/google/events")
        assert reconnect_events.status_code == 200
        reconnect_started = next(
            event
            for event in reconnect_events.json()["events"]
            if event["event_type"] == "evt.connector.google.reconnect.started"
        )
        reconnect_started_payload = reconnect_started["payload"]
        assert reconnect_started_payload["capability_intent"] == "cap.calendar.propose_slots"
        assert GOOGLE_CALENDAR_FREEBUSY_SCOPE in reconnect_started_payload["requested_scopes"]

        reconnect_state = reconnect.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": reconnect_state, "code": "reconnect-freebusy"},
        )
        assert callback.status_code == 200
        adapter.assistant_text_by_message["plan team sync"] = (
            "The selected time works for all attendees."
        )

        after_reconnect = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "plan team sync"},
        )
        assert after_reconnect.status_code == 200
        after_payload = after_reconnect.json()
        after_attempt = _surface_attempt(after_payload["turn"])
        assert after_attempt["execution"]["status"] == "succeeded"
        assert after_attempt["execution"]["output"]["partial"] is False
        after_message = after_payload["assistant"]["message"].lower()
        assert "works for all attendees" in after_message
        assert "user-calendar-only" not in after_message
