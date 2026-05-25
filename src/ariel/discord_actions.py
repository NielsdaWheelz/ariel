from __future__ import annotations

from typing import Literal

APPROVAL_CUSTOM_ID_PREFIX = "ariel:approval:"
ApprovalDecision = Literal["approve", "deny"]


def approval_custom_id(decision: ApprovalDecision, approval_ref: str) -> str:
    normalized_ref = approval_ref.strip()
    if not normalized_ref:
        msg = "approval_ref must not be blank"
        raise ValueError(msg)
    return f"{APPROVAL_CUSTOM_ID_PREFIX}{decision}:{normalized_ref}"


def is_ariel_custom_id(custom_id: str) -> bool:
    return custom_id.startswith(APPROVAL_CUSTOM_ID_PREFIX)


def parse_approval_custom_id(custom_id: str) -> tuple[ApprovalDecision, str] | None:
    if not is_ariel_custom_id(custom_id):
        return None
    decision_text, separator, approval_ref = custom_id.removeprefix(
        APPROVAL_CUSTOM_ID_PREFIX
    ).partition(":")
    normalized_ref = approval_ref.strip()
    if not separator or decision_text not in {"approve", "deny"} or not normalized_ref:
        return None
    decision: ApprovalDecision = "approve" if decision_text == "approve" else "deny"
    return decision, normalized_ref
