from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import (
    ActionAttemptRecord,
    AIJudgmentRecord,
    ApprovalRequestRecord,
    BackgroundTaskRecord,
    CaptureRecord,
    GoogleConnectorRecord,
    JobRecord,
    MemoryAssertionRecord,
    MemoryEntityRecord,
    MemoryEvidenceRecord,
    ProactiveCaseRecord,
    ProactiveObservationRecord,
    SessionRecord,
    TurnRecord,
    WorkspaceItemEventRecord,
    WorkspaceItemRecord,
)
from ariel.proactivity import process_ambient_interpretation_due


EXPECTED_SOURCE_TYPES = {
    "approval_request",
    "capture",
    "google_connector",
    "job",
    "memory_assertion",
    "workspace_item",
}


@dataclass
class IdFactory:
    counters: dict[str, int] = field(default_factory=dict)

    def __call__(self, prefix: str) -> str:
        next_value = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = next_value
        return f"{prefix}_{next_value:06d}"


@dataclass
class AmbientInterpreterAdapter:
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, history
        assert tools == []
        assert context_bundle["origin"] == "ambient_interpretation"
        raw_content = input_items[1]["content"]
        assert isinstance(raw_content, str)
        payload = json.loads(raw_content)
        self.candidates = [
            candidate for candidate in payload["candidates"] if isinstance(candidate, dict)
        ]
        observations = [
            {
                "candidate_id": str(candidate["candidate_id"]),
                "observation_key": f"selected-{index}",
                "case_key": f"ambient:{candidate['source_type']}:{candidate['source_id']}",
                "observation_type": "ambient_event",
                "subject": f"AI selected {candidate['source_type']}",
                "summary": "The ambient interpreter selected this source event.",
                "payload": {"source_type": candidate["source_type"]},
                "evidence": {"source_type": candidate["source_type"]},
                "rationale": "The fixture selected this candidate.",
            }
            for index, candidate in enumerate(self.candidates)
        ]
        return {
            "provider": "provider.ambient-test",
            "model": "model.ambient-test",
            "provider_response_id": "resp_ambient_test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "observations": observations,
                                    "omitted": [],
                                    "rationale": "Fixture selected every durable source.",
                                },
                                sort_keys=True,
                            ),
                        }
                    ],
                }
            ],
        }


@dataclass
class InvalidAmbientInterpreterAdapter:
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
            "provider": "provider.ambient-test",
            "model": "model.ambient-test",
            "provider_response_id": "resp_ambient_invalid",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps({"omitted": []})}],
                }
            ],
        }


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    engine.dispose()


