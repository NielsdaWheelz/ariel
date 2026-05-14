from __future__ import annotations

import copy
import json
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from ariel.action_runtime import process_action_execution_task
from ariel.app import ModelAdapter, create_app
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.config import AppSettings
import ariel.memory as memory
from ariel.memory import (
    MEMORY_PROJECTION_VERSION,
    process_memory_extract_turn,
    process_memory_projection_job,
)
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryContextBlockRecord,
    MemoryConflictSetRecord,
    MemoryDeletionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryExportArtifactRecord,
    MemoryGraphProjectionRecord,
    MemoryEvidenceRecord,
    MemoryKeywordProjectionRecord,
    MemoryProcedureRecord,
    MemoryRetentionPolicyRecord,
    MemoryReviewRecord,
    MemoryScopeBindingRecord,
    MemoryTopicRecord,
    MemoryVersionRecord,
    ProjectStateSnapshotRecord,
    ProactiveCaseEventRecord,
    SessionRecord,
    TurnRecord,
)
from ariel.proactivity import process_proactive_deliberation_due, upsert_proactive_observation
from tests.integration.responses_helpers import responses_message


_id_counter = count(1)


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


def _new_id(prefix: str) -> str:
    return f"{prefix}_nsm_{next(_id_counter)}"


def _settings(**overrides: Any) -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None, **overrides))


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


@dataclass
class ProactiveRememberAdapter:
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
            "provider": "provider.north-star-memory",
            "model": "model.north-star-memory",
            "provider_response_id": "resp_north_star_memory",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "decision": "remember",
                                    "confidence": 0.91,
                                    "urgency": "normal",
                                    "rationale": "The observation contains durable project state.",
                                    "evidence_refs": ["latest_observation"],
                                    "tool_refs": [],
                                    "actions": [],
                                    "follow_up": None,
                                    "memory": {
                                        "subject_key": "project:phoenix",
                                        "predicate": "project.deadline",
                                        "value": "ship tomorrow",
                                        "assertion_type": "project_state",
                                    },
                                }
                            ),
                        }
                    ],
                }
            ],
        }


@dataclass
class MemoryContextProbeAdapter:
    provider: str = "provider.north-star-memory"
    model: str = "model.north-star-memory"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, history
        if context_bundle.get("origin") == "tool_strategy":
            return responses_message(
                assistant_text=json.dumps(
                    {
                        "decision": "no_tools",
                        "selected_capability_ids": [],
                        "rationale": "memory context test needs no tools",
                        "unavailable_reason": None,
                        "confidence": 1.0,
                    },
                    sort_keys=True,
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_north_star_memory_strategy",
                input_tokens=2,
                output_tokens=2,
            )
        self.context_bundles.append(copy.deepcopy(context_bundle))
        return responses_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_north_star_memory_context",
            input_tokens=12,
            output_tokens=8,
        )


def _build_client(
    postgres_url: str,
    adapter: ModelAdapter,
    *,
    reset_database: bool = True,
) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=reset_database,
    )
    return TestClient(app)


def _fake_memory_embedding(text: str, *, settings: AppSettings) -> list[float]:
    vector = [0.0] * settings.memory_embedding_dimensions
    vector[0 if "phoenix" in text.lower() else 1] = 1.0
    return vector


def _fake_memory_curation(
    *,
    user_message: str,
    history: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    max_selected: int,
    settings: AppSettings,
) -> dict[str, Any]:
    del user_message, history, settings
    selected = [
        {
            "id": candidate["id"],
            "kind": candidate.get("kind", "semantic_assertion"),
            "rationale": "fixture selected",
        }
        for candidate in candidates[:max_selected]
    ]
    selected_ids = {item["id"] for item in selected}
    return {
        "selected_memories": selected,
        "omitted_memories": [
            {
                "id": candidate["id"],
                "kind": candidate.get("kind", "semantic_assertion"),
                "rationale": "fixture omitted by selection budget",
            }
            for candidate in candidates
            if candidate["id"] not in selected_ids
        ],
        "rationale": "fixture curation",
        "uncertainty": "",
        "confidence": 0.9,
        "model": "fixture-memory-curator",
        "prompt_version": memory.MEMORY_CURATION_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_memory_curator",
        "parse_status": "parsed",
    }


def _use_fake_memory_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _fake_memory_curation)


def _session_id(client: TestClient) -> str:
    response = client.get("/v1/sessions/active")
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _candidate(
    client: TestClient,
    *,
    value: str = "phoenix ships tomorrow",
    assertion_type: str = "project_state",
    subject_key: str = "project:phoenix",
    predicate: str = "project.deadline",
    evidence_text: str | None = None,
    is_multi_valued: bool = False,
) -> dict[str, Any]:
    response = client.post(
        "/v1/memory/candidates",
        json={
            "subject_key": subject_key,
            "predicate": predicate,
            "assertion_type": assertion_type,
            "value": value,
            "evidence_text": evidence_text or f"The user said {value}.",
            "confidence": 0.94,
            "is_multi_valued": is_multi_valued,
        },
    )
    assert response.status_code == 200
    return response.json()["candidates"][0]


