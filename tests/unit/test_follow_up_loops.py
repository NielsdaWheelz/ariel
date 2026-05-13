from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ariel.workspace_reasoning import (
    CommitmentOwner,
    CommitmentState,
    DueWindow,
    FollowUpAction,
    FollowUpKind,
    FollowUpLoop,
    WorkCommitment,
    evaluate_follow_up,
)


def _commitment(*, state: CommitmentState, due_at: datetime | None) -> WorkCommitment:
    due_window = None
    if due_at is not None:
        due_window = DueWindow(start_at=due_at, end_at=None, source_text=due_at.isoformat())
    return WorkCommitment(
        commitment_id="commitment-1",
        state=state,
        owner=CommitmentOwner.USER,
        action_text="Send the invoice",
        evidence_block_ids=("block-1",),
        due_window=due_window,
    )


def _loop(
    *,
    kind: FollowUpKind,
    now: datetime,
    stale_after: datetime | None = None,
    snoozed_until: datetime | None = None,
    version: int = 1,
    scheduled_version: int = 1,
) -> FollowUpLoop:
    return FollowUpLoop(
        loop_id="loop-1",
        kind=kind,
        commitment_id="commitment-1",
        version=version,
        scheduled_version=scheduled_version,
        scheduled_for=now,
        stale_after=stale_after or now + timedelta(days=7),
        snoozed_until=snoozed_until,
    )


def test_due_date_follow_up_requires_deliberation_when_due_soon() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.ACTIVE, due_at=now + timedelta(hours=4)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_due_date"


def test_due_date_follow_up_requires_deliberation_when_overdue() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.ACTIVE, due_at=now - timedelta(minutes=1)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_due_date"


def test_snoozed_follow_up_no_ops_until_snooze_expires() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    snoozed_until = now + timedelta(hours=2)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.ACTIVE, due_at=now - timedelta(minutes=1)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now, snoozed_until=snoozed_until),
        now=now,
    )

    assert evaluation.action == FollowUpAction.NO_OP
    assert evaluation.reason == "snoozed"
    assert evaluation.next_check_at == snoozed_until


def test_resolved_commitment_no_ops_before_follow_up_notification() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.RESOLVED, due_at=now - timedelta(minutes=1)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.NO_OP
    assert evaluation.reason == "resolved"


def test_stale_loop_no_ops_without_notification() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.ACTIVE, due_at=now - timedelta(minutes=1)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now, version=2, scheduled_version=1),
        now=now,
    )

    assert evaluation.action == FollowUpAction.NO_OP
    assert evaluation.reason == "stale_loop"


def test_candidate_commitment_no_ops_in_due_date_follow_up() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.CANDIDATE, due_at=now - timedelta(minutes=1)),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.NO_OP
    assert evaluation.reason == "candidate"


def test_needs_review_commitment_no_ops_in_due_date_follow_up() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(
            state=CommitmentState.NEEDS_REVIEW,
            due_at=now - timedelta(minutes=1),
        ),
        loop=_loop(kind=FollowUpKind.DUE_DATE, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.NO_OP
    assert evaluation.reason == "needs_review"


def test_waiting_on_user_follow_up_requires_deliberation_when_overdue() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(
            state=CommitmentState.WAITING_ON_USER,
            due_at=now - timedelta(minutes=1),
        ),
        loop=_loop(kind=FollowUpKind.WAITING_ON_USER, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_waiting_on_user"


def test_waiting_on_counterparty_follow_up_requires_deliberation_when_due_soon() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(
            state=CommitmentState.WAITING_ON_COUNTERPARTY,
            due_at=now + timedelta(hours=1),
        ),
        loop=_loop(kind=FollowUpKind.WAITING_ON_COUNTERPARTY, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_waiting_on_counterparty"


def test_waiting_on_user_follow_up_requires_deliberation_without_due_window() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.WAITING_ON_USER, due_at=None),
        loop=_loop(kind=FollowUpKind.WAITING_ON_USER, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_waiting_on_user"


def test_waiting_on_counterparty_follow_up_requires_deliberation_without_due_window() -> None:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    evaluation = evaluate_follow_up(
        commitment=_commitment(state=CommitmentState.WAITING_ON_COUNTERPARTY, due_at=None),
        loop=_loop(kind=FollowUpKind.WAITING_ON_COUNTERPARTY, now=now),
        now=now,
    )

    assert evaluation.action == FollowUpAction.DELIBERATE
    assert evaluation.reason == "scheduled_waiting_on_counterparty"
