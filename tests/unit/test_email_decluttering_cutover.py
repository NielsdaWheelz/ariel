from __future__ import annotations

from ariel.capability_registry import (
    get_capability,
)
from ariel.google_connector import (
    GOOGLE_CAPABILITY_SCOPES,
    GOOGLE_GMAIL_MODIFY_SCOPE,
    GOOGLE_GMAIL_READ_SCOPE,
)
from ariel.run_runtime import run_tool_definitions

FINAL_EMAIL_CAPABILITY_IDS = {
    "cap.email.search",
    "cap.email.read",
    "cap.email.draft",
    "cap.email.send",
    "cap.email.archive",
    "cap.email.trash",
    "cap.email.labels.modify",
    "cap.email.undo",
    "cap.email.thread_watch.create",
    "cap.email.thread_watch.cancel",
    "cap.email.thread_watch.list",
}
BROAD_GMAIL_SCOPE = "https://mail.google.com/"


def test_email_registry_contains_final_decluttering_family_but_model_gets_only_run() -> None:
    assert [tool["name"] for tool in run_tool_definitions()] == ["run"]
    for capability_id in FINAL_EMAIL_CAPABILITY_IDS:
        assert get_capability(capability_id) is not None


def test_email_mutations_require_idempotency_and_narrow_gmail_modify_scope() -> None:
    for capability_id in {
        "cap.email.archive",
        "cap.email.trash",
        "cap.email.labels.modify",
        "cap.email.undo",
    }:
        capability = get_capability(capability_id)
        assert capability is not None
        assert capability.impact_level == "write_reversible"
        assert GOOGLE_CAPABILITY_SCOPES[capability_id] == {GOOGLE_GMAIL_MODIFY_SCOPE}
        assert BROAD_GMAIL_SCOPE not in capability.contract_metadata.get("required_scopes", [])

    archive = get_capability("cap.email.archive")
    assert archive is not None
    assert archive.validate_input({"message_ids": ["m1"]}) == (None, "schema_invalid")
    normalized, error = archive.validate_input(
        {
            "message_ids": ["m1", "m1", "m2"],
            "idempotency_key": " k ",
            "user_instruction_ref": "turn:turn_1",
        }
    )
    assert error is None
    assert normalized == {
        "message_ids": ["m1", "m2"],
        "idempotency_key": "k",
        "user_instruction_ref": "turn:turn_1",
    }


def test_email_capabilities_do_not_request_broad_gmail_scope() -> None:
    for capability_id in FINAL_EMAIL_CAPABILITY_IDS:
        capability = get_capability(capability_id)
        assert capability is not None
        assert BROAD_GMAIL_SCOPE not in capability.contract_metadata.get("required_scopes", [])
        if capability_id in {"cap.email.thread_watch.cancel", "cap.email.thread_watch.list"}:
            assert capability.allowed_egress_destinations == ()
        else:
            assert capability.allowed_egress_destinations == ("gmail.googleapis.com",)

    for scopes in GOOGLE_CAPABILITY_SCOPES.values():
        assert BROAD_GMAIL_SCOPE not in scopes


def test_email_draft_requires_approval_like_other_write_surfaces() -> None:
    draft = get_capability("cap.email.draft")
    assert draft is not None
    assert draft.policy_decision == "requires_approval"


def test_email_capability_input_contracts_remain_final() -> None:
    archive = get_capability("cap.email.archive")
    trash = get_capability("cap.email.trash")
    draft = get_capability("cap.email.draft")
    send = get_capability("cap.email.send")
    labels = get_capability("cap.email.labels.modify")
    assert archive is not None
    assert trash is not None
    assert draft is not None
    assert send is not None
    assert labels is not None
    archive_input = {
        "message_ids": ["m1"],
        "idempotency_key": "archive-1",
        "user_instruction_ref": "turn:turn_1",
    }
    draft_input = {
        "to": ["person@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Hello",
        "body": "Body",
        "idempotency_key": "draft-1",
        "user_instruction_ref": "turn:turn_1",
    }
    labels_input = {
        "message_ids": ["m1"],
        "add_labels": ["Receipts"],
        "remove_labels": [],
        "idempotency_key": "label-1",
        "user_instruction_ref": "turn:turn_1",
    }
    assert archive.validate_input(archive_input)[1] is None
    assert trash.validate_input(archive_input)[1] is None
    assert draft.validate_input(draft_input)[1] is None
    assert send.validate_input(draft_input)[1] is None
    assert labels.validate_input(labels_input)[1] is None


def test_email_label_modify_contract_is_single_primary_shape() -> None:
    capability = get_capability("cap.email.labels.modify")
    assert capability is not None

    normalized, error = capability.validate_input(
        {
            "message_ids": ["m1"],
            "add_labels": ["Receipts"],
            "remove_labels": [],
            "idempotency_key": "label-1",
            "user_instruction_ref": "turn:turn_1",
        }
    )

    assert error is None
    assert normalized == {
        "message_ids": ["m1"],
        "add_labels": ["Receipts"],
        "remove_labels": [],
        "idempotency_key": "label-1",
        "user_instruction_ref": "turn:turn_1",
    }
    assert capability.validate_input(
        {
            "message_ids": ["m1"],
            "add_labels": [],
            "remove_labels": [],
            "idempotency_key": "label-1",
            "user_instruction_ref": "turn:turn_1",
        }
    ) == (None, "schema_invalid")


def test_email_thread_watch_contract_only_exposes_implemented_conditions() -> None:
    capability = get_capability("cap.email.thread_watch.create")
    assert capability is not None

    normalized, error = capability.validate_input(
        {
            "provider_thread_id": "thr-1",
            "anchor_message_id": "msg-1",
            "condition": "any_reply_arrives",
            "deadline": "2026-05-08T12:00:00Z",
            "note": "waiting on this thread",
            "idempotency_key": "watch-1",
        }
    )

    assert error is None
    assert normalized == {
        "provider_thread_id": "thr-1",
        "anchor_message_id": "msg-1",
        "condition": "any_reply_arrives",
        "deadline": "2026-05-08T12:00:00Z",
        "note": "waiting on this thread",
        "idempotency_key": "watch-1",
    }
    assert capability.validate_input(
        {
            "provider_thread_id": "thr-1",
            "anchor_message_id": "msg-1",
            "condition": "matching_reply_arrives",
            "deadline": "2026-05-08T12:00:00Z",
            "note": "waiting on this thread",
            "idempotency_key": "watch-1",
        }
    ) == (None, "schema_invalid")


def test_email_thread_watch_scopes_and_local_only_capabilities() -> None:
    create = get_capability("cap.email.thread_watch.create")
    cancel = get_capability("cap.email.thread_watch.cancel")
    list_watches = get_capability("cap.email.thread_watch.list")
    assert create is not None
    assert cancel is not None
    assert list_watches is not None

    assert create.contract_metadata["execution_mode"] == "local_durable_workflow"
    assert create.contract_metadata["required_scopes"] == [GOOGLE_GMAIL_READ_SCOPE]
    assert GOOGLE_CAPABILITY_SCOPES["cap.email.thread_watch.create"] == {GOOGLE_GMAIL_READ_SCOPE}

    for capability in (cancel, list_watches):
        assert capability.contract_metadata["execution_mode"] == "local_runtime_only"
        assert "required_scopes" not in capability.contract_metadata
        assert capability.allowed_egress_destinations == ()
        assert capability.declare_egress_intent is None
        assert capability.capability_id not in GOOGLE_CAPABILITY_SCOPES
