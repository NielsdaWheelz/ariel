from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_message, responses_run_message
from ariel.config import AppSettings
from ariel.persistence import JobRecord
from ariel.worker import (
    claim_next_task,
    enqueue_background_task,
    process_one_task,
    reap_stale_tasks,
)


@dataclass
class DurableWorkflowAdapter:
    provider: str = "provider.discord-primary-durable"
    model: str = "model.discord-primary-durable-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history
        if context_bundle.get("origin") == "ambient_interpretation":
            return responses_message(
                assistant_text=json.dumps(
                    _ambient_interpretation_fixture(input_items), sort_keys=True
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_ambient_interpretation_123",
                input_tokens=8,
                output_tokens=5,
            )
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_discord_primary_durable_123",
            input_tokens=8,
            output_tokens=5,
        )


def _first_ambient_candidate(input_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in input_items:
        content = item.get("content")
        if item.get("role") == "system" and isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                continue
            candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
            if isinstance(candidates, list):
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        return candidate
    raise AssertionError("ambient candidate not provided")


def _ambient_interpretation_fixture(input_items: list[dict[str, Any]]) -> dict[str, Any]:
    candidate = _first_ambient_candidate(input_items)
    source_type = str(candidate["source_type"])
    source_id = str(candidate["source_id"])
    if source_type == "workspace_item":
        observation = {
            "candidate_id": candidate["candidate_id"],
            "observation_key": "calendar-design-review",
            "case_key": f"workspace-item:{source_id}",
            "observation_type": "workspace_delta",
            "subject": "Design review changed",
            "summary": "The calendar design review changed.",
            "payload": {"workspace_status": "active"},
            "evidence": {"workspace_item_id": source_id},
            "rationale": "The changed calendar event may affect planning.",
        }
    else:
        observation = {
            "candidate_id": candidate["candidate_id"],
            "observation_key": "selected",
            "case_key": f"{source_type}:{source_id}",
            "observation_type": "job_state" if source_type == "job" else "ambient_event",
            "subject": f"{source_type} needs deliberation",
            "summary": "The ambient interpreter selected this candidate.",
            "payload": {"source_type": source_type},
            "evidence": {"job_id": source_id} if source_type == "job" else {"source_id": source_id},
            "rationale": "The model selected this ambient candidate for proactive deliberation.",
        }
    return {
        "observations": [observation],
        "omitted": [],
        "rationale": "One ambient candidate matters.",
    }


def _feedback_learning_fixture(context_bundle: dict[str, Any]) -> dict[str, Any]:
    model_input = context_bundle.get("model_input")
    feedback_context: dict[str, Any] = {}
    if isinstance(model_input, list):
        for item in model_input:
            content = item.get("content") if isinstance(item, dict) else None
            if not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("feedback"), dict):
                feedback_context = parsed
                break
    raw_feedback = feedback_context.get("feedback") if isinstance(feedback_context, dict) else {}
    feedback = raw_feedback if isinstance(raw_feedback, dict) else {}
    raw_case = feedback_context.get("case") if isinstance(feedback_context, dict) else {}
    case = raw_case if isinstance(raw_case, dict) else {}
    return {
        "learning_records": [
            {
                "record_type": "example",
                "content": {
                    "case_id": case.get("id"),
                    "feedback_type": feedback.get("feedback_type"),
                    "note": feedback.get("note"),
                    "instruction": "Use this feedback as a proactive correction example.",
                },
            }
        ]
    }


