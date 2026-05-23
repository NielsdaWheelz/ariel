from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from ariel.app import build_google_runtime
from ariel.clock import utcnow
from ariel.model_adapter import ModelAdapter
from tests.integration.app_helpers import create_test_app
from ariel.google_connector import (
    GOOGLE_CONNECTOR_ID,
    GoogleOAuthExchangeFailure,
    GoogleOAuthRefreshFailure,
    GoogleOAuthRevokeFailure,
    GoogleProviderRequestFailure,
    GoogleWorkspaceProvider,
)
from ariel.secret_cipher import SecretDecryptionFailure
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_message,
    responses_with_run_calls,
)
from tests.fake_sandbox import FakeSandboxRuntime


GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_OPENID_SCOPE = "openid"
GOOGLE_USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_USERINFO_PROFILE_SCOPE = "https://www.googleapis.com/auth/userinfo.profile"


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.google-read"
    model: str = "model.google-read-v1"
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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, history
        if context_bundle.get("origin") == "tool_result_interpretation":
            interpreter_input = context_bundle.get("tool_result_interpreter_input")
            if not isinstance(interpreter_input, dict):
                interpreter_input = {}
            audited_outputs = interpreter_input.get("audited_tool_outputs")
            selected_output_refs = []
            if isinstance(audited_outputs, list):
                selected_output_refs = [
                    output["output_ref"]
                    for output in audited_outputs
                    if isinstance(output, dict) and isinstance(output.get("output_ref"), str)
                ]
            return responses_message(
                assistant_text=json.dumps(
                    {
                        "findings": ["workspace evidence inspected"],
                        "contradictions": [],
                        "uncertainty": [],
                        "selected_output_refs": selected_output_refs,
                        "omitted_output_refs": [],
                        "citation_refs": interpreter_input.get("citation_refs", []),
                        "artifact_refs": interpreter_input.get("artifact_refs", []),
                        "recommended_next_evidence": [],
                        "confidence": 0.9,
                    },
                    sort_keys=True,
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_google_read_interpreter",
                input_tokens=31,
                output_tokens=20,
            )
        run_calls = copy.deepcopy(self.run_calls_by_message.get(user_message, []))
        assistant_text = self.assistant_text_by_message.get(
            user_message,
            {
                "show schedule": "schedule: team sync and design review.",
                "propose slots": "availability: two slots are available.",
                "search inbox": "invoice #44 appears in the inbox.",
                "open inbox item": "email msg-1 is available for review.",
                "plan team sync": "attendee availability is limited to user-calendar-only; reconnect to include attendee calendars.",
            }.get(user_message, f"assistant::{user_message}"),
        )
        if any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        if not run_calls:
            run_calls = [{"name": "agent.emit_message", "input": {"text": assistant_text}}]
        return responses_with_run_calls(
            calls=run_calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_google_read_123",
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
    exchange_errors_by_code: dict[str, Exception] = field(default_factory=dict)
    refresh_mode: str = "ok"
    revoke_errors_by_token: dict[str, Exception] = field(default_factory=dict)
    revoke_calls: list[str] = field(default_factory=list)
    exchanged_states: list[str] = field(default_factory=list)

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
        self.exchanged_states.append(state)
        exchange_error = self.exchange_errors_by_code.get(code)
        if exchange_error is not None:
            raise exchange_error
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
            raise GoogleOAuthRefreshFailure(code="access_revoked")
        if self.refresh_mode == "transient_failure":
            raise GoogleOAuthRefreshFailure(code="provider_timeout")
        return {
            "access_token": f"refreshed::{refresh_token}",
            "refresh_token": refresh_token,
            "expires_in_seconds": 3600,
        }

    def revoke_token(self, *, token: str) -> None:
        self.revoke_calls.append(token)
        revoke_error = self.revoke_errors_by_token.get(token)
        if revoke_error is not None:
            raise revoke_error


@dataclass
class FakeGoogleWorkspaceProvider:
    fail_scope_missing_for: set[str] = field(default_factory=set)
    fail_token_expired_for: set[str] = field(default_factory=set)

    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        provider_account_id: str,
    ) -> dict[str, Any]:
        del access_token
        if "cap.calendar.list" in self.fail_scope_missing_for:
            raise GoogleProviderRequestFailure("insufficient_permissions")
        return {
            "schema_version": "google.calendar.events.v1",
            "status": "succeeded",
            "events": [
                {
                    "provider_account_id": provider_account_id,
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
                },
                {
                    "provider_account_id": provider_account_id,
                    "calendar_id": "primary",
                    "event_id": "evt-design-review",
                    "status": "confirmed",
                    "summary": "design review",
                    "description_blocks": [],
                    "attendees": [],
                    "start": {
                        "value": "2026-03-04T15:00:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "end": {
                        "value": "2026-03-04T15:45:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "all_day": False,
                    "recurrence": [],
                    "updated": "2026-03-03T09:00:00Z",
                    "provider_url": "https://calendar.google.com/event?eid=evt-design-review",
                    "raw_payload_digest": "d" * 64,
                },
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
        provider_account_id: str,
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        del access_token
        if "cap.calendar.propose_slots" in self.fail_scope_missing_for:
            raise GoogleProviderRequestFailure("insufficient_permissions")
        attendees = normalized_input.get("attendees", [])
        if attendee_intersection_enabled:
            return {
                "schema_version": "google.calendar.slot_options.v1",
                "provider_account_id": provider_account_id,
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
                    },
                    {
                        "slot_id": "slot_2",
                        "start": {
                            "value": "2026-03-04T14:00:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "end": {
                            "value": "2026-03-04T14:30:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "availability_scope": "all_attendees",
                        "partial": False,
                    },
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
            "provider_account_id": provider_account_id,
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
                },
                {
                    "slot_id": "slot_2",
                    "start": {
                        "value": "2026-03-04T16:00:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "end": {
                        "value": "2026-03-04T16:30:00Z",
                        "timezone": "UTC",
                        "all_day": False,
                    },
                    "availability_scope": "primary_calendar_only",
                    "partial": True,
                },
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
        provider_account_id: str,
    ) -> dict[str, Any]:
        del access_token
        if "cap.email.search" in self.fail_scope_missing_for:
            raise GoogleProviderRequestFailure("insufficient_permissions")
        if "cap.email.search" in self.fail_token_expired_for:
            raise GoogleProviderRequestFailure("token_expired")
        query = normalized_input["query"]
        return {
            "schema_version": "google.gmail.message_refs.v1",
            "status": "succeeded",
            "messages": [
                {
                    "provider_account_id": provider_account_id,
                    "message_id": "msg-1",
                    "thread_id": "thr-1",
                    "history_id": "hist-1",
                    "subject": "invoice from acme",
                    "subject_key": "invoice from acme",
                    "sender": {
                        "raw": "Acme Billing <billing@acme.test>",
                        "name": "Acme Billing",
                        "email": "billing@acme.test",
                    },
                    "recipients": [],
                    "header_date": "2026-03-02T09:00:00Z",
                    "internal_date": "2026-03-02T09:00:00Z",
                    "label_ids": ["INBOX"],
                    "direction": "received",
                    "preview": f"subject: invoice #44 matches query `{query}`",
                    "provider_url": "https://mail.google.com/mail/u/0/#all/msg-1",
                    "evidence_status": "needs_read",
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
            "total_estimate": 1,
        }

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        provider_account_id: str,
    ) -> dict[str, Any]:
        del access_token
        if "cap.email.read" in self.fail_scope_missing_for:
            raise GoogleProviderRequestFailure("insufficient_permissions")
        message_id = normalized_input["message_id"]
        return {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {
                "provider_account_id": provider_account_id,
                "message_id": message_id,
                "thread_id": "thr-1",
                "history_id": "hist-1",
                "subject": f"email {message_id}",
                "subject_key": f"email {message_id}",
                "sender": {
                    "raw": "Acme Billing <billing@acme.test>",
                    "name": "Acme Billing",
                    "email": "billing@acme.test",
                },
                "recipients": [],
                "direction": "received",
                "labels": ["INBOX"],
                "attachments": [],
                "provider_url": f"https://mail.google.com/mail/u/0/#all/{message_id}",
                "raw_payload_digest": "e" * 64,
            },
            "published_at": "2026-03-02T09:00:00Z",
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": message_id,
                "thread_id": "thr-1",
                "body_digest": "f" * 64,
                "blocks": [
                    {
                        "block_id": f"gmail:{message_id}:body:0",
                        "kind": "body",
                        "text": "body preview: payment confirmed for invoice #44",
                        "digest": "a" * 64,
                        "truncated": False,
                        "source_mime_type": "text/plain",
                        "charset": "utf-8",
                    }
                ],
                "truncated": False,
                "decode_notes": [],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
            "status": "succeeded",
        }


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _turn_data(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    turns = resp.json()["turns"]
    assert turns, "no turns in timeline"
    return turns[-1]


def _surface_attempt(turn_data: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_data.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    return item


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


def test_google_connector_start_reports_typed_oauth_misconfiguration(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 503
        error = started.json()["error"]
        assert error["code"] == "E_CONNECTOR_START_FAILED"
        assert error["details"]["reason"] == "oauth_client_not_configured"
        assert error["retryable"] is False

        with cast(Any, client.app).state.session_factory() as db:
            connector = (
                db.execute(
                    text("SELECT status, last_error_code FROM google_connectors WHERE id = :id"),
                    {"id": GOOGLE_CONNECTOR_ID},
                )
                .mappings()
                .one()
            )
            event_types = [
                row[0]
                for row in db.execute(
                    text(
                        "SELECT event_type FROM google_connector_events "
                        "WHERE connector_id = :id ORDER BY created_at ASC"
                    ),
                    {"id": GOOGLE_CONNECTOR_ID},
                ).all()
            ]
    assert connector["status"] == "not_connected"
    assert connector["last_error_code"] == "oauth_start_failed"
    assert "evt.connector.google.connect.started" in event_types
    assert "evt.connector.google.connect.failed" in event_types


def test_google_connector_start_does_not_map_unexpected_authorization_exception(
    postgres_url: str,
) -> None:
    class DefectiveOAuthClient:
        def build_authorization_url(self, **_: Any) -> str:
            raise RuntimeError("authorization url defect")

    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        cast(Any, client.app).state.google_oauth_client = DefectiveOAuthClient()

        with pytest.raises(RuntimeError, match="authorization url defect"):
            client.post("/v1/connectors/google/start")

        with cast(Any, client.app).state.session_factory() as db:
            connector_count = db.scalar(
                text("SELECT COUNT(*) FROM google_connectors WHERE id = :id"),
                {"id": GOOGLE_CONNECTOR_ID},
            )
            event_count = db.scalar(
                text("SELECT COUNT(*) FROM google_connector_events WHERE connector_id = :id"),
                {"id": GOOGLE_CONNECTOR_ID},
            )
    assert connector_count == 0
    assert event_count == 0


def test_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                ],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            ),
            "reconnect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_CALENDAR_FREEBUSY_SCOPE,
                ],
                access_token="tok_access_plain_reconnect",
                refresh_token="tok_refresh_plain_reconnect",
            ),
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        initial_status = client.get("/v1/connectors/google")
        assert initial_status.status_code == 200
        initial_connector = initial_status.json()["connector"]
        assert initial_connector["readiness"] == "not_connected"
        assert initial_connector["status"] == "not_connected"

        start = client.post("/v1/connectors/google/start")
        assert start.status_code == 200
        start_payload = start.json()
        assert start_payload["ok"] is True
        auth_url = start_payload["oauth"]["authorization_url"]
        state = start_payload["oauth"]["state"]
        assert "code_challenge_method=S256" in auth_url
        assert f"state={state}" in auth_url
        assert GOOGLE_CALENDAR_READ_SCOPE in auth_url
        assert GOOGLE_GMAIL_READ_SCOPE in auth_url
        assert GOOGLE_OPENID_SCOPE in auth_url
        assert GOOGLE_USERINFO_EMAIL_SCOPE in auth_url
        assert GOOGLE_USERINFO_PROFILE_SCOPE in auth_url
        assert "calendar.events" not in auth_url
        assert "gmail.send" not in auth_url

        invalid_callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": "st_invalid", "code": "connect-code"},
        )
        assert invalid_callback.status_code == 400
        invalid_error = invalid_callback.json()
        assert invalid_error["ok"] is False
        assert invalid_error["error"]["code"] == "E_CONNECTOR_CALLBACK_INVALID"

        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-code"},
        )
        assert callback.status_code == 200
        callback_connector = callback.json()["connector"]
        assert callback_connector["provider"] == "google"
        assert callback_connector["status"] == "connected"
        assert callback_connector["readiness"] == "connected"
        assert callback_connector["account_subject"] == "sub_connect"
        assert callback_connector["account_email"] == "owner@example.com"
        assert "access_token_enc" not in callback.text
        assert "refresh_token_enc" not in callback.text
        assert "tok_access_plain_connect" not in callback.text
        assert "tok_refresh_plain_connect" not in callback.text

        replay = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-code"},
        )
        assert replay.status_code == 400
        replay_error = replay.json()
        assert replay_error["ok"] is False
        assert replay_error["error"]["code"] == "E_CONNECTOR_CALLBACK_INVALID"

        reconnect = client.post("/v1/connectors/google/reconnect")
        assert reconnect.status_code == 200
        reconnect_state = reconnect.json()["oauth"]["state"]
        reconnect_callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": reconnect_state, "code": "reconnect-code"},
        )
        assert reconnect_callback.status_code == 200
        reconnect_connector = reconnect_callback.json()["connector"]
        assert reconnect_connector["readiness"] == "connected"
        assert GOOGLE_CALENDAR_FREEBUSY_SCOPE in reconnect_connector["granted_scopes"]

        events = client.get("/v1/connectors/google/events")
        assert events.status_code == 200
        event_types = [event["event_type"] for event in events.json()["events"]]
        assert "evt.connector.google.connect.started" in event_types
        assert "evt.connector.google.connect.succeeded" in event_types
        assert "evt.connector.google.connect.failed" in event_types
        assert "evt.connector.google.reconnect.started" in event_types
        assert "evt.connector.google.reconnect.succeeded" in event_types
        assert "evt.connector.google.disconnected" not in event_types
        assert "tok_access_plain_connect" not in events.text
        assert "tok_refresh_plain_connect" not in events.text
        assert "tok_access_plain_reconnect" not in events.text
        assert "tok_refresh_plain_reconnect" not in events.text

        disconnected = client.delete("/v1/connectors/google")
        assert disconnected.status_code == 200
        disconnected_connector = disconnected.json()["connector"]
        assert disconnected_connector["readiness"] == "not_connected"
        assert disconnected_connector["status"] in {"revoked", "not_connected"}

        events_after_disconnect = client.get("/v1/connectors/google/events")
        events_after_disconnect_payload = events_after_disconnect.json()["events"]
        event_types_after_disconnect = [
            event["event_type"] for event in events_after_disconnect_payload
        ]
        assert "evt.connector.google.disconnected" in event_types_after_disconnect
        assert oauth_client.revoke_calls == [
            "tok_refresh_plain_reconnect",
            "tok_access_plain_reconnect",
        ]
        disconnected_event = [
            event
            for event in events_after_disconnect_payload
            if event["event_type"] == "evt.connector.google.disconnected"
        ][-1]
        assert disconnected_event["payload"]["token_revoke_results"] == [
            {"slot": "refresh_token", "status": "succeeded", "stage": "revoke"},
            {"slot": "access_token", "status": "succeeded", "stage": "revoke"},
        ]
        assert "revoked_remote" not in disconnected_event["payload"]
        assert "tok_access_plain_reconnect" not in events_after_disconnect.text
        assert "tok_refresh_plain_reconnect" not in events_after_disconnect.text
        with cast(Any, client.app).state.session_factory() as db:
            tokens = (
                db.execute(
                    text(
                        "SELECT access_token_enc, refresh_token_enc FROM google_connectors "
                        "WHERE id = :connector_id"
                    ),
                    {"connector_id": GOOGLE_CONNECTOR_ID},
                )
                .mappings()
                .one()
            )
        assert tokens["access_token_enc"] is None
        assert tokens["refresh_token_enc"] is None


