from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ariel.agency_daemon import AgencyRuntime
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.persistence import ActionAttemptRecord, JobRecord, TurnRecord


NOW = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)


class FakeAgencyReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_task(self, *, repo_id: str, task_ref: str) -> dict[str, Any]:
        self.calls.append(("task", repo_id, task_ref))
        return {
            "repo_id": "repo_1",
            "task_id": "task_1",
            "state": "completed",
            "task_name": "Updated smoke job",
            "primary_invocation_id": "inv_1",
            "worktree_id": "wt_1",
            "branch": "smoke/branch",
        }

    def get_invocation(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        self.calls.append(("invocation", repo_id, invocation_ref))
        return {"invocation_id": invocation_ref, "state": "completed"}

    def get_invocation_check(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        self.calls.append(("check", repo_id, invocation_ref))
        return {"status": "passed", "invocation_id": invocation_ref}

    def get_invocation_diff(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        self.calls.append(("diff", repo_id, invocation_ref))
        return {"files_changed": 2, "invocation_id": invocation_ref}

    def get_invocation_timeline(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        self.calls.append(("timeline", repo_id, invocation_ref))
        return {"events": [{"kind": "completed"}], "invocation_id": invocation_ref}


def _seed_action_attempt(
    db: Session,
    *,
    capability_id: str,
    action_attempt_id: str,
    proposal_index: int,
    proposed_input: dict[str, Any],
) -> ActionAttemptRecord:
    capability = get_capability(capability_id)
    assert capability is not None
    attempt = ActionAttemptRecord(
        id=action_attempt_id,
        turn_id="trn_agency_reads",
        proposal_index=proposal_index,
        capability_id=capability.capability_id,
        capability_version=capability.version,
        capability_contract_hash=capability_contract_hash(capability),
        impact_level=capability.impact_level,
        proposed_input=proposed_input,
        payload_hash=payload_hash(
            canonical_action_payload(
                capability_id=capability.capability_id,
                input_payload=proposed_input,
            )
        ),
        policy_decision="allow_inline",
        policy_reason=None,
        status="executing",
        approval_required=False,
        execution_output=None,
        execution_error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(attempt)
    return attempt


def test_agency_status_and_artifacts_execute_against_daemon_and_update_job(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeAgencyReadClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    with session_factory() as db:
        with db.begin():
            db.add(
                TurnRecord(
                    id="trn_agency_reads",
                    user_message="check agency job",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                JobRecord(
                    id="job_agency_reads",
                    turn_id="trn_agency_reads",
                    action_attempt_id=None,
                    source="agency.daemon",
                    external_job_id="task_1",
                    title="Original smoke job",
                    status="running",
                    summary=None,
                    latest_payload={"state": "running"},
                    agency_repo_root=str(Path.cwd()),
                    agency_repo_id="repo_1",
                    agency_task_id="task_1",
                    agency_invocation_id=None,
                    agency_worktree_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            status_attempt = _seed_action_attempt(
                db,
                capability_id="cap.agency.status",
                action_attempt_id="aat_agency_status",
                proposal_index=1,
                proposed_input={"job_id": "job_agency_reads"},
            )
            artifacts_attempt = _seed_action_attempt(
                db,
                capability_id="cap.agency.artifacts",
                action_attempt_id="aat_agency_artifacts",
                proposal_index=2,
                proposed_input={"job_id": "job_agency_reads"},
            )

            status = runtime.execute_capability(
                db=db,
                capability_id="cap.agency.status",
                normalized_input={"job_id": "job_agency_reads"},
                action_attempt=status_attempt,
                turn_id="trn_agency_reads",
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_agency_reads",
            )
            artifacts = runtime.execute_capability(
                db=db,
                capability_id="cap.agency.artifacts",
                normalized_input={"job_id": "job_agency_reads"},
                action_attempt=artifacts_attempt,
                turn_id="trn_agency_reads",
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_agency_reads",
            )

        job = db.get(JobRecord, "job_agency_reads")

    assert status.status == "succeeded"
    assert status.error is None
    assert status.output is not None
    assert status.output["job_id"] == "job_agency_reads"
    assert status.output["task"]["state"] == "completed"
    assert status.output["invocation"] == {"invocation_id": "inv_1", "state": "completed"}
    assert status.output["check"] == {"status": "passed", "invocation_id": "inv_1"}
    assert artifacts.status == "succeeded"
    assert artifacts.error is None
    assert artifacts.output is not None
    assert artifacts.output["job_id"] == "job_agency_reads"
    assert artifacts.output["diff"] == {"files_changed": 2, "invocation_id": "inv_1"}
    assert artifacts.output["timeline"] == {
        "events": [{"kind": "completed"}],
        "invocation_id": "inv_1",
    }
    assert job is not None
    assert job.status == "succeeded"
    assert job.title == "Updated smoke job"
    assert job.agency_invocation_id == "inv_1"
    assert job.agency_worktree_id == "wt_1"
    assert job.agency_branch == "smoke/branch"
    assert client.calls == [
        ("task", "repo_1", "task_1"),
        ("invocation", "repo_1", "inv_1"),
        ("check", "repo_1", "inv_1"),
        ("diff", "repo_1", "inv_1"),
        ("timeline", "repo_1", "inv_1"),
    ]
