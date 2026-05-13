from __future__ import annotations

from datetime import datetime, timezone

from ariel.workspace_reasoning import (
    CandidateKind,
    CommitmentCandidate,
    CommitmentOwner,
    CommitmentState,
    EvidenceBlock,
    validate_commitment_candidate,
    validate_lifecycle_transition,
)


def test_commitment_candidate_rejects_missing_evidence_anchor() -> None:
    candidate = CommitmentCandidate(
        kind=CandidateKind.COMMITMENT,
        action_text="Send the invoice",
        owner=CommitmentOwner.USER,
        confidence=0.9,
        evidence_block_ids=(),
    )

    validation = validate_commitment_candidate(candidate, evidence_blocks=())

    assert validation.accepted is False
    assert validation.reason == "missing_evidence_anchor"


def test_commitment_candidate_rejects_unknown_evidence_anchor() -> None:
    source_timestamp = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    candidate = CommitmentCandidate(
        kind=CandidateKind.COMMITMENT,
        action_text="Send the invoice",
        owner=CommitmentOwner.USER,
        confidence=0.9,
        evidence_block_ids=("block-missing",),
    )

    validation = validate_commitment_candidate(
        candidate,
        evidence_blocks=(
            EvidenceBlock(
                block_id="block-1",
                evidence_id="evidence-1",
                source_timestamp=source_timestamp,
            ),
        ),
    )

    assert validation.accepted is False
    assert validation.reason == "unknown_evidence_anchor"


def test_commitment_candidate_rejects_non_finite_confidence() -> None:
    candidate = CommitmentCandidate(
        kind=CandidateKind.COMMITMENT,
        action_text="Send the invoice",
        owner=CommitmentOwner.USER,
        confidence=float("nan"),
        evidence_block_ids=("block-1",),
    )

    validation = validate_commitment_candidate(
        candidate,
        evidence_blocks=(
            EvidenceBlock(
                block_id="block-1",
                evidence_id="evidence-1",
                source_timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            ),
        ),
    )

    assert validation.accepted is False
    assert validation.reason == "confidence_out_of_range"


def test_iso_due_date_is_anchored_to_source_timezone() -> None:
    candidate = CommitmentCandidate(
        kind=CandidateKind.COMMITMENT,
        action_text="Send the launch note",
        owner=CommitmentOwner.USER,
        confidence=0.9,
        evidence_block_ids=("block-1",),
        due_expression="2026-05-11",
    )

    validation = validate_commitment_candidate(
        candidate,
        evidence_blocks=(
            EvidenceBlock(
                block_id="block-1",
                evidence_id="evidence-1",
                source_timestamp=datetime(2026, 5, 10, 23, 30, tzinfo=timezone.utc),
            ),
        ),
    )

    assert validation.accepted is True
    assert validation.due_window is not None
    assert validation.due_window.start_at == datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    assert validation.due_window.end_at == datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc)


def test_unparseable_due_date_is_kept_for_review() -> None:
    candidate = CommitmentCandidate(
        kind=CandidateKind.COMMITMENT,
        action_text="Send the launch note",
        owner=CommitmentOwner.USER,
        confidence=0.9,
        evidence_block_ids=("block-1",),
        due_expression="after the thing settles",
    )

    validation = validate_commitment_candidate(
        candidate,
        evidence_blocks=(
            EvidenceBlock(
                block_id="block-1",
                evidence_id="evidence-1",
                source_timestamp=datetime(2026, 5, 10, 23, 30, tzinfo=timezone.utc),
            ),
        ),
    )

    assert validation.accepted is True
    assert validation.reason == "due_window_unparseable"
    assert validation.due_window is None


def test_vague_due_date_expressions_require_model_structured_due_window() -> None:
    source_timestamp = datetime(2026, 5, 12, 9, 15, tzinfo=timezone.utc)

    validation = validate_commitment_candidate(
        CommitmentCandidate(
            kind=CandidateKind.COMMITMENT,
            action_text="Send the launch note",
            owner=CommitmentOwner.USER,
            confidence=0.9,
            evidence_block_ids=("block-1",),
            due_expression="tonight",
        ),
        evidence_blocks=(EvidenceBlock("block-1", "evidence-1", source_timestamp),),
    )

    assert validation.accepted is True
    assert validation.reason == "due_window_unparseable"
    assert validation.due_window is None


def test_lifecycle_transition_requires_authority_for_resolution() -> None:
    rejected = validate_lifecycle_transition(
        CommitmentState.ACTIVE,
        CommitmentState.RESOLVED,
    )
    accepted = validate_lifecycle_transition(
        CommitmentState.ACTIVE,
        CommitmentState.RESOLVED,
        source_evidence_is_newer=True,
    )

    assert rejected.allowed is False
    assert rejected.reason == "resolution_requires_authority"
    assert accepted.allowed is True
    assert accepted.reason is None
