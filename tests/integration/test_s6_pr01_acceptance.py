from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls


GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_DRIVE_METADATA_READ_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
GOOGLE_DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DRIVE_SHARE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s6-pr01"
    model: str = "model.s6-pr01-v1"
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
            provider_response_id="resp_s6_pr01_123",
            input_tokens=37,
            output_tokens=22,
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
            raise RuntimeError("google_upstream_timeout")
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
    provider_error_by_capability: dict[str, str] = field(default_factory=dict)
    drive_read_outcomes_by_file_id: dict[str, str] = field(default_factory=dict)
    drive_search_calls: list[dict[str, Any]] = field(default_factory=list)
    drive_read_calls: list[dict[str, Any]] = field(default_factory=list)
    drive_share_calls: list[dict[str, Any]] = field(default_factory=list)

    def _raise_if_configured(self, capability_id: str) -> None:
        if capability_id in self.fail_scope_missing_for:
            raise RuntimeError("insufficient_permissions")
        provider_error = self.provider_error_by_capability.get(capability_id)
        if provider_error is not None:
            raise RuntimeError(provider_error)

    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {"results": [], "retrieved_at": "2026-03-06T12:00:00Z"}

    def calendar_propose_slots(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        del access_token, normalized_input, attendee_intersection_enabled
        return {"results": [], "retrieved_at": "2026-03-06T12:00:00Z"}

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {"results": [], "retrieved_at": "2026-03-06T12:00:00Z"}

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {"results": [], "retrieved_at": "2026-03-06T12:00:00Z"}

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {
            "status": "created",
            "event_id": "evt_ignored",
            "title": "ignored",
            "start_time": "2026-03-06T10:00:00Z",
            "end_time": "2026-03-06T10:30:00Z",
            "provider_event_ref": "calendar://ignored",
        }

    def email_create_draft(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {"provider_draft_ref": "gmail://draft/ignored"}

    def email_send(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return {
            "status": "sent",
            "message_id": "msg_ignored",
            "provider_message_ref": "gmail://sent/msg_ignored",
            "to": [],
            "subject": "ignored",
        }

    def drive_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        self._raise_if_configured("cap.drive.search")
        self.drive_search_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        query = normalized_input["query"]
        return {
            "query": query,
            "retrieved_at": "2026-03-06T12:00:00Z",
            "results": [
                {
                    "title": "Q3 Launch Plan",
                    "source": "https://drive.google.com/file/d/drv_plan/view",
                    "snippet": (
                        "mime_type=application/vnd.google-apps.document "
                        "owner=ops@example.com modified=2026-03-05T09:30:00Z"
                    ),
                    "published_at": "2026-03-05T09:30:00Z",
                }
            ],
        }

    def drive_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        self._raise_if_configured("cap.drive.read")
        self.drive_read_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        file_id = normalized_input["file_id"]
        base_source = f"https://drive.google.com/file/d/{file_id}/view"
        outcome = self.drive_read_outcomes_by_file_id.get(file_id, "ok")
        if outcome == "unsupported":
            return {
                "file_id": file_id,
                "retrieved_at": "2026-03-06T12:00:00Z",
                "content_excerpt": "",
                "truncated": False,
                "read_outcome": {
                    "status": "unsupported",
                    "reason_code": "drive_read_unsupported",
                    "recovery": "Export this file to Google Docs or plain text, then retry.",
                },
                "results": [
                    {
                        "title": f"Drive file {file_id}",
                        "source": base_source,
                        "snippet": (
                            "Unsupported content format. Export this file to Google Docs "
                            "or plain text, then retry."
                        ),
                        "published_at": "2026-03-05T09:30:00Z",
                    }
                ],
            }
        if outcome == "too_large":
            return {
                "file_id": file_id,
                "retrieved_at": "2026-03-06T12:00:00Z",
                "content_excerpt": "",
                "truncated": False,
                "read_outcome": {
                    "status": "too_large",
                    "reason_code": "drive_read_too_large",
                    "recovery": "Open the file and request a smaller section, then retry.",
                },
                "results": [
                    {
                        "title": f"Drive file {file_id}",
                        "source": base_source,
                        "snippet": (
                            "File exceeds read budget. Request a smaller section and retry."
                        ),
                        "published_at": "2026-03-05T09:30:00Z",
                    }
                ],
            }
        if outcome == "unavailable":
            return {
                "file_id": file_id,
                "retrieved_at": "2026-03-06T12:00:00Z",
                "content_excerpt": "",
                "truncated": False,
                "read_outcome": {
                    "status": "unavailable",
                    "reason_code": "drive_read_unavailable",
                    "recovery": "Verify file access and file ID, then retry.",
                },
                "results": [
                    {
                        "title": f"Drive file {file_id}",
                        "source": base_source,
                        "snippet": (
                            "File is unavailable. Verify file access and file ID, then retry."
                        ),
                        "published_at": "2026-03-05T09:30:00Z",
                    }
                ],
            }
        return {
            "file_id": file_id,
            "retrieved_at": "2026-03-06T12:00:00Z",
            "content_excerpt": (
                "launch plan excerpt: phase 1 closes security review by mar 12; "
                "phase 2 starts staged rollout by mar 18."
            ),
            "truncated": False,
            "read_outcome": {
                "status": "ok",
                "reason_code": None,
                "recovery": None,
            },
            "results": [
                {
                    "title": "Q3 Launch Plan",
                    "source": base_source,
                    "snippet": (
                        "launch plan excerpt: phase 1 closes security review by mar 12; "
                        "phase 2 starts staged rollout by mar 18."
                    ),
                    "published_at": "2026-03-05T09:30:00Z",
                }
            ],
        }

    def drive_share(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        self._raise_if_configured("cap.drive.share")
        self.drive_share_calls.append(
            {
                "access_token": access_token,
                "normalized_input": copy.deepcopy(normalized_input),
            }
        )
        return {
            "status": "shared",
            "file_id": normalized_input["file_id"],
            "grantee_email": normalized_input["grantee_email"],
            "role": normalized_input["role"],
            "permission_id": f"perm_{len(self.drive_share_calls)}",
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


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


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


def test_s6_pr01_drive_search_and_read_execute_inline_with_retrieval_citations(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "find launch plan": [{"capability_id": "cap.drive.search", "input": {"query": "launch plan"}}],
            "read launch plan": [{"capability_id": "cap.drive.read", "input": {"file_id": "drv_plan"}}],
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-drive-read": FakeTokenBundle(
                account_subject="sub_drive_read",
                account_email="drive-read@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_METADATA_READ_SCOPE,
                    GOOGLE_DRIVE_READ_SCOPE,
                ],
                access_token="tok_access_drive_read",
                refresh_token="tok_refresh_drive_read",
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
        _connect_google(client, code="connect-drive-read")
        session_id = _session_id(client)

        search = client.post(f"/v1/sessions/{session_id}/message", json={"message": "find launch plan"})
        assert search.status_code == 200
        search_payload = search.json()
        search_attempt = _surface_attempt(search_payload["turn"])
        assert search_attempt["proposal"]["capability_id"] == "cap.drive.search"
        assert search_attempt["policy"]["decision"] == "allow_inline"
        assert search_attempt["approval"]["status"] == "not_requested"
        assert search_attempt["execution"]["status"] == "succeeded"
        search_output = search_attempt["execution"]["output"]
        assert isinstance(search_output["results"], list)
        assert search_output["results"][0]["title"] == "Q3 Launch Plan"
        assert "mime_type=" in search_output["results"][0]["snippet"]
        assert "[1]" in search_payload["assistant"]["message"]
        assert len(search_payload["assistant"]["sources"]) == 1

        read = client.post(f"/v1/sessions/{session_id}/message", json={"message": "read launch plan"})
        assert read.status_code == 200
        read_payload = read.json()
        read_attempt = _surface_attempt(read_payload["turn"])
        assert read_attempt["proposal"]["capability_id"] == "cap.drive.read"
        assert read_attempt["policy"]["decision"] == "allow_inline"
        assert read_attempt["approval"]["status"] == "not_requested"
        assert read_attempt["execution"]["status"] == "succeeded"
        read_output = read_attempt["execution"]["output"]
        assert read_output["read_outcome"]["status"] == "ok"
        assert "launch plan excerpt" in read_output["content_excerpt"]
        assert read_output["truncated"] is False
        assert len(read_output["content_excerpt"]) <= 2000
        assert "[1]" in read_payload["assistant"]["message"]
        assert len(read_payload["assistant"]["sources"]) == 1
        assert "drive.google.com" in read_payload["assistant"]["sources"][0]["source"]


@pytest.mark.parametrize(
    ("message", "file_id", "expected_status", "expected_hint"),
    [
        ("read unsupported file", "drv_unsupported", "unsupported", "export"),
        ("read too large file", "drv_too_large", "too_large", "smaller"),
        ("read unavailable file", "drv_unavailable", "unavailable", "verify"),
    ],
)
def test_s6_pr01_drive_read_typed_outcomes_are_explicit_and_recoverable(
    postgres_url: str,
    message: str,
    file_id: str,
    expected_status: str,
    expected_hint: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={message: [{"capability_id": "cap.drive.read", "input": {"file_id": file_id}}]}
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-drive-read": FakeTokenBundle(
                account_subject="sub_drive_read",
                account_email="drive-read@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_READ_SCOPE,
                ],
                access_token="tok_access_drive_read",
                refresh_token="tok_refresh_drive_read",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        drive_read_outcomes_by_file_id={
            "drv_unsupported": "unsupported",
            "drv_too_large": "too_large",
            "drv_unavailable": "unavailable",
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-drive-read")
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": message})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["read_outcome"]["status"] == expected_status
        assert expected_hint in output["read_outcome"]["recovery"].lower()
        assert expected_hint in payload["assistant"]["message"].lower()
        assert len(payload["assistant"]["sources"]) == 1


def test_s6_pr01_drive_reconnect_intent_is_capability_scoped_and_least_privilege(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter()
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-read-only": FakeTokenBundle(
                account_subject="sub_drive_scope",
                account_email="drive-scope@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_read_only",
                refresh_token="tok_refresh_read_only",
            )
        }
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=FakeGoogleWorkspaceProvider(),
        )
        _connect_google(client, code="connect-read-only")

        search_reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.drive.search"},
        )
        assert search_reconnect.status_code == 200
        search_scopes = set(search_reconnect.json()["oauth"]["requested_scopes"])
        assert GOOGLE_CALENDAR_READ_SCOPE in search_scopes
        assert GOOGLE_GMAIL_READ_SCOPE in search_scopes
        assert GOOGLE_DRIVE_METADATA_READ_SCOPE in search_scopes
        assert GOOGLE_DRIVE_READ_SCOPE not in search_scopes
        assert GOOGLE_DRIVE_SHARE_SCOPE not in search_scopes

        read_reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.drive.read"},
        )
        assert read_reconnect.status_code == 200
        read_scopes = set(read_reconnect.json()["oauth"]["requested_scopes"])
        assert GOOGLE_CALENDAR_READ_SCOPE in read_scopes
        assert GOOGLE_GMAIL_READ_SCOPE in read_scopes
        assert GOOGLE_DRIVE_READ_SCOPE in read_scopes
        assert GOOGLE_DRIVE_SHARE_SCOPE not in read_scopes
        assert GOOGLE_GMAIL_SEND_SCOPE not in read_scopes
        assert GOOGLE_CALENDAR_WRITE_SCOPE not in read_scopes

        share_reconnect = client.post(
            "/v1/connectors/google/reconnect",
            params={"capability_intent": "cap.drive.share"},
        )
        assert share_reconnect.status_code == 200
        share_scopes = set(share_reconnect.json()["oauth"]["requested_scopes"])
        assert GOOGLE_CALENDAR_READ_SCOPE in share_scopes
        assert GOOGLE_GMAIL_READ_SCOPE in share_scopes
        assert GOOGLE_DRIVE_SHARE_SCOPE in share_scopes
        assert GOOGLE_GMAIL_SEND_SCOPE not in share_scopes
        assert GOOGLE_GMAIL_COMPOSE_SCOPE not in share_scopes
        assert GOOGLE_CALENDAR_WRITE_SCOPE not in share_scopes


def test_s6_pr01_drive_share_is_approval_gated_exact_payload_and_exactly_once(
    postgres_url: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "share launch plan": [
                {
                    "capability_id": "cap.drive.share",
                    "input": {
                        "file_id": "drv_plan",
                        "grantee_email": "partner@example.com",
                        "role": "reader",
                    },
                }
            ]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-drive-share": FakeTokenBundle(
                account_subject="sub_drive_share",
                account_email="drive-share@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_SHARE_SCOPE,
                ],
                access_token="tok_access_drive_share",
                refresh_token="tok_refresh_drive_share",
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
        _connect_google(client, code="connect-drive-share")
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "share launch plan"})
        assert sent.status_code == 200
        turn = sent.json()["turn"]
        attempt = _surface_attempt(turn)
        assert attempt["proposal"]["capability_id"] == "cap.drive.share"
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
        assert latest_attempt["execution"]["output"]["status"] == "shared"
        assert latest_attempt["execution"]["output"]["file_id"] == "drv_plan"
        assert latest_attempt["execution"]["output"]["grantee_email"] == "partner@example.com"
        assert latest_attempt["execution"]["output"]["role"] == "reader"

        event_types = _event_types(latest_turn)
        assert event_types.count("evt.action.execution.started") == 1
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert event_types.index("evt.action.approval.approved") < event_types.index(
            "evt.action.execution.started"
        )

        assert len(workspace_provider.drive_share_calls) == 1
        assert workspace_provider.drive_share_calls[0]["normalized_input"] == {
            "file_id": "drv_plan",
            "grantee_email": "partner@example.com",
            "role": "reader",
        }


