from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import ariel.executor as executor_module
from ariel.capability_registry import CapabilityDefinition, CapabilityExecutionError
from ariel.executor import execute_capability, preflight_capability_execution


def _capability(
    *,
    impact_level: str = "read",
    allowed_egress_destinations: tuple[str, ...] = (),
    execute: Callable[[dict[str, Any]], dict[str, Any]] | None,
    declare_egress_intent: Callable[[dict[str, Any]], list[dict[str, Any]] | None] | None = None,
) -> CapabilityDefinition:
    def validate_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        return raw_input, None

    return CapabilityDefinition(
        capability_id="cap.test",
        version="1.0",
        impact_level=impact_level,
        policy_decision="allow_inline",
        contract_metadata={},
        allowed_egress_destinations=allowed_egress_destinations,
        validate_input=validate_input,
        execute=execute,
        declare_egress_intent=declare_egress_intent,
    )


def test_execute_capability_maps_expected_capability_execution_error() -> None:
    def fail(_: dict[str, Any]) -> dict[str, Any]:
        raise CapabilityExecutionError("provider_timeout")

    result = execute_capability(capability=_capability(execute=fail), normalized_input={})

    assert result.status == "failed"
    assert result.output is None
    assert result.error == "provider_timeout"


def test_execute_capability_propagates_unexpected_capability_defect() -> None:
    def fail(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        execute_capability(capability=_capability(execute=fail), normalized_input={})


def test_egress_intent_defect_propagates_from_preflight() -> None:
    def declare(_: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("intent bug")

    capability = _capability(
        impact_level="external_send",
        allowed_egress_destinations=("example.test",),
        execute=lambda _: {},
        declare_egress_intent=declare,
    )

    with pytest.raises(RuntimeError, match="intent bug"):
        preflight_capability_execution(capability=capability, normalized_input={})


def test_egress_dispatch_defect_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dispatch(*, destination: str, payload: dict[str, Any]) -> str | None:
        del destination, payload
        raise RuntimeError("dispatch bug")

    capability = _capability(
        impact_level="external_send",
        allowed_egress_destinations=("example.test",),
        execute=lambda _: {"status": "ok"},
        declare_egress_intent=lambda _: [
            {"destination": "https://example.test/send", "payload": {"value": "ok"}}
        ],
    )
    monkeypatch.setattr(executor_module, "_dispatch_egress_request", dispatch)

    with pytest.raises(RuntimeError, match="dispatch bug"):
        execute_capability(capability=capability, normalized_input={})
