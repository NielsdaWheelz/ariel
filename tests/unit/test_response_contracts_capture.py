from __future__ import annotations

import pytest

from ariel.response_contracts import (
    ResponseContractViolation,
    build_surface_capture_record_response,
)


def _capture_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "cpt_1",
        "kind": "text",
        "effective_session_id": "ses_1",
        "turn_id": "trn_1",
        "idempotency_key": None,
        "created_at": "2026-05-22T00:00:00Z",
        "updated_at": "2026-05-22T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_capture_record_response_accepts_durable_capture_shape() -> None:
    assert build_surface_capture_record_response(capture=_capture_payload()) == {
        "ok": True,
        "capture": _capture_payload(),
    }


def test_capture_record_response_rejects_dead_capture_states() -> None:
    with pytest.raises(ResponseContractViolation):
        build_surface_capture_record_response(capture=_capture_payload(kind="unknown"))

    with pytest.raises(ResponseContractViolation):
        build_surface_capture_record_response(
            capture=_capture_payload(terminal_state="ingest_failed")
        )
