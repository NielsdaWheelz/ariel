from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from ariel.app import create_app
from ariel.config import AppSettings
from ariel.executor import ExecutionResult
from ariel.memory import AIJudgmentFailure
from ariel.persistence import (
    AIJudgmentRecord,
    AutonomyScopeRecord,
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryReviewRecord,
    ProactiveActionExecutionRecord,
    ProactiveActionPlanRecord,
    ProactiveCaseEventRecord,
    ProactiveCaseRecord,
    ProactiveContextSnapshotRecord,
    ProactiveDecisionRecord,
    ProactivePolicyValidationRecord,
    ProactiveTurnRecord,
)
from ariel.proactivity import (
    process_proactive_action_execution_due,
    process_proactive_deliberation_due,
    upsert_proactive_observation,
)


@dataclass
class ProactiveAdapter:
    assistant_text: str

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
        return {
            "provider": "provider.proactive-test",
            "model": "model.proactive-test",
            "provider_response_id": "resp_proactive_test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.assistant_text}],
                }
            ],
        }


@dataclass
class ToolCallingProactiveAdapter:
    calls: int = 0

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, history, context_bundle
        self.calls += 1
        if not any(item.get("type") == "function_call_output" for item in input_items):
            assert tools == []
            return {
                "provider": "provider.proactive-test",
                "model": "model.proactive-test",
                "provider_response_id": "resp_proactive_tool_denied",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    _decision_payload(
                                        decision="speak_now",
                                        user_visible_message="Leave now from the case evidence.",
                                        tool_refs=[],
                                    )
                                ),
                            }
                        ],
                    }
                ],
            }
        raise AssertionError("case-scoped proactive test should not reach a tool round")


_id_counter = 0


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


def _new_id(prefix: str) -> str:
    global _id_counter
    _id_counter += 1
    return f"{prefix}_prt_{_id_counter}"


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _build_client(
    postgres_url: str,
    adapter: ProactiveAdapter | ToolCallingProactiveAdapter,
) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=cast(Any, adapter),
        reset_database=True,
    )
    return TestClient(app)


def _settings() -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def _seed_case(
    client: TestClient,
    *,
    now: datetime,
    taint: dict[str, Any] | None = None,
) -> str:
    with _session_factory(client)() as db:
        with db.begin():
            case_id = upsert_proactive_observation(
                db,
                dedupe_key=f"dedupe:{_new_id('obs')}",
                case_key=f"case:{_new_id('pca')}",
                source_type="job",
                source_id="job_proactive_test",
                observation_type="job_state",
                subject="Proactive runtime test",
                summary="The runtime should make a proactive decision.",
                payload={"status": "waiting_approval"},
                evidence={"job_id": "job_proactive_test"},
                taint=taint or {"provenance_status": "trusted_internal"},
                trust_boundary="trusted_internal",
                observed_at=now,
                workspace_item_id=None,
                now=now,
                new_id_fn=_new_id,
            )
            assert case_id is not None
            return case_id


def _seed_scope(
    client: TestClient,
    *,
    action_type: str,
    target_system: str,
    max_impact: str = "low",
    allowed_payload: dict[str, Any] | None = None,
    now: datetime,
) -> None:
    with _session_factory(client)() as db:
        with db.begin():
            scope = AutonomyScopeRecord(
                id=_new_id("asc"),
                scope_key=f"scope:{action_type}:{target_system}:{_new_id('skp')}",
                actor="proactive",
                action_type=action_type,
                target_system=target_system,
                allowed_payload=allowed_payload or {},
                max_impact=max_impact,
                notification_rule="notify_after",
                status="active",
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
            for field_name, value in {
                "source_context": {
                    "allowed_targets": ["owner-discord"]
                    if action_type == "send_discord_message"
                    else ["framework"]
                },
                "allowed_target_systems": [target_system],
                "allowed_payload_shape": (
                    {"required": {"message": "string"}, "allow_extra": False}
                    if action_type == "send_discord_message"
                    else {"required": {"note": "string"}, "allow_extra": False}
                ),
                "revocation_rule": "manual",
                "audit_visibility": "private",
                "version": 1,
            }.items():
                if hasattr(scope, field_name):
                    setattr(scope, field_name, value)
            db.add(scope)


def _run_deliberation(
    client: TestClient,
    *,
    case_id: str,
    adapter: ProactiveAdapter | ToolCallingProactiveAdapter,
    now: datetime,
) -> None:
    process_proactive_deliberation_due(
        session_factory=_session_factory(client),
        task_payload={"case_id": case_id},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: now,
        new_id_fn=_new_id,
    )


def _decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "decision": "ignore",
        "confidence": 0.9,
        "urgency": "normal",
        "user_visible_message": None,
        "rationale": "The runtime test supplied this decision.",
        "evidence_refs": ["latest_observation"],
        "tool_refs": [],
        "actions": [],
        "follow_up": None,
    }
    payload.update(overrides)
    return payload


