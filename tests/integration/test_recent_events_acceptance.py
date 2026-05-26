"""End-to-end acceptance: the recent-events block surfaces canonical IDs from
a prior provider-sync turn into the next user-message wake's initial context."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker, Session

from ariel.config import AppSettings
from ariel.conversational_continuity import build_recent_events_block
from ariel.persistence import EventRecord, SessionRecord, TurnRecord


def _utc(offset_seconds: int) -> datetime:
    return datetime(2026, 5, 25, 22, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _seed_session_and_turn(
    db: Session, *, session_id: str, turn_id: str, user_message: str, created_offset: int
) -> None:
    db.add(
        SessionRecord(
            id=session_id,
            is_active=True,
            lifecycle_state="active",
            created_at=_utc(created_offset),
            updated_at=_utc(created_offset),
        )
    )
    db.add(
        TurnRecord(
            id=turn_id,
            session_id=session_id,
            user_message=user_message,
            status="completed",
            kind="agent_turn",
            created_at=_utc(created_offset),
            updated_at=_utc(created_offset + 4),
        )
    )


def _seed_event(
    db: Session,
    *,
    event_id: str,
    session_id: str,
    turn_id: str,
    sequence: int,
    event_type: str,
    payload: dict,
    created_offset: int,
) -> None:
    db.add(
        EventRecord(
            id=event_id,
            session_id=session_id,
            turn_id=turn_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            created_at=_utc(created_offset),
        )
    )


def test_recent_events_block_surfaces_prior_turn_message_ids(
    session_factory: sessionmaker[Session],
) -> None:
    """The agent must see canonical message_ids from a prior provider-sync turn's
    cap.email.read tool outputs when later asked to act on those messages."""
    session_id = "ses_recent_events_acceptance"
    prior_turn_id = "trn_provider_sync_prior"
    current_turn_id = "trn_user_message_current"

    with session_factory() as db:
        _seed_session_and_turn(
            db,
            session_id=session_id,
            turn_id=prior_turn_id,
            user_message="Provider sync wake: Google Gmail",
            created_offset=0,
        )
        _seed_event(
            db,
            event_id="evt_t0_start",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=1,
            event_type="evt.turn.started",
            payload={
                "wake_kind": "provider_sync",
                "user_message": "Provider sync wake: Google Gmail",
            },
            created_offset=0,
        )
        _seed_event(
            db,
            event_id="evt_t0_email_read_a",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=2,
            event_type="evt.action.execution.succeeded",
            payload={
                "capability_id": "cap.email.read",
                "action_attempt_id": "aat_a",
                "status": "succeeded",
                "execution_output": {
                    "message": {
                        "message_id": "19c638912663c9e5",
                        "thread_id": "19c638912663c9e5",
                        "subject": "Reminder: $X balance due",
                        "sender": "accounting@junehomes.com",
                    }
                },
            },
            created_offset=1,
        )
        _seed_event(
            db,
            event_id="evt_t0_email_read_b",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=3,
            event_type="evt.action.execution.succeeded",
            payload={
                "capability_id": "cap.email.read",
                "action_attempt_id": "aat_b",
                "status": "succeeded",
                "execution_output": {
                    "message": {
                        "message_id": "19c63892fa1b4c10",
                        "thread_id": "19c638912663c9e5",
                        "subject": "Re: balance reminder (auto)",
                        "sender": "accounting@junehomes.com",
                    }
                },
            },
            created_offset=2,
        )
        _seed_event(
            db,
            event_id="evt_t0_model_started_loop_trace",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=4,
            event_type="evt.model.started",
            payload={"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
            created_offset=3,
        )
        _seed_event(
            db,
            event_id="evt_t0_assistant_emitted",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=5,
            event_type="evt.assistant.emitted",
            payload={
                "text": "June Homes just sent an unread balance reminder from accounting@junehomes.com."
            },
            created_offset=4,
        )
        _seed_event(
            db,
            event_id="evt_t0_completed",
            session_id=session_id,
            turn_id=prior_turn_id,
            sequence=6,
            event_type="evt.turn.completed",
            payload={"outcome": "message"},
            created_offset=4,
        )

        db.add(
            TurnRecord(
                id=current_turn_id,
                session_id=session_id,
                user_message="try again (delete both)",
                status="in_progress",
                kind="agent_turn",
                created_at=_utc(300),
                updated_at=_utc(300),
            )
        )
        _seed_event(
            db,
            event_id="evt_t1_start",
            session_id=session_id,
            turn_id=current_turn_id,
            sequence=1,
            event_type="evt.turn.started",
            payload={"wake_kind": "user_message", "user_message": "try again (delete both)"},
            created_offset=300,
        )
        db.commit()

        block = build_recent_events_block(db=db, session_id=session_id, settings=AppSettings())

    assert block is not None
    assert block.startswith("recent_external_events")

    # Canonical message_ids from the prior turn's reads must be present verbatim.
    assert "19c638912663c9e5" in block
    assert "19c63892fa1b4c10" in block
    # Assistant message must be present.
    assert "June Homes just sent an unread balance reminder" in block
    # Loop trace must NOT leak in.
    assert "evt.model.started" not in block
    # Chronological order: prior turn's events appear before the current turn's.
    assert block.index("19c638912663c9e5") < block.index("try again (delete both)")


def test_recent_events_block_returns_none_on_empty_log(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        block = build_recent_events_block(
            db=db, session_id="ses_does_not_exist", settings=AppSettings()
        )
    assert block is None


def test_recent_events_block_compacts_oversize_payload(
    session_factory: sessionmaker[Session],
) -> None:
    """A succeeded event with a 10KB nested body is compacted; canonical IDs
    remain visible so the agent can re-fetch."""
    session_id = "ses_compact_check"
    turn_id = "trn_compact"
    with session_factory() as db:
        _seed_session_and_turn(
            db,
            session_id=session_id,
            turn_id=turn_id,
            user_message="x",
            created_offset=0,
        )
        big_body = "b" * 10_000
        _seed_event(
            db,
            event_id="evt_big",
            session_id=session_id,
            turn_id=turn_id,
            sequence=1,
            event_type="evt.action.execution.succeeded",
            payload={
                "capability_id": "cap.email.read",
                "status": "succeeded",
                "execution_output": {
                    "message": {
                        "message_id": "msg_keep_me",
                        "thread_id": "thr_keep_me",
                        "body": big_body,
                    }
                },
            },
            created_offset=1,
        )
        db.commit()

        block = build_recent_events_block(db=db, session_id=session_id, settings=AppSettings())

    assert block is not None
    assert "msg_keep_me" in block
    assert "thr_keep_me" in block
    assert "_truncated" in block
    # The 10KB body is not present verbatim in the block.
    assert big_body not in block
    # Block stays well under the budget when compacted.
    decoded_lines = [json.loads(line) for line in block.split("\n") if line.startswith("{")]
    assert len(decoded_lines) == 1
