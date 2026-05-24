"""Defect propagation tests for the run-program host path."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from ariel import run_runtime
from ariel.persistence import TurnRecord
from ariel.run_runtime import RunProgramResult, execute_run_program
from tests.fake_sandbox import FakeSandboxRuntime

NOW = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)


def _turn() -> TurnRecord:
    return TurnRecord(
        id="trn_run_defect",
        session_id="ses_run_defect",
        user_message="test",
        assistant_message=None,
        status="in_progress",
        created_at=NOW,
        updated_at=NOW,
    )


def _execute(
    *,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_one_call: Any,
    source: str,
) -> RunProgramResult:
    monkeypatch.setattr(run_runtime, "process_one_call", fake_process_one_call)
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    try:
        return execute_run_program(
            sandbox=sandbox,
            source=source,
            db=cast(Session, object()),
            session_factory=None,
            session_id="ses_run_defect",
            turn=_turn(),
            proposal_index_start=0,
            approval_ttl_seconds=300,
            approval_actor_id="user:test",
            add_event=lambda *_: None,
            now_fn=lambda: NOW,
            new_id_fn=lambda prefix: f"{prefix}_run_defect",
            runtime_provenance=None,
            google_runtime=None,
            execute_google_reads_outside_transaction=False,
            agency_runtime=None,
            attachment_runtime=None,
            allowed_capability_ids={"cap.memory.recall"},
            settings=None,
            scratch={},
        )
    finally:
        sandbox.close()


def test_unknown_process_one_call_status_propagates_as_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_process_one_call(**kwargs: Any) -> None:
        ctx = kwargs["ctx"]
        ctx.function_call_outputs.append(
            {
                "type": "function_call_output",
                "call_id": "run_call_1",
                "output": json.dumps({"status": "surprise"}),
            }
        )

    source = (
        "try:\n"
        "    memory.recall(query='x')\n"
        "except Exception:\n"
        "    agent.emit_message(text='caught')\n"
    )

    with pytest.raises(RuntimeError, match="unknown_call_status: surprise"):
        _execute(
            monkeypatch=monkeypatch, fake_process_one_call=fake_process_one_call, source=source
        )


def test_missing_process_one_call_output_propagates_as_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_process_one_call(**_: Any) -> None:
        return None

    source = (
        "try:\n"
        "    memory.recall(query='x')\n"
        "except Exception:\n"
        "    agent.emit_message(text='caught')\n"
    )

    with pytest.raises(RuntimeError, match=r"memory\.recall: process_one_call_output_count:0"):
        _execute(
            monkeypatch=monkeypatch, fake_process_one_call=fake_process_one_call, source=source
        )
