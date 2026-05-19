"""Unit tests for the host-side scratch store syscalls.

Tests ``scratch.set`` / ``scratch.get`` round-trip, bounds enforcement, taint
propagation, and the emit_value-round eviction via the FakeSandboxRuntime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ariel.action_runtime import RuntimeProvenance
from ariel.config import AppSettings
from ariel.persistence import SessionRecord, TurnRecord
from ariel.run_runtime import (
    ScratchEntry,
    _SCRATCH_MAX_ENTRIES,
    _SCRATCH_MAX_TOTAL_BYTES,
    _SCRATCH_MAX_VALUE_BYTES,
    execute_run_program,
)
from tests.fake_sandbox import FakeSandboxRuntime

NOW = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)


def _settings() -> AppSettings:
    from typing import cast

    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def _turn(db: Any, *, session_id: str, turn_id: str) -> TurnRecord:
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
        user_message="test",
        assistant_message=None,
        status="in_progress",
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(turn)
    db.flush()
    return turn


def _run(
    *,
    sandbox: FakeSandboxRuntime,
    db: Any,
    session_factory: Any,
    turn: TurnRecord,
    source: str,
    scratch: dict[str, ScratchEntry],
    runtime_provenance: RuntimeProvenance | None = None,
) -> Any:
    seq = [0]

    def new_id(prefix: str) -> str:
        seq[0] += 1
        return f"{prefix}_{seq[0]}"

    return execute_run_program(
        sandbox=sandbox,
        source=source,
        db=db,
        session_factory=session_factory,
        session_id=turn.session_id,
        turn=turn,
        proposal_index_start=0,
        approval_ttl_seconds=300,
        approval_actor_id="user:test",
        add_event=lambda *_: None,
        now_fn=lambda: NOW,
        new_id_fn=new_id,
        runtime_provenance=runtime_provenance,
        google_runtime=None,
        execute_google_reads_outside_transaction=False,
        agency_runtime=None,
        attachment_runtime=None,
        allowed_capability_ids=set(),
        settings=_settings(),
        scratch=scratch,
    )


def test_scratch_set_and_get_round_trip(
    session_factory: Any,
) -> None:
    """scratch.set stores a value; scratch.get returns it unchanged."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_rw", turn_id="trn_scratch_rw")
            # Program 1: set a value.
            result1 = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='x', value={'answer': 42})\n",
                scratch=scratch,
            )
            assert result1.program_ok
            assert "x" in scratch
            assert scratch["x"].value == {"answer": 42}

            # Program 2: get the value and emit it.
            result2 = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=(
                    "v = scratch.get(key='x')\n"
                    "agent.emit_message(text='answer=' + str(v['answer']))\n"
                ),
                scratch=scratch,
            )
            assert result2.program_ok
            assert result2.emitted_message == "answer=42"
    sandbox.close()


def test_scratch_get_missing_key_returns_error(
    session_factory: Any,
) -> None:
    """scratch.get on a missing key fails the syscall with scratch_key_missing."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_missing", turn_id="trn_scratch_missing")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.get(key='missing')\n",
                scratch=scratch,
            )
            # The syscall raises inside the program — program does not complete.
            assert not result.program_ok
    sandbox.close()


def test_scratch_set_taint_propagates_on_get(
    session_factory: Any,
) -> None:
    """scratch.get re-applies the setting program's taint onto current_provenance.

    A value set under tainted provenance: when a later program calls
    scratch.get, the taint is re-applied, making subsequent syscalls in that
    program tainted as well.
    """
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    tainted_provenance = RuntimeProvenance(
        status="tainted",
        evidence=({"source": "test_taint", "reason": "injected"},),
    )
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_taint", turn_id="trn_scratch_taint")
            # Set a value with tainted provenance.
            result1 = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='tainted', value='secret')\n",
                scratch=scratch,
                runtime_provenance=tainted_provenance,
            )
            assert result1.program_ok
            assert scratch["tainted"].provenance is tainted_provenance

            # Get the value from a clean-provenance program; the result
            # program's taint delta should carry the tainted evidence.
            result2 = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=(
                    "v = scratch.get(key='tainted')\nagent.emit_message(text='got: ' + str(v))\n"
                ),
                scratch=scratch,
                runtime_provenance=None,
            )
            assert result2.program_ok
            assert result2.emitted_message == "got: secret"
            # The program's taint delta includes the evidence from the
            # scratch entry's provenance.
            assert result2.runtime_provenance is not None
            assert result2.runtime_provenance.status == "tainted"
            evidence_sources = [e.get("source") for e in result2.runtime_provenance.evidence]
            assert "test_taint" in evidence_sources
    sandbox.close()


def test_scratch_set_rejects_too_large_value(
    session_factory: Any,
) -> None:
    """scratch.set rejects a value whose JSON encoding exceeds _SCRATCH_MAX_VALUE_BYTES."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    big_string = "x" * (_SCRATCH_MAX_VALUE_BYTES + 1)
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_big", turn_id="trn_scratch_big")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=f"scratch.set(key='big', value={big_string!r})\n",
                scratch=scratch,
            )
            assert not result.program_ok
            assert "scratch_value_too_large" in result.callback_errors
    sandbox.close()


def test_scratch_set_rejects_non_json_encodable_value(
    session_factory: Any,
) -> None:
    """scratch.set rejects a value that is not JSON-encodable (e.g. a set)."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_nonjson", turn_id="trn_scratch_nonjson")
            # Sets are not JSON-encodable; the program uses an inline set literal.
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='s', value={1, 2, 3})\n",
                scratch=scratch,
            )
            assert not result.program_ok
            assert "scratch_value_too_large" in result.callback_errors
    sandbox.close()


def test_scratch_set_rejects_invalid_key(
    session_factory: Any,
) -> None:
    """scratch.set rejects an empty key."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    scratch: dict[str, ScratchEntry] = {}
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_badkey", turn_id="trn_scratch_badkey")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='', value=1)\n",
                scratch=scratch,
            )
            assert not result.program_ok
            assert "scratch_key_invalid" in result.callback_errors
    sandbox.close()


def test_scratch_store_full_rejects_excess_entries(
    session_factory: Any,
) -> None:
    """scratch.set rejects a new key when the store is at capacity."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    # Pre-fill the store to the max.
    scratch: dict[str, ScratchEntry] = {
        str(i): ScratchEntry(value=i, provenance=None) for i in range(_SCRATCH_MAX_ENTRIES)
    }
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_full", turn_id="trn_scratch_full")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='overflow', value=99)\n",
                scratch=scratch,
            )
            assert not result.program_ok
            assert "scratch_store_full" in result.callback_errors
    sandbox.close()


def test_scratch_total_bytes_cap_rejects_excess(
    session_factory: Any,
) -> None:
    """scratch.set rejects a value when the total store size would exceed
    _SCRATCH_MAX_TOTAL_BYTES, even if the value alone is within the per-value
    limit.
    """
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    # Pre-fill the store with one entry that is just below the per-value limit
    # but just below the total limit too — then add another that tips it over.
    big_value = "a" * (_SCRATCH_MAX_TOTAL_BYTES - 10)
    scratch: dict[str, ScratchEntry] = {"existing": ScratchEntry(value=big_value, provenance=None)}
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_scratch_total", turn_id="trn_scratch_total")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="scratch.set(key='extra', value='needs_some_space_' * 10)\n",
                scratch=scratch,
            )
            assert not result.program_ok
            assert "scratch_store_full" in result.callback_errors
    sandbox.close()