def test_google_disconnect_records_revoke_failure_without_leaking_token_material(
    postgres_url: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        },
        revoke_errors_by_token={
            "tok_refresh_plain_connect": GoogleOAuthRevokeFailure(code="provider_timeout")
        },
    )
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-code")

        disconnected = client.delete("/v1/connectors/google")

        assert disconnected.status_code == 200
        assert disconnected.json()["connector"]["readiness"] == "not_connected"
        events = client.get("/v1/connectors/google/events")
        disconnected_event = [
            event
            for event in events.json()["events"]
            if event["event_type"] == "evt.connector.google.disconnected"
        ][-1]
        assert disconnected_event["payload"]["token_revoke_results"] == [
            {
                "slot": "refresh_token",
                "status": "failed",
                "stage": "revoke",
                "reason": "provider_timeout",
            },
            {"slot": "access_token", "status": "succeeded", "stage": "revoke"},
        ]
        assert "tok_refresh_plain_connect" not in events.text
        assert "tok_access_plain_connect" not in events.text
        with cast(Any, client.app).state.session_factory() as db:
            tokens = (
                db.execute(
                    text(
                        "SELECT access_token_enc, refresh_token_enc FROM google_connectors "
                        "WHERE id = :connector_id"
                    ),
                    {"connector_id": GOOGLE_CONNECTOR_ID},
                )
                .mappings()
                .one()
            )
        assert tokens["access_token_enc"] is None
        assert tokens["refresh_token_enc"] is None