@pytest.mark.parametrize(
    ("case_name", "connect_code", "refresh_mode", "scope_missing", "expected_class"),
    [
        ("not_connected", None, "ok", False, "not_connected"),
        ("consent_required", "connect-baseline", "ok", False, "consent_required"),
        ("scope_missing", "connect-drive-read", "ok", True, "scope_missing"),
        ("token_expired", "connect-drive-read-expired", "transient_failure", False, "token_expired"),
        ("access_revoked", "connect-drive-read-expired", "invalid_grant", False, "access_revoked"),
    ],
)
def test_s6_pr01_drive_auth_scope_failures_are_typed_and_recoverable(
    postgres_url: str,
    case_name: str,
    connect_code: str | None,
    refresh_mode: str,
    scope_missing: bool,
    expected_class: str,
) -> None:
    del case_name
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "read strategy doc": [{"capability_id": "cap.drive.read", "input": {"file_id": "drv_auth"}}]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-baseline": FakeTokenBundle(
                account_subject="sub_baseline",
                account_email="baseline@example.com",
                granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                access_token="tok_access_baseline",
                refresh_token="tok_refresh_baseline",
            ),
            "connect-drive-read": FakeTokenBundle(
                account_subject="sub_drive_read",
                account_email="drive-read@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_READ_SCOPE,
                ],
                access_token="tok_access_drive_read",
                refresh_token="tok_refresh_drive_read",
            ),
            "connect-drive-read-expired": FakeTokenBundle(
                account_subject="sub_drive_expired",
                account_email="drive-expired@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_READ_SCOPE,
                ],
                access_token="tok_access_drive_expired",
                refresh_token="tok_refresh_drive_expired",
                expires_in_seconds=-5,
            ),
        },
        refresh_mode=refresh_mode,
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        fail_scope_missing_for={"cap.drive.read"} if scope_missing else set()
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

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "read strategy doc"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_class

        rendered_message = payload["assistant"]["message"].lower()
        assert expected_class in rendered_message
        if expected_class == "not_connected":
            assert "connect" in rendered_message
        if expected_class in {"consent_required", "scope_missing", "access_revoked"}:
            assert "reconnect" in rendered_message
        if expected_class == "token_expired":
            assert "retry" in rendered_message
            assert "reconnect" in rendered_message


