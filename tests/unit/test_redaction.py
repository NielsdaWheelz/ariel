from __future__ import annotations

from ariel.redaction import safe_failure_reason


def test_safe_failure_reason_preserves_safe_detail() -> None:
    assert (
        safe_failure_reason(
            "  provider returned quota_exceeded  ",
            safe_reason="provider_error",
        )
        == "provider returned quota_exceeded"
    )


def test_safe_failure_reason_replaces_blank_or_secret_like_detail() -> None:
    assert safe_failure_reason("", safe_reason="provider_error") == "provider_error"
    assert (
        safe_failure_reason(
            "request failed with bearer sk-testsecret1234",
            safe_reason="provider_error",
        )
        == "provider_error"
    )


def test_safe_failure_reason_bounds_detail_length() -> None:
    assert safe_failure_reason("x" * 600, safe_reason="provider_error") == "x" * 500