def test_google_disconnect_records_decrypt_failure_without_leaking_token_material(
    postgres_url: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        },
    )
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-code")
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                db.execute(
                    text(
                        "UPDATE google_connectors SET refresh_token_enc = :ciphertext "
                        "WHERE id = :connector_id"
                    ),
                    {
                        "ciphertext": "not-aead:v1",
                        "connector_id": GOOGLE_CONNECTOR_ID,
                    },
                )

        disconnected = client.delete("/v1/connectors/google")

        assert disconnected.status_code == 200
        assert oauth_client.revoke_calls == ["tok_access_plain_connect"]
        events = client.get("/v1/connectors/google/events")
        disconnected_event = [
            event
            for event in events.json()["events"]
            if event["event_type"] == "evt.connector.google.disconnected"
        ][-1]
        assert disconnected_event["payload"]["token_revoke_results"] == [
            {
                "slot": "refresh_token",
                "status": "failed",
                "stage": "decrypt",
                "reason": "malformed_envelope",
            },
            {"slot": "access_token", "status": "succeeded", "stage": "revoke"},
        ]
        assert "tok_access_plain_connect" not in events.text
        assert "tok_refresh_plain_connect" not in events.text
        with cast(Any, client.app).state.session_factory() as db:
            tokens = (
                db.execute(
                    text(
                        "SELECT access_token_enc, refresh_token_enc FROM google_connectors "
                        "WHERE id = :connector_id"
                    ),
                    {"connector_id": GOOGLE_CONNECTOR_ID},
                )
                .mappings()
                .one()
            )
        assert tokens["access_token_enc"] is None
        assert tokens["refresh_token_enc"] is None


