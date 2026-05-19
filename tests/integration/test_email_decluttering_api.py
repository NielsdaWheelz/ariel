from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ariel.app import create_app
from ariel.persistence import (
    ActionAttemptRecord,
    ProviderWriteReceiptRecord,
    SessionRecord,
    TurnRecord,
)
from tests.fake_sandbox import FakeSandboxRuntime


class NoopModelAdapter:
    provider = "provider.test"
    model = "model.test"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
        return {
            "assistant_message": "unused",
            "provider": self.provider,
            "model": self.model,
            "provider_response_id": "resp_unused",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


@pytest.fixture
def client(postgres_url: str) -> Generator[TestClient, None, None]:
    app = create_app(
        database_url=postgres_url,
        model_adapter=NoopModelAdapter(),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_email_state_inspection_endpoints_return_serialized_records(
    client: TestClient,
) -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    session_factory = cast(Any, client.app).state.session_factory

    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_email_api",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_email_api",
                    session_id="ses_email_api",
                    user_message="clean up email",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_email_api",
                    session_id="ses_email_api",
                    turn_id="trn_email_api",
                    proposal_index=1,
                    capability_id="cap.email.archive",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={"message_ids": ["msg_1"], "idempotency_key": "idem_1"},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                ProviderWriteReceiptRecord(
                    id="ema_api",
                    provider="google",
                    provider_account_id="con_google",
                    action_attempt_id="aat_email_api",
                    capability_id="cap.email.archive",
                    idempotency_key="provider-write:archive:idem_1",
                    status="succeeded",
                    provider_object_ids={"message_ids": ["msg_1"], "thread_ids": ["thr_1"]},
                    request_digest="p" * 64,
                    response_payload={"provider_result": {"archived": ["msg_1"]}},
                    ambiguity_reason=None,
                    provider_timestamp=None,
                    provider_etag=None,
                    provider_history_id=None,
                    response_digest="d" * 64,
                    before_state={"messages": [{"message_id": "msg_1", "label_ids": ["INBOX"]}]},
                    after_state={"messages": [{"message_id": "msg_1", "label_ids": []}]},
                    undo_token_hash="u" * 64,
                    undo_expires_at=now + timedelta(days=30),
                    created_at=now,
                    updated_at=now,
                )
            )

    action_list = client.get(
        "/v1/email/actions",
        params={"provider_account_id": "con_google", "status": "succeeded"},
    )
    assert action_list.status_code == 200
    action_payload = action_list.json()
    assert action_payload["ok"] is True
    assert [action["id"] for action in action_payload["email_actions"]] == ["ema_api"]
    assert action_payload["email_actions"][0]["undo_available"] is True

    action_detail = client.get(
        "/v1/email/actions/ema_api",
        params={"provider_account_id": "con_google"},
    )
    assert action_detail.status_code == 200
    assert action_detail.json()["email_action"]["provider_message_ids"] == ["msg_1"]

    wrong_account = client.get(
        "/v1/email/actions/ema_api",
        params={"provider_account_id": "other_google_account"},
    )
    assert wrong_account.status_code == 404

    missing_action = client.get(
        "/v1/email/actions/ema_missing",
        params={"provider_account_id": "con_google"},
    )
    assert missing_action.status_code == 404
    assert missing_action.json()["error"]["code"] == "E_EMAIL_ACTION_NOT_FOUND"
