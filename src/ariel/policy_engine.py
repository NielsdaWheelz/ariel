from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ariel.capability_registry import CapabilityDefinition, PolicyDecision, get_capability


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    capability: CapabilityDefinition | None
    normalized_input: dict[str, Any] | None
    impact_level: str
    decision: PolicyDecision
    reason: str


def evaluate_proposal(
    *,
    capability_id: str,
    input_payload: dict[str, Any],
    pending_approval_exists: bool,
    influenced_by_untrusted_content: bool = False,
    provenance_status: Literal["clean", "tainted", "ambiguous"] | None = None,
) -> PolicyEvaluation:
    capability = get_capability(capability_id)
    if capability is None:
        return PolicyEvaluation(
            capability=None,
            normalized_input=None,
            impact_level="read",
            decision="deny",
            reason="unknown_capability",
        )

    normalized_input, input_error = capability.validate_input(input_payload)
    if input_error is not None or normalized_input is None:
        return PolicyEvaluation(
            capability=capability,
            normalized_input=None,
            impact_level=capability.impact_level,
            decision="deny",
            reason="schema_invalid",
        )

    if capability.policy_decision == "deny":
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="deny",
            reason="policy_denied",
        )

    is_side_effecting = capability.impact_level != "read"
    effective_taint = influenced_by_untrusted_content
    if provenance_status is not None:
        effective_taint = provenance_status in {"tainted", "ambiguous"}
    if is_side_effecting and effective_taint:
        if capability.impact_level in {"write_irreversible", "external_send"}:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="taint_denied_untrusted_side_effect",
            )
        if pending_approval_exists:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="pending_approval_limit_reached",
            )
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="requires_approval",
            reason="taint_escalated_requires_approval",
        )

    if capability.policy_decision == "requires_approval":
        if pending_approval_exists:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="pending_approval_limit_reached",
            )
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="requires_approval",
            reason="approval_required",
        )

    return PolicyEvaluation(
        capability=capability,
        normalized_input=normalized_input,
        impact_level=capability.impact_level,
        decision="allow_inline",
        reason="allowlisted_read",
    )