def test_google_disconnect_unexpected_revoke_exception_propagates(
    postgres_url: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        },
        revoke_errors_by_token={
            "tok_refresh_plain_connect": RuntimeError("unexpected revoke defect")
        },
    )
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-code")

        with pytest.raises(RuntimeError, match="unexpected revoke defect"):
            client.delete("/v1/connectors/google")

        status = client.get("/v1/connectors/google")
        assert status.status_code == 200
        assert status.json()["connector"]["status"] == "connected"
        events = client.get("/v1/connectors/google/events")
        event_types = [event["event_type"] for event in events.json()["events"]]
        assert "evt.connector.google.disconnected" not in event_types


def test_google_connector_callback_rejects_oauth_payload_without_account_identity(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="unknown-subject",
                account_email="unknown-email",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-code"},
        )

        assert callback.status_code == 400
        error = callback.json()["error"]
        assert error["code"] == "E_CONNECTOR_CALLBACK_INVALID"
        assert error["details"]["reason"] == "oauth_payload_invalid"

        status = client.get("/v1/connectors/google")
        assert status.status_code == 200
        connector = status.json()["connector"]
        assert connector["status"] == "not_connected"
        assert connector["last_error_code"] == "oauth_payload_invalid"
        assert connector["account_subject"] is None
        assert connector["account_email"] is None

        events = client.get("/v1/connectors/google/events")
        assert events.status_code == 200
        event_types = [event["event_type"] for event in events.json()["events"]]
        assert "evt.connector.google.connect.failed" in event_types
        assert "evt.connector.google.connect.succeeded" not in event_types