def _process_projection(client: TestClient) -> None:
    processed = process_memory_projection_job(
        session_factory=_session_factory(client),
        settings=_settings(),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    )
    assert processed is True


def _seed_proactive_case(client: TestClient, *, now: datetime) -> str:
    with _session_factory(client)() as db:
        with db.begin():
            case_id = upsert_proactive_observation(
                db,
                dedupe_key=f"dedupe:{_new_id('obs')}",
                case_key=f"case:{_new_id('pca')}",
                source_type="job",
                source_id="job_north_star_memory",
                observation_type="job_state",
                subject="Phoenix launch",
                summary="Phoenix launch should ship tomorrow.",
                payload={"status": "waiting"},
                evidence={"job_id": "job_north_star_memory"},
                taint={"provenance_status": "trusted_internal"},
                trust_boundary="trusted_internal",
                observed_at=now,
                workspace_item_id=None,
                now=now,
                new_id_fn=_new_id,
            )
            assert case_id is not None
            return case_id


def _seed_memory_action_attempt(
    client: TestClient,
    *,
    action_attempt_id: str,
    capability_id: str,
    proposed_input: dict[str, Any],
) -> None:
    capability = get_capability(capability_id)
    assert capability is not None
    session_id = _session_id(client)
    action_hash = payload_hash(
        canonical_action_payload(capability_id=capability_id, input_payload=proposed_input)
    )
    now = datetime.now(tz=UTC)
    with _session_factory(client)() as db:
        with db.begin():
            if db.get(SessionRecord, session_id) is None:
                db.add(
                    SessionRecord(
                        id=session_id,
                        is_active=True,
                        lifecycle_state="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
            turn_id = f"turn_{action_attempt_id}"
            if db.get(TurnRecord, turn_id) is None:
                db.add(
                    TurnRecord(
                        id=turn_id,
                        session_id=session_id,
                        user_message="memory privacy action",
                        assistant_message=None,
                        status="in_progress",
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
                    capability_id=capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=proposed_input,
                    payload_hash=action_hash,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="executing",
                    approval_required=True,
                    execution_output=None,
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _run_memory_action(client: TestClient, action_attempt_id: str) -> dict[str, Any]:
    processed = process_action_execution_task(
        session_factory=_session_factory(client),
        action_attempt_id=action_attempt_id,
        google_runtime=None,
        agency_runtime=None,
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    )
    assert processed is True
    with _session_factory(client)() as db:
        attempt = db.get(ActionAttemptRecord, action_attempt_id)
        assert attempt is not None
        assert attempt.status == "succeeded"
        assert isinstance(attempt.execution_output, dict)
        return attempt.execution_output


def test_proactive_remember_uses_candidate_lifecycle_with_evidence_not_direct_active_write(
    postgres_url: str,
) -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    adapter = ProactiveRememberAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        case_id = _seed_proactive_case(client, now=now)

        process_proactive_deliberation_due(
            session_factory=_session_factory(client),
            task_payload={"case_id": case_id},
            settings=_settings(),
            model_adapter=adapter,
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            assertions = db.scalars(
                select(MemoryAssertionRecord).where(
                    MemoryAssertionRecord.subject_key == "project:phoenix",
                    MemoryAssertionRecord.predicate == "project.deadline",
                )
            ).all()
            assert len(assertions) == 1
            assertion = assertions[0]
            assert assertion.lifecycle_state == "candidate"

            evidence_link_count = db.scalar(
                select(func.count())
                .select_from(MemoryAssertionEvidenceRecord)
                .where(MemoryAssertionEvidenceRecord.assertion_id == assertion.id)
            )
            assert evidence_link_count == 1
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEvidenceRecord)
                    .where(MemoryEvidenceRecord.lifecycle_state == "available")
                )
                >= 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryReviewRecord)
                    .where(
                        MemoryReviewRecord.assertion_id == assertion.id,
                        MemoryReviewRecord.decision == "needs_user_review",
                    )
                )
                == 1
            )
            resolved_event = db.scalar(
                select(ProactiveCaseEventRecord)
                .where(
                    ProactiveCaseEventRecord.case_id == case_id,
                    ProactiveCaseEventRecord.event_type == "resolved",
                )
                .order_by(ProactiveCaseEventRecord.created_at.desc())
                .limit(1)
            )
            assert resolved_event is not None
            memory_event_types = [
                event.get("event_type")
                for event in resolved_event.payload.get("memory_events", [])
                if isinstance(event, dict)
            ]
            assert "evt.memory.evidence_recorded" in memory_event_types
            assert "evt.memory.candidate_proposed" in memory_event_types
            assert "evt.memory.candidate_approved" not in memory_event_types
            assert "evt.memory.assertion_activated" not in memory_event_types
            assert resolved_event.payload["memory_candidate_assertion_id"] == assertion.id


