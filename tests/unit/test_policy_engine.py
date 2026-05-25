from __future__ import annotations

from typing import Any

import pytest

from ariel.policy_engine import evaluate_proposal


@pytest.mark.parametrize(
    ("input_payload", "expected_reason"),
    [
        ({"url": "definitely-not-a-url"}, "url_invalid"),
        ({"url": "ftp://example.com/resource"}, "url_scheme_unsupported"),
        ({"url": "http://127.0.0.1/private"}, "url_destination_unsafe"),
    ],
)
def test_web_extract_typed_url_failures_deny_at_policy_time(
    input_payload: dict[str, Any],
    expected_reason: str,
) -> None:
    evaluation = evaluate_proposal(
        capability_id="cap.web.extract",
        input_payload=input_payload,
        pending_approval_exists=False,
    )

    assert evaluation.decision == "deny"
    assert evaluation.reason == expected_reason
    assert evaluation.normalized_input is None


def test_schema_invalid_validator_failures_still_use_schema_invalid_reason() -> None:
    evaluation = evaluate_proposal(
        capability_id="cap.web.extract",
        input_payload={"url": ""},
        pending_approval_exists=False,
    )

    assert evaluation.decision == "deny"
    assert evaluation.reason == "schema_invalid"
    assert evaluation.normalized_input is None