def _latest_validation(client: TestClient) -> ProactivePolicyValidationRecord:
    with _session_factory(client)() as db:
        with db.begin():
            validation = db.scalar(
                select(ProactivePolicyValidationRecord)
                .order_by(ProactivePolicyValidationRecord.created_at.desc())
                .limit(1)
            )
            assert validation is not None
            return validation


def test_json_parse_failure_persists_invalid_decision_record(postgres_url: str) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    adapter = ProactiveAdapter("{not json")
    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                decision = db.scalar(select(ProactiveDecisionRecord).limit(1))
                case = db.get(ProactiveCaseRecord, case_id)
                validation = db.scalar(select(ProactivePolicyValidationRecord).limit(1))

                assert decision is not None
                assert decision.status == "invalid"
                assert decision.raw_model_output["parse_error"]
                assert case is not None
                assert case.status == "failed"
                assert validation is not None
                assert validation.result == "invalid_decision"


def test_proactive_memory_curation_failure_is_case_audited_before_deliberation(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    adapter = ProactiveAdapter(json.dumps(_decision_payload(decision="ignore")))

    def fail_memory_context(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        del args, kwargs
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="fixture memory curation failed",
            retryable=True,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id="resp_memory_failure",
        )

    monkeypatch.setattr("ariel.proactivity.build_memory_context", fail_memory_context)

    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        with pytest.raises(RuntimeError, match="fixture memory curation failed"):
            _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            case = db.get(ProactiveCaseRecord, case_id)
            assert case is not None
            assert case.status == "failed"
            judgments = db.scalars(
                select(AIJudgmentRecord).order_by(AIJudgmentRecord.created_at.asc())
            ).all()
            assert [judgment.judgment_type for judgment in judgments] == [
                "memory_curation",
                "proactive_deliberation",
            ]
            assert all(judgment.source_id == case_id for judgment in judgments)
            assert judgments[0].provider_response_id == "resp_memory_failure"
            assert judgments[0].failure_code == "E_AI_JUDGMENT_SCHEMA"
            assert judgments[0].parse_status == "schema_invalid"
            assert judgments[0].validation_status == "invalid"
            assert judgments[1].input_refs["dependency"] == "memory_curation"
            assert db.scalar(select(func.count()).select_from(ProactiveContextSnapshotRecord)) == 0
            assert db.scalar(select(func.count()).select_from(ProactiveDecisionRecord)) == 0
            assert db.scalar(select(func.count()).select_from(ProactiveTurnRecord)) == 0
            assert db.scalar(select(func.count()).select_from(ProactiveActionPlanRecord)) == 0


def test_deliberation_does_not_expose_generic_read_tools(
    postgres_url: str,
) -> None:
    now = datetime(2026, 5, 7, 12, 2, tzinfo=UTC)
    adapter = ToolCallingProactiveAdapter()
    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                snapshot = db.scalar(select(ProactiveContextSnapshotRecord).limit(1))
                decision = db.scalar(select(ProactiveDecisionRecord).limit(1))
                turn = db.scalar(select(ProactiveTurnRecord).limit(1))

                assert adapter.calls == 1
                assert snapshot is not None
                assert snapshot.context["tool_outputs"] == []
                assert not any(
                    item.get("type") == "function_call_output" for item in snapshot.model_input
                )
                assert decision is not None
                assert decision.tool_refs == []
                assert turn is not None
                assert turn.message == "Leave now from the case evidence."


def test_remember_creates_reviewable_memory_candidate_and_ask_user_sets_asked(
    postgres_url: str,
) -> None:
    now = datetime(2026, 5, 7, 12, 5, tzinfo=UTC)
    remember = ProactiveAdapter(
        json.dumps(
            _decision_payload(
                decision="remember",
                memory={
                    "subject_key": "project:phoenix",
                    "predicate": "deadline",
                    "value": "Ship tomorrow.",
                    "assertion_type": "project_state",
                },
            )
        )
    )
    ask = ProactiveAdapter(
        json.dumps(
            _decision_payload(
                decision="ask_user",
                user_visible_message="Should I keep watching this approval?",
            )
        )
    )
    with _build_client(postgres_url, remember) as client:
        remember_case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=remember_case_id, adapter=remember, now=now)
        ask_case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=ask_case_id, adapter=ask, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                assertion = db.scalar(select(MemoryAssertionRecord).limit(1))
                assertion_evidence = db.scalar(select(MemoryAssertionEvidenceRecord).limit(1))
                review = db.scalar(select(MemoryReviewRecord).limit(1))
                remember_event = db.scalar(
                    select(ProactiveCaseEventRecord)
                    .where(
                        ProactiveCaseEventRecord.case_id == remember_case_id,
                        ProactiveCaseEventRecord.event_type == "resolved",
                    )
                    .order_by(ProactiveCaseEventRecord.created_at.desc())
                    .limit(1)
                )
                asked_case = db.get(ProactiveCaseRecord, ask_case_id)
                turn = db.scalar(
                    select(ProactiveTurnRecord).where(ProactiveTurnRecord.case_id == ask_case_id)
                )

                assert assertion is not None
                assert assertion.subject_key == "project:phoenix"
                assert assertion.object_value == {"text": "Ship tomorrow."}
                assert assertion.lifecycle_state == "candidate"
                assert assertion_evidence is not None
                assert assertion_evidence.assertion_id == assertion.id
                assert review is not None
                assert review.assertion_id == assertion.id
                assert review.decision == "needs_user_review"
                assert remember_event is not None
                assert remember_event.payload["memory_candidate_assertion_id"] == assertion.id
                assert asked_case is not None
                assert asked_case.status == "asked"
                assert turn is not None
                assert turn.message == "Should I keep watching this approval?"


