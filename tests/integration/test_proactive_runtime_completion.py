from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text

from ariel.app import create_app
from ariel.config import AppSettings
from ariel.executor import ExecutionResult
from ariel.google_connector import GoogleCapabilityExecutionResult
from ariel.persistence import (
    AIJudgmentRecord,
    AutonomyScopeRecord,
    NotificationRecord,
    ProactiveActionExecutionRecord,
    ProactiveActionPlanRecord,
    ProactiveCaseEventRecord,
    ProactiveCaseRecord,
    ProactiveDecisionRecord,
)
from ariel.proactivity import (
    process_proactive_action_execution_due,
    process_proactive_deliberation_due,
    upsert_proactive_observation,
)
from tests.fake_sandbox import FakeSandboxRuntime


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
                        "type": "function_call",
                        "call_id": "call_unadvertised_tool",
                        "name": "unadvertised_tool",
                        "arguments": json.dumps({"query": "case evidence"}),
                    }
                ],
            }
        assert tools == []
        tool_output = next(
            item for item in input_items if item.get("type") == "function_call_output"
        )
        assert json.loads(tool_output["output"]) == {
            "status": "failed",
            "error": "proactive_deliberation_tool_denied",
        }
        return {
            "provider": "provider.proactive-test",
            "model": "model.proactive-test",
            "provider_response_id": "resp_proactive_after_tool_denial",
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


@dataclass
class ProactiveGoogleRuntime:
    results: list[ExecutionResult]

    def prepare_capability_access(self, **_: Any) -> tuple[str, set[str], str, None]:
        return "token", set(), "acct_google", None

    def _typed_failure(self, *, failure_class: str) -> GoogleCapabilityExecutionResult:
        return GoogleCapabilityExecutionResult(
            status="failed",
            output=None,
            auth_failure=None,
            error=failure_class,
        )

    def execute_provider_capability(self, **_: Any) -> GoogleCapabilityExecutionResult:
        result = self.results.pop(0)
        return GoogleCapabilityExecutionResult(
            status=result.status,
            output=result.output,
            auth_failure=None,
            error=result.error,
        )


_id_counter = 0


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
        sandbox=FakeSandboxRuntime(),
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
                discord_message_id=None,
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
    source_context = {"allowed_targets": ["framework"]}
    allowed_payload_shape = {"required": {"note": "string"}, "allow_extra": False}
    if action_type == "cap.email.draft":
        source_context = {
            "allowed_targets": ["team-email"],
            "allowed_recipients": ["ops@example.com"],
        }
        allowed_payload_shape = {
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
                "source_context": source_context,
                "allowed_target_systems": [target_system],
                "allowed_payload_shape": allowed_payload_shape,
                "revocation_rule": "manual",
                "audit_visibility": "private",
                "version": 1,
            }.items():
                if hasattr(scope, field_name):
                    setattr(scope, field_name, value)
            db.add(scope)


def _email_draft_payload(idempotency_key: str, body: str) -> dict[str, Any]:
    return {
        "to": ["ops@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Status",
        "body": body,
        "idempotency_key": idempotency_key,
        "user_instruction_ref": f"turn:{idempotency_key}",
    }


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


def _latest_decision(client: TestClient) -> ProactiveDecisionRecord:
    with _session_factory(client)() as db:
        with db.begin():
            decision = db.scalar(
                select(ProactiveDecisionRecord)
                .order_by(ProactiveDecisionRecord.created_at.desc())
                .limit(1)
            )
            assert decision is not None
            return decision


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

                assert decision is not None
                assert decision.status == "invalid"
                assert decision.policy_result == "invalid_decision"
                judgment = db.get(AIJudgmentRecord, decision.ai_judgment_id)
                assert judgment is not None
                assert judgment.output["parse_error"]
                assert decision.denial_reason == judgment.output["parse_error"]
                assert case is not None
                assert case.status == "failed"


def test_proactive_retriever_failure_is_audited_but_deliberation_still_completes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-deliberation retriever call is non-fatal: it is audited as a
    failed ``memory_recall`` judgment, and the deliberation still runs on the
    profile alone -- recall is never on the deliberation's critical-failure
    path. The retriever's bounded model call is stubbed to return HTTP 500."""

    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    adapter = ProactiveAdapter(json.dumps(_decision_payload(decision="ignore")))

    class _FailingResponse:
        status_code = 500

        def json(self) -> dict[str, Any]:
            return {"error": "boom"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.httpx.post", lambda *args, **kwargs: _FailingResponse())

    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            case = db.get(ProactiveCaseRecord, case_id)
            assert case is not None
            # The deliberation completed despite the failed retriever call.
            assert case.status == "ignored"
            failed_recall = db.scalar(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "memory_recall",
                    AIJudgmentRecord.status == "failed",
                )
            )
            assert failed_recall is not None
            assert failed_recall.source_id == case_id
            # The deliberation judgment still ran and recorded its decision.
            deliberation = db.scalar(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "proactive_deliberation"
                )
            )
            assert deliberation is not None
            assert db.scalar(select(func.count()).select_from(ProactiveDecisionRecord)) == 1


def test_deliberation_denies_unadvertised_function_calls(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 7, 12, 2, tzinfo=UTC)
    adapter = ToolCallingProactiveAdapter()

    def fail_execute_capability(**_: Any) -> ExecutionResult:
        raise AssertionError("unadvertised proactive tool call executed a capability")

    monkeypatch.setattr("ariel.proactivity.execute_capability", fail_execute_capability)

    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                decision = db.scalar(select(ProactiveDecisionRecord).limit(1))
                turn = db.scalar(
                    select(NotificationRecord).where(
                        NotificationRecord.source_type == "proactive_turn"
                    )
                )

                assert adapter.calls == 2
                assert decision is not None
                assert decision.context["tool_outputs"] == [
                    {
                        "call_id": "call_unadvertised_tool",
                        "tool_name": "unadvertised_tool",
                        "capability_id": None,
                        "result": {
                            "status": "failed",
                            "error": "proactive_deliberation_tool_denied",
                        },
                    }
                ]
                assert any(
                    item.get("type") == "function_call_output" for item in decision.model_input
                )
                assert decision.tool_refs == []
                assert turn is not None
                assert turn.body == "Leave now from the case evidence."


def test_remember_decision_runs_the_rememberer_and_ask_user_sets_asked(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proactive ``remember`` decision delegates to the rememberer subagent --
    which writes facts directly, with no candidate or review step -- and the
    case resolves with the applied operation count. An ``ask_user`` decision
    sets the case to ``asked`` and emits the user-visible turn."""

    now = datetime(2026, 5, 7, 12, 5, tzinfo=UTC)
    remember = ProactiveAdapter(
        json.dumps(_decision_payload(decision="remember", memory="Phoenix ships tomorrow."))
    )
    ask = ProactiveAdapter(
        json.dumps(
            _decision_payload(
                decision="ask_user",
                user_visible_message="Should I keep watching this approval?",
            )
        )
    )

    # The rememberer is a bounded model call; stub it to write one fact, and
    # stub the embeddings call the write triggers.
    class _Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            self.status_code = 200

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, **_: Any) -> _Response:
        if "embeddings" in url:
            return _Response({"data": [{"embedding": [0.0] * 1535 + [1.0]}]})
        return _Response(
            {
                "id": "resp_rememberer",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "operations": [
                                            {"op": "write", "content": "Phoenix ships tomorrow."}
                                        ],
                                        "profile": None,
                                        "digest": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }
        )

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.httpx.post", fake_post)

    with _build_client(postgres_url, remember) as client:
        remember_case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=remember_case_id, adapter=remember, now=now)
        ask_case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=ask_case_id, adapter=ask, now=now)

        with _session_factory(client)() as db:
            # The rememberer wrote the fact directly -- active immediately, no
            # candidate or review state.
            facts = list(db.execute(text("SELECT content, status FROM memory_facts")))
            assert len(facts) == 1
            assert facts[0][0] == "Phoenix ships tomorrow."
            assert facts[0][1] == "active"
            # Every rememberer run is audited as one memory_remember judgment.
            remember_judgment = db.scalar(
                select(AIJudgmentRecord).where(AIJudgmentRecord.judgment_type == "memory_remember")
            )
            assert remember_judgment is not None
            remember_event = db.scalar(
                select(ProactiveCaseEventRecord)
                .where(
                    ProactiveCaseEventRecord.case_id == remember_case_id,
                    ProactiveCaseEventRecord.event_type == "resolved",
                )
                .order_by(ProactiveCaseEventRecord.created_at.desc())
                .limit(1)
            )
            assert remember_event is not None
            assert remember_event.payload["memory_operation_count"] == 1
            asked_case = db.get(ProactiveCaseRecord, ask_case_id)
            turn = db.scalar(
                select(NotificationRecord).where(
                    NotificationRecord.source_type == "proactive_turn",
                    NotificationRecord.proactive_case_id == ask_case_id,
                )
            )
            assert asked_case is not None
            assert asked_case.status == "asked"
            assert turn is not None
            assert turn.body == "Should I keep watching this approval?"


@pytest.mark.parametrize(
    ("action_type", "target_system", "expected_reason"),
    [
        (
            "send_discord_message",
            "discord",
            "proactive Discord messages must use speak_now or ask_user",
        ),
        (
            "cap.memory.remember",
            "memory",
            "proactive memory updates must use decision=remember",
        ),
    ],
)
def test_act_now_duplicate_action_shapes_are_invalid(
    postgres_url: str,
    action_type: str,
    target_system: str,
    expected_reason: str,
) -> None:
    now = datetime(2026, 5, 7, 12, 10, tzinfo=UTC)
    action = {
        "action_type": action_type,
        "target": "framework",
        "target_system": target_system,
        "payload": {"note": "Duplicate action shape regression."},
        "risk_tier": "low",
    }
    adapter = ProactiveAdapter(json.dumps(_decision_payload(decision="act_now", actions=[action])))

    with _build_client(postgres_url, adapter) as client:
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)
        decision = _latest_decision(client)
        assert decision.policy_result == "invalid_decision"
        assert decision.denial_reason == expected_reason

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                plan = db.scalar(select(ProactiveActionPlanRecord).limit(1))
                assert case is not None
                assert case.status == "failed"
                assert plan is None


def test_failed_action_execution_is_replayable_with_same_execution_record(
    postgres_url: str,
) -> None:
    now = datetime(2026, 5, 7, 12, 15, tzinfo=UTC)
    action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": _email_draft_payload("proactive-replay-draft", "Draft the note."),
        "risk_tier": "low",
    }
    adapter = ProactiveAdapter(json.dumps(_decision_payload(decision="act_now", actions=[action])))
    google_runtime = ProactiveGoogleRuntime(
        [
            ExecutionResult(status="failed", output=None, error="temporary_failure"),
            ExecutionResult(status="succeeded", output={"status": "drafted"}, error=None),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        _seed_scope(
            client,
            action_type="cap.email.draft",
            target_system="gmail",
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
            google_runtime=google_runtime,
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )
        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan_id},
            google_runtime=google_runtime,
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
) -> None:
    now = datetime(2026, 5, 7, 12, 18, tzinfo=UTC)
    action = {
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": _email_draft_payload("proactive-speak-and-act", "Draft the proactive note."),
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

    google_runtime = ProactiveGoogleRuntime(
        [ExecutionResult(status="succeeded", output={"status": "drafted"}, error=None)]
    )
    with _build_client(postgres_url, adapter) as client:
        _seed_scope(
            client,
            action_type="cap.email.draft",
            target_system="gmail",
            now=now,
        )
        case_id = _seed_case(client, now=now)
        _run_deliberation(client, case_id=case_id, adapter=adapter, now=now)

        with _session_factory(client)() as db:
            with db.begin():
                case = db.get(ProactiveCaseRecord, case_id)
                plan = db.scalar(select(ProactiveActionPlanRecord).limit(1))
                turn = db.scalar(
                    select(NotificationRecord).where(
                        NotificationRecord.source_type == "proactive_turn"
                    )
                )
                decision = db.scalar(select(ProactiveDecisionRecord).limit(1))
                assert case is not None
                assert case.status == "spoken"
                assert plan is not None
                assert turn is not None
                assert turn.body == "I am drafting a short note."
                assert decision is not None
                assert decision.policy_result == "authorized"
                plan_id = plan.id

        process_proactive_action_execution_due(
            session_factory=_session_factory(client),
            task_payload={"action_plan_id": plan_id},
            google_runtime=google_runtime,
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
        "action_type": "cap.email.draft",
        "target": "team-email",
        "target_system": "gmail",
        "payload": _email_draft_payload("proactive-tainted-medium", "Draft from tainted text."),
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
            action_type="cap.email.draft",
            target_system="gmail",
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
                decision = db.scalar(select(ProactiveDecisionRecord).limit(1))
                action_count = db.scalar(select(func.count(ProactiveActionPlanRecord.id)))
                turn_count = db.scalar(
                    select(func.count(NotificationRecord.id)).where(
                        NotificationRecord.source_type == "proactive_turn"
                    )
                )

                assert decision is not None
                assert decision.policy_result == "denied"
                assert (
                    decision.denial_reason == "tainted context cannot execute non-low-risk action"
                )
                assert action_count == 0
                assert turn_count == 0
