from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer
import ulid

from ariel.action_runtime import process_action_execution_task
from ariel.app import ModelAdapter, create_app
from ariel.google_connector import GoogleConnectorRuntime
from tests.integration.responses_helpers import responses_with_function_calls


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
        del tools, history, context_bundle
        proposals = self.proposals_by_message.get(user_message, [])
        assistant_text = self.assistant_text_by_message.get(
            user_message,
            f"assistant::{user_message}",
        )
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text=assistant_text,
            proposals=copy.deepcopy(proposals),
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
            "results": [
                {
                    "title": "team sync",
                    "source": "calendar://primary",
                    "snippet": "today 10:00-10:30 team sync",
                    "published_at": None,
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
                "results": [
                    {
                        "title": "slot option 1",
                        "source": "calendar://availability",
                        "snippet": "wed 10:30-11:00 works for all attendees",
                        "published_at": None,
                    }
                ],
                "retrieved_at": "2026-03-03T12:00:00Z",
                "attendees_considered": attendees,
                "attendee_intersection_used": True,
                "attendee_recovery_hint": None,
            }
        return {
            "results": [
                {
                    "title": "slot option 1",
                    "source": "calendar://availability",
                    "snippet": "wed 09:30-10:00 available on your calendar only",
                    "published_at": None,
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
            "attendees_considered": attendees,
            "attendee_intersection_used": False,
            "attendee_recovery_hint": (
                "Reconnect Google and grant attendee free/busy scope to include attendee intersection."
            ),
        }

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {
            "results": [],
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
            "results": [],
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
            "status": "created",
            "event_id": "evt_1",
            "title": "event",
            "start_time": "2026-03-04T10:00:00Z",
            "end_time": "2026-03-04T10:30:00Z",
            "provider_event_ref": "calendar://evt_1",
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


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def _run_queued_action(client: TestClient, action_attempt_id: str) -> None:
    app_state = cast(Any, client.app).state
    process_action_execution_task(
        session_factory=app_state.session_factory,
        action_attempt_id=action_attempt_id,
        google_runtime=GoogleConnectorRuntime(
            oauth_client=app_state.google_oauth_client,
            workspace_provider=app_state.google_workspace_provider,
            redirect_uri=str(app_state.google_oauth_redirect_uri),
            oauth_state_ttl_seconds=int(app_state.google_oauth_state_ttl_seconds),
            encryption_secret=str(app_state.connector_encryption_secret),
            encryption_key_version=str(app_state.connector_encryption_key_version),
            encryption_keys=(
                str(app_state.connector_encryption_keys)
                if app_state.connector_encryption_keys is not None
                else None
            ),
        ),
        agency_runtime=None,
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    )


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
        proposals_by_message={
            "draft follow-up": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
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
        attempt = _surface_attempt(sent.json()["turn"])
        assert attempt["execution"]["status"] == "in_progress"
        _run_queued_action(client, attempt["action_attempt_id"])

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
        proposals_by_message={
            "search inbox": [{"capability_id": "cap.email.search", "input": {"query": "invoice"}}]
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
        proposals_by_message={
            "draft follow-up": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                    },
                }
            ],
            "show schedule": [
                {
                    "capability_id": "cap.calendar.list",
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
            "connect-read-only": FakeTokenBundle(
                account_subject="sub_sticky",
                account_email="sticky@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
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
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-read-only")
        session_id = _session_id(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "draft follow-up"}
        )
        assert first.status_code == 200
        first_attempt = _surface_attempt(first.json()["turn"])
        assert first_attempt["execution"]["status"] == "in_progress"
        _run_queued_action(client, first_attempt["action_attempt_id"])

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        first_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert first_attempt["execution"]["error"] == "consent_required"

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
        proposals_by_message={
            "draft follow-up": [
                {
                    "capability_id": "cap.email.draft",
                    "input": {
                        "to": ["ops@example.com"],
                        "subject": "status",
                        "body": "hello",
                    },
                }
            ],
            "show schedule": [
                {
                    "capability_id": "cap.calendar.list",
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
            "connect-read-only-expired": FakeTokenBundle(
                account_subject="sub_sticky_transient",
                account_email="sticky-transient@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_sticky_transient",
                refresh_token="tok_refresh_sticky_transient",
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
        _connect_google(client, code="connect-read-only-expired")
        session_id = _session_id(client)

        blocking_failure = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "draft follow-up"},
        )
        assert blocking_failure.status_code == 200
        blocking_attempt = _surface_attempt(blocking_failure.json()["turn"])
        assert blocking_attempt["execution"]["status"] == "in_progress"
        _run_queued_action(client, blocking_attempt["action_attempt_id"])

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        blocking_attempt = _surface_attempt(timeline.json()["turns"][-1])
        assert blocking_attempt["execution"]["error"] == "consent_required"

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
        assert connector_payload["last_error_code"] == "consent_required"


def test_s4_pr03_attendee_reconnect_intent_requests_freebusy_and_closes_fallback_path(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "plan team sync": [
                {
                    "capability_id": "cap.calendar.propose_slots",
                    "input": {
                        "window_start": "2026-03-04T00:00:00Z",
                        "window_end": "2026-03-05T00:00:00Z",
                        "duration_minutes": 30,
                        "attendees": ["a@example.com", "b@example.com"],
                    },
                }
            ]
        }
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
        assert before_attempt["execution"]["output"]["attendee_intersection_used"] is False
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

        after_reconnect = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "plan team sync"},
        )
        assert after_reconnect.status_code == 200
        after_payload = after_reconnect.json()
        after_attempt = _surface_attempt(after_payload["turn"])
        assert after_attempt["execution"]["status"] == "succeeded"
        assert after_attempt["execution"]["output"]["attendee_intersection_used"] is True
        after_message = after_payload["assistant"]["message"].lower()
        assert "works for all attendees" in after_message
        assert "user-calendar-only" not in after_message
