from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_message, responses_with_run_calls


GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s4-pr01"
    model: str = "model.s4-pr01-v1"
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
                provider_response_id="resp_s4_pr01_interpreter",
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
            assistant_text=assistant_text,
            calls=run_calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s4_pr01_123",
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
        self.revoke_calls.append(token)


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
        if "cap.calendar.list" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
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
                },
                {
                    "provider_account_id": "google",
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
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        del access_token
        if "cap.calendar.propose_slots" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
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
    ) -> dict[str, Any]:
        del access_token
        if "cap.email.search" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        query = normalized_input["query"]
        return {
            "schema_version": "google.gmail.message_refs.v1",
            "messages": [
                {
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
        }

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token
        if "cap.email.read" in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        message_id = normalized_input["message_id"]
        return {
            "schema_version": "google.gmail.message_evidence.v1",
            "message": {
                "provider_account_id": "google",
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
        }


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


def test_s4_pr01_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable(
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
        event_types_after_disconnect = [
            event["event_type"] for event in events_after_disconnect.json()["events"]
        ]
        assert "evt.connector.google.disconnected" in event_types_after_disconnect
        assert len(oauth_client.revoke_calls) >= 1


def test_s4_pr01_connector_state_is_durable_and_token_material_is_not_persisted_in_plaintext(
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


def test_s4_pr01_calendar_and_email_read_caps_execute_allowlisted_without_approval(
    postgres_url: str,
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
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-read-scopes")

        session_id = _session_id(client)
        for message in ("show schedule", "propose slots", "search inbox", "open inbox item"):
            sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": message})
            assert sent.status_code == 200
            payload = sent.json()

            attempt = _surface_attempt(payload["turn"])
            assert attempt["policy"]["decision"] == "allow_inline"
            assert attempt["approval"]["status"] == "not_requested"
            assert attempt["execution"]["status"] == "succeeded"

            rendered_message = payload["assistant"]["message"].lower()
            assert "approval required" not in rendered_message
            if message == "show schedule":
                assert "schedule" in rendered_message
            if message == "propose slots":
                assert "availability" in rendered_message
            if message == "search inbox":
                assert "invoice" in rendered_message
            if message == "open inbox item":
                assert "payment confirmed" not in rendered_message
                assert "email msg-1" in rendered_message


def test_s4_pr01_attendee_slot_fallback_is_explicit_and_recoverable_without_freebusy_scope(
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
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-no-freebusy")

        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "plan team sync"})
        assert sent.status_code == 200
        payload = sent.json()
        rendered_message = payload["assistant"]["message"].lower()
        assert "attendee" in rendered_message
        assert "user-calendar-only" in rendered_message or "your calendar only" in rendered_message
        assert "reconnect" in rendered_message

        attempt = _surface_attempt(payload["turn"])
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("case_name", "connect_code", "refresh_mode", "scope_missing_capability", "expected_class"),
    [
        ("not_connected", None, "ok", None, "not_connected"),
        ("consent_required", "connect-calendar-only", "ok", None, "consent_required"),
        ("scope_missing", "connect-gmail-only", "ok", "cap.email.search", "scope_missing"),
        ("token_expired", "connect-gmail-expired", "transient_failure", None, "token_expired"),
        ("access_revoked", "connect-gmail-expired", "invalid_grant", None, "access_revoked"),
    ],
)
def test_s4_pr01_typed_auth_scope_failures_are_deterministic_and_recoverable(
    postgres_url: str,
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
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        if connect_code is not None:
            _connect_google(client, code=connect_code)

        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "read emails"})
        assert sent.status_code == 200
        payload = sent.json()
        rendered_message = payload["assistant"]["message"].lower()
        assert expected_class in rendered_message
        if expected_class == "not_connected":
            assert "connect" in rendered_message
        if expected_class in {"consent_required", "scope_missing", "access_revoked"}:
            assert "reconnect" in rendered_message
        if expected_class == "token_expired":
            assert "retry" in rendered_message
            assert "reconnect" in rendered_message

        if expected_class == "not_connected":
            assert payload["turn"]["surface_action_lifecycle"] == []
            assert all(
                event["event_type"] != "evt.action.execution.failed"
                for event in payload["turn"]["events"]
            )
            return
        if (
            expected_class == "consent_required"
            and payload["turn"]["surface_action_lifecycle"] == []
        ):
            assert "reconnect" in rendered_message
            assert all(
                event["event_type"] != "evt.action.execution.started"
                for event in payload["turn"]["events"]
            )
            return

        attempt = _surface_attempt(payload["turn"])
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_class

        failed_event_payload = next(
            event["payload"]
            for event in payload["turn"]["events"]
            if event["event_type"] == "evt.action.execution.failed"
        )
        assert failed_event_payload["error"] == expected_class
