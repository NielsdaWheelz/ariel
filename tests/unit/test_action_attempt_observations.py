from __future__ import annotations

from datetime import UTC, datetime

from ariel.agent_loop import _action_attempt_observations
from ariel.persistence import ActionAttemptRecord


_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)


def _attempt(
    *,
    status: str = "succeeded",
    execution_output: dict[str, object] | None = None,
    execution_error: str | None = None,
) -> ActionAttemptRecord:
    return ActionAttemptRecord(
        id="aat_test_1",
        session_id="ses_test",
        turn_id="trn_test",
        proposal_index=1,
        capability_id="cap.memory.search",
        capability_version="1.0",
        capability_contract_hash="h" * 64,
        impact_level="read",
        proposed_input={},
        payload_hash="p" * 64,
        policy_decision="allow_inline",
        policy_reason=None,
        status=status,
        approval_required=False,
        execution_output=execution_output,
        execution_error=execution_error,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_action_attempt_observations_do_not_echo_execution_output() -> None:
    observations = _action_attempt_observations(
        [
            _attempt(
                execution_output={
                    "hits": [
                        {
                            "snippet": "private search result",
                            "source": "memory",
                        }
                    ]
                }
            )
        ]
    )

    assert observations == [
        {
            "action_attempt_id": "aat_test_1",
            "capability_id": "cap.memory.search",
            "status": "succeeded",
            "policy_decision": "allow_inline",
            "approval_required": False,
        }
    ]


def test_action_attempt_observations_keep_safe_execution_error() -> None:
    observations = _action_attempt_observations(
        [_attempt(status="failed", execution_error="provider_unavailable")]
    )

    assert observations[0]["execution_error"] == "provider_unavailable"
    assert "execution_output" not in observations[0]
