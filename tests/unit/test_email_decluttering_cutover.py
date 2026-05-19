from __future__ import annotations

from ariel.capability_registry import (
    get_capability,
)
from ariel.google_connector import (
    GOOGLE_CAPABILITY_SCOPES,
    GOOGLE_GMAIL_MODIFY_SCOPE,
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
