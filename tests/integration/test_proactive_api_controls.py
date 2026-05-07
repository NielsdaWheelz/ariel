from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
from ariel.persistence import (
    ProactiveActionExecutionRecord,
    ProactiveActionPlanRecord,
    ProactiveCaseEventRecord,
    ProactiveCaseRecord,
    ProactiveContextSnapshotRecord,
    ProactiveDecisionRecord,
    ProactiveObservationRecord,
    ProactivePolicyValidationRecord,
    ProactiveTurnRecord,
)
from ariel.worker import process_one_task
from tests.integration.responses_helpers import responses_message


@dataclass
class StaticAdapter:
    provider: str = "provider.proactive-api"
    model: str = "model.proactive-api-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
        return responses_message(
            assistant_text="ok",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_proactive_api_123",
            input_tokens=1,
            output_tokens=1,
        )


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _seed_case(
    client: TestClient,
    *,
    case_id: str = "case_api",
    undo_supported: bool = True,
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    with _session_factory(client)() as db:
        with db.begin():
            observation = ProactiveObservationRecord(
                id=f"pob_{case_id}",
                workspace_item_id=None,
                source_type="job",
                source_id=f"job_{case_id}",
                dedupe_key=f"job:{case_id}",
                observation_type="job_status",
                subject="Proactive API case",
                summary="The job reached a state worth surfacing.",
                payload={"status": "waiting"},
                evidence={"job_id": f"job_{case_id}"},
                taint={"status": "clean"},
                trust_boundary="trusted_internal",
                status="linked",
                observed_at=now,
                created_at=now,
                updated_at=now,
            )
            proactive_case = ProactiveCaseRecord(
                id=case_id,
                case_key=f"job:{case_id}",
                status="spoken",
                title="Review proactive API case",
                summary="A direct API test case.",
                latest_observation_id=observation.id,
                last_decision_id=None,
                next_recheck_after=None,
                created_at=now,
                updated_at=now,
            )
            snapshot = ProactiveContextSnapshotRecord(
                id=f"pcs_{case_id}",
                case_id=case_id,
                snapshot_key=f"{case_id}:snapshot",
                context={"observation_id": observation.id},
                model_input=[{"role": "system", "content": "Inspect this case."}],
                omitted_context={},
                taint={"status": "clean"},
                created_at=now,
            )
            decision = ProactiveDecisionRecord(
                id=f"pdc_{case_id}",
                case_id=case_id,
                context_snapshot_id=snapshot.id,
                provider="provider.proactive-api",
                model="model.proactive-api-v1",
                provider_response_id="resp_proactive_api_decision",
                decision_type="speak_now",
                status="validated",
                confidence=0.83,
                urgency="normal",
                user_visible_message="Please review this proactive case.",
                rationale="The job state was worth interrupting for.",
                evidence_refs=["latest_observation"],
                tool_refs=[],
                actions=[],
                follow_up=None,
                raw_model_output={"decision": "speak_now"},
                created_at=now,
            )
            validation = ProactivePolicyValidationRecord(
                id=f"ppv_{case_id}",
                case_id=case_id,
                decision_id=decision.id,
                result="authorized",
                policy_version="test-policy-v1",
                action_plan_hash="hash_proactive_api_validation",
                constraints={},
                denial_reason=None,
                created_at=now,
            )
            turn = ProactiveTurnRecord(
                id=f"ptr_{case_id}",
                case_id=case_id,
                decision_id=decision.id,
                dedupe_key=f"{case_id}:turn",
                origin="proactive",
                channel="discord",
                status="delivered",
                message="Please review this proactive case.",
                delivery_payload={"case_id": case_id},
                delivered_at=now,
                acked_at=None,
                created_at=now,
                updated_at=now,
            )
            db.add(observation)
            db.flush()
            db.add(proactive_case)
            db.flush()
            db.add_all([snapshot, decision, validation, turn])
            db.flush()
            proactive_case.last_decision_id = decision.id
            db.add(
                ProactiveCaseEventRecord(
                    id=f"pce_{case_id}_opened",
                    case_id=case_id,
                    event_type="opened",
                    payload={"observation_id": observation.id},
                    created_at=now,
                )
            )
            if undo_supported:
                plan = ProactiveActionPlanRecord(
                    id=f"pap_{case_id}",
                    case_id=case_id,
                    decision_id=decision.id,
                    plan_key=f"{case_id}:action",
                    action_type="calendar.update",
                    target="google_calendar",
                    payload={"event_id": "evt_test"},
                    payload_hash="hash_proactive_api_action",
                    risk_tier="low",
                    status="succeeded",
                    policy_validation_id=validation.id,
                    created_at=now,
                    updated_at=now,
                )
                execution = ProactiveActionExecutionRecord(
                    id=f"pae_{case_id}",
                    action_plan_id=plan.id,
                    idempotency_key=f"{case_id}:action:execute",
                    status="succeeded",
                    external_receipt={
                        "provider": "google",
                        "undo": {"supported": True, "description": "Restore prior event."},
                    },
                    error=None,
                    started_at=now,
                    completed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(plan)
                db.flush()
                db.add(execution)


def test_missing_proactive_case_subresources_return_404(postgres_url: str) -> None:
    with _build_client(postgres_url, StaticAdapter()) as client:
        get_paths = [
            "/v1/proactive/cases/case_missing/events",
            "/v1/proactive/cases/case_missing/context-snapshots",
            "/v1/proactive/cases/case_missing/decisions",
            "/v1/proactive/cases/case_missing/validations",
            "/v1/proactive/cases/case_missing/actions",
            "/v1/proactive/cases/case_missing/inspect-why",
        ]

        for path in get_paths:
            response = client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "E_PROACTIVE_CASE_NOT_FOUND"

        undo = client.post("/v1/proactive/cases/case_missing/undo")
        assert undo.status_code == 404
        assert undo.json()["error"]["code"] == "E_PROACTIVE_CASE_NOT_FOUND"


def test_proactive_case_controls_update_state_and_explain_why(postgres_url: str) -> None:
    with _build_client(postgres_url, StaticAdapter()) as client:
        _seed_case(client)

        inspected = client.get("/v1/proactive/cases/case_api/inspect-why")
        assert inspected.status_code == 200
        inspect_payload = inspected.json()
        assert inspect_payload["why"]["trigger"]["summary"] == (
            "The job reached a state worth surfacing."
        )
        assert inspect_payload["why"]["decision"]["rationale"] == (
            "The job state was worth interrupting for."
        )
        assert [control["id"] for control in inspect_payload["controls"]] == [
            "ack",
            "correct",
            "stop_pattern",
            "more_aggressive",
            "inspect_why",
            "undo",
        ]

        ack = client.post("/v1/proactive/cases/case_api/ack")
        assert ack.status_code == 200
        assert ack.json()["case"]["status"] == "acknowledged"
        assert ack.json()["feedback"]["feedback_type"] == "ack"
        turns = client.get("/v1/proactive/turns")
        assert turns.json()["turns"][0]["status"] == "acknowledged"

        correct = client.post(
            "/v1/proactive/cases/case_api/correct",
            json={"feedback_type": "wrong", "note": "Wrong priority."},
        )
        assert correct.status_code == 200
        assert correct.json()["feedback"]["feedback_type"] == "correct"
        assert correct.json()["feedback"]["note"] == "Wrong priority."

        stop = client.post("/v1/proactive/cases/case_api/stop-pattern")
        assert stop.status_code == 200
        assert stop.json()["feedback"]["feedback_type"] == "stop_pattern"

        more = client.post("/v1/proactive/cases/case_api/more-aggressive")
        assert more.status_code == 200
        assert more.json()["feedback"]["feedback_type"] == "more_aggressive"

        undo = client.post("/v1/proactive/cases/case_api/undo")
        assert undo.status_code == 200
        assert undo.json()["undo"] == {
            "status": "requested",
            "action_execution_id": "pae_case_api",
            "metadata": {"supported": True, "description": "Restore prior event."},
        }

        events = client.get("/v1/proactive/cases/case_api/events")
        assert events.status_code == 200
        assert {
            "feedback_type": "undo_requested",
            "action_execution_id": "pae_case_api",
        } in [event["payload"] for event in events.json()["events"]]


def test_proactive_undo_is_absent_and_rejected_without_metadata(postgres_url: str) -> None:
    with _build_client(postgres_url, StaticAdapter()) as client:
        _seed_case(client, case_id="case_no_undo", undo_supported=False)

        inspected = client.get("/v1/proactive/cases/case_no_undo/inspect-why")
        assert inspected.status_code == 200
        assert [control["id"] for control in inspected.json()["controls"]] == [
            "ack",
            "correct",
            "stop_pattern",
            "more_aggressive",
            "inspect_why",
        ]

        undo = client.post("/v1/proactive/cases/case_no_undo/undo")
        assert undo.status_code == 409
        assert undo.json()["error"]["code"] == "E_PROACTIVE_UNDO_NOT_SUPPORTED"


def test_automatic_next_time_feedback_creates_autonomy_request_learning_record(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, StaticAdapter()) as client:
        _seed_case(client)

        feedback = client.post(
            "/v1/proactive/cases/case_api/feedback",
            json={"feedback_type": "automatic_next_time", "note": "Handle this next time."},
        )
        assert feedback.status_code == 200
        assert feedback.json()["feedback"]["feedback_type"] == "automatic_next_time"

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=cast(Any, AppSettings)(_env_file=None),
            worker_id="worker-proactive-api",
            model_adapter=StaticAdapter(),
        )

        learning = client.get("/v1/proactive/learning-records")
        assert learning.status_code == 200
        record = learning.json()["learning_records"][0]
        assert record["record_type"] == "autonomy_request"
        assert record["content"] == {
            "instruction": "Propose an autonomy scope for this pattern.",
            "case_id": "case_api",
            "note": "Handle this next time.",
        }

        with _session_factory(client)() as db:
            with db.begin():
                task_status = db.execute(
                    text("SELECT status FROM background_tasks ORDER BY created_at DESC LIMIT 1")
                ).scalar_one()
                assert task_status == "completed"
