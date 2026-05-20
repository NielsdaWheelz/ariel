"""Integration tests for ``execute_run_program`` against the real sandbox.

These run actual model-style Python programs in a real ``runsc`` sandbox and
dispatch their syscalls through ``execute_run_program`` — the run-program
host path. The capability read tests call ``memory.search`` (allow_inline,
no model_adapter dependency) through ``process_one_call`` against a real DB;
the approval-gated test uses ``agency.run`` (approval-gated); the within-program
taint test stubs ``process_one_call`` so the run_runtime taint-merge wiring
itself is exercised.

``memory.recall`` is not used here because ``execute_run_program`` does not
thread ``model_adapter`` through to ``process_one_call``, so ``cap.memory.recall``
(which runs the retriever subagent) always fails with ``memory_runtime_not_bound``
in the direct-execute path.

They are skipped when ``runsc`` is unavailable so the unit suite still runs on
any host; CI provides ``runsc`` and the Systrap platform needs no special host
capability.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariel import run_runtime
from ariel.action_runtime import RuntimeProvenance
from ariel.config import AppSettings
from ariel.persistence import SessionRecord, TurnRecord
from ariel.run_runtime import execute_run_program
from ariel.sandbox_runtime import SandboxRuntime

NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


def _settings() -> AppSettings:
    """Minimal AppSettings for memory capability dispatch (no API key needed)."""
    from typing import cast

    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def _runsc_available() -> bool:
    if shutil.which("runsc") is not None:
        return True
    return (Path.home() / ".local" / "bin" / "runsc").exists()


pytestmark = pytest.mark.skipif(
    not _runsc_available(),
    reason="runsc is not installed; the real-gVisor run-program layer cannot run",
)


@pytest.fixture
def sandbox() -> Iterator[SandboxRuntime]:
    runtime = SandboxRuntime(container_id="ariel-run-program-test")
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.close()


def _seed_turn(db: Session, *, session_id: str, turn_id: str) -> TurnRecord:
    db.add(
        SessionRecord(
            id=session_id,
            is_active=True,
            lifecycle_state="active",
            rotated_from_session_id=None,
            rotation_reason=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    turn = TurnRecord(
        id=turn_id,
        session_id=session_id,
        user_message="run a program",
        assistant_message=None,
        status="in_progress",
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(turn)
    db.flush()
    return turn


def _execute(
    *,
    sandbox: SandboxRuntime,
    db: Session,
    session_factory: sessionmaker[Session],
    turn: TurnRecord,
    source: str,
    allowed_capability_ids: set[str],
    events: list[tuple[str, dict[str, Any]]],
    new_id_seq: list[int],
    settings: AppSettings | None = None,
    runtime_provenance: RuntimeProvenance | None = None,
) -> run_runtime.RunProgramResult:
    def new_id(prefix: str) -> str:
        new_id_seq[0] += 1
        return f"{prefix}_{new_id_seq[0]}"

    return execute_run_program(
        sandbox=sandbox,
        source=source,
        db=db,
        session_factory=session_factory,
        session_id=turn.session_id,
        turn=turn,
        # Each test runs a single program against a fresh turn, so the first
        # capability syscall starts at proposal_index 1.
        proposal_index_start=0,
        approval_ttl_seconds=300,
        approval_actor_id="user:default",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: NOW,
        new_id_fn=new_id,
        runtime_provenance=runtime_provenance,
        google_runtime=None,
        execute_google_reads_outside_transaction=False,
        agency_runtime=None,
        attachment_runtime=None,
        allowed_capability_ids=allowed_capability_ids,
        settings=settings,
        scratch={},
    )


def test_program_reads_a_capability_then_composes_an_emit_message(
    sandbox: SandboxRuntime,
    session_factory: sessionmaker[Session],
) -> None:
    """A memory syscall returns a real result; emit_message is composed from it.

    ``memory.search`` is ``allow_inline`` with a ``read`` impact, so it runs
    inline with no approval round. It executes purely from session_factory,
    without requiring model_adapter or sandbox to be threaded through.
    """

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = _seed_turn(db, session_id="ses_read", turn_id="turn_read")
            # The program searches memory, branches on the real result, and emits
            # a mechanical confirmation derived from it. The store may have some
            # wake-log entries; the structural assertion is what matters.
            source = (
                "result = memory.search(query='project status')\n"
                "assert result['status'] == 'succeeded', result\n"
                "count = len(result['hits'])\n"
                "agent.emit_message(text='Found ' + str(count) + ' results.')\n"
            )
            result = _execute(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                allowed_capability_ids={"cap.memory.search"},
                events=events,
                new_id_seq=[0],
                settings=_settings(),
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    assert result.program_ok is True, result.program_error
    assert result.callback_errors == []
    assert result.emitted_message.startswith("Found ")
    assert result.emitted_values == []
    assert result.paused is False
    assert len(result.action_attempts) == 1
    assert result.action_attempts[0].capability_id == "cap.memory.search"
    assert result.action_attempts[0].status == "succeeded"
    assert "evt.action.execution.succeeded" in {event_type for event_type, _ in events}


def test_approval_gated_syscall_returns_a_pending_value(
    sandbox: SandboxRuntime,
    session_factory: sessionmaker[Session],
) -> None:
    """An approval-gated capability stages a proposal and returns a pending value.

    Memory's two syscalls are ``allow_inline`` after the cutover, so the
    approval-gated path is exercised with ``agency.run`` -- an approval-gated
    capability whose proposal stages without executing the runtime.
    """

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = _seed_turn(db, session_id="ses_appr", turn_id="turn_appr")
            # agency.run requires approval; the program sees a pending value and
            # emits its approval_ref, proving it did not block on a human.
            source = (
                "pending = agency.run(\n"
                "    repo_root='/srv/repo', name='ship-it', prompt='do the work',\n"
                ")\n"
                "assert pending['status'] == 'approval_required', pending\n"
                "agent.emit_message(text='Proposed; ref ' + pending['approval_ref'])\n"
            )
            result = _execute(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                allowed_capability_ids={"cap.agency.run"},
                events=events,
                new_id_seq=[0],
            )

    assert result.program_ok is True, result.program_error
    assert result.callback_errors == []
    assert len(result.action_attempts) == 1
    attempt = result.action_attempts[0]
    assert attempt.capability_id == "cap.agency.run"
    assert attempt.status == "awaiting_approval"
    assert attempt.approval_required is True
    # The pending value carried the real approval ref into the program.
    assert result.emitted_message.startswith("Proposed; ref apr_")
    assert "evt.action.approval.requested" in {event_type for event_type, _ in events}


def test_within_program_taint_is_seen_by_a_later_syscall(
    sandbox: SandboxRuntime,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A syscall after a same-turn tainting read is evaluated with that taint.

    ``process_one_call`` is stubbed so the run_runtime taint-merge wiring is
    what is exercised: the first capability syscall sets a tainted
    ``result_runtime_provenance``; the second must receive it as its input
    ``runtime_provenance``.
    """

    seen_provenance: list[RuntimeProvenance | None] = []

    def fake_process_one_call(**kwargs: Any) -> None:
        ctx = kwargs["ctx"]
        index = kwargs["function_call_index"]
        seen_provenance.append(kwargs["runtime_provenance"])
        # process_one_call appends exactly one output per call.
        ctx.function_call_outputs.append(
            {
                "type": "function_call_output",
                "call_id": f"run_call_{index}",
                "output": '{"status":"succeeded","output":{"ok":true}}',
            }
        )
        if index == 1:
            # The first read returned untrusted-influenced content.
            ctx.result_runtime_provenance = RuntimeProvenance(
                status="tainted",
                evidence=({"kind": "untrusted_read"},),
            )

    monkeypatch.setattr(run_runtime, "process_one_call", fake_process_one_call)

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = _seed_turn(db, session_id="ses_taint", turn_id="turn_taint")
            source = (
                "first = memory.recall(query='x')\n"
                "second = memory.remember(note='y')\n"
                "agent.emit_message(text='done')\n"
            )
            result = _execute(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                allowed_capability_ids={"cap.memory.recall", "cap.memory.remember"},
                events=events,
                new_id_seq=[0],
            )

    assert result.program_ok is True, result.program_error
    assert len(seen_provenance) == 2
    # First syscall ran clean (no prior taint); second saw the merged taint.
    assert seen_provenance[0] is None
    assert seen_provenance[1] is not None
    assert seen_provenance[1].status == "tainted"
    assert seen_provenance[1].evidence == ({"kind": "untrusted_read"},)


def test_raising_program_is_reported_as_a_program_failure(
    sandbox: SandboxRuntime,
    session_factory: sessionmaker[Session],
) -> None:
    """A program that raises after a syscall is a program failure: no output."""

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = _seed_turn(db, session_id="ses_raise", turn_id="turn_raise")
            # The search succeeds, then the program raises before completing.
            source = (
                "memory.search(query='anything')\nraise ValueError('program failed deliberately')\n"
            )
            result = _execute(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                allowed_capability_ids={"cap.memory.search"},
                events=events,
                new_id_seq=[0],
                settings=_settings(),
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    assert result.program_ok is False
    assert result.program_error is not None
    assert "ValueError" in result.program_error
    # Program Failure: no emitted output is surfaced as intended.
    assert result.emitted_message == ""
    assert result.emitted_values == []
    assert result.paused is False
    # The inline search still ran — it is the syscall trace (audit spine).
    assert len(result.action_attempts) == 1
    assert result.action_attempts[0].capability_id == "cap.memory.search"