def test_act_now_send_discord_requires_scope_and_marks_acted_after_receipt(
    postgres_url: str,
) -> None:
    now = datetime(2026, 5, 7, 12, 10, tzinfo=UTC)
    action = {
        "action_type": "send_discord_message",
        "target": "owner-discord",
        "target_system": "discord",
        "payload": {"text": "Approval needs attention now."},
        "risk_tier": "low",
    }
    denied = ProactiveAdapter(json.dumps(_decision_payload(decision="act_now", actions=[action])))
    authorized = ProactiveAdapter(
        json.dumps(_decision_payload(decision="act_now", actions=[action]))
    )

    with _build_client(postgres_url, denied) as client:
        denied_case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=denied_case_id, adapter=denied, now=now)
        validation = _latest_validation(client)
        assert validation.result == "needs_user_authority"

    with _build_client(postgres_url, authorized) as client:
        _seed_scope(
            client,
            action_type="send_discord_message",
            target_system="discord",
            now=now,
        )
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=authorized, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                plan = db.scalar(select(ProactiveActionPlanRecord).limit(1))
                validation = db.scalar(select(ProactivePolicyValidationRecord).limit(1))
                assert case is not None
                assert case.status == "open"
                assert plan is not None
                assert plan.payload == {"message": "Approval needs attention now."}
                assert validation is not None
                assert validation.action_plan_hash is not None

        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan.id},
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                turn = db.scalar(select(ProactiveTurnRecord).limit(1))
                execution = db.scalar(select(ProactiveActionExecutionRecord).limit(1))
                assert case is not None
                assert case.status == "acted"
                assert turn is not None
                assert turn.message == "Approval needs attention now."
                assert execution is not None
                assert execution.status == "succeeded"


