from __future__ import annotations

import pytest

from ariel.discord_actions import approval_custom_id, is_ariel_custom_id, parse_approval_custom_id


def test_approval_custom_id_round_trips_supported_decisions() -> None:
    for decision in ("approve", "deny"):
        custom_id = approval_custom_id(decision, " apr_123 ")

        assert custom_id == f"ariel:approval:{decision}:apr_123"
        assert is_ariel_custom_id(custom_id)
        assert parse_approval_custom_id(custom_id) == (decision, "apr_123")


def test_approval_custom_id_rejects_blank_refs() -> None:
    with pytest.raises(ValueError, match="approval_ref must not be blank"):
        approval_custom_id("approve", " ")


@pytest.mark.parametrize(
    "custom_id",
    [
        "ariel:approval:approve:",
        "ariel:approval:approve:   ",
        "ariel:approval:deny:\t",
        "ariel:approval:maybe:apr_123",
        "ariel:proactive:ack:case_123",
    ],
)
def test_parse_approval_custom_id_rejects_unsupported_shapes(custom_id: str) -> None:
    assert parse_approval_custom_id(custom_id) is None
