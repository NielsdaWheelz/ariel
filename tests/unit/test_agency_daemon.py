from __future__ import annotations

from pathlib import Path
from typing import Any

from ariel.agency_daemon import (
    AGENCY_EGRESS_POLICY_VERSION,
    AGENCY_SANDBOX_POLICY_VERSION,
    AgencyRuntime,
)


class FakeAgencyClient:
    def __init__(self) -> None:
        self.task_start_payload: dict[str, Any] | None = None
        self.land_calls: list[dict[str, Any]] = []
        self.pr_sync_calls: list[dict[str, Any]] = []

    def task_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.task_start_payload = payload
        return {"task_id": "task_123", "repo_id": "repo_123", "state": "running"}

    def get_invocation(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        assert repo_id == "repo_123"
        assert invocation_ref == "inv_123"
        return {"landing_status": "pending"}

    def land_invocation(
        self,
        *,
        repo_id: str,
        invocation_ref: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        self.land_calls.append(
            {
                "repo_id": repo_id,
                "invocation_ref": invocation_ref,
                "client_request_id": client_request_id,
            }
        )
        return {"request_id": "land_req_123"}

    def worktree_pr_sync(
        self,
        *,
        repo_id: str,
        worktree_ref: str,
        allow_dirty: bool,
        force_with_lease: bool,
        client_request_id: str,
    ) -> dict[str, Any]:
        self.pr_sync_calls.append(
            {
                "repo_id": repo_id,
                "worktree_ref": worktree_ref,
                "allow_dirty": allow_dirty,
                "force_with_lease": force_with_lease,
                "client_request_id": client_request_id,
            }
        )
        return {"pr_url": "https://github.test/acme/repo/pull/1", "request_id": "pr_req_123"}


def test_agency_run_sends_and_returns_policy_metadata(tmp_path: Path) -> None:
    client = FakeAgencyClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(tmp_path),),
        default_base_branch="main",
        default_runner="codex",
    )

    result = runtime.start_run(
        input_payload={
            "repo_root": str(tmp_path / "repo"),
            "name": "Clean up tool surface",
            "prompt": "Use the terminal.",
            "base_branch": None,
            "runner": None,
            "runner_args": [],
            "env": {"SECRET": "redacted"},
            "no_include_untracked": True,
        },
        action_attempt_id="aat_123",
    )

    assert client.task_start_payload is not None
    assert client.task_start_payload["client_request_id"] == "aat_123"
    assert client.task_start_payload["sandbox_policy"] == {
        "version": AGENCY_SANDBOX_POLICY_VERSION,
        "repo_root": str(tmp_path / "repo"),
        "include_untracked": False,
        "runner": "codex",
        "mode": "headless",
        "base_branch": "main",
        "runner_args_count": 0,
        "env_keys": ["SECRET"],
        "env_values_redacted": True,
        "client_request_id": "aat_123",
    }
    assert result["sandbox_policy"] == client.task_start_payload["sandbox_policy"]
    assert "redacted" not in result["sandbox_policy"].values()
    assert result["egress_policy"] == {
        "version": AGENCY_EGRESS_POLICY_VERSION,
        "allowed_destinations": ["agency.daemon.local"],
        "declared_destination": "agency.daemon.local",
        "capability_id": "cap.agency.run",
        "client_request_id": "aat_123",
    }


def test_agency_pr_request_uses_client_request_id(tmp_path: Path) -> None:
    client = FakeAgencyClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(tmp_path),),
        default_base_branch="main",
        default_runner="codex",
    )

    result = runtime.request_pr(
        prepared={
            "job_id": "job_123",
            "repo_id": "repo_123",
            "invocation_id": "inv_123",
            "worktree_id": "wt_123",
            "allow_dirty": False,
            "force_with_lease": True,
            "client_request_id": "aat_pr_123",
            "land_client_request_id": "pwr_123:land",
            "pr_sync_client_request_id": "pwr_123:pr-sync",
        }
    )

    assert result["pr"]["pr_url"] == "https://github.test/acme/repo/pull/1"
    assert client.land_calls == [
        {
            "repo_id": "repo_123",
            "invocation_ref": "inv_123",
            "client_request_id": "pwr_123:land",
        }
    ]
    assert client.pr_sync_calls == [
        {
            "repo_id": "repo_123",
            "worktree_ref": "wt_123",
            "allow_dirty": False,
            "force_with_lease": True,
            "client_request_id": "pwr_123:pr-sync",
        }
    ]