def test_session_no_memory_disables_search_endpoint_and_proactive_remember(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    adapter = ProactiveRememberAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        response = client.put(
            f"/v1/sessions/{session_id}/memory-mode",
            json={"memory_mode": "no_memory"},
        )
        assert response.status_code == 200

        search = client.get("/v1/memory/search", params={"q": "phoenix", "limit": 10})
        assert search.status_code == 200
        assert search.json()["results"] == []

        diagnostics = client.get(
            "/v1/memory/recall-diagnostics",
            params={"q": "phoenix", "limit": 10, "scope_key": "project:phoenix"},
        )
        assert diagnostics.status_code == 200
        assert diagnostics.json()["recall_diagnostics"]["selected_memory_ids"] == []

        case_id = _seed_proactive_case(client, now=now)
        before_assertion_count = 0
        with _session_factory(client)() as db:
            before_assertion_count = db.scalar(
                select(func.count()).select_from(MemoryAssertionRecord)
            )

        process_proactive_deliberation_due(
            session_factory=_session_factory(client),
            task_payload={"case_id": case_id},
            settings=_settings(),
            model_adapter=adapter,
            now_fn=lambda: now,
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            assert (
                db.scalar(select(func.count()).select_from(MemoryAssertionRecord))
                == before_assertion_count
            )
            resolved_event = db.scalar(
                select(ProactiveCaseEventRecord)
                .where(
                    ProactiveCaseEventRecord.case_id == case_id,
                    ProactiveCaseEventRecord.event_type == "resolved",
                )
                .order_by(ProactiveCaseEventRecord.created_at.desc())
                .limit(1)
            )
            if resolved_event is not None:
                assert resolved_event.payload.get("memory_candidate_assertion_id") is None
                assert resolved_event.payload.get("memory_events") == []


def test_extracted_candidate_links_original_turn_evidence(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember phoenix ships tomorrow"},
        )
        assert response.status_code == 200

        with _session_factory(client)() as db:
            evidence_id = db.scalar(
                select(MemoryEvidenceRecord.id)
                .where(
                    MemoryEvidenceRecord.source_session_id == session_id,
                    MemoryEvidenceRecord.content_class == "user_message",
                )
                .order_by(MemoryEvidenceRecord.created_at.desc())
                .limit(1)
            )
            assert evidence_id is not None

        class ExtractionResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "id": "resp_extract_memory",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "candidates": [
                                                {
                                                    "subject_key": "project:phoenix",
                                                    "predicate": "project.deadline",
                                                    "assertion_type": "project_state",
                                                    "value": "phoenix ships tomorrow",
                                                    "confidence": 0.9,
                                                    "is_multi_valued": False,
                                                }
                                            ]
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                }

        monkeypatch.setattr(memory.httpx, "post", lambda *args, **kwargs: ExtractionResponse())
        process_memory_extract_turn(
            session_factory=_session_factory(client),
            task_payload={"session_id": session_id, "evidence_id": evidence_id},
            settings=_settings(openai_api_key="test-key"),
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            assertion_id = db.scalar(
                select(MemoryAssertionRecord.id).where(
                    MemoryAssertionRecord.subject_key == "project:phoenix"
                )
            )
            assert assertion_id is not None
            linked_evidence_ids = db.scalars(
                select(MemoryAssertionEvidenceRecord.evidence_id).where(
                    MemoryAssertionEvidenceRecord.assertion_id == assertion_id
                )
            ).all()
            assert linked_evidence_ids == [evidence_id]
            assert db.scalar(select(func.count()).select_from(MemoryEvidenceRecord)) == 2


def test_conflicted_candidates_require_conflict_resolution_and_reject_losers(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200

        second = _candidate(client, value="phoenix ships next week")
        approve_conflicted = client.post(f"/v1/memory/candidates/{second['id']}/approve")
        assert approve_conflicted.status_code == 409
        payload = client.get("/v1/memory").json()
        assert [item["id"] for item in payload["active_assertions"]] == [first["id"]]
        assert payload["conflicts"]
        conflict_id = payload["conflicts"][0]["id"]

        with _session_factory(client)() as db:
            conflicted = db.get(MemoryAssertionRecord, second["id"])
            assert conflicted is not None
            assert conflicted.lifecycle_state == "conflicted"

        resolved = client.post(
            f"/v1/memory/conflicts/{conflict_id}/resolve",
            json={"assertion_id": second["id"]},
        )
        assert resolved.status_code == 200
        assert [item["id"] for item in resolved.json()["active_assertions"]] == [second["id"]]

        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            assert conflict.lifecycle_state == "resolved"
            assert conflict.resolution_assertion_id == second["id"]
            winner = db.get(MemoryAssertionRecord, second["id"])
            loser = db.get(MemoryAssertionRecord, first["id"])
            assert winner is not None
            assert loser is not None
            assert winner.lifecycle_state == "active"
            assert loser.lifecycle_state == "rejected"


def test_rejected_conflict_member_cannot_be_reactivated_by_resolution(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        reject = client.post(f"/v1/memory/candidates/{second['id']}/reject")
        assert reject.status_code == 200
        revived = client.post(
            f"/v1/memory/conflicts/{conflict_id}/resolve",
            json={"assertion_id": second["id"]},
        )
        assert revived.status_code == 409
        assert revived.json()["error"]["code"] == "E_MEMORY_CONFLICT_NOT_APPLICABLE"
        with _session_factory(client)() as db:
            rejected = db.get(MemoryAssertionRecord, second["id"])
            active = db.get(MemoryAssertionRecord, first["id"])
            assert rejected is not None
            assert active is not None
            assert rejected.lifecycle_state == "rejected"
            assert active.lifecycle_state == "active"


def test_memory_context_exposes_hot_index_and_topic_projection_fields_if_implemented(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="what is hot for phoenix?",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        assert isinstance(memory_context["hot_index"], list)
        assert isinstance(memory_context["topic_index"], list)
        assert memory_context["hot_index"]
        assert memory_context["topic_index"]
        assert memory_context["hot_index"][0]["source_assertion_ids"]
        assert "phoenix" in memory_context["topic_index"][0]["content"].lower()


def test_delete_memory_assertion_removes_active_memory_and_projection_rows(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        candidate = _candidate(client)
        assertion_id = candidate["id"]
        assert client.post(f"/v1/memory/candidates/{assertion_id}/approve").status_code == 200
        _process_projection(client)

        delete = client.delete(f"/v1/memory/assertions/{assertion_id}")
        assert delete.status_code == 200
        assert delete.json()["active_assertions"] == []

        with _session_factory(client)() as db:
            assertion = db.get(MemoryAssertionRecord, assertion_id)
            assert assertion is not None
            assert assertion.lifecycle_state == "deleted"
            assert assertion.valid_to is not None
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProjectionRecord)
                    .where(
                        MemoryEmbeddingProjectionRecord.assertion_id == assertion_id,
                        MemoryEmbeddingProjectionRecord.projection_version
                        == MEMORY_PROJECTION_VERSION,
                    )
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryKeywordProjectionRecord)
                    .where(
                        MemoryKeywordProjectionRecord.canonical_id == assertion_id,
                        MemoryKeywordProjectionRecord.projection_version
                        == MEMORY_PROJECTION_VERSION,
                    )
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ProjectStateSnapshotRecord)
                    .where(
                        ProjectStateSnapshotRecord.lifecycle_state == "active",
                        ProjectStateSnapshotRecord.source_assertion_ids.contains([assertion_id]),
                    )
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryContextBlockRecord)
                    .where(
                        MemoryContextBlockRecord.lifecycle_state == "active",
                        MemoryContextBlockRecord.source_assertion_ids.contains([assertion_id]),
                    )
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryDeletionRecord)
                    .where(
                        MemoryDeletionRecord.target_table == "memory_assertions",
                        MemoryDeletionRecord.target_id == assertion_id,
                        MemoryDeletionRecord.deletion_type == "delete",
                    )
                )
                == 1
            )

        response = client.post(
            f"/v1/sessions/{_session_id(client)}/message",
            json={"message": "what is hot for phoenix now?"},
        )
        assert response.status_code == 200, response.text
        memory_context = adapter.context_bundles[-1]["memory_context"]
        assert memory_context["project_state"] == []
        assert memory_context["hot_index"] == []
        assert memory_context["topic_index"] == []