@dataclass
class ProactiveDecisionAdapter:
    decision: dict[str, Any]
    provider: str = "provider.proactive-deliberation"
    model: str = "model.proactive-deliberation-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history
        if context_bundle.get("origin") == "ambient_interpretation":
            return responses_message(
                assistant_text=json.dumps(
                    _ambient_interpretation_fixture(input_items), sort_keys=True
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_ambient_interpretation_123",
                input_tokens=32,
                output_tokens=24,
            )
        if context_bundle.get("origin") == "feedback_learning":
            return responses_message(
                assistant_text=json.dumps(
                    _feedback_learning_fixture(context_bundle), sort_keys=True
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_feedback_learning_123",
                input_tokens=32,
                output_tokens=24,
            )
        if context_bundle.get("origin") == "proactive":
            return responses_message(
                assistant_text=json.dumps(self.decision, sort_keys=True),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_proactive_deliberation_123",
                input_tokens=32,
                output_tokens=24,
            )
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_proactive_chat_123",
            input_tokens=8,
            output_tokens=5,
        )


@dataclass(frozen=True)
class SignedAgencyBody:
    body: bytes
    headers: dict[str, str]


@dataclass
class FrozenClock:
    timestamp: int

    def __call__(self) -> float:
        return float(self.timestamp)


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _count_rows(client: TestClient, table_name: str) -> int:
    with _session_factory(client)() as db:
        with db.begin():
            result = (
                db.execute(text(f"SELECT COUNT(*) AS count FROM {table_name}")).mappings().one()
            )
            return int(result["count"])


def _signed_agency_body(
    payload: dict[str, Any], *, secret: str, timestamp: int
) -> SignedAgencyBody:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return SignedAgencyBody(
        body=body,
        headers={
            "content-type": "application/json",
            "X-Ariel-Agency-Timestamp": str(timestamp),
            "X-Ariel-Agency-Signature": f"sha256={signature}",
        },
    )


def _seed_job(client: TestClient, *, job_id: str, status: str, now: datetime) -> None:
    with _session_factory(client)() as db:
        with db.begin():
            db.add(
                JobRecord(
                    id=job_id,
                    source="agency.local",
                    external_job_id=f"external-{job_id}",
                    title="Proactive cutover",
                    status=status,
                    summary="Proactive worker should notice this job.",
                    latest_payload={"status": status},
                    created_at=now - timedelta(minutes=5),
                    updated_at=now,
                )
            )


def test_background_tasks_claim_retry_and_reap_are_durable_and_worker_safe(
    postgres_url: str,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    adapter = DurableWorkflowAdapter()
    with _build_client(postgres_url, adapter) as client:
        with _session_factory(client)() as db:
            with db.begin():
                first = enqueue_background_task(
                    db,
                    task_type="reap_stale_tasks",
                    payload={},
                    now=now,
                )
                enqueue_background_task(
                    db,
                    task_type="reap_stale_tasks",
                    payload={},
                    now=now + timedelta(minutes=20),
                )

            with db.begin():
                claimed = claim_next_task(db, worker_id="worker-a", now=now)
                assert claimed is not None
                assert claimed.id == first.id
                assert claimed.status == "running"
                assert claimed.attempts == 1
                assert claim_next_task(db, worker_id="worker-b", now=now) is None

            with db.begin():
                assert (
                    reap_stale_tasks(
                        db, now=now + timedelta(minutes=10), heartbeat_timeout_seconds=60
                    )
                    == 1
                )

            with db.begin():
                retried = claim_next_task(db, worker_id="worker-c", now=now + timedelta(minutes=10))
                assert retried is not None
                assert retried.id == first.id
                assert retried.status == "running"
                assert retried.attempts == 2

        assert _count_rows(client, "background_tasks") == 2


def test_agency_event_ingress_is_signed_idempotent_and_rejects_conflicts(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload = {
        "source": "agency.local",
        "event_id": "agency-event-001",
        "event_type": "job.completed",
        "external_job_id": "agency-job-001",
        "title": "Discord-primary cutover",
        "summary": "Implementation finished.",
        "payload": {"branch": "main"},
    }

    adapter = DurableWorkflowAdapter()
    with _build_client(postgres_url, adapter) as client:
        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        first = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False

        replay = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True

        changed = _signed_agency_body(
            {**payload, "summary": "Different payload."},
            secret=secret,
            timestamp=timestamp,
        )
        conflict = client.post("/v1/agency/events", content=changed.body, headers=changed.headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_AGENCY_EVENT_CONFLICT"

        assert _count_rows(client, "agency_events") == 1
        assert _count_rows(client, "background_tasks") == 1


def test_agency_event_worker_creates_job_event_and_proactive_case_once(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_100
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload = {
        "source": "agency.local",
        "event_id": "agency-event-002",
        "event_type": "job.completed",
        "external_job_id": "agency-job-002",
        "title": "Agency bridge",
        "summary": "Agency bridge finished.",
        "payload": {"artifact": "pr"},
    }

    adapter = DurableWorkflowAdapter()
    with _build_client(postgres_url, adapter) as client:
        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert response.status_code == 202

        settings = cast(Any, AppSettings)(_env_file=None)
        assert (
            process_one_task(
                session_factory=_session_factory(client),
                settings=settings,
                worker_id="worker-a",
                model_adapter=adapter,
            )
            is True
        )
        assert (
            process_one_task(
                session_factory=_session_factory(client),
                settings=settings,
                worker_id="worker-a",
                model_adapter=adapter,
            )
            is True
        )

        assert _count_rows(client, "jobs") == 1
        assert _count_rows(client, "job_events") == 1
        assert _count_rows(client, "proactive_observations") == 1
        assert _count_rows(client, "proactive_cases") == 1
        assert _count_rows(client, "notifications") == 0
        assert _count_rows(client, "notification_deliveries") == 0

        with _session_factory(client)() as db:
            with db.begin():
                job_id = str(db.execute(text("SELECT id FROM jobs")).scalar_one())

        job = client.get(f"/v1/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["job"]["status"] == "succeeded"

        events = client.get(f"/v1/jobs/{job_id}/events")
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()["events"]] == ["job.completed"]

        cases = client.get("/v1/proactive/cases")
        assert cases.status_code == 200
        assert cases.json()["cases"][0]["case_key"] == f"job:{job_id}"


def test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN", "provider-token")
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        headers = {
            "X-Goog-Channel-Token": "provider-token",
            "X-Goog-Channel-ID": "channel-1",
            "X-Goog-Message-Number": "42",
            "X-Goog-Resource-State": "exists",
            "content-type": "application/json",
        }
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["events"]}',
        )
        assert response.status_code == 202
        assert response.json()["duplicate"] is False

        duplicate = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["events"]}',
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["duplicate"] is True

        conflict = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["different"]}',
        )
        assert conflict.status_code == 409

        listed = client.get("/v1/provider-events")
        assert listed.status_code == 200
        assert len(listed.json()["events"]) == 1

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-provider",
        )
        with _session_factory(client)() as db:
            with db.begin():
                task_type = db.execute(
                    text(
                        "SELECT task_type FROM background_tasks "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "provider_sync_due"


def test_google_calendar_sync_persists_provider_evidence_without_ambient_case(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGoogleWorkspaceProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {
                "nextSyncToken": "sync-token-2",
                "items": [
                    {
                        "id": "event-1",
                        "summary": "Design review",
                        "description": "Please prepare design review notes by Friday.",
                        "status": "confirmed",
                        "updated": "2026-04-30T12:00:00Z",
                        "start": {"dateTime": "2026-05-01T17:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-05-01T17:30:00Z", "timeZone": "UTC"},
                        "htmlLink": "https://calendar.google.com/event?eid=event-1",
                    }
                ],
            }

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeGoogleWorkspaceProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeGoogleWorkspaceProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        with _session_factory(client)() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="provider_sync_due",
                    payload={
                        "provider": "google",
                        "resource_type": "calendar",
                        "resource_id": "primary",
                    },
                    now=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
                )

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-sync",
        )
        sync_runs = client.get("/v1/sync-runs")
        assert sync_runs.status_code == 200
        assert sync_runs.json()["sync_runs"][0]["status"] == "succeeded"
        assert sync_runs.json()["sync_runs"][0]["item_count"] == 1

        workspace_items = client.get("/v1/workspace-items")
        assert workspace_items.status_code == 200
        assert workspace_items.json()["workspace_items"] == []

        observations = client.get("/v1/proactive/observations", params={"status": "linked"})
        assert observations.status_code == 200
        assert observations.json()["observations"] == []

        cases = client.get("/v1/proactive/cases")
        assert cases.status_code == 200
        assert cases.json()["cases"] == []

        with _session_factory(client)() as db:
            with db.begin():
                evidence = (
                    db.execute(
                        text(
                            "SELECT id, source_kind, external_id, calendar_id, source_uri, "
                            "extraction_status, lifecycle_state "
                            "FROM provider_evidence "
                            "WHERE source_kind = 'calendar_event' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert evidence["external_id"] == "event-1"
                assert evidence["calendar_id"] == "primary"
                assert evidence["source_uri"] == "https://calendar.google.com/event?eid=event-1"
                assert evidence["extraction_status"] == "pending"
                assert evidence["lifecycle_state"] == "available"

                pending_tasks = (
                    db.execute(
                        text(
                            "SELECT task_type, payload FROM background_tasks "
                            "WHERE status = 'pending' "
                            "ORDER BY created_at ASC"
                        )
                    )
                    .mappings()
                    .all()
                )
                assert {
                    "task_type": "workspace_commitment_extraction_due",
                    "payload": {"evidence_id": evidence["id"]},
                } in [dict(task) for task in pending_tasks]
                assert all(
                    task["task_type"] != "ambient_interpretation_due" for task in pending_tasks
                )


def test_proactive_interpretation_deliberates_speaks_and_acknowledges_case_and_turn(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.app._utcnow", lambda: now)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    adapter = ProactiveDecisionAdapter(
        {
            "decision": "speak_now",
            "confidence": 0.93,
            "urgency": "high",
            "user_visible_message": "Sir, you should review this approval now.",
            "rationale": "The job is waiting on the user.",
            "evidence_refs": ["latest_observation"],
            "tool_refs": [],
            "actions": [],
            "follow_up": None,
        }
    )

    with _build_client(postgres_url, adapter) as client:
        _seed_job(client, job_id="job_proactive_ack", status="waiting_approval", now=now)
        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        observations = client.get("/v1/proactive/observations", params={"status": "linked"})
        assert observations.status_code == 200
        observation = observations.json()["observations"][0]
        assert observation["source_type"] == "job"
        assert observation["source_id"] == "job_proactive_ack"
        assert observation["evidence"]["job_id"] == "job_proactive_ack"

        cases = client.get("/v1/proactive/cases", params={"status": "open"})
        assert cases.status_code == 200
        proactive_case = cases.json()["cases"][0]
        assert proactive_case["case_key"] == "job:job_proactive_ack"
        assert proactive_case["latest_observation_id"] == observation["id"]

        with _session_factory(client)() as db:
            with db.begin():
                task_type = db.execute(
                    text(
                        "SELECT task_type FROM background_tasks "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "proactive_deliberation_due"

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        context_snapshots = client.get(
            f"/v1/proactive/cases/{proactive_case['id']}/context-snapshots"
        )
        assert context_snapshots.status_code == 200
        context = context_snapshots.json()["context_snapshots"][0]["context"]
        assert context["latest_observation"]["id"] == observation["id"]
        assert context["latest_observation"]["source_type"] == "job"

        decisions = client.get(f"/v1/proactive/cases/{proactive_case['id']}/decisions")
        assert decisions.status_code == 200
        decision = decisions.json()["decisions"][0]
        assert decision["decision_type"] == "speak_now"
        assert decision["status"] == "executed"
        assert decision["user_visible_message"] == "Sir, you should review this approval now."

        with _session_factory(client)() as db:
            with db.begin():
                judgment = (
                    db.execute(
                        text(
                            "SELECT status, model, prompt_version, provider_response_id, "
                            "parse_status, validation_status "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'proactive_deliberation' "
                            "AND source_id = :case_id "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"case_id": proactive_case["id"]},
                    )
                    .mappings()
                    .one()
                )
                assert judgment == {
                    "status": "succeeded",
                    "model": "model.proactive-deliberation-v1",
                    "prompt_version": "proactive-ai-deliberation-v1",
                    "provider_response_id": "resp_proactive_deliberation_123",
                    "parse_status": "parsed",
                    "validation_status": "valid",
                }

        validations = client.get(f"/v1/proactive/cases/{proactive_case['id']}/validations")
        assert validations.status_code == 200
        assert validations.json()["validations"][0]["result"] == "authorized"

        turns = client.get("/v1/proactive/turns")
        assert turns.status_code == 200
        turn = turns.json()["turns"][0]
        assert turn["case_id"] == proactive_case["id"]
        assert turn["decision_id"] == decision["id"]
        assert turn["status"] == "pending"
        assert turn["message"] == "Sir, you should review this approval now."

        notifications = client.get("/v1/notifications")
        assert notifications.status_code == 200
        notification = notifications.json()["notifications"][0]
        assert notification["source_type"] == "proactive_turn"
        assert notification["source_id"] == turn["id"]
        assert notification["payload"] == {
            "proactive_turn_id": turn["id"],
            "case_id": proactive_case["id"],
        }

        acked = client.post(f"/v1/notifications/{notification['id']}/ack")
        assert acked.status_code == 200

        case_after_ack = client.get(f"/v1/proactive/cases/{proactive_case['id']}")
        assert case_after_ack.status_code == 200
        assert case_after_ack.json()["case"]["status"] == "acknowledged"

        turns_after_ack = client.get("/v1/proactive/turns")
        assert turns_after_ack.status_code == 200
        assert turns_after_ack.json()["turns"][0]["status"] == "acknowledged"

        events = client.get(f"/v1/proactive/cases/{proactive_case['id']}/events")
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()["events"]] == [
            "opened",
            "context_built",
            "decided",
            "validated",
            "turn_created",
            "acknowledged",
        ]


def test_proactive_wait_decision_schedules_and_runs_durable_follow_up(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 13, 0, tzinfo=UTC)
    follow_up_at = now + timedelta(minutes=5)
    monkeypatch.setattr("ariel.app._utcnow", lambda: now)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    adapter = ProactiveDecisionAdapter(
        {
            "decision": "wait",
            "confidence": 0.76,
            "urgency": "normal",
            "user_visible_message": None,
            "rationale": "The running job should be checked again shortly.",
            "evidence_refs": ["latest_observation"],
            "tool_refs": [],
            "actions": [],
            "follow_up": {"after": "PT5M"},
        }
    )

    with _build_client(postgres_url, adapter) as client:
        _seed_job(client, job_id="job_proactive_follow_up", status="running", now=now)

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        cases = client.get("/v1/proactive/cases", params={"status": "waiting"})
        assert cases.status_code == 200
        proactive_case = cases.json()["cases"][0]
        assert proactive_case["case_key"] == "job:job_proactive_follow_up"
        assert proactive_case["next_recheck_after"] == follow_up_at.isoformat().replace(
            "+00:00", "Z"
        )

        with _session_factory(client)() as db:
            with db.begin():
                pending_tasks = (
                    db.execute(
                        text(
                            "SELECT task_type FROM background_tasks "
                            "WHERE status = 'pending' "
                            "ORDER BY created_at DESC"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert pending_tasks == ["proactive_follow_up_due"]

        monkeypatch.setattr("ariel.worker._utcnow", lambda: follow_up_at + timedelta(seconds=1))
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        reopened = client.get(f"/v1/proactive/cases/{proactive_case['id']}")
        assert reopened.status_code == 200
        assert reopened.json()["case"]["status"] == "open"
        assert reopened.json()["case"]["next_recheck_after"] is None

        with _session_factory(client)() as db:
            with db.begin():
                task_type = db.execute(
                    text(
                        "SELECT task_type FROM background_tasks "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "proactive_deliberation_due"


def test_proactive_feedback_creates_durable_learning_record(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.app._utcnow", lambda: now)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    adapter = ProactiveDecisionAdapter(
        {
            "decision": "speak_now",
            "confidence": 0.84,
            "urgency": "normal",
            "user_visible_message": "This running job may need your attention.",
            "rationale": "The model judged this job worth surfacing.",
            "evidence_refs": ["latest_observation"],
            "tool_refs": [],
            "actions": [],
            "follow_up": None,
        }
    )

    with _build_client(postgres_url, adapter) as client:
        _seed_job(client, job_id="job_proactive_feedback", status="running", now=now)

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        proactive_case = client.get("/v1/proactive/cases", params={"status": "spoken"}).json()[
            "cases"
        ][0]

        feedback = client.post(
            f"/v1/proactive/cases/{proactive_case['id']}/feedback",
            json={"feedback_type": "wrong", "note": "not worth interrupting"},
        )
        assert feedback.status_code == 200
        assert feedback.json()["feedback"]["feedback_type"] == "wrong"
        assert feedback.json()["feedback"]["case_id"] == proactive_case["id"]

        with _session_factory(client)() as db:
            with db.begin():
                task_type = db.execute(
                    text(
                        "SELECT task_type FROM background_tasks "
                        "WHERE status = 'pending' "
                        "AND task_type = 'proactive_feedback_learning_due' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "proactive_feedback_learning_due"

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
            model_adapter=adapter,
        )

        with _session_factory(client)() as db:
            with db.begin():
                record = (
                    db.execute(
                        text(
                            "SELECT record_type, status, content, model, prompt_version, "
                            "parse_status, validation_status "
                            "FROM proactive_learning_records "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert record["record_type"] == "example"
                assert record["status"] == "active"
                assert record["content"]["case_id"] == proactive_case["id"]
                assert record["content"]["note"] == "not worth interrupting"
                assert record["model"] == "model.proactive-deliberation-v1"
                assert record["prompt_version"] == "proactive-feedback-learning-v1"
                assert record["parse_status"] == "parsed"
                assert record["validation_status"] == "valid"
