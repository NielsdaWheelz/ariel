from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariel.executor import ExecutionResult
from ariel.persistence import ActionAttemptRecord, JobEventRecord, JobRecord


class AgencyDaemonError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AgencyDaemonClient:
    socket_path: str
    timeout_seconds: float

    def health(self) -> dict[str, Any]:
        payload = self._request("GET", "/health")
        if payload.get("ok") is not True or payload.get("api_version") != 2:
            raise AgencyDaemonError("agency daemon health check failed")
        return payload

    def task_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._action("POST", "/tasks/start", json_payload=payload)

    def get_task(self, *, repo_id: str, task_ref: str) -> dict[str, Any]:
        return self._data("GET", f"/tasks/{task_ref}", params={"repo_id": repo_id})

    def get_invocation(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        return self._data("GET", f"/invocations/{invocation_ref}", params={"repo_id": repo_id})

    def get_invocation_check(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        return self._data(
            "GET",
            f"/invocations/{invocation_ref}/check",
            params={"repo_id": repo_id},
        )

    def get_invocation_diff(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        return self._data(
            "GET",
            f"/invocations/{invocation_ref}/diff",
            params={"repo_id": repo_id, "include_patch": "false"},
        )

    def get_invocation_timeline(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        return self._data(
            "GET",
            f"/invocations/{invocation_ref}/timeline",
            params={"repo_id": repo_id, "order": "desc", "limit": "20"},
        )

    def land_invocation(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        return self._action(
            "POST",
            f"/invocations/{invocation_ref}/land",
            params={"repo_id": repo_id},
            json_payload={"apply": True},
        )

    def worktree_pr_sync(
        self,
        *,
        repo_id: str,
        worktree_ref: str,
        allow_dirty: bool,
        force_with_lease: bool,
    ) -> dict[str, Any]:
        return self._action(
            "POST",
            f"/worktrees/{worktree_ref}/pr/sync",
            params={"repo_id": repo_id},
            json_payload={
                "allow_dirty": allow_dirty,
                "force_with_lease": force_with_lease,
            },
        )

    def _action(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._request(method, path, params=params, json_payload=json_payload)
        if payload.get("ok") is not True:
            raise AgencyDaemonError(str(payload.get("message") or "agency daemon action failed"))
        return payload

    def _data(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._request(method, path, params=params)
        if payload.get("ok") is not True:
            raise AgencyDaemonError(str(payload.get("message") or "agency daemon read failed"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AgencyDaemonError("agency daemon returned invalid data")
        return data

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transport = httpx.HTTPTransport(uds=self.socket_path)
        try:
            with httpx.Client(
                transport=transport,
                base_url="http://agency-daemon",
                timeout=self.timeout_seconds,
            ) as client:
                response = client.request(method, path, params=params, json=json_payload)
        except httpx.TimeoutException as exc:
            raise AgencyDaemonError("agency daemon request timed out") from exc
        except httpx.HTTPError as exc:
            raise AgencyDaemonError("agency daemon request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AgencyDaemonError("agency daemon returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AgencyDaemonError("agency daemon returned invalid payload")
        if response.status_code < 200 or response.status_code >= 300:
            message = payload.get("message") if isinstance(payload.get("message"), str) else None
            raise AgencyDaemonError(message or f"agency daemon returned HTTP {response.status_code}")
        return payload


@dataclass(frozen=True, slots=True)
class AgencyRuntime:
    client: AgencyDaemonClient
    allowed_repo_roots: tuple[str, ...]
    default_base_branch: str
    default_runner: str

    def execute_capability(
        self,
        *,
        db: Session,
        capability_id: str,
        normalized_input: dict[str, Any],
        action_attempt: ActionAttemptRecord,
        session_id: str,
        turn_id: str,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> ExecutionResult:
        try:
            if capability_id == "cap.agency.run":
                return ExecutionResult(
                    status="succeeded",
                    output=self._run(
                        db=db,
                        input_payload=normalized_input,
                        action_attempt=action_attempt,
                        session_id=session_id,
                        turn_id=turn_id,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    ),
                    error=None,
                )
            if capability_id == "cap.agency.status":
                return ExecutionResult(
                    status="succeeded",
                    output=self._status(db=db, input_payload=normalized_input, now_fn=now_fn),
                    error=None,
                )
            if capability_id == "cap.agency.artifacts":
                return ExecutionResult(
                    status="succeeded",
                    output=self._artifacts(input_payload=normalized_input, db=db),
                    error=None,
                )
            if capability_id == "cap.agency.request_pr":
                return ExecutionResult(
                    status="succeeded",
                    output=self._request_pr(db=db, input_payload=normalized_input, now_fn=now_fn),
                    error=None,
                )
        except AgencyDaemonError as exc:
            return ExecutionResult(status="failed", output=None, error=str(exc))
        return ExecutionResult(status="failed", output=None, error="unknown_agency_capability")

    def _run(
        self,
        *,
        db: Session,
        input_payload: dict[str, Any],
        action_attempt: ActionAttemptRecord,
        session_id: str,
        turn_id: str,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> dict[str, Any]:
        repo_root = self._allowed_repo_root(input_payload["repo_root"])
        response = self.client.task_start(
            {
                "repo_root": repo_root,
                "name": input_payload["name"],
                "base_branch": input_payload.get("base_branch") or self.default_base_branch,
                "mode": "headless",
                "runner": input_payload.get("runner") or self.default_runner,
                "prompt": input_payload["prompt"],
                "invocation_name": input_payload["name"],
                "runner_args": input_payload.get("runner_args", []),
                "env": input_payload.get("env", {}),
                "client_request_id": action_attempt.id,
                "no_include_untracked": input_payload.get("no_include_untracked", False),
            }
        )
        task_id = self._required_text(response, "task_id")
        now = now_fn()
        job = db.scalar(
            select(JobRecord)
            .where(
                JobRecord.source == "agency.daemon",
                JobRecord.external_job_id == task_id,
            )
            .with_for_update()
            .limit(1)
        )
        if job is None:
            job = JobRecord(
                id=new_id_fn("job"),
                session_id=session_id,
                turn_id=turn_id,
                action_attempt_id=action_attempt.id,
                source="agency.daemon",
                external_job_id=task_id,
                title=self._optional_text(response, "task_name") or input_payload["name"],
                status=self._job_status(str(response.get("state") or "running")),
                summary=None,
                latest_payload=response,
                agency_repo_root=repo_root,
                agency_repo_id=self._optional_text(response, "repo_id"),
                agency_task_id=task_id,
                agency_invocation_id=self._optional_text(response, "invocation_id"),
                agency_worktree_id=self._optional_text(response, "worktree_id"),
                agency_worktree_path=self._optional_text(response, "worktree_path"),
                agency_branch=self._optional_text(response, "branch"),
                agency_runner=self._optional_text(response, "runner"),
                agency_request_id=self._optional_text(response, "request_id"),
                agency_last_synced_at=now,
                agency_pr_number=None,
                agency_pr_url=None,
                discord_thread_id=None,
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            db.flush()
        else:
            self._update_job_from_payload(job, response, now=now)
        self._record_job_event(
            db=db,
            job=job,
            event_type="agency.task.started",
            payload=response,
            now=now,
            new_id_fn=new_id_fn,
        )
        return {"job_id": job.id, "agency": response}

    def _status(
        self,
        *,
        db: Session,
        input_payload: dict[str, Any],
        now_fn: Callable[[], datetime],
    ) -> dict[str, Any]:
        job = self._job_for_input(db=db, input_payload=input_payload)
        repo_id = self._lookup_text(job, input_payload, "agency_repo_id", "repo_id")
        task_id = self._lookup_text(job, input_payload, "agency_task_id", "task_id")
        task = self.client.get_task(repo_id=repo_id, task_ref=task_id)
        invocation_id = self._optional_text(task, "primary_invocation_id") or (
            job.agency_invocation_id if job is not None else None
        )
        invocation = None
        check = None
        if invocation_id is not None:
            invocation = self.client.get_invocation(repo_id=repo_id, invocation_ref=invocation_id)
            check = self.client.get_invocation_check(repo_id=repo_id, invocation_ref=invocation_id)
        if job is not None:
            self._update_job_from_payload(job, task, now=now_fn())
        return {
            "job_id": job.id if job is not None else None,
            "task": task,
            "invocation": invocation,
            "check": check,
        }

    def _artifacts(self, *, input_payload: dict[str, Any], db: Session) -> dict[str, Any]:
        job = self._job_for_input(db=db, input_payload=input_payload)
        repo_id = self._lookup_text(job, input_payload, "agency_repo_id", "repo_id")
        task_id = self._lookup_text(job, input_payload, "agency_task_id", "task_id")
        invocation_id = job.agency_invocation_id if job is not None else None
        if invocation_id is None:
            task = self.client.get_task(repo_id=repo_id, task_ref=task_id)
            invocation_id = self._required_text(task, "primary_invocation_id")
        return {
            "job_id": job.id if job is not None else None,
            "diff": self.client.get_invocation_diff(repo_id=repo_id, invocation_ref=invocation_id),
            "timeline": self.client.get_invocation_timeline(
                repo_id=repo_id,
                invocation_ref=invocation_id,
            ),
        }

    def _request_pr(
        self,
        *,
        db: Session,
        input_payload: dict[str, Any],
        now_fn: Callable[[], datetime],
    ) -> dict[str, Any]:
        job = self._job_for_input(db=db, input_payload=input_payload)
        if job is None:
            raise AgencyDaemonError("agency job is not tracked")
        repo_id = self._lookup_text(job, input_payload, "agency_repo_id", "repo_id")
        invocation_id = input_payload.get("invocation_id") or job.agency_invocation_id
        worktree_id = input_payload.get("worktree_id") or job.agency_worktree_id
        if not isinstance(invocation_id, str) or not invocation_id:
            raise AgencyDaemonError("agency invocation id is missing")
        if not isinstance(worktree_id, str) or not worktree_id:
            raise AgencyDaemonError("agency worktree id is missing")
        if job.agency_repo_root is not None:
            self._allowed_repo_root(job.agency_repo_root)
        invocation = self.client.get_invocation(repo_id=repo_id, invocation_ref=invocation_id)
        land_response = None
        if invocation.get("landing_status") == "pending":
            land_response = self.client.land_invocation(
                repo_id=repo_id,
                invocation_ref=invocation_id,
            )
        pr_response = self.client.worktree_pr_sync(
            repo_id=repo_id,
            worktree_ref=worktree_id,
            allow_dirty=bool(input_payload.get("allow_dirty")),
            force_with_lease=bool(input_payload.get("force_with_lease")),
        )
        now = now_fn()
        job.agency_pr_number = (
            int(pr_response["pr_number"]) if isinstance(pr_response.get("pr_number"), int) else None
        )
        job.agency_pr_url = self._optional_text(pr_response, "pr_url")
        job.agency_request_id = self._optional_text(pr_response, "request_id") or job.agency_request_id
        job.agency_last_synced_at = now
        job.latest_payload = {"land": land_response, "pr": pr_response}
        job.updated_at = now
        return {"job_id": job.id, "land": land_response, "pr": pr_response}

    def _allowed_repo_root(self, repo_root: str) -> str:
        if not self.allowed_repo_roots:
            raise AgencyDaemonError("agency allowed repo roots are not configured")
        candidate = Path(repo_root).expanduser().resolve(strict=False)
        for allowed_raw in self.allowed_repo_roots:
            allowed = Path(allowed_raw).expanduser().resolve(strict=False)
            if candidate == allowed or allowed in candidate.parents:
                return str(candidate)
        raise AgencyDaemonError("agency repo root is not allowlisted")

    def _job_for_input(self, *, db: Session, input_payload: dict[str, Any]) -> JobRecord | None:
        job_id = input_payload.get("job_id")
        if isinstance(job_id, str) and job_id:
            return db.scalar(select(JobRecord).where(JobRecord.id == job_id).limit(1))
        task_id = input_payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            return db.scalar(
                select(JobRecord)
                .where(
                    JobRecord.source == "agency.daemon",
                    JobRecord.agency_task_id == task_id,
                )
                .limit(1)
            )
        return None

    def _lookup_text(
        self,
        job: JobRecord | None,
        input_payload: dict[str, Any],
        job_attr: str,
        input_key: str,
    ) -> str:
        candidate = getattr(job, job_attr) if job is not None else None
        if isinstance(candidate, str) and candidate:
            return candidate
        raw_value = input_payload.get(input_key)
        if isinstance(raw_value, str) and raw_value:
            return raw_value
        raise AgencyDaemonError(f"agency {input_key} is missing")

    def _update_job_from_payload(self, job: JobRecord, payload: dict[str, Any], *, now: datetime) -> None:
        job.status = self._job_status(str(payload.get("state") or job.status))
        job.title = self._optional_text(payload, "task_name") or self._optional_text(payload, "name") or job.title
        job.latest_payload = payload
        job.agency_repo_id = self._optional_text(payload, "repo_id") or job.agency_repo_id
        job.agency_task_id = self._optional_text(payload, "task_id") or job.agency_task_id
        job.agency_invocation_id = (
            self._optional_text(payload, "invocation_id")
            or self._optional_text(payload, "primary_invocation_id")
            or job.agency_invocation_id
        )
        job.agency_worktree_id = self._optional_text(payload, "worktree_id") or job.agency_worktree_id
        job.agency_worktree_path = self._optional_text(payload, "worktree_path") or job.agency_worktree_path
        job.agency_branch = self._optional_text(payload, "branch") or job.agency_branch
        job.agency_runner = self._optional_text(payload, "runner") or job.agency_runner
        job.agency_request_id = self._optional_text(payload, "request_id") or job.agency_request_id
        job.agency_last_synced_at = now
        job.updated_at = now

    def _record_job_event(
        self,
        *,
        db: Session,
        job: JobRecord,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
        new_id_fn: Callable[[str], str],
    ) -> None:
        db.add(
            JobEventRecord(
                id=new_id_fn("jev"),
                job_id=job.id,
                agency_event_id=None,
                event_type=event_type,
                payload=payload,
                created_at=now,
            )
        )

    @staticmethod
    def _job_status(state: str) -> str:
        normalized = state.strip().lower()
        if normalized in {"queued", "pending", "starting"}:
            return "queued"
        if normalized in {"running", "active"}:
            return "running"
        if normalized in {"waiting", "waiting_approval"}:
            return "waiting_approval"
        if normalized in {"succeeded", "completed", "done"}:
            return "succeeded"
        if normalized in {"cancelled", "canceled"}:
            return "cancelled"
        if normalized in {"timed_out", "timeout"}:
            return "timed_out"
        return "failed" if normalized == "failed" else "running"

    @staticmethod
    def _optional_text(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _required_text(self, payload: dict[str, Any], key: str) -> str:
        value = self._optional_text(payload, key)
        if value is None:
            raise AgencyDaemonError(f"agency response missing {key}")
        return value