def test_delete_memory_assertion_invalidates_graph_and_export_artifacts(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client)
        second = _candidate(
            client,
            subject_key="project:atlas",
            predicate="project.deadline",
            value="atlas ships next week",
            evidence_text="The user said Atlas ships next week.",
        )
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 200

        with _session_factory(client)() as db:
            first_assertion = db.get(MemoryAssertionRecord, first["id"])
            second_assertion = db.get(MemoryAssertionRecord, second["id"])
            assert first_assertion is not None
            assert second_assertion is not None
            evidence_id = db.scalar(
                select(MemoryAssertionEvidenceRecord.evidence_id)
                .where(MemoryAssertionEvidenceRecord.assertion_id == first["id"])
                .limit(1)
            )
            assert evidence_id is not None
            first_entity_id = first_assertion.subject_entity_id
            second_entity_id = second_assertion.subject_entity_id
        relationship = client.post(
            "/v1/memory/relationships",
            json={
                "source_entity_id": first_entity_id,
                "target_entity_id": second_entity_id,
                "relationship_type": "related_project",
                "evidence_id": evidence_id,
                "scope_key": "global",
                "confidence": 0.9,
            },
        )
        assert relationship.status_code == 200

        export = client.post("/v1/memory/export", json={"scope_key": "global"})
        assert export.status_code == 200

        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryGraphProjectionRecord)) == 1
            assert db.scalar(select(func.count()).select_from(MemoryExportArtifactRecord)) == 1

        delete = client.delete(f"/v1/memory/assertions/{first['id']}")
        assert delete.status_code == 200
        deletion = delete.json()["deletions"][0]
        assert deletion["projection_invalidation"]["deleted_rows"]["memory_graph_projections"]
        assert deletion["projection_invalidation"]["invalidated_exports"]

        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryGraphProjectionRecord)) == 0
            artifact = db.scalar(select(MemoryExportArtifactRecord).limit(1))
            assert artifact is not None
            assert artifact.status == "failed"