def test_google_connector_callback_maps_typed_pkce_decryption_failure(
    postgres_url: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        }
    )
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]
        with cast(Any, client.app).state.session_factory() as db:
            db.execute(
                text(
                    "UPDATE google_oauth_states SET pkce_verifier_enc = :ciphertext "
                    "WHERE state_handle = :state"
                ),
                {"ciphertext": "not-aead", "state": state},
            )
            db.commit()

        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-code"},
        )

        assert callback.status_code == 400
        error = callback.json()["error"]
        assert error["code"] == "E_CONNECTOR_CALLBACK_INVALID"
        assert error["details"]["reason"] == "malformed_envelope"
        assert oauth_client.exchanged_states == []

        with cast(Any, client.app).state.session_factory() as db:
            consumed_at = db.scalar(
                text("SELECT consumed_at FROM google_oauth_states WHERE state_handle = :state"),
                {"state": state},
            )
            event_types = [
                row[0]
                for row in db.execute(
                    text(
                        "SELECT event_type FROM google_connector_events "
                        "WHERE connector_id = :id ORDER BY created_at ASC"
                    ),
                    {"id": GOOGLE_CONNECTOR_ID},
                ).all()
            ]
    assert consumed_at is not None
    assert "evt.connector.google.connect.failed" in event_types
    assert "evt.connector.google.connect.succeeded" not in event_types


def test_google_connector_callback_does_not_map_unexpected_pkce_decryption_exception(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-code": FakeTokenBundle(
                account_subject="sub_connect",
                account_email="owner@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_plain_connect",
                refresh_token="tok_refresh_plain_connect",
            )
        }
    )
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]

        def fail_decrypt_secret(**_: Any) -> str:
            raise RuntimeError("pkce decrypt defect")

        monkeypatch.setattr("ariel.google_connector.decrypt_secret", fail_decrypt_secret)
        with pytest.raises(RuntimeError, match="pkce decrypt defect"):
            client.get(
                "/v1/connectors/google/callback",
                params={"state": state, "code": "connect-code"},
            )

        with cast(Any, client.app).state.session_factory() as db:
            consumed_at = db.scalar(
                text("SELECT consumed_at FROM google_oauth_states WHERE state_handle = :state"),
                {"state": state},
            )
            event_types = [
                row[0]
                for row in db.execute(
                    text(
                        "SELECT event_type FROM google_connector_events "
                        "WHERE connector_id = :id ORDER BY created_at ASC"
                    ),
                    {"id": GOOGLE_CONNECTOR_ID},
                ).all()
            ]
    assert oauth_client.exchanged_states == []
    assert consumed_at is None
    assert "evt.connector.google.connect.failed" not in event_types
    assert "evt.connector.google.connect.succeeded" not in event_types


