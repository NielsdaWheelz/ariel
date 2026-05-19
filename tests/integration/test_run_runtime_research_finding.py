"""Integration tests for the ``research.finding`` syscall.

Tests the happy path, schema/size violations, and the gating behaviour
(``research.finding`` is not eligible in a main-agent run) via the
FakeSandboxRuntime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ariel.config import AppSettings
from ariel.persistence import SessionRecord, TurnRecord
from ariel.run_runtime import (
    ScratchEntry,
    _MAX_RESEARCH_FINDING_BYTES,
    execute_run_program,
)
from tests.fake_sandbox import FakeSandboxRuntime

NOW = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)

_VALID_FINDING_SOURCE = (
    "research.finding(\n"
    "    summary='Found key facts.',\n"
    "    claims=[{'statement': 'Fact A', 'sources': ['https://example.test'], 'confidence': 'high'}],\n"
    "    gaps=['Could not determine X.'],\n"
    "    sources=[{'title': 'Example', 'reference': 'https://example.test', 'retrieved_at': '2026-05-20T10:00:00Z'}],\n"
    ")\n"
)


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
    is_research_run: bool = False,
) -> Any:
    scratch: dict[str, ScratchEntry] = {}
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
        runtime_provenance=None,
        google_runtime=None,
        execute_google_reads_outside_transaction=False,
        agency_runtime=None,
        attachment_runtime=None,
        allowed_capability_ids=set(),
        settings=_settings(),
        scratch=scratch,
        is_research_run=is_research_run,
    )


def test_research_finding_happy_path_sets_emitted_finding(
    session_factory: Any,
) -> None:
    """research.finding stores the finding dict in emitted_finding on a clean run."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_ok", turn_id="trn_finding_ok")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=_VALID_FINDING_SOURCE,
                is_research_run=True,
            )
            assert result.program_ok
            assert result.emitted_finding is not None
            assert result.emitted_finding["summary"] == "Found key facts."
            assert isinstance(result.emitted_finding["claims"], list)
            assert isinstance(result.emitted_finding["gaps"], list)
            assert isinstance(result.emitted_finding["sources"], list)
    sandbox.close()


def test_research_finding_emitted_finding_is_none_on_failed_program(
    session_factory: Any,
) -> None:
    """emitted_finding is scrubbed to None when program_ok is False."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_fail", turn_id="trn_finding_fail")
            # Program calls research.finding then raises, so program_ok=False.
            source = _VALID_FINDING_SOURCE + "raise RuntimeError('boom')\n"
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                is_research_run=True,
            )
            assert not result.program_ok
            assert result.emitted_finding is None
    sandbox.close()


def test_research_finding_schema_invalid_missing_field(
    session_factory: Any,
) -> None:
    """research.finding with a missing required field appends the error and fails."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_schema", turn_id="trn_finding_schema")
            # Missing 'sources' key.
            source = "research.finding(\n    summary='ok',\n    claims=[],\n    gaps=[],\n)\n"
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                is_research_run=True,
            )
            assert not result.program_ok
            assert "research_finding_schema_invalid" in result.callback_errors
    sandbox.close()


def test_research_finding_schema_invalid_wrong_type(
    session_factory: Any,
) -> None:
    """research.finding with a non-list claims field rejects as schema_invalid."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_type", turn_id="trn_finding_type")
            source = (
                "research.finding(\n"
                "    summary='ok',\n"
                "    claims='not a list',\n"
                "    gaps=[],\n"
                "    sources=[],\n"
                ")\n"
            )
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                is_research_run=True,
            )
            assert not result.program_ok
            assert "research_finding_schema_invalid" in result.callback_errors
    sandbox.close()


def test_research_finding_too_large_rejected(
    session_factory: Any,
) -> None:
    """research.finding whose JSON encoding exceeds _MAX_RESEARCH_FINDING_BYTES is rejected."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    big_summary = "x" * (_MAX_RESEARCH_FINDING_BYTES + 1)
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_big", turn_id="trn_finding_big")
            source = (
                f"research.finding(\n"
                f"    summary={big_summary!r},\n"
                f"    claims=[],\n"
                f"    gaps=[],\n"
                f"    sources=[],\n"
                f")\n"
            )
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=source,
                is_research_run=True,
            )
            assert not result.program_ok
            assert "research_finding_too_large" in result.callback_errors
    sandbox.close()


def test_research_finding_not_eligible_in_main_agent_run(
    session_factory: Any,
) -> None:
    """research.finding is not in the eligible syscall set for a main-agent run
    (is_research_run=False, the default), so the call fails as unknown_callable."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_gate", turn_id="trn_finding_gate")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source=_VALID_FINDING_SOURCE,
                is_research_run=False,
            )
            assert not result.program_ok
    sandbox.close()


def test_main_agent_run_emitted_finding_is_none(
    session_factory: Any,
) -> None:
    """emitted_finding is None for a normal (non-research) run even when it succeeds."""
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    with session_factory() as db:
        with db.begin():
            turn = _turn(db, session_id="ses_finding_none", turn_id="trn_finding_none")
            result = _run(
                sandbox=sandbox,
                db=db,
                session_factory=session_factory,
                turn=turn,
                source="agent.emit_message(text='hello')\n",
                is_research_run=False,
            )
            assert result.program_ok
            assert result.emitted_finding is None
    sandbox.close()