def _settings() -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def test_enabled_durable_sources_enter_ai_ambient_interpretation_before_cases_open(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    adapter = AmbientInterpreterAdapter()

    with session_factory() as db:
        with db.begin():
            workspace_event_id = _seed_ambient_sources(db, now=now, new_id=new_id)
            assert db.scalar(select(func.count()).select_from(ProactiveObservationRecord)) == 0
            assert db.scalar(select(func.count()).select_from(ProactiveCaseRecord)) == 0

    process_ambient_interpretation_due(
        session_factory=session_factory,
        task_payload={"workspace_item_event_id": workspace_event_id},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    assert {candidate["source_type"] for candidate in adapter.candidates} == EXPECTED_SOURCE_TYPES

    with session_factory() as db:
        with db.begin():
            observations = db.scalars(select(ProactiveObservationRecord)).all()
            cases = db.scalars(select(ProactiveCaseRecord)).all()
            tasks = db.scalars(select(BackgroundTaskRecord)).all()
            judgment = db.scalar(select(AIJudgmentRecord))

    assert {observation.source_type for observation in observations} == EXPECTED_SOURCE_TYPES
    assert len(cases) == len(EXPECTED_SOURCE_TYPES)
    assert [task.task_type for task in tasks] == ["proactive_deliberation_due"] * len(
        EXPECTED_SOURCE_TYPES
    )
    assert all(
        observation.evidence["ambient_interpretation"]["model"] == "model.ambient-test"
        for observation in observations
    )
    assert judgment is not None
    assert judgment.judgment_type == "ambient_interpretation"
    assert judgment.status == "succeeded"
    assert judgment.model == "model.ambient-test"
    assert judgment.prompt_version == "proactive-ambient-interpretation-v1"
    assert judgment.parse_status == "parsed"
    assert judgment.validation_status == "valid"
    assert len(judgment.selected) == len(EXPECTED_SOURCE_TYPES)


def test_ai_judgment_failure_code_constraint_rejects_unknown_codes(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            db.add(
                AIJudgmentRecord(
                    id="ajg_valid_failure_code",
                    judgment_type="ambient_interpretation",
                    source_type="ambient_batch",
                    source_id="ambient",
                    status="failed",
                    model="model.test",
                    prompt_version="proactive-ambient-interpretation-v1",
                    provider_response_id=None,
                    input_summary="ambient source interpretation",
                    input_refs={},
                    selected=[],
                    omitted=[],
                    output={},
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status="missing_output",
                    validation_status="not_validated",
                    failure_code="E_AI_JUDGMENT_REQUIRED",
                    failure_reason="required",
                    created_at=now,
                    updated_at=now,
                )
            )

    with pytest.raises(IntegrityError):
        with session_factory() as db:
            with db.begin():
                db.add(
                    AIJudgmentRecord(
                        id="ajg_bad_failure_code",
                        judgment_type="ambient_interpretation",
                        source_type="ambient_batch",
                        source_id="ambient",
                        status="failed",
                        model="model.test",
                        prompt_version="proactive-ambient-interpretation-v1",
                        provider_response_id=None,
                        input_summary="ambient source interpretation",
                        input_refs={},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_BAD",
                        failure_reason="bad",
                        created_at=now,
                        updated_at=now,
                    )
                )


def test_invalid_ambient_interpreter_output_is_durably_audited(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
        with db.begin():
            workspace_event_id = _seed_ambient_sources(db, now=now, new_id=new_id)

    with pytest.raises(RuntimeError, match="missing observations"):
        process_ambient_interpretation_due(
            session_factory=session_factory,
            task_payload={"workspace_item_event_id": workspace_event_id},
            settings=_settings(),
            model_adapter=InvalidAmbientInterpreterAdapter(),
            now_fn=lambda: now,
            new_id_fn=new_id,
        )

    with session_factory() as db:
        with db.begin():
            judgment = db.scalar(select(AIJudgmentRecord))
            observation_count = db.scalar(
                select(func.count()).select_from(ProactiveObservationRecord)
            )

    assert observation_count == 0
    assert judgment is not None
    assert judgment.judgment_type == "ambient_interpretation"
    assert judgment.status == "failed"
    assert judgment.provider_response_id == "resp_ambient_invalid"
    assert judgment.parse_status == "schema_invalid"
    assert judgment.validation_status == "invalid"
    assert judgment.failure_code == "E_AI_JUDGMENT_SCHEMA"


def test_empty_ambient_interpretation_sweep_is_durably_audited(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    adapter = AmbientInterpreterAdapter()

    process_ambient_interpretation_due(
        session_factory=session_factory,
        task_payload={"origin": "periodic_sweep"},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            judgment = db.scalar(select(AIJudgmentRecord))
            observation_count = db.scalar(
                select(func.count()).select_from(ProactiveObservationRecord)
            )
            case_count = db.scalar(select(func.count()).select_from(ProactiveCaseRecord))

    assert adapter.candidates == []
    assert observation_count == 0
    assert case_count == 0
    assert judgment is not None
    assert judgment.judgment_type == "ambient_interpretation"
    assert judgment.source_type == "ambient_batch"
    assert judgment.source_id == "periodic_sweep"
    assert judgment.status == "succeeded"
    assert judgment.provider_response_id is None
    assert judgment.input_refs["candidate_count"] == 0
    assert judgment.input_refs["candidate_refs"] == []
    assert judgment.input_refs["task_payload"] == {"origin": "periodic_sweep"}
    assert judgment.selected == []
    assert judgment.omitted == []
    assert judgment.output == {"observations": [], "omitted": []}
    assert judgment.parse_status == "not_required_no_candidates"
    assert judgment.validation_status == "not_validated"
    assert judgment.failure_code is None


def _seed_ambient_sources(db: Session, *, now: datetime, new_id: IdFactory) -> str:
    session_id = new_id("ses")
    turn_id = new_id("trn")
    action_attempt_id = new_id("aat")
    db.add(
        SessionRecord(
            id=session_id,
            is_active=True,
            lifecycle_state="active",
            rotated_from_session_id=None,
            rotation_reason=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        TurnRecord(
            id=turn_id,
            session_id=session_id,
            user_message="Track the Orion launch review.",
            assistant_message="Tracking it.",
            status="completed",
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ActionAttemptRecord(
            id=action_attempt_id,
            session_id=session_id,
            turn_id=turn_id,
            proposal_index=1,
            capability_id="cap.framework.write_draft",
            capability_version="1.0",
            capability_contract_hash="hash",
            impact_level="write_reversible",
            proposed_input={"text": "draft"},
            payload_hash="payload-hash",
            policy_decision="requires_approval",
            policy_reason=None,
            status="awaiting_approval",
            approval_required=True,
            execution_output=None,
            execution_error=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        ApprovalRequestRecord(
            id=new_id("apr"),
            action_attempt_id=action_attempt_id,
            session_id=session_id,
            turn_id=turn_id,
            actor_id="usr_owner",
            status="pending",
            payload_hash="payload-hash",
            expires_at=now + timedelta(minutes=30),
            decision_reason=None,
            decided_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()

    evidence_id = new_id("mev")
    entity_id = new_id("men")
    assertion_id = new_id("mas")
    db.add(
        MemoryEvidenceRecord(
            id=evidence_id,
            source_turn_id=turn_id,
            source_session_id=session_id,
            actor_id="usr_owner",
            content_class="user_message",
            trust_boundary="trusted_user",
            lifecycle_state="available",
            source_uri=None,
            source_artifact_id=None,
            source_text="Remind me when Orion launch review status changes.",
            evidence_snippet="Orion launch review status changes",
            redaction_posture="none",
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryEntityRecord(
            id=entity_id,
            entity_type="project",
            entity_key="project:orion",
            display_name="Orion",
            summary=None,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryAssertionRecord(
            id=assertion_id,
            subject_entity_id=entity_id,
            subject_key="project:orion",
            predicate="commitment.monitor_status",
            scope_key="project:orion",
            object_value={"value": "Watch launch review status changes."},
            assertion_type="commitment",
            is_multi_valued=False,
            scope={},
            lifecycle_state="active",
            confidence=0.9,
            valid_from=None,
            valid_to=None,
            superseded_by_assertion_id=None,
            extraction_model="model.memory-fixture",
            extraction_prompt_version="memory-v1",
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()

    workspace_item_id = new_id("wki")
    workspace_event_id = new_id("wie")
    db.add(
        WorkspaceItemRecord(
            id=workspace_item_id,
            provider="google",
            item_type="calendar_event",
            external_id="calendar-orion",
            title="Orion launch review",
            summary="Launch review moved.",
            source_uri="https://calendar.google.com/event?eid=calendar-orion",
            status="active",
            item_metadata={"resource_id": "primary"},
            observed_at=now,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        WorkspaceItemEventRecord(
            id=workspace_event_id,
            workspace_item_id=workspace_item_id,
            dedupe_key="google:calendar:primary:calendar-orion:active:2026-05-07T12:00:00Z",
            provider_event_id=None,
            event_type="created",
            payload={"title": "Orion launch review", "status": "active"},
            created_at=now,
        )
    )

    db.add(
        JobRecord(
            id=new_id("job"),
            session_id=None,
            turn_id=None,
            action_attempt_id=None,
            source="agency",
            external_job_id="agency-orion",
            title="Orion launch review",
            status="waiting_approval",
            summary="Agency is waiting for approval.",
            latest_payload={"status": "waiting_approval"},
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        GoogleConnectorRecord(
            id=new_id("gcn"),
            provider="google",
            status="error",
            account_subject="sub-1",
            account_email="owner@example.com",
            granted_scopes=["calendar.readonly"],
            access_token_enc=None,
            refresh_token_enc=None,
            access_token_expires_at=None,
            token_obtained_at=None,
            encryption_key_version="v1",
            last_error_code="refresh_failed",
            last_error_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        CaptureRecord(
            id=new_id("cap"),
            capture_kind="text",
            idempotency_key=None,
            request_hash="capture-hash",
            original_payload={"text": "Orion review moved"},
            normalized_turn_input="Orion review moved",
            effective_session_id=session_id,
            turn_id=turn_id,
            terminal_state="turn_created",
            ingest_error_code=None,
            ingest_error_message=None,
            ingest_error_details=None,
            ingest_error_retryable=None,
            status_code=201,
            response_payload={"turn_id": turn_id},
            created_at=now,
            updated_at=now,
        )
    )
    return workspace_event_id