def test_google_connector_callback_maps_typed_oauth_exchange_failure(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        exchange_errors_by_code={
            "connect-code": GoogleOAuthExchangeFailure(code="provider_timeout")
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-code"},
        )

        assert callback.status_code == 502
        error = callback.json()["error"]
        assert error["code"] == "E_CONNECTOR_CALLBACK_FAILED"
        assert error["details"]["reason"] == "provider_timeout"
        assert error["retryable"] is True

        status = client.get("/v1/connectors/google")
        assert status.status_code == 200
        connector = status.json()["connector"]
        assert connector["status"] == "error"
        assert connector["last_error_code"] == "oauth_exchange_failed"

        events = client.get("/v1/connectors/google/events")
        assert events.status_code == 200
        event_types = [event["event_type"] for event in events.json()["events"]]
        assert "evt.connector.google.connect.failed" in event_types
        assert "evt.connector.google.connect.succeeded" not in event_types


def test_google_connector_callback_does_not_map_unexpected_exchange_exception(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        exchange_errors_by_code={"connect-code": RuntimeError("config defect")}
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]
        with pytest.raises(RuntimeError, match="config defect"):
            client.get(
                "/v1/connectors/google/callback",
                params={"state": state, "code": "connect-code"},
            )


def test_connector_state_is_durable_and_token_material_is_not_persisted_in_plaintext(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-encryption-check": FakeTokenBundle(
                account_subject="sub_encrypted",
                account_email="encryption@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_plain_encryption_check",
                refresh_token="tok_refresh_plain_encryption_check",
            )
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-encryption-check")

        with cast(Any, client.app).state.session_factory() as db:
            row = db.execute(
                text(
                    "SELECT access_token_enc, refresh_token_enc "
                    "FROM google_connectors WHERE id = :connector_id"
                ),
                {"connector_id": "con_google"},
            ).one()
            access_token_enc = row[0]
            refresh_token_enc = row[1]
            assert isinstance(access_token_enc, str)
            assert isinstance(refresh_token_enc, str)
            assert access_token_enc != "tok_access_plain_encryption_check"
            assert refresh_token_enc != "tok_refresh_plain_encryption_check"
            assert "tok_access_plain_encryption_check" not in access_token_enc
            assert "tok_refresh_plain_encryption_check" not in refresh_token_enc


@pytest.mark.parametrize(
    ("token_column", "connect_code"),
    [
        ("access_token_enc", "connect-token-corrupt"),
        ("refresh_token_enc", "connect-token-corrupt-expired"),
    ],
)
def test_prepare_capability_access_propagates_token_decryption_defects(
    postgres_url: str,
    token_column: str,
    connect_code: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-token-corrupt": FakeTokenBundle(
                account_subject="sub_token_corrupt",
                account_email="token-corrupt@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_corrupt",
                refresh_token="tok_refresh_corrupt",
            ),
            "connect-token-corrupt-expired": FakeTokenBundle(
                account_subject="sub_token_corrupt_expired",
                account_email="token-corrupt-expired@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_corrupt_expired",
                refresh_token="tok_refresh_corrupt_expired",
                expires_in_seconds=-5,
            ),
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code=connect_code)
        with cast(Any, client.app).state.session_factory() as db:
            db.execute(
                text(f"UPDATE google_connectors SET {token_column} = :ciphertext WHERE id = :id"),
                {"ciphertext": "not-aead:v1", "id": GOOGLE_CONNECTOR_ID},
            )
            db.commit()

        runtime = build_google_runtime(
            cast(Any, client.app).state.runtime.settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        )
        with pytest.raises(SecretDecryptionFailure) as raised:
            with cast(Any, client.app).state.session_factory() as db:
                with db.begin():
                    runtime.prepare_capability_access(
                        db=db,
                        capability_id="cap.email.search",
                        now_fn=utcnow,
                        new_id_fn=lambda prefix: f"{prefix}_decrypt_defect",
                    )

        with cast(Any, client.app).state.session_factory() as db:
            last_error_code = db.scalar(
                text("SELECT last_error_code FROM google_connectors WHERE id = :id"),
                {"id": GOOGLE_CONNECTOR_ID},
            )
    assert raised.value.code == "malformed_envelope"
    assert last_error_code is None