@pytest.mark.parametrize(
    ("provider_error", "expected_class", "expected_hint"),
    [
        ("google_upstream_timeout", "provider_timeout", "retry"),
        ("google_upstream_429", "provider_rate_limited", "rate"),
        ("google_forbidden", "provider_permission_denied", "permission"),
    ],
)
def test_s6_pr01_drive_provider_failures_are_typed_and_recoverable(
    postgres_url: str,
    provider_error: str,
    expected_class: str,
    expected_hint: str,
) -> None:
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "find risk register": [
                {"capability_id": "cap.drive.search", "input": {"query": "risk register"}}
            ]
        }
    )
    oauth_client = FakeGoogleOAuthClient(
        tokens_by_code={
            "connect-drive-search": FakeTokenBundle(
                account_subject="sub_drive_search",
                account_email="drive-search@example.com",
                granted_scopes=[
                    GOOGLE_CALENDAR_READ_SCOPE,
                    GOOGLE_GMAIL_READ_SCOPE,
                    GOOGLE_DRIVE_METADATA_READ_SCOPE,
                ],
                access_token="tok_access_drive_search",
                refresh_token="tok_refresh_drive_search",
            )
        }
    )
    workspace_provider = FakeGoogleWorkspaceProvider(
        provider_error_by_capability={"cap.drive.search": provider_error}
    )
    with _build_client(postgres_url, adapter) as client:
        _bind_google_fakes(
            client,
            oauth_client=oauth_client,
            workspace_provider=workspace_provider,
        )
        _connect_google(client, code="connect-drive-search")
        session_id = _session_id(client)

        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "find risk register"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_class

        message = payload["assistant"]["message"].lower()
        assert expected_class in message
        assert expected_hint in message