def test_failed_action_execution_is_replayable_with_same_execution_record(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 7, 12, 15, tzinfo=UTC)
    action = {
        "action_type": "cap.framework.write_draft",
        "target": "framework",
        "payload": {"note": "Draft the note."},
        "risk_tier": "low",
    }
    adapter = ProactiveAdapter(json.dumps(_decision_payload(decision="act_now", actions=[action])))
    results = [
        ExecutionResult(status="failed", output=None, error="temporary_failure"),
        ExecutionResult(status="succeeded", output={"status": "drafted"}, error=None),
    ]

    def fake_execute_capability(**_: Any) -> ExecutionResult:
        return results.pop(0)

    monkeypatch.setattr("ariel.proactivity.execute_capability", fake_execute_capability)
    with _build_client(postgres_url, adapter) as client:
        _seed_scope(
            client,
            action_type="cap.framework.write_draft",
            target_system="cap.framework.write_draft",
            now=now,
        )
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)
        with _session_factory(client)() as db:
            with db.begin():
                plan = db.scalar(select(ProactiveActionPlanRecord).limit(1))
                assert plan is not None
                plan_id = plan.id

        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan_id},
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )
        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan_id},
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            with db.begin():
                plan = db.get(ProactiveActionPlanRecord, plan_id)
                execution_count = db.scalar(select(func.count(ProactiveActionExecutionRecord.id)))
                execution = db.scalar(select(ProactiveActionExecutionRecord).limit(1))
                case = db.get(ProactiveCaseRecord, case_id)
                assert plan is not None
                assert plan.status == "succeeded"
                assert execution_count == 1
                assert execution is not None
                assert execution.status == "succeeded"
                assert case is not None
                assert case.status == "acted"


def test_speak_and_act_authorizes_turn_then_marks_acted_after_action_receipt(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 7, 12, 18, tzinfo=UTC)
    action = {
        "action_type": "cap.framework.write_draft",
        "target": "framework",
        "payload": {"note": "Draft the proactive note."},
        "risk_tier": "low",
    }
    adapter = ProactiveAdapter(
        json.dumps(
            _decision_payload(
                decision="speak_and_act",
                user_visible_message="I am drafting a short note.",
                actions=[action],
            )
        )
    )

    def fake_execute_capability(**_: Any) -> ExecutionResult:
        return ExecutionResult(status="succeeded", output={"status": "drafted"}, error=None)

    monkeypatch.setattr("ariel.proactivity.execute_capability", fake_execute_capability)
    with _build_client(postgres_url, adapter) as client:
        _seed_scope(
            client,
            action_type="cap.framework.write_draft",
            target_system="cap.framework.write_draft",
            now=now,
        )
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                plan = db.scalar(select(ProactiveActionPlanRecord).limit(1))
                turn = db.scalar(select(ProactiveTurnRecord).limit(1))
                validation = db.scalar(select(ProactivePolicyValidationRecord).limit(1))
                assert case is not None
                assert case.status == "spoken"
                assert plan is not None
                assert turn is not None
                assert turn.message == "I am drafting a short note."
                assert validation is not None
                assert validation.result == "authorized"
                plan_id = plan.id

        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan_id},
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                execution = db.scalar(select(ProactiveActionExecutionRecord).limit(1))
                assert case is not None
                assert case.status == "acted"
                assert execution is not None
                assert execution.external_receipt == {"status": "drafted"}


def test_speak_and_act_denies_non_low_risk_from_tainted_context(postgres_url: str) -> None:
    now = datetime(2026, 5, 7, 12, 20, tzinfo=UTC)
    action = {
        "action_type": "cap.framework.write_draft",
        "target": "framework",
        "payload": {"note": "Draft from tainted text."},
        "risk_tier": "medium",
    }
    adapter = ProactiveAdapter(
        json.dumps(
            _decision_payload(
                decision="speak_and_act",
                user_visible_message="I can draft this if allowed.",
                actions=[action],
            )
        )
    )
    with _build_client(postgres_url, adapter) as client:
        _seed_scope(
            client,
            action_type="cap.framework.write_draft",
            target_system="cap.framework.write_draft",
            max_impact="medium",
            now=now,
        )
        case_id = _seed_case(
            client,
            now=now,
            taint={"provenance_status": "tainted", "reason": "prompt_injection"},
        )
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                validation = db.scalar(select(ProactivePolicyValidationRecord).limit(1))
                action_count = db.scalar(select(func.count(ProactiveActionPlanRecord.id)))
                turn_count = db.scalar(select(func.count(ProactiveTurnRecord.id)))

                assert validation is not None
                assert validation.result == "denied"
                assert (
                    validation.denial_reason == "tainted context cannot execute non-low-risk action"
                )
                assert action_count == 0
                assert turn_count == 0