def test_prepare_capability_access_returns_typed_failure_when_refresh_token_missing(
    postgres_url: str,
) -> None:
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-refresh-missing": FakeTokenBundle(
                account_subject="sub_refresh_missing",
                account_email="refresh-missing@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_refresh_missing",
                refresh_token="tok_refresh_missing",
                expires_in_seconds=-5,
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    with _build_client(postgres_url, ActionProposalAdapter()) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-refresh-missing")
        with cast(Any, client.app).state.session_factory() as db:
            db.execute(
                text("UPDATE google_connectors SET refresh_token_enc = NULL WHERE id = :id"),
                {"id": GOOGLE_CONNECTOR_ID},
            )
            db.commit()

        runtime = build_google_runtime(
            cast(Any, client.app).state.runtime.settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        )
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                access_token, granted_scopes, provider_account_id, access_failure = (
                    runtime.prepare_capability_access(
                        db=db,
                        capability_id="cap.email.search",
                        now_fn=utcnow,
                        new_id_fn=lambda prefix: f"{prefix}_refresh_missing",
                    )
                )

        with cast(Any, client.app).state.session_factory() as db:
            connector = (
                db.execute(
                    text("SELECT last_error_code FROM google_connectors WHERE id = :id"),
                    {"id": GOOGLE_CONNECTOR_ID},
                )
                .mappings()
                .one()
            )
            refresh_failed_events = db.execute(
                text(
                    "SELECT payload FROM google_connector_events "
                    "WHERE connector_id = :id "
                    "AND event_type = 'evt.connector.google.refresh.failed'"
                ),
                {"id": GOOGLE_CONNECTOR_ID},
            ).all()

    assert access_token is None
    assert granted_scopes == {GOOGLE_GMAIL_READ_SCOPE}
    assert provider_account_id == "sub_refresh_missing"
    assert access_failure is not None
    assert access_failure.error == "token_expired"
    assert access_failure.auth_failure is not None
    assert access_failure.auth_failure.failure_class == "token_expired"
    assert connector["last_error_code"] == "refresh_missing"
    assert len(refresh_failed_events) == 1
    assert refresh_failed_events[0][0]["failure_reason"] == "token_expired"


