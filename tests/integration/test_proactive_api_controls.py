from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import text

from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
from ariel.persistence import (
    AutonomyScopeRecord,
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
from ariel.proactivity import process_proactive_deliberation_due
from ariel.worker import process_one_task
from tests.integration.responses_helpers import responses_message
from tests.fake_sandbox import FakeSandboxRuntime


_ID_COUNTER = 0


def _new_test_id(prefix: str) -> str:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return f"{prefix}_api_{_ID_COUNTER}"


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
        del input_items, tools, user_message, history
        if context_bundle.get("origin") == "feedback_learning":
            assistant_text = json.dumps(
                {
                    "learning_records": [
                        {
                            "record_type": "autonomy_request",
                            "content": {
                                "instruction": "Propose an autonomy scope for this pattern.",
                                "case_id": "case_api",
                                "note": "Handle this next time.",
                            },
                        }
                    ]
                },
                sort_keys=True,
            )
        else:
            assistant_text = "ok"
        return responses_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_proactive_api_123",
            input_tokens=1,
            output_tokens=1,
        )


def _feedback_learning_context(context_bundle: dict[str, Any]) -> dict[str, Any]:
    model_input = context_bundle.get("model_input")
    if not isinstance(model_input, list):
        return {}
    for item in model_input:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("feedback"), dict):
            return parsed
    return {}


@dataclass
class FeedbackTypeEchoAdapter:
    provider: str = "provider.proactive-feedback"
    model: str = "model.proactive-feedback-v1"
    seen_feedback_types: list[str] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history
        context = _feedback_learning_context(context_bundle)
        raw_feedback = context.get("feedback") if isinstance(context, dict) else {}
        feedback = raw_feedback if isinstance(raw_feedback, dict) else {}
        raw_proactive_case = context.get("case") if isinstance(context, dict) else {}
        proactive_case = raw_proactive_case if isinstance(raw_proactive_case, dict) else {}
        feedback_type = str(feedback.get("feedback_type") or "")
        self.seen_feedback_types.append(feedback_type)
        return responses_message(
            assistant_text=json.dumps(
                {
                    "learning_records": [
                        {
                            "record_type": "example",
                            "content": {
                                "case_id": proactive_case.get("id"),
                                "feedback_type": feedback_type,
                                "note": feedback.get("note"),
                            },
                        }
                    ]
                },
                sort_keys=True,
            ),
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_feedback_{feedback_type or 'unknown'}",
            input_tokens=1,
            output_tokens=1,
        )


@dataclass
class InvalidFeedbackLearnerAdapter:
    provider: str = "provider.proactive-feedback-invalid"
    model: str = "model.proactive-feedback-invalid-v1"

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
            assistant_text="{not json",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_feedback_invalid",
            input_tokens=1,
            output_tokens=1,
        )


@dataclass
class SchemaInvalidFeedbackLearnerAdapter:
    provider: str = "provider.proactive-feedback-schema-invalid"
    model: str = "model.proactive-feedback-schema-invalid-v1"

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
            assistant_text=json.dumps(
                {"learning_records": [{"record_type": "example"}]},
                sort_keys=True,
            ),
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_feedback_schema_invalid",
            input_tokens=1,
            output_tokens=1,
        )