def test_unrelated_recall_has_no_semantic_recency_candidate(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(
            client,
            subject_key="user:default",
            predicate="notebook.preference",
            assertion_type="preference",
            value="Use matte notebooks.",
            evidence_text="The user said to use matte notebooks.",
        )
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="zebra migration window",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )

        semantic_candidates = [
            item
            for item in memory_context["recall_window"]["candidate_memories"]
            if item["kind"] == "semantic_assertion"
        ]
        assert semantic_candidates == []
        assert client.get("/v1/memory/search", params={"q": "zebra"}).json()["results"] == []


def test_recall_diagnostics_endpoint_exposes_curation_and_policy(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200

        response = client.get("/v1/memory/recall-diagnostics", params={"q": "phoenix"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert candidate["id"] in payload["recall_diagnostics"]["candidate_memory_ids"]
        assert candidate["id"] in payload["recall_diagnostics"]["selected_memory_ids"]
        assert payload["projection_health"]["selected_memory_count"] >= 1


def test_project_scope_binding_blocks_project_recall_and_search(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        actor_id = str(cast(Any, client.app).state.approval_actor_id)
        scope_response = client.put(
            "/v1/memory/scope-bindings",
            json={
                "scope_type": "project",
                "scope_key": "project:phoenix",
                "memory_mode": "no_memory",
                "reason": "project memory disabled",
            },
        )
        assert scope_response.status_code == 200
        assert scope_response.json()["scope_bindings"][0]["memory_mode"] == "no_memory"
        with _session_factory(client)() as db:
            with db.begin():
                memory_context, _event_payload = memory.build_memory_context(
                    db,
                    user_message="what is phoenix deadline?",
                    max_recalled_assertions=8,
                    settings=_settings(),
                    current_session_id=session_id,
                    scope_key="project:phoenix",
                    actor_id=actor_id,
                )
                proactive_context, _proactive_payload = memory.build_memory_context(
                    db,
                    user_message="what is phoenix deadline?",
                    max_recalled_assertions=8,
                    settings=_settings(),
                    current_session_id=session_id,
                    scope_key="project:phoenix",
                    actor_id="system",
                )
        assert memory_context["semantic_assertions"] == []
        assert memory_context["memory_policy"]["binding_scope_type"] == "project"
        assert proactive_context["semantic_assertions"] == []
        assert proactive_context["memory_policy"]["binding_scope_type"] == "project"

        search = client.get(
            "/v1/memory/search",
            params={"q": "phoenix", "limit": 10, "scope_key": "project:phoenix"},
        )
        assert search.status_code == 200
        assert search.json()["results"] == []

        diagnostics = client.get(
            "/v1/memory/recall-diagnostics",
            params={"q": "phoenix", "limit": 10, "scope_key": "project:phoenix"},
        )
        assert diagnostics.status_code == 200
        assert diagnostics.json()["recall_diagnostics"]["selected_memory_ids"] == []


def test_consolidation_respects_no_memory_mode(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        assert (
            client.put(
                f"/v1/sessions/{session_id}/memory-mode", json={"memory_mode": "no_memory"}
            ).status_code
            == 200
        )

        response = client.post("/v1/memory/consolidate", json={"scope_key": "global"})
        assert response.status_code == 200
        assert response.json()["context_blocks"] == []
        with _session_factory(client)() as db:
            with db.begin():
                result = memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    source_session_id=session_id,
                    now_fn=lambda: datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                )
        assert result["status"] == "skipped"
        assert result["memory_policy"]["memory_mode"] == "no_memory"


def test_import_is_cutover_only(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        response = client.post(
            "/v1/memory/import",
            json={
                "candidates": [
                    {
                        "subject_key": "user:default",
                        "predicate": "notebook.preference",
                        "assertion_type": "preference",
                        "value": "Use matte notebooks.",
                        "evidence_text": "The user said to use matte notebooks.",
                        "confidence": 0.9,
                        "scope_key": "global",
                        "is_multi_valued": False,
                    }
                ]
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "E_MEMORY_IMPORT_DISABLED"


def test_eval_runs_recall_cases_and_records_metrics(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200

        response = client.post(
            "/v1/memory/evals",
            json={
                "eval_name": "phoenix recall",
                "cases": [
                    {
                        "query": "what is phoenix deadline?",
                        "expected_memory_ids": [candidate["id"]],
                        "expected_kinds": ["semantic_assertion"],
                    }
                ],
            },
        )
        assert response.status_code == 200
        eval_run = response.json()["eval_runs"][0]
        assert eval_run["status"] == "completed"
        assert eval_run["metrics"]["passed_cases"] == 1
        assert eval_run["metrics"]["failed_cases"] == 0


def test_memory_public_api_inspects_versions_mutations_and_typed_errors(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        second = _candidate(
            client,
            subject_key="project:phoenix",
            predicate="project.codename",
            value="phoenix codename is ember",
        )
        edit = client.post(
            f"/v1/memory/candidates/{first['id']}/edit",
            json={"value": "phoenix ships Friday"},
        )
        assert edit.status_code == 200
        merge = client.post(
            "/v1/memory/candidates/merge",
            json={"assertion_ids": [first["id"], second["id"]]},
        )
        assert merge.status_code == 200

        assertion = client.get(f"/v1/memory/assertions/{first['id']}")
        assert assertion.status_code == 200
        assert (
            assertion.json()["assertion"]["value"]
            == "phoenix ships Friday; phoenix codename is ember"
        )
        evidence_id = assertion.json()["assertion"]["evidence_refs"][0]["evidence_id"]
        evidence = client.get(f"/v1/memory/evidence/{evidence_id}")
        assert evidence.status_code == 200
        assert evidence.json()["evidence"]["id"] == evidence_id
        versions = client.get(f"/v1/memory/versions/memory_assertions/{first['id']}")
        assert versions.status_code == 200
        assert [item["change_type"] for item in versions.json()["versions"]] == [
            "created",
            "updated",
            "updated",
        ]

        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        consolidate = client.post("/v1/memory/consolidate", json={"scope_key": "global"})
        assert consolidate.status_code == 200
        hot_index_id = next(
            block["id"]
            for block in consolidate.json()["context_blocks"]
            if block["block_type"] == "hot_index"
        )
        consolidation = client.get(f"/v1/memory/consolidations/{hot_index_id}")
        assert consolidation.status_code == 200
        assert consolidation.json()["consolidation"]["context_block_id"] == hot_index_id
        health = client.get("/v1/memory/projection-health")
        assert health.status_code == 200
        assert health.json()["projection_health"]["projection_version"] == MEMORY_PROJECTION_VERSION

        stale = client.post(
            f"/v1/memory/assertions/{first['id']}/mark-stale",
            json={"reason": "deadline passed"},
        )
        assert stale.status_code == 200
        assert stale.json()["active_assertions"] == []

        missing_conflict = client.get("/v1/memory/conflicts/mcf_missing")
        assert missing_conflict.status_code == 404
        assert missing_conflict.json()["error"]["code"] == "E_MEMORY_CONFLICT_NOT_FOUND"
        approve_stale = client.post(f"/v1/memory/candidates/{first['id']}/approve")
        assert approve_stale.status_code == 409
        assert approve_stale.json()["error"]["code"] == "E_MEMORY_OPERATION_NOT_APPLICABLE"
        correct_stale = client.post(
            f"/v1/memory/assertions/{first['id']}/correct",
            json={"value": "phoenix ships later"},
        )
        assert correct_stale.status_code == 409
        assert correct_stale.json()["error"]["code"] == "E_MEMORY_OPERATION_NOT_APPLICABLE"
        prioritize_stale = client.post(f"/v1/memory/assertions/{first['id']}/prioritize")
        assert prioritize_stale.status_code == 409
        assert prioritize_stale.json()["error"]["code"] == "E_MEMORY_OPERATION_NOT_APPLICABLE"
        approve_missing = client.post("/v1/memory/candidates/mas_missing/approve")
        assert approve_missing.status_code == 404
        assert approve_missing.json()["error"]["code"] == "E_MEMORY_ASSERTION_NOT_FOUND"
        redact_missing = client.post("/v1/memory/evidence/mev_missing/redact")
        assert redact_missing.status_code == 404
        assert redact_missing.json()["error"]["code"] == "E_MEMORY_EVIDENCE_NOT_FOUND"


def test_delete_procedure_assertion_removes_procedural_recall(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(
            client,
            subject_key="project:phoenix",
            predicate="procedure.deploy",
            assertion_type="procedure",
            value="Before deploying Phoenix, run smoke tests.",
            evidence_text="The user said the Phoenix deploy procedure is to run smoke tests first.",
        )
        assertion_id = candidate["id"]
        assert client.post(f"/v1/memory/candidates/{assertion_id}/approve").status_code == 200

        memory_payload = client.get("/v1/memory")
        assert memory_payload.status_code == 200
        assert [
            procedure["source_assertion_id"] for procedure in memory_payload.json()["procedures"]
        ] == [assertion_id]

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="how should i deploy phoenix?",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        assert [item["source_assertion_id"] for item in memory_context["procedural_memory"]] == [
            assertion_id
        ]

        delete = client.delete(f"/v1/memory/assertions/{assertion_id}")
        assert delete.status_code == 200
        assert delete.json()["procedures"] == []

        with _session_factory(client)() as db:
            procedure = db.scalar(
                select(MemoryProcedureRecord).where(
                    MemoryProcedureRecord.source_assertion_id == assertion_id
                )
            )
            assert procedure is not None
            assert procedure.lifecycle_state == "deleted"

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="how should i deploy phoenix now?",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        assert memory_context["procedural_memory"] == []


def test_memory_privacy_delete_redact_and_never_remember_actions(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        delete_candidate = _candidate(
            client,
            value="phoenix launch secret is code blue",
            evidence_text="The user's private launch secret is code blue.",
        )
        delete_id = delete_candidate["id"]
        assert client.post(f"/v1/memory/candidates/{delete_id}/approve").status_code == 200
        redact_candidate = _candidate(
            client,
            subject_key="project:orion",
            predicate="project.secret",
            value="orion secret is code red",
            evidence_text="The user's private Orion secret is code red.",
        )
        redact_id = redact_candidate["id"]
        assert client.post(f"/v1/memory/candidates/{redact_id}/approve").status_code == 200

        with _session_factory(client)() as db:
            redact_evidence_id = db.scalar(
                select(MemoryAssertionEvidenceRecord.evidence_id)
                .where(MemoryAssertionEvidenceRecord.assertion_id == redact_id)
                .limit(1)
            )
            assert redact_evidence_id is not None

        _seed_memory_action_attempt(
            client,
            action_attempt_id="act_privacy_delete_memory",
            capability_id="cap.memory.privacy_delete",
            proposed_input={"assertion_id": delete_id},
        )
        privacy_output = _run_memory_action(client, "act_privacy_delete_memory")
        assert privacy_output["status"] == "privacy_deleted"

        _seed_memory_action_attempt(
            client,
            action_attempt_id="act_redact_memory_evidence",
            capability_id="cap.memory.redact_evidence",
            proposed_input={
                "evidence_id": redact_evidence_id,
                "reason": "user requested source redaction",
            },
        )
        redact_output = _run_memory_action(client, "act_redact_memory_evidence")
        assert redact_output["status"] == "redacted"

        _seed_memory_action_attempt(
            client,
            action_attempt_id="act_never_remember_memory",
            capability_id="cap.memory.set_never_remember",
            proposed_input={"scope_key": "global", "rule": "do not remember launch secrets"},
        )
        never_output = _run_memory_action(client, "act_never_remember_memory")
        assert never_output["status"] == "recorded"

        blocked = client.post(
            "/v1/memory/candidates",
            json={
                "subject_key": "project:blocked",
                "predicate": "project.secret",
                "assertion_type": "project_state",
                "value": "launch secrets should not be retained",
                "evidence_text": "Please do not remember launch secrets.",
                "confidence": 0.94,
            },
        )
        assert blocked.status_code == 200
        assert blocked.json()["candidates"] == []

        with _session_factory(client)() as db:
            deleted_assertion = db.get(MemoryAssertionRecord, delete_id)
            redacted_evidence = db.get(MemoryEvidenceRecord, redact_evidence_id)
            assert deleted_assertion is not None
            assert redacted_evidence is not None
            assert deleted_assertion.lifecycle_state == "privacy_deleted"
            assert deleted_assertion.object_value == {"text": "[privacy_deleted]"}
            assert redacted_evidence.lifecycle_state == "redacted"
            assert redacted_evidence.redaction_posture == "redacted"
            assert redacted_evidence.source_text == "[redacted]"
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryDeletionRecord)
                    .where(
                        MemoryDeletionRecord.target_id == delete_id,
                        MemoryDeletionRecord.deletion_type == "privacy_delete",
                        MemoryDeletionRecord.redaction_posture == "privacy_deleted",
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryDeletionRecord)
                    .where(
                        MemoryDeletionRecord.target_id == redact_evidence_id,
                        MemoryDeletionRecord.deletion_type == "redact",
                        MemoryDeletionRecord.redaction_posture == "redacted",
                    )
                )
                == 1
            )
            policy = db.scalar(
                select(MemoryRetentionPolicyRecord)
                .where(
                    MemoryRetentionPolicyRecord.policy_kind == "never_remember",
                    MemoryRetentionPolicyRecord.pattern == "do not remember launch secrets",
                    MemoryRetentionPolicyRecord.lifecycle_state == "active",
                )
                .limit(1)
            )
            assert policy is not None


def test_privacy_delete_scrubs_projection_content_and_scoped_export_filters_evidence(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        secret = _candidate(
            client,
            value="phoenix launch secret is code violet",
            evidence_text="The Phoenix launch secret is code violet.",
        )
        unrelated = _candidate(
            client,
            subject_key="project:orion",
            predicate="project.secret",
            value="orion secret is code orange",
            evidence_text="The Orion secret is code orange.",
        )
        assert client.post(f"/v1/memory/candidates/{secret['id']}/approve").status_code == 200
        assert client.post(f"/v1/memory/candidates/{unrelated['id']}/approve").status_code == 200

        scoped_export = client.post("/v1/memory/export", json={"scope_key": "project:phoenix"})
        assert scoped_export.status_code == 200
        with _session_factory(client)() as db:
            export_artifact = db.scalar(
                select(MemoryExportArtifactRecord)
                .where(MemoryExportArtifactRecord.scope_key == "project:phoenix")
                .limit(1)
            )
            assert export_artifact is not None
            assert "code violet" in json.dumps(export_artifact.content)
            assert "code orange" not in json.dumps(export_artifact.content)

        assert (
            client.post(f"/v1/memory/assertions/{secret['id']}/privacy-delete").status_code == 200
        )
        with _session_factory(client)() as db:
            serialized_tables: list[str] = []
            for table in (
                MemoryProcedureRecord,
                ProjectStateSnapshotRecord,
                MemoryContextBlockRecord,
                MemoryExportArtifactRecord,
                MemoryTopicRecord,
                MemoryVersionRecord,
            ):
                rows = db.scalars(select(table)).all()
                serialized_tables.extend(json.dumps(row.__dict__, default=str) for row in rows)
            assert "code violet" not in "\n".join(serialized_tables)
            artifact = db.scalar(select(MemoryExportArtifactRecord).limit(1))
            assert artifact is not None
            assert artifact.content == {}
            assert artifact.redaction_posture == "privacy_deleted"


def test_no_memory_and_temporary_modes_disable_recall_and_extraction(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        candidate = _candidate(client)
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        for memory_mode in ("no_memory", "temporary"):
            response = client.put(
                f"/v1/sessions/{session_id}/memory-mode",
                json={"memory_mode": memory_mode},
            )
            assert response.status_code == 200
            assert response.json()["session"]["memory_mode"] == memory_mode

            with _session_factory(client)() as db:
                binding = db.scalar(
                    select(MemoryScopeBindingRecord)
                    .where(
                        MemoryScopeBindingRecord.scope_type == "session",
                        MemoryScopeBindingRecord.scope_key == session_id,
                    )
                    .limit(1)
                )
                assert binding is not None
                assert binding.memory_mode == memory_mode
                assert binding.extraction_enabled is False
                assert binding.recall_enabled is False
                before_evidence_count = db.scalar(
                    select(func.count()).select_from(MemoryEvidenceRecord)
                )
                before_extract_task_count = db.scalar(
                    select(func.count())
                    .select_from(BackgroundTaskRecord)
                    .where(BackgroundTaskRecord.task_type == "memory_extract_turn")
                )

            response = client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": f"what do you remember in {memory_mode}?"},
            )
            assert response.status_code == 200, response.text

            memory_context = adapter.context_bundles[-1]["memory_context"]
            assert memory_context["semantic_assertions"] == []
            assert memory_context["project_state"] == []
            assert memory_context["hot_index"] == []
            assert memory_context["topic_index"] == []
            assert memory_context["recall_window"]["memory_candidate_count"] == 0

            with _session_factory(client)() as db:
                assert (
                    db.scalar(select(func.count()).select_from(MemoryEvidenceRecord))
                    == before_evidence_count
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(BackgroundTaskRecord)
                        .where(BackgroundTaskRecord.task_type == "memory_extract_turn")
                    )
                    == before_extract_task_count
                )


def test_stale_memory_extraction_task_noops_after_session_memory_mode_changes(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember phoenix ships tomorrow"},
        )
        assert response.status_code == 200

        with _session_factory(client)() as db:
            evidence_id = db.scalar(
                select(MemoryEvidenceRecord.id)
                .where(
                    MemoryEvidenceRecord.source_session_id == session_id,
                    MemoryEvidenceRecord.content_class == "user_message",
                )
                .order_by(MemoryEvidenceRecord.created_at.desc())
                .limit(1)
            )
            assert evidence_id is not None

        response = client.put(
            f"/v1/sessions/{session_id}/memory-mode",
            json={"memory_mode": "no_memory"},
        )
        assert response.status_code == 200

        process_memory_extract_turn(
            session_factory=_session_factory(client),
            task_payload={"session_id": session_id, "evidence_id": evidence_id},
            settings=_settings(),
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryAssertionRecord)) == 0