def test_calendar_and_email_read_caps_execute_allowlisted_without_approval(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "show schedule": [
                {
                    "name": "calendar.list",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                    },
                }
            ],
            "propose slots": [
                {
                    "name": "calendar.propose_slots",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                        "duration_minutes": 30,
                        "attendees": ["teammate@example.com"],
                        "timezone": "UTC",
                        "source_evidence_ids": [],
                        "quoted_content_caveat": False,
                        "participants": ["teammate@example.com"],
                        "proposed_windows": [],
                        "timezone_evidence": {
                            "source": None,
                            "rationale": None,
                            "confidence": None,
                        },
                        "constraints": {"hard": [], "soft": [], "attendee_notes": []},
                    },
                }
            ],
            "search inbox": [{"name": "email.search", "input": {"query": "invoice #44"}}],
            "open inbox item": [{"name": "email.read", "input": {"message_id": "msg-1"}}],
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-read-scopes": FakeTokenBundle(
                account_subject="sub_reads",
                account_email="reads@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_reads",
                refresh_token="tok_refresh_reads",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    monkeypatch.setattr(
        "ariel.worker.build_google_runtime",
        lambda settings: build_google_runtime(
            settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        ),
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-read-scopes")

        session_id = _session_id(client)
        for message in ("show schedule", "propose slots", "search inbox", "open inbox item"):
            post_message_and_drain(client, session_id, message=message)
            turn_data = _turn_data(client, session_id)

            attempt = _surface_attempt(turn_data)
            assert attempt["policy"]["decision"] == "allow_inline"
            assert attempt["approval"]["status"] == "not_requested"
            assert attempt["execution"]["status"] == "succeeded"
            output = attempt["execution"]["output"]

            rendered_message = turn_data["assistant_message"].lower()
            assert "approval required" not in rendered_message
            if message == "show schedule":
                assert {event["provider_account_id"] for event in output["events"]} == {"sub_reads"}
                assert "schedule" in rendered_message
            if message == "propose slots":
                assert output["provider_account_id"] == "sub_reads"
                assert "availability" in rendered_message
            if message == "search inbox":
                assert {
                    message_ref["provider_account_id"] for message_ref in output["messages"]
                } == {"sub_reads"}
                assert "invoice" in rendered_message
            if message == "open inbox item":
                assert output["message"]["provider_account_id"] == "sub_reads"
                assert "payment confirmed" not in rendered_message
                assert "email msg-1" in rendered_message


def test_attendee_slots_are_limited_scope_and_recoverable_without_freebusy_scope(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
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
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-no-freebusy": FakeTokenBundle(
                account_subject="sub_no_freebusy",
                account_email="limited@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_limited",
                refresh_token="tok_refresh_limited",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider()
    monkeypatch.setattr(
        "ariel.worker.build_google_runtime",
        lambda settings: build_google_runtime(
            settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        ),
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-no-freebusy")

        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="plan team sync")
        turn_data = _turn_data(client, session_id)

        rendered_message = turn_data["assistant_message"].lower()
        assert "attendee" in rendered_message
        assert "user-calendar-only" in rendered_message or "your calendar only" in rendered_message
        assert "reconnect" in rendered_message

        attempt = _surface_attempt(turn_data)
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("case_name", "connect_code", "refresh_mode", "scope_missing_capability", "expected_class"),
    [
        ("not_connected", None, "ok", None, "not_connected"),
        ("consent_required", "connect-calendar-only", "ok", None, "consent_required"),
        ("scope_missing", "connect-gmail-only", "ok", "cap.email.search", "scope_missing"),
        (
            "provider_timeout",
            "connect-gmail-expired",
            "transient_failure",
            None,
            "provider_timeout",
        ),
        ("access_revoked", "connect-gmail-expired", "invalid_grant", None, "access_revoked"),
    ],
)
def test_typed_auth_scope_failures_are_deterministic_and_recoverable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    connect_code: str | None,
    refresh_mode: str,
    scope_missing_capability: str | None,
    expected_class: str,
) -> None:
    del case_name
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "read emails": [{"name": "email.search", "input": {"query": "latest invoice"}}]
        },
        assistant_text_by_message={"read emails": f"{expected_class} connect reconnect retry"},
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-calendar-only": FakeTokenBundle(
                account_subject="sub_calendar_only",
                account_email="calendar-only@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                access_token="tok_access_calendar_only",
                refresh_token="tok_refresh_calendar_only",
            ),
            "connect-gmail-only": FakeTokenBundle(
                account_subject="sub_gmail_only",
                account_email="gmail-only@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_gmail_only",
                refresh_token="tok_refresh_gmail_only",
            ),
            "connect-gmail-expired": FakeTokenBundle(
                account_subject="sub_gmail_expired",
                account_email="gmail-expired@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_gmail_expired",
                refresh_token="tok_refresh_gmail_expired",
                expires_in_seconds=-5,
            ),
        },
        refresh_mode=refresh_mode,
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        fail_scope_missing_for={scope_missing_capability} if scope_missing_capability else set()
    )
    monkeypatch.setattr(
        "ariel.worker.build_google_runtime",
        lambda settings: build_google_runtime(
            settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        ),
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
        post_message_and_drain(client, session_id, message="read emails")
        turn_data = _turn_data(client, session_id)

        rendered_message = turn_data["assistant_message"].lower()
        assert expected_class in rendered_message
        if expected_class == "not_connected":
            assert "connect" in rendered_message
        if expected_class in {"consent_required", "scope_missing", "access_revoked"}:
            assert "reconnect" in rendered_message
        if expected_class == "not_connected":
            assert turn_data["surface_action_lifecycle"] == []
            assert all(
                event["event_type"] != "evt.action.execution.failed"
                for event in turn_data["events"]
            )
            return
        if expected_class == "consent_required" and turn_data["surface_action_lifecycle"] == []:
            assert "reconnect" in rendered_message
            assert all(
                event["event_type"] != "evt.action.execution.started"
                for event in turn_data["events"]
            )
            return

        attempt = _surface_attempt(turn_data)
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_class

        failed_event_payload = next(
            event["payload"]
            for event in turn_data["events"]
            if event["event_type"] == "evt.action.execution.failed"
        )
        assert failed_event_payload["error"] == expected_class


def test_bearer_token_rejection_remains_token_expired(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ActionProposalAdapter(
        run_calls_by_message={
            "read emails": [{"name": "email.search", "input": {"query": "latest invoice"}}]
        },
        assistant_text_by_message={"read emails": "token_expired retry reconnect"},
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-gmail": FakeTokenBundle(
                account_subject="sub_gmail",
                account_email="gmail@example.com",
                granted_scopes=[GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_gmail",
                refresh_token="tok_refresh_gmail",
            ),
        },
    )
    workspace_provider = FakeGoogleWorkspaceProvider(fail_token_expired_for={"cap.email.search"})
    monkeypatch.setattr(
        "ariel.worker.build_google_runtime",
        lambda settings: build_google_runtime(
            settings,
            oauth_client=oauth_client,
            workspace_provider=cast(GoogleWorkspaceProvider, workspace_provider),
        ),
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-gmail")

        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="read emails")
        turn_data = _turn_data(client, session_id)

        attempt = _surface_attempt(turn_data)
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "token_expired"