@dataclass
class DecisionAdapter:
    decision: dict[str, Any]
    provider: str = "provider.proactive-action"
    model: str = "model.proactive-action-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history
        assistant_text = json.dumps(self.decision, sort_keys=True)
        if context_bundle.get("origin") != "proactive":
            assistant_text = "ok"
        return responses_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_proactive_action",
            input_tokens=1,
            output_tokens=1,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _seed_case(
    client: TestClient,
    *,
    case_id: str = "case_api",
    undo_supported: bool = True,
    taint: dict[str, Any] | None = None,
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
                taint=taint or {"status": "clean"},
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


def _seed_scope(
    client: TestClient,
    *,
    scope_id: str,
    action_type: str,
    target_system: str,
    source_context: dict[str, Any] | None = None,
    allowed_payload_shape: dict[str, Any] | None = None,
    max_impact: str = "low",
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    with _session_factory(client)() as db:
        with db.begin():
            db.add(
                AutonomyScopeRecord(
                    id=scope_id,
                    scope_key=f"scope:{scope_id}",
                    actor="proactive",
                    source_context=source_context or {},
                    action_type=action_type,
                    target_system=target_system,
                    allowed_target_systems=[target_system],
                    allowed_payload={},
                    allowed_payload_shape=allowed_payload_shape or {},
                    max_impact=max_impact,
                    revocation_rule="manual",
                    notification_rule="notify_after",
                    audit_visibility="operator_visible",
                    version=1,
                    status="active",
                    revoked_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _run_decision(client: TestClient, *, case_id: str, adapter: DecisionAdapter) -> None:
    process_proactive_deliberation_due(
        session_factory=_session_factory(client),
        task_payload={"case_id": case_id},
        settings=cast(Any, AppSettings)(_env_file=None),
        model_adapter=adapter,
        now_fn=lambda: datetime(2026, 5, 7, 12, 1, tzinfo=UTC),
        new_id_fn=_new_test_id,
    )


def _decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "decision": "act_now",
        "confidence": 0.9,
        "urgency": "normal",
        "user_visible_message": None,
        "rationale": "The proactive API test supplied this action.",
        "evidence_refs": ["latest_observation"],
        "tool_refs": [],
        "actions": [],
        "follow_up": None,
    }
    payload.update(overrides)
    return payload


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
        feedback_id = feedback.json()["feedback"]["id"]

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=cast(Any, AppSettings)(_env_file=None),
            worker_id="worker-proactive-api",
            model_adapter=StaticAdapter(),
        )

        with _session_factory(client)() as db:
            with db.begin():
                record = (
                    db.execute(
                        text(
                            "SELECT feedback_id, record_type, content, model, prompt_version, "
                            "provider_response_id, parse_status, validation_status "
                            "FROM proactive_learning_records LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert record["record_type"] == "autonomy_request"
                assert record["content"]["instruction"] == (
                    "Propose an autonomy scope for this pattern."
                )
                assert record["content"]["case_id"] == "case_api"
                assert record["content"]["note"] == "Handle this next time."
                assert record["feedback_id"] == feedback_id
                assert record["model"] == "model.proactive-api-v1"
                assert record["provider_response_id"] == "resp_proactive_api_123"
                assert record["prompt_version"] == "proactive-feedback-learning-v1"
                assert record["parse_status"] == "parsed"
                assert record["validation_status"] == "valid"
                task_status = db.execute(
                    text("SELECT status FROM background_tasks ORDER BY created_at DESC LIMIT 1")
                ).scalar_one()
                assert task_status == "completed"


def test_all_feedback_types_are_processed_by_ai_feedback_learner(postgres_url: str) -> None:
    adapter = FeedbackTypeEchoAdapter()
    feedback_types = [
        "stop_pattern",
        "more_aggressive",
        "useful",
        "wrong",
        "correct",
        "ack",
        "automatic_next_time",
    ]
    with _build_client(postgres_url, adapter) as client:
        for index, feedback_type in enumerate(feedback_types):
            case_id = f"case_f{index}"
            _seed_case(client, case_id=case_id, undo_supported=False)
            feedback = client.post(
                f"/v1/proactive/cases/{case_id}/feedback",
                json={"feedback_type": feedback_type, "note": f"note {feedback_type}"},
            )
            assert feedback.status_code == 200
            assert process_one_task(
                session_factory=_session_factory(client),
                settings=cast(Any, AppSettings)(_env_file=None),
                worker_id="worker-proactive-feedback",
                model_adapter=adapter,
            )

        with _session_factory(client)() as db:
            with db.begin():
                records = [
                    dict(row)
                    for row in db.execute(
                        text(
                            "SELECT feedback_id, content, model, prompt_version, "
                            "parse_status, validation_status "
                            "FROM proactive_learning_records"
                        )
                    ).mappings()
                ]
                assert adapter.seen_feedback_types == feedback_types
                assert {record["content"]["feedback_type"] for record in records} == set(
                    feedback_types
                )
                for record in records:
                    assert record["model"] == "model.proactive-feedback-v1"
                    assert record["prompt_version"] == "proactive-feedback-learning-v1"
                    assert record["parse_status"] == "parsed"
                    assert record["validation_status"] == "valid"
                    assert record["feedback_id"] is not None
                judgments = [
                    dict(row)
                    for row in db.execute(
                        text(
                            "SELECT source_id, status, model, prompt_version, "
                            "provider_response_id, parse_status, validation_status "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'feedback_learning' "
                            "ORDER BY created_at ASC"
                        )
                    ).mappings()
                ]
                assert len(judgments) == len(feedback_types)
                assert {judgment["source_id"] for judgment in judgments} == {
                    record["feedback_id"] for record in records
                }
                for judgment in judgments:
                    assert judgment["status"] == "succeeded"
                    assert judgment["model"] == "model.proactive-feedback-v1"
                    assert judgment["prompt_version"] == "proactive-feedback-learning-v1"
                    assert str(judgment["provider_response_id"]).startswith("resp_feedback_")
                    assert judgment["parse_status"] == "parsed"
                    assert judgment["validation_status"] == "valid"


def test_invalid_feedback_learner_output_fails_closed(postgres_url: str) -> None:
    adapter = InvalidFeedbackLearnerAdapter()
    with _build_client(postgres_url, adapter) as client:
        _seed_case(client, undo_supported=False)
        feedback = client.post(
            "/v1/proactive/cases/case_api/feedback",
            json={"feedback_type": "useful", "note": "good interruption"},
        )
        assert feedback.status_code == 200

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=cast(Any, AppSettings)(_env_file=None),
            worker_id="worker-proactive-feedback-invalid",
            model_adapter=adapter,
        )

        learning = client.get("/v1/proactive/learning-records")
        assert learning.status_code == 200
        assert learning.json()["learning_records"] == []

        with _session_factory(client)() as db:
            with db.begin():
                task = (
                    db.execute(
                        text(
                            "SELECT status, error FROM background_tasks "
                            "WHERE task_type = 'proactive_feedback_learning_due' "
                            "ORDER BY updated_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert task["status"] == "pending"
                assert str(task["error"]).startswith("feedback_learner_parse_failed:")
                failure_type = db.execute(
                    text(
                        "SELECT payload ->> 'failure_type' FROM proactive_case_events "
                        "WHERE case_id = 'case_api' AND event_type = 'failed' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert failure_type == "feedback_learning_failed"
                judgment = (
                    db.execute(
                        text(
                            "SELECT status, model, provider_response_id, parse_status, "
                            "validation_status, failure_code "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'feedback_learning' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert judgment == {
                    "status": "failed",
                    "model": "model.proactive-feedback-invalid-v1",
                    "provider_response_id": "resp_feedback_invalid",
                    "parse_status": "invalid_json",
                    "validation_status": "not_validated",
                    "failure_code": "E_AI_JUDGMENT_INVALID_JSON",
                }


def test_schema_invalid_feedback_learner_output_fails_closed(postgres_url: str) -> None:
    adapter = SchemaInvalidFeedbackLearnerAdapter()
    with _build_client(postgres_url, adapter) as client:
        _seed_case(client, undo_supported=False)
        feedback = client.post(
            "/v1/proactive/cases/case_api/feedback",
            json={"feedback_type": "useful", "note": "good interruption"},
        )
        assert feedback.status_code == 200
        feedback_id = feedback.json()["feedback"]["id"]

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=cast(Any, AppSettings)(_env_file=None),
            worker_id="worker-proactive-feedback-schema-invalid",
            model_adapter=adapter,
        )

        learning = client.get("/v1/proactive/learning-records")
        assert learning.status_code == 200
        assert learning.json()["learning_records"] == []

        with _session_factory(client)() as db:
            with db.begin():
                task = (
                    db.execute(
                        text(
                            "SELECT status, error FROM background_tasks "
                            "WHERE task_type = 'proactive_feedback_learning_due' "
                            "ORDER BY updated_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert task["status"] == "pending"
                assert task["error"] == ("feedback_learner_validation_failed:record_schema_invalid")
                assert (
                    db.execute(text("SELECT COUNT(*) FROM proactive_learning_records")).scalar_one()
                    == 0
                )
                judgment = (
                    db.execute(
                        text(
                            "SELECT source_id, status, model, provider_response_id, "
                            "parse_status, validation_status, failure_code, failure_reason "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'feedback_learning' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert judgment == {
                    "source_id": feedback_id,
                    "status": "failed",
                    "model": "model.proactive-feedback-schema-invalid-v1",
                    "provider_response_id": "resp_feedback_schema_invalid",
                    "parse_status": "schema_invalid",
                    "validation_status": "invalid",
                    "failure_code": "E_AI_JUDGMENT_SCHEMA",
                    "failure_reason": "feedback_learner_validation_failed:record_schema_invalid",
                }


def test_autonomy_scope_enforces_target_recipient_and_payload_shape(
    postgres_url: str,
) -> None:
    email_shape = {
        "required": {
            "to": "list",
            "cc": "list",
            "bcc": "list",
            "subject": "string",
            "body": "string",
            "idempotency_key": "string",
            "user_instruction_ref": "string",
        },
        "allow_extra": False,
    }
    good_action: dict[str, Any] = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": {
            "to": ["ops@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Status",
            "body": "Please review the blocked job.",
            "idempotency_key": "scope-email-draft",
            "user_instruction_ref": "turn:scope-email",
        },
        "risk_tier": "low",
    }
    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_scope(
            client,
            scope_id="scope_email",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["team-email"],
                "allowed_recipients": ["ops@example.com"],
            },
            allowed_payload_shape=email_shape,
        )

        _seed_case(client, case_id="case_allowed", undo_supported=False)
        _run_decision(
            client,
            case_id="case_allowed",
            adapter=DecisionAdapter(_decision_payload(actions=[good_action])),
        )
        with _session_factory(client)() as db:
            with db.begin():
                allowed = (
                    db.execute(
                        text(
                            "SELECT result, denial_reason FROM proactive_policy_validations "
                            "WHERE case_id = 'case_allowed' ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                plan_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM proactive_action_plans "
                        "WHERE case_id = 'case_allowed' AND action_type = 'cap.email.draft'"
                    )
                ).scalar_one()
                assert allowed["result"] == "authorized"
                assert allowed["denial_reason"] is None
                assert plan_count == 1
                judgment = (
                    db.execute(
                        text(
                            "SELECT status, prompt_version, parse_status, validation_status "
                            "FROM ai_judgments WHERE source_id = 'case_allowed' "
                            "AND judgment_type = 'proactive_deliberation'"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert judgment == {
                    "status": "succeeded",
                    "prompt_version": "proactive-ai-deliberation-v1",
                    "parse_status": "parsed",
                    "validation_status": "valid",
                }

        _seed_case(client, case_id="case_bad_target", undo_supported=False)
        bad_target = {**good_action, "target": "other-email"}
        _run_decision(
            client,
            case_id="case_bad_target",
            adapter=DecisionAdapter(_decision_payload(actions=[bad_target])),
        )

        _seed_case(client, case_id="case_bad_recipient", undo_supported=False)
        bad_recipient = {
            **good_action,
            "payload": {**good_action["payload"], "to": ["outside@example.com"]},
        }
        _run_decision(
            client,
            case_id="case_bad_recipient",
            adapter=DecisionAdapter(_decision_payload(actions=[bad_recipient])),
        )

        _seed_scope(
            client,
            scope_id="scope_shape",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["shape-email"],
                "allowed_recipients": ["ops@example.com"],
            },
            allowed_payload_shape={
                "required": {
                    "to": "list",
                    "cc": "list",
                    "bcc": "list",
                    "subject": "string",
                    "body": "string",
                    "idempotency_key": "string",
                    "commitment_id": "string",
                },
                "allow_extra": False,
            },
        )
        _seed_case(client, case_id="case_bad_shape", undo_supported=False)
        bad_shape = {
            **good_action,
            "target": "shape-email",
            "payload": {**good_action["payload"]},
        }
        _run_decision(
            client,
            case_id="case_bad_shape",
            adapter=DecisionAdapter(_decision_payload(actions=[bad_shape])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                denials = {
                    row["case_id"]: row["denial_reason"]
                    for row in db.execute(
                        text(
                            "SELECT case_id, denial_reason FROM proactive_policy_validations "
                            "WHERE case_id IN ("
                            "'case_bad_target', 'case_bad_recipient', 'case_bad_shape'"
                            ") ORDER BY case_id"
                        )
                    ).mappings()
                }
                assert denials == {
                    "case_bad_recipient": "recipient is outside autonomy scope for cap.email.draft",
                    "case_bad_shape": "payload shape is outside autonomy scope for cap.email.draft",
                    "case_bad_target": "target is outside autonomy scope for cap.email.draft",
                }


def test_autonomy_scope_selection_checks_later_full_scope(
    postgres_url: str,
) -> None:
    email_shape = {
        "required": {
            "to": "list",
            "cc": "list",
            "bcc": "list",
            "subject": "string",
            "body": "string",
            "idempotency_key": "string",
            "user_instruction_ref": "string",
        },
        "allow_extra": False,
    }
    action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": {
            "to": ["ops@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Status",
            "body": "Please review the blocked job.",
            "idempotency_key": "multi-scope-email-draft",
            "user_instruction_ref": "turn:multi-scope-email",
        },
        "risk_tier": "low",
    }

    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_scope(
            client,
            scope_id="scope_multi_a_bad_recipient",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["team-email"],
                "allowed_recipients": ["finance@example.com"],
            },
            allowed_payload_shape=email_shape,
        )
        _seed_scope(
            client,
            scope_id="scope_multi_b_authorized",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["team-email"],
                "allowed_recipients": ["ops@example.com"],
            },
            allowed_payload_shape=email_shape,
        )
        _seed_case(client, case_id="case_multi_scope", undo_supported=False)
        _run_decision(
            client,
            case_id="case_multi_scope",
            adapter=DecisionAdapter(_decision_payload(actions=[action])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                validation = (
                    db.execute(
                        text(
                            "SELECT result, denial_reason, constraints "
                            "FROM proactive_policy_validations "
                            "WHERE case_id = 'case_multi_scope' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                plan_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM proactive_action_plans "
                        "WHERE case_id = 'case_multi_scope'"
                    )
                ).scalar_one()

    assert validation["result"] == "authorized"
    assert validation["denial_reason"] is None
    assert validation["constraints"]["considered_scope_ids"] == [
        "scope_multi_a_bad_recipient",
        "scope_multi_b_authorized",
    ]
    assert plan_count == 1


def test_autonomy_scope_selection_does_not_union_partial_scopes(
    postgres_url: str,
) -> None:
    email_shape = {
        "required": {
            "to": "list",
            "cc": "list",
            "bcc": "list",
            "subject": "string",
            "body": "string",
            "idempotency_key": "string",
            "user_instruction_ref": "string",
        },
        "allow_extra": False,
    }
    action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": {
            "to": ["ops@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Status",
            "body": "Please review the blocked job.",
            "idempotency_key": "partial-scope-email-draft",
            "user_instruction_ref": "turn:partial-scope-email",
        },
        "risk_tier": "low",
    }

    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_scope(
            client,
            scope_id="scope_partial_target",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["team-email"],
                "allowed_recipients": ["finance@example.com"],
            },
            allowed_payload_shape=email_shape,
        )
        _seed_scope(
            client,
            scope_id="scope_partial_recipient",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["finance-email"],
                "allowed_recipients": ["ops@example.com"],
            },
            allowed_payload_shape=email_shape,
        )
        _seed_case(client, case_id="case_partial_scopes", undo_supported=False)
        _run_decision(
            client,
            case_id="case_partial_scopes",
            adapter=DecisionAdapter(_decision_payload(actions=[action])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                validation = (
                    db.execute(
                        text(
                            "SELECT result, denial_reason, constraints "
                            "FROM proactive_policy_validations "
                            "WHERE case_id = 'case_partial_scopes' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                plan_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM proactive_action_plans "
                        "WHERE case_id = 'case_partial_scopes'"
                    )
                ).scalar_one()

    assert validation["result"] == "needs_user_authority"
    assert validation["denial_reason"] in {
        "recipient is outside autonomy scope for cap.email.draft",
        "target is outside autonomy scope for cap.email.draft",
    }
    assert validation["constraints"]["considered_scope_ids"] == [
        "scope_partial_recipient",
        "scope_partial_target",
    ]
    assert plan_count == 0


def test_autonomy_scope_missing_target_or_recipient_scope_denies_writes(
    postgres_url: str,
) -> None:
    email_shape = {
        "required": {
            "to": "list",
            "cc": "list",
            "bcc": "list",
            "subject": "string",
            "body": "string",
            "idempotency_key": "string",
            "user_instruction_ref": "string",
        },
        "allow_extra": False,
    }
    email_action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": {
            "to": ["ops@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Status",
            "body": "Please review the blocked job.",
            "idempotency_key": "missing-scope-email-draft",
            "user_instruction_ref": "turn:missing-scope-email",
        },
        "risk_tier": "low",
    }
    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_scope(
            client,
            scope_id="scope_no_target",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={"allowed_recipients": ["ops@example.com"]},
            allowed_payload_shape=email_shape,
        )
        _seed_case(client, case_id="case_no_target", undo_supported=False)
        _run_decision(
            client,
            case_id="case_no_target",
            adapter=DecisionAdapter(_decision_payload(actions=[email_action])),
        )
        with _session_factory(client)() as db:
            with db.begin():
                scope = db.get(AutonomyScopeRecord, "scope_no_target")
                assert scope is not None
                scope.status = "revoked"

        _seed_scope(
            client,
            scope_id="scope_no_recipient",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={"allowed_targets": ["team-email"]},
            allowed_payload_shape=email_shape,
        )
        _seed_case(client, case_id="case_no_recipient", undo_supported=False)
        _run_decision(
            client,
            case_id="case_no_recipient",
            adapter=DecisionAdapter(_decision_payload(actions=[email_action])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                denials = {
                    row["case_id"]: row["denial_reason"]
                    for row in db.execute(
                        text(
                            "SELECT case_id, denial_reason FROM proactive_policy_validations "
                            "WHERE case_id IN ('case_no_target', 'case_no_recipient')"
                        )
                    ).mappings()
                }

    assert denials == {
        "case_no_recipient": "recipient is outside autonomy scope for cap.email.draft",
        "case_no_target": "target is outside autonomy scope for cap.email.draft",
    }


def test_tainted_context_cannot_execute_low_risk_autonomous_write(
    postgres_url: str,
) -> None:
    email_shape = {
        "required": {
            "to": "list",
            "cc": "list",
            "bcc": "list",
            "subject": "string",
            "body": "string",
            "idempotency_key": "string",
            "user_instruction_ref": "string",
        },
        "allow_extra": False,
    }
    action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": {
            "to": ["ops@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Status",
            "body": "Draft from tainted content.",
            "idempotency_key": "tainted-email-draft",
            "user_instruction_ref": "turn:tainted-email",
        },
        "risk_tier": "low",
    }
    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_scope(
            client,
            scope_id="scope_tainted",
            action_type="cap.email.draft",
            target_system="gmail",
            source_context={
                "allowed_targets": ["team-email"],
                "allowed_recipients": ["ops@example.com"],
            },
            allowed_payload_shape=email_shape,
        )
        _seed_case(
            client,
            case_id="case_tainted",
            undo_supported=False,
            taint={"provenance_status": "tainted", "reason": "prompt_injection"},
        )
        _run_decision(
            client,
            case_id="case_tainted",
            adapter=DecisionAdapter(_decision_payload(actions=[action])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                validation = (
                    db.execute(
                        text(
                            "SELECT result, denial_reason FROM proactive_policy_validations "
                            "WHERE case_id = 'case_tainted' ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                plan_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM proactive_action_plans WHERE case_id = 'case_tainted'"
                    )
                ).scalar_one()
                assert validation["result"] == "denied"
                assert validation["denial_reason"] == (
                    "tainted context cannot execute autonomous write"
                )
                assert plan_count == 0


def test_proactive_write_rejects_unknown_capability_before_action_plan(
    postgres_url: str,
) -> None:
    action = {
        "action_type": "cap.external.notify",
        "target": "external-notify",
        "target_system": "external",
        "payload": {"destination": "evil.example", "message": "Notify outside scope."},
        "risk_tier": "low",
    }
    with _build_client(postgres_url, DecisionAdapter(_decision_payload())) as client:
        _seed_case(client, case_id="case_egress", undo_supported=False)
        _run_decision(
            client,
            case_id="case_egress",
            adapter=DecisionAdapter(_decision_payload(actions=[action])),
        )

        with _session_factory(client)() as db:
            with db.begin():
                validation = (
                    db.execute(
                        text(
                            "SELECT result, denial_reason FROM proactive_policy_validations "
                            "WHERE case_id = 'case_egress' ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                plan_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM proactive_action_plans WHERE case_id = 'case_egress'"
                    )
                ).scalar_one()
                assert validation["result"] == "invalid_decision"
                assert validation["denial_reason"] == "unknown capability cap.external.notify"
                assert plan_count == 0
