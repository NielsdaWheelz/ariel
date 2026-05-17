from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

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
    process_memory_graph_projection_job,
    process_memory_projection_job,
    record_action_trace,
)
from ariel.persistence import (
    AIJudgmentRecord,
    ActionAttemptRecord,
    BackgroundTaskRecord,
    MemoryActionTraceRecord,
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryContextBlockRecord,
    MemoryConflictSetRecord,
    MemoryDeletionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEpisodeRecord,
    MemoryExportArtifactRecord,
    MemoryGraphProjectionRecord,
    MemoryEvidenceRecord,
    MemoryKeywordProjectionRecord,
    MemoryProcedureRecord,
    MemoryProjectionJobRecord,
    MemoryReasoningTraceRecord,
    MemorySymbolProjectionRecord,
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
from tests.integration.responses_helpers import responses_run_message, responses_with_run_calls
from tests.fake_sandbox import FakeSandboxRuntime


_id_counter = count(1)


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
        self.context_bundles.append(copy.deepcopy(context_bundle))
        return responses_run_message(
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
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


# Each topic word maps to its own embedding dimension. Text that shares a topic
# word lands close in cosine space; unrelated text is orthogonal (distance 1.0,
# beyond the recall distance ceiling). This is non-degenerate: the vector signal
# and the lexical signal genuinely diverge, so neither alone satisfies recall.
_EMBEDDING_TOPIC_DIMENSIONS: dict[str, int] = {
    "phoenix": 0,
    "notebook": 1,
    "notebooks": 1,
    "zebra": 2,
    "migration": 3,
    "deploy": 4,
    "smoke": 5,
    "worker": 6,
    "espresso": 7,
    "milestone": 8,
    "vendor": 9,
}


def _fake_memory_embedding(text: str, *, settings: AppSettings) -> list[float]:
    vector = [0.0] * settings.memory_embedding_dimensions
    lowered = text.lower()
    for token, dimension in _EMBEDDING_TOPIC_DIMENSIONS.items():
        if token in lowered:
            vector[dimension] = 1.0
    if not any(vector):
        # Text with no known topic word lands on a stable fallback dimension so it
        # is still orthogonal to every topic vector.
        vector[settings.memory_embedding_dimensions - 1] = 1.0
    norm = sum(component * component for component in vector) ** 0.5
    return [component / norm for component in vector]


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


def _fake_memory_reflection(
    *,
    scope_key: str,
    episodes: Sequence[dict[str, Any]],
    reasoning_traces: Sequence[dict[str, Any]],
    action_traces: Sequence[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    """Reflection fixture that proposes nothing. The default fake keeps tests
    that merely exercise consolidation with traces present from depending on a
    reflective synthesis; tests that assert FO-4 behaviour install their own
    fake that proposes a synthesised insight or negative-memory item."""
    del scope_key, episodes, reasoning_traces, action_traces, settings
    return {
        "proposed_memory": [],
        "rationale": "fixture reflection proposed nothing",
        "uncertainty": "",
        "confidence": 0.9,
        "model": "fixture-memory-reflector",
        "prompt_version": memory.MEMORY_REFLECTION_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_memory_reflector",
        "parse_status": "parsed",
    }


def _use_fake_memory_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _fake_memory_curation)
    monkeypatch.setattr(memory, "_reflect_on_scope_with_model", _fake_memory_reflection)


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


def _process_graph_job(client: TestClient) -> bool:
    return process_memory_graph_projection_job(
        session_factory=_session_factory(client),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    )


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
    status: str = "executing",
    policy_decision: str = "requires_approval",
    execution_error: str | None = None,
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
                    policy_decision=policy_decision,
                    policy_reason=None,
                    status=status,
                    approval_required=policy_decision == "requires_approval",
                    execution_output=None,
                    execution_error=execution_error,
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


def test_conflicted_candidates_require_conflict_resolution_and_supersede_active_loser(
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
            # A previously-active loser is superseded by the winner, not rejected:
            # its history is preserved and links forward to the resolution.
            assert loser.lifecycle_state == "superseded"
            assert loser.superseded_by_assertion_id == second["id"]


def test_rejecting_conflict_member_settles_conflict_toward_surviving_assertion(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        # Rejecting the conflicted member leaves exactly one live member, so the
        # set settles to resolved toward the surviving active assertion. It does
        # not stay open, and a stale resolution attempt is no longer applicable.
        reject = client.post(f"/v1/memory/candidates/{second['id']}/reject")
        assert reject.status_code == 200
        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            assert conflict.lifecycle_state == "resolved"
            assert conflict.resolution_assertion_id == first["id"]
            rejected = db.get(MemoryAssertionRecord, second["id"])
            active = db.get(MemoryAssertionRecord, first["id"])
            assert rejected is not None
            assert active is not None
            assert rejected.lifecycle_state == "rejected"
            assert active.lifecycle_state == "active"

        revived = client.post(
            f"/v1/memory/conflicts/{conflict_id}/resolve",
            json={"assertion_id": second["id"]},
        )
        assert revived.status_code == 409
        assert revived.json()["error"]["code"] == "E_MEMORY_CONFLICT_NOT_APPLICABLE"


def test_conflict_opens_only_for_conflict_policy_predicate(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        # project.deadline is a "conflict"-policy predicate: a second contradicting
        # candidate against the active assertion opens exactly one conflict set.
        assert memory.resolve_predicate_spec("project.deadline").resolution_policy == "conflict"
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        with _session_factory(client)() as db:
            conflicted = db.get(MemoryAssertionRecord, second["id"])
            assert conflicted is not None
            assert conflicted.lifecycle_state == "conflicted"
            conflict_sets = db.scalars(select(MemoryConflictSetRecord)).all()
            assert len(conflict_sets) == 1
            assert conflict_sets[0].lifecycle_state == "open"
            assert conflict_sets[0].conflict_type == "value_contradiction"

        # A "supersede"-policy predicate (profile.display_name) opens no conflict;
        # the new candidate supersedes the active assertion on activation instead.
        assert (
            memory.resolve_predicate_spec("profile.display_name").resolution_policy == "supersede"
        )
        old_name = _candidate(
            client,
            value="Ada",
            assertion_type="profile",
            subject_key="user:default",
            predicate="profile.display_name",
        )
        assert client.post(f"/v1/memory/candidates/{old_name['id']}/approve").status_code == 200
        new_name = _candidate(
            client,
            value="Ada Lovelace",
            assertion_type="profile",
            subject_key="user:default",
            predicate="profile.display_name",
        )
        assert client.post(f"/v1/memory/candidates/{new_name['id']}/approve").status_code == 200
        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryConflictSetRecord)) == 1
            superseded = db.get(MemoryAssertionRecord, old_name["id"])
            replacement = db.get(MemoryAssertionRecord, new_name["id"])
            assert superseded is not None and replacement is not None
            assert superseded.lifecycle_state == "superseded"
            assert superseded.superseded_by_assertion_id == new_name["id"]
            assert replacement.lifecycle_state == "active"


def test_resolve_conflict_rejects_non_member_assertion(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        # An unrelated assertion is not a member of this conflict set: the
        # membership guard rejects resolving the conflict toward it.
        outsider = _candidate(
            client,
            value="meeting at noon",
            assertion_type="commitment",
            subject_key="user:default",
            predicate="commitment.todo",
        )
        assert client.post(f"/v1/memory/candidates/{outsider['id']}/approve").status_code == 200
        rejected = client.post(
            f"/v1/memory/conflicts/{conflict_id}/resolve",
            json={"assertion_id": outsider["id"]},
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "E_MEMORY_CONFLICT_NOT_APPLICABLE"
        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            assert conflict.lifecycle_state == "open"


def test_resolve_conflict_preserves_winner_history_and_supersedes_active_loser(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        winner_versions_before = client.get(
            f"/v1/memory/versions/memory_assertions/{second['id']}"
        ).json()["versions"]
        resolved = client.post(
            f"/v1/memory/conflicts/{conflict_id}/resolve",
            json={"assertion_id": second["id"]},
        )
        assert resolved.status_code == 200

        winner_versions_after = client.get(
            f"/v1/memory/versions/memory_assertions/{second['id']}"
        ).json()["versions"]
        # The winner's prior version rows are preserved and its activation appends
        # new ones; version history is never discarded by resolution.
        assert len(winner_versions_after) > len(winner_versions_before)
        for prior in winner_versions_before:
            assert prior["id"] in {row["id"] for row in winner_versions_after}
        with _session_factory(client)() as db:
            loser = db.get(MemoryAssertionRecord, first["id"])
            assert loser is not None
            assert loser.lifecycle_state == "superseded"
            assert loser.superseded_by_assertion_id == second["id"]


def test_correcting_conflict_member_closes_conflict_set(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        # Correcting the conflicted member supersedes it, and the fresh correction
        # supersedes the formerly-active member on activation. Both original
        # members are off the live set, so the conflict reaches a terminal state.
        corrected = client.post(
            f"/v1/memory/assertions/{second['id']}/correct",
            json={"value": "phoenix ships in two weeks"},
        )
        assert corrected.status_code == 200
        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            assert conflict.lifecycle_state == "ignored"
            for member_id in (first["id"], second["id"]):
                member = db.get(MemoryAssertionRecord, member_id)
                assert member is not None
                assert member.lifecycle_state == "superseded"


def test_retracting_conflict_member_closes_conflict_set(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        retracted = client.post(f"/v1/memory/assertions/{first['id']}/retract")
        assert retracted.status_code == 200
        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            # The active member retracted; only the conflicted candidate is live,
            # so the conflict resolves toward it and activates it.
            assert conflict.lifecycle_state == "resolved"
            assert conflict.resolution_assertion_id == second["id"]
            winner = db.get(MemoryAssertionRecord, second["id"])
            assert winner is not None
            assert winner.lifecycle_state == "active"


def test_conflict_with_all_members_invalidated_reaches_ignored(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        with _session_factory(client)() as db:
            shared_evidence_id = db.scalar(
                select(MemoryAssertionEvidenceRecord.evidence_id).where(
                    MemoryAssertionEvidenceRecord.assertion_id == first["id"]
                )
            )
        assert shared_evidence_id is not None

        # The conflicted member is built on the same evidence as the active one,
        # so a single privacy-delete invalidates every member of the conflict set
        # at once. With no live member left, the set reaches ignored.
        with _session_factory(client)() as db:
            with db.begin():
                memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text="phoenix ships next week",
                    subject_key="project:phoenix",
                    predicate="project.deadline",
                    assertion_type="project_state",
                    value="phoenix ships next week",
                    confidence=0.9,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model="fixture",
                    extraction_prompt_version="fixture",
                    now_fn=lambda: datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                    source_evidence_id=shared_evidence_id,
                )
        conflict_id = client.get("/v1/memory").json()["conflicts"][0]["id"]

        assert client.post(f"/v1/memory/assertions/{first['id']}/privacy-delete").status_code == 200
        with _session_factory(client)() as db:
            conflict = db.get(MemoryConflictSetRecord, conflict_id)
            assert conflict is not None
            assert conflict.lifecycle_state == "ignored"
            members = db.scalars(
                select(MemoryAssertionRecord).where(
                    MemoryAssertionRecord.subject_key == "project:phoenix",
                    MemoryAssertionRecord.predicate == "project.deadline",
                )
            ).all()
            assert members
            assert all(member.lifecycle_state == "privacy_deleted" for member in members)


def test_recall_surfaces_open_conflict_as_uncertainty(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        _process_projection(client)
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="when does phoenix ship?",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        # An open conflict is surfaced as uncertainty, and the contradicted fact
        # is never presented as a settled semantic assertion while the conflict
        # remains open.
        assert memory_context["conflicts"]
        assert memory_context["conflicts"][0]["state"] == "open"
        for item in memory_context["semantic_assertions"]:
            if item["subject_key"] == "project:phoenix" and item["predicate"] == "project.deadline":
                assert item["conflict_status"]["state"] == "open"
        rendered = memory.context_text(memory_context)
        assert "unresolved memory conflicts exist" in rendered

        # context_text renders an open-conflict semantic assertion as a conflict,
        # never as a settled fact, whenever such a candidate is surfaced.
        conflicted_render = memory.context_text(
            {
                "semantic_assertions": [
                    {
                        "type": "project_state",
                        "subject_key": "project:phoenix",
                        "predicate": "project.deadline",
                        "value": "phoenix ships next week",
                        "conflict_status": {"state": "open", "conflict_ids": ["mcf_x"]},
                    }
                ],
                "conflicts": [{"id": "mcf_x", "state": "open"}],
            }
        )
        assert "- conflict: project_state:" in conflicted_render
        assert "- project_state: project:phoenix project.deadline" not in conflicted_render


def test_mark_assertion_stale_requires_reason(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        candidate = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200

        # A missing reason is a typed E_MEMORY_STALE_REASON_REQUIRED failure.
        missing = client.post(f"/v1/memory/assertions/{candidate['id']}/mark-stale")
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "E_MEMORY_STALE_REASON_REQUIRED"
        blank = client.post(
            f"/v1/memory/assertions/{candidate['id']}/mark-stale",
            json={"reason": "   "},
        )
        assert blank.status_code == 422
        assert blank.json()["error"]["code"] == "E_MEMORY_STALE_REASON_REQUIRED"

        # A non-empty reason succeeds and is recorded in the version row.
        ok = client.post(
            f"/v1/memory/assertions/{candidate['id']}/mark-stale",
            json={"reason": "deadline passed without confirmation"},
        )
        assert ok.status_code == 200
        versions = client.get(f"/v1/memory/versions/memory_assertions/{candidate['id']}").json()[
            "versions"
        ]
        stale_version = next(row for row in versions if row["new_state"].get("staleness_reason"))
        assert (
            stale_version["new_state"]["staleness_reason"] == "deadline passed without confirmation"
        )


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
        # The graph projection is written by the async graph projection job.
        assert _process_graph_job(client) is True

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


def test_relationship_change_enqueues_graph_job_and_recall_uses_multi_hop_results(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each subject gets a distinct one-hot embedding so vector and lexical signals
    # surface only the directly-queried entity; the third entity can be reached
    # only through the depth-3 graph projection the graph job builds.
    def distinct_embedding(text: str, *, settings: AppSettings) -> list[float]:
        vector = [0.0] * settings.memory_embedding_dimensions
        lowered = text.lower()
        for index, token in enumerate(("alphaco", "bravoco", "charlieco")):
            if token in lowered:
                vector[index] = 1.0
                return vector
        vector[3] = 1.0
        return vector

    monkeypatch.setattr(memory, "embed_memory_text", distinct_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _fake_memory_curation)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        chain = []
        for token in ("alphaco", "bravoco", "charlieco"):
            candidate = _candidate(
                client,
                subject_key=f"project:{token}",
                predicate="project.deadline",
                value=f"{token} ships next month",
                evidence_text=f"The user said {token} ships next month.",
            )
            assert (
                client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
            )
            chain.append(candidate["id"])
        _process_projection(client)
        _process_projection(client)
        _process_projection(client)

        entity_ids: list[str] = []
        with _session_factory(client)() as db:
            for assertion_id in chain:
                assertion = db.get(MemoryAssertionRecord, assertion_id)
                assert assertion is not None
                entity_ids.append(assertion.subject_entity_id)
            evidence_id = db.scalar(
                select(MemoryAssertionEvidenceRecord.evidence_id)
                .where(MemoryAssertionEvidenceRecord.assertion_id == chain[0])
                .limit(1)
            )
            assert evidence_id is not None

        for source_id, target_id in (
            (entity_ids[0], entity_ids[1]),
            (entity_ids[1], entity_ids[2]),
        ):
            relationship = client.post(
                "/v1/memory/relationships",
                json={
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "relationship_type": "depends_on",
                    "evidence_id": evidence_id,
                    "scope_key": "global",
                    "confidence": 0.9,
                },
            )
            assert relationship.status_code == 200

        with _session_factory(client)() as db:
            pending_graph_jobs = db.scalars(
                select(MemoryProjectionJobRecord).where(
                    MemoryProjectionJobRecord.projection_kind == "graph",
                    MemoryProjectionJobRecord.lifecycle_state == "pending",
                )
            ).all()
            assert len(pending_graph_jobs) == 2

        assert _process_graph_job(client) is True
        assert _process_graph_job(client) is True
        assert _process_graph_job(client) is False

        with _session_factory(client)() as db:
            two_hop = db.scalar(
                select(MemoryGraphProjectionRecord).where(
                    MemoryGraphProjectionRecord.source_entity_id == entity_ids[0],
                    MemoryGraphProjectionRecord.target_entity_id == entity_ids[2],
                )
            )
            assert two_hop is not None
            assert two_hop.distance == 2
            assert len(two_hop.relationship_path) == 2

        with _session_factory(client)() as db:
            memory_context, _event_payload = memory.build_memory_context(
                db,
                user_message="status of alphaco",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        candidate_ids = memory_context["recall_window"]["candidate_memory_ids"]
        # charlieco is two hops from alphaco: only the graph signal can surface it.
        assert chain[2] in candidate_ids


def test_symbol_projection_rows_written_for_repo_scoped_assertions(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        repo_candidate = _candidate(
            client,
            subject_key="repo:ariel",
            predicate="repo.convention",
            assertion_type="fact",
            value="The retry helper lives in src/ariel/worker.py as reap_stale_tasks.",
            evidence_text="The user described where the retry helper lives.",
        )
        user_candidate = _candidate(
            client,
            subject_key="user:default",
            predicate="profile.role",
            assertion_type="profile",
            value="staff engineer",
            evidence_text="The user said they are a staff engineer.",
        )
        assert (
            client.post(f"/v1/memory/candidates/{repo_candidate['id']}/approve").status_code == 200
        )
        assert (
            client.post(f"/v1/memory/candidates/{user_candidate['id']}/approve").status_code == 200
        )

        with _session_factory(client)() as db:
            repo_symbols = db.scalars(
                select(MemorySymbolProjectionRecord).where(
                    MemorySymbolProjectionRecord.canonical_id == repo_candidate["id"]
                )
            ).all()
            user_symbols = db.scalars(
                select(MemorySymbolProjectionRecord).where(
                    MemorySymbolProjectionRecord.canonical_id == user_candidate["id"]
                )
            ).all()
        # Only the repo-scoped assertion yields symbol/path projection rows.
        assert user_symbols == []
        assert {row.repo_key for row in repo_symbols} == {"repo:ariel"}
        tokens = {row.path for row in repo_symbols} | {row.symbol for row in repo_symbols}
        assert "src/ariel/worker.py" in tokens
        assert "reap_stale_tasks" in tokens


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
        assert memory_context["memory_policy"]["controlling_scope_type"] == "project"
        assert proactive_context["semantic_assertions"] == []
        assert proactive_context["memory_policy"]["controlling_scope_type"] == "project"

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
        assert result["memory_policy"]["effective_mode"] == "no_memory"


def test_candidate_backlog_crossing_threshold_enqueues_consolidation_job(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        now = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
        settings = _settings(memory_consolidation_candidate_threshold=2)
        with _session_factory(client)() as db:
            with db.begin():
                first_events = memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="user.local",
                    evidence_text="The user said to ship the alpha task.",
                    subject_key="user:default",
                    predicate="commitment.todo",
                    assertion_type="commitment",
                    value="ship the alpha task",
                    confidence=0.9,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model=None,
                    extraction_prompt_version=None,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                    settings=settings,
                )
                second_events = memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="user.local",
                    evidence_text="The user said to ship the bravo task.",
                    subject_key="user:default",
                    predicate="commitment.todo",
                    assertion_type="commitment",
                    value="ship the bravo task",
                    confidence=0.9,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model=None,
                    extraction_prompt_version=None,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                    settings=settings,
                )

        first_kinds = {event["event_type"] for event in first_events}
        second_kinds = {event["event_type"] for event in second_events}
        # The first candidate leaves the backlog below threshold; the second
        # crosses it and enqueues a consolidation job, surfaced as an event.
        assert "evt.memory.consolidation_enqueued" not in first_kinds
        assert "evt.memory.consolidation_enqueued" in second_kinds

        with _session_factory(client)() as db:
            jobs = db.scalars(
                select(MemoryProjectionJobRecord).where(
                    MemoryProjectionJobRecord.projection_kind == "hot_index",
                    MemoryProjectionJobRecord.target_table == "memory_scopes",
                    MemoryProjectionJobRecord.target_id == "global",
                )
            ).all()
            assert len(jobs) == 1
            assert jobs[0].lifecycle_state == "pending"


def test_session_rotation_enqueues_consolidation_job(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        _session_id(client)
        rotate = client.post("/v1/sessions/rotate", json={})
        assert rotate.status_code == 200

        with _session_factory(client)() as db:
            jobs = db.scalars(
                select(MemoryProjectionJobRecord).where(
                    MemoryProjectionJobRecord.projection_kind == "hot_index",
                    MemoryProjectionJobRecord.target_table == "memory_scopes",
                    MemoryProjectionJobRecord.target_id == "global",
                )
            ).all()
            # Session rotation enqueued exactly one global consolidation job.
            assert len(jobs) == 1
            assert jobs[0].lifecycle_state == "pending"


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


def test_action_trace_completion_records_every_outcome_and_excludes_current_session(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        cases = [
            ("act_trace_ok", "cap.memory.inspect", "succeeded", "succeeded"),
            ("act_trace_fail", "cap.memory.inspect", "failed", "failed"),
            ("act_trace_denied", "cap.memory.inspect", "denied", "denied"),
            ("act_trace_undone", "cap.email.undo", "succeeded", "succeeded"),
        ]
        for attempt_id, capability_id, status, _outcome in cases:
            _seed_memory_action_attempt(
                client,
                action_attempt_id=attempt_id,
                capability_id=capability_id,
                proposed_input={"section": "all"}
                if capability_id == "cap.memory.inspect"
                else {"prior_action_id": "ema_prior", "idempotency_key": "undo-trace-1"},
                status=status,
                policy_decision="deny" if status == "denied" else "requires_approval",
                execution_error="boom" if status == "failed" else None,
            )

        with _session_factory(client)() as db:
            with db.begin():
                for attempt_id, _capability_id, _status, expected_outcome in cases:
                    attempt = db.get(ActionAttemptRecord, attempt_id)
                    assert attempt is not None
                    trace, events = record_action_trace(
                        db,
                        action_attempt=attempt,
                        scope_key=f"session:{session_id}",
                        primary_evidence_id=None,
                        source_turn_id=attempt.turn_id,
                        trace_type="execution"
                        if attempt.status in {"executing", "succeeded", "failed"}
                        else "policy_decision",
                        now=datetime.now(tz=UTC),
                        new_id_fn=_new_id,
                    )
                    assert trace.outcome == expected_outcome
                    assert trace.action_attempt_id == attempt_id
                    assert trace.capability_id == _capability_id
                    # primary_evidence_id is NOT NULL and was self-recorded.
                    evidence = db.get(MemoryEvidenceRecord, trace.primary_evidence_id)
                    assert evidence is not None
                    assert evidence.content_class == "system"
                    assert len(events) == 1
                    assert events[0]["event_type"] == "evt.memory.evidence_recorded"

        # The async outcome hook updates a record_action_trace-created trace.
        _seed_memory_action_attempt(
            client,
            action_attempt_id="act_trace_worker",
            capability_id="cap.memory.set_never_remember",
            proposed_input={"scope_key": "global", "rule": "do not remember trace probes"},
            status="executing",
        )
        with _session_factory(client)() as db:
            with db.begin():
                worker_attempt = db.get(ActionAttemptRecord, "act_trace_worker")
                assert worker_attempt is not None
                worker_trace, _events = record_action_trace(
                    db,
                    action_attempt=worker_attempt,
                    scope_key=f"session:{session_id}",
                    primary_evidence_id=None,
                    source_turn_id=worker_attempt.turn_id,
                    trace_type="policy_decision",
                    now=datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                )
                worker_trace_id = worker_trace.id
        assert (
            process_action_execution_task(
                session_factory=_session_factory(client),
                action_attempt_id="act_trace_worker",
                google_runtime=None,
                agency_runtime=None,
                now_fn=lambda: datetime.now(tz=UTC),
                new_id_fn=_new_id,
            )
            is True
        )
        with _session_factory(client)() as db:
            updated = db.get(MemoryActionTraceRecord, worker_trace_id)
            assert updated is not None
            assert updated.trace_type == "execution"
            assert updated.outcome == "succeeded"
            assert updated.result_refs["execution_status"] == "succeeded"
            # No duplicate trace was created for the same attempt.
            trace_count = db.scalar(
                select(func.count())
                .select_from(MemoryActionTraceRecord)
                .where(MemoryActionTraceRecord.action_attempt_id == "act_trace_worker")
            )
            assert trace_count == 1

        # Trace recall excludes traces scoped to the current session.
        with _session_factory(client)() as db:
            memory_context, _payload = memory.build_memory_context(
                db,
                user_message="action trace recall probe",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
            )
        session_traces = [
            item
            for item in memory_context["recall_window"]["candidate_memories"]
            if item["kind"] == "action_trace" and item["scope_key"] == f"session:{session_id}"
        ]
        assert session_traces == []


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
                # Session mode lives only on SessionRecord; no session-type
                # binding row is written for it.
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(MemoryScopeBindingRecord)
                        .where(MemoryScopeBindingRecord.scope_type == "session")
                    )
                    == 0
                )
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


def test_predicate_registry_derives_cardinality_deterministically(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        first = _candidate(client, value="phoenix ships tomorrow")
        second = _candidate(client, value="phoenix ships next week")
        with _session_factory(client)() as db:
            first_row = db.get(MemoryAssertionRecord, first["id"])
            second_row = db.get(MemoryAssertionRecord, second["id"])
            assert first_row is not None and second_row is not None
            # Same predicate always resolves to the same cardinality, registry-derived.
            assert first_row.is_multi_valued == second_row.is_multi_valued
            assert (
                first_row.is_multi_valued
                == memory.resolve_predicate_spec("project.deadline").is_multi_valued
            )

        # The candidate request contract no longer accepts is_multi_valued.
        rejected = client.post(
            "/v1/memory/candidates",
            json={
                "subject_key": "project:phoenix",
                "predicate": "project.deadline",
                "assertion_type": "project_state",
                "value": "phoenix ships friday",
                "evidence_text": "The user said phoenix ships friday.",
                "confidence": 0.9,
                "is_multi_valued": True,
            },
        )
        assert rejected.status_code == 422


def test_unknown_predicate_resolves_single_valued_conflict() -> None:
    spec = memory.resolve_predicate_spec("entirely.unregistered.predicate")
    assert spec is memory._DEFAULT_PREDICATE_SPEC
    assert spec.resolution_policy == "conflict"
    assert spec.is_multi_valued is False


def test_value_violating_value_kind_is_rejected(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        # project.status is an enum predicate; an unlisted value must be rejected.
        response = client.post(
            "/v1/memory/candidates",
            json={
                "subject_key": "project:phoenix",
                "predicate": "project.status",
                "assertion_type": "project_state",
                "value": "not-a-real-status",
                "evidence_text": "The user described the project status.",
                "confidence": 0.9,
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "E_MEMORY_VALUE_KIND"
        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryAssertionRecord)) == 0


def _set_scope_binding(
    client: TestClient,
    *,
    scope_type: str,
    scope_key: str,
    memory_mode: str,
    expires_at: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "memory_mode": memory_mode,
        "reason": "ws3 test binding",
    }
    if expires_at is not None:
        body["expires_at"] = expires_at
    response = client.put("/v1/memory/scope-bindings", json=body)
    assert response.status_code == 200, response.text


def test_project_no_memory_binding_blocks_chat_turn_writes_under_normal_session(
    postgres_url: str,
) -> None:
    # A project no_memory binding must block the chat-turn evidence, episode, and
    # extraction-enqueue writes even though SessionRecord.memory_mode is normal.
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        _set_scope_binding(
            client, scope_type="project", scope_key="project:phoenix", memory_mode="no_memory"
        )

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember the phoenix deadline is tomorrow"},
        )
        assert response.status_code == 200, response.text

        with _session_factory(client)() as db:
            assert db.scalar(select(func.count()).select_from(MemoryEvidenceRecord)) == 0
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(BackgroundTaskRecord)
                    .where(BackgroundTaskRecord.task_type == "memory_extract_turn")
                )
                == 0
            )
            # SessionRecord.memory_mode itself is untouched: the block is the binding.
            session = db.get(SessionRecord, session_id)
            assert session is not None and session.memory_mode == "normal"


def test_project_no_memory_binding_blocks_recall_and_extraction(
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
        _set_scope_binding(
            client, scope_type="project", scope_key="project:phoenix", memory_mode="no_memory"
        )

        with _session_factory(client)() as db:
            with db.begin():
                db.add(
                    MemoryEvidenceRecord(
                        id=_new_id("mev"),
                        source_turn_id=None,
                        source_session_id=session_id,
                        actor_id="user.local",
                        content_class="user_message",
                        trust_boundary="trusted_user",
                        source_text="phoenix ships friday",
                        source_uri=None,
                        lifecycle_state="available",
                        metadata_json={},
                        created_at=datetime.now(tz=UTC),
                        updated_at=datetime.now(tz=UTC),
                    )
                )
            evidence_id = db.scalar(
                select(MemoryEvidenceRecord.id)
                .where(MemoryEvidenceRecord.source_text == "phoenix ships friday")
                .limit(1)
            )
        assert evidence_id is not None

        with _session_factory(client)() as db:
            with db.begin():
                # Recall is blocked: no candidates surface for the project scope.
                memory_context, _event = memory.build_memory_context(
                    db,
                    user_message="what is the phoenix deadline?",
                    max_recalled_assertions=8,
                    settings=_settings(),
                    current_session_id=session_id,
                    scope_key="project:phoenix",
                )
                assert memory_context["semantic_assertions"] == []
                assert memory_context["memory_policy"]["effective_mode"] == "no_memory"
                # Extraction is blocked: proposing a project-scoped candidate no-ops.
                events = memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text="phoenix ships friday",
                    subject_key="project:phoenix",
                    predicate="project.deadline",
                    assertion_type="project_state",
                    value="phoenix ships friday",
                    confidence=0.9,
                    scope_key="project:phoenix",
                    valid_from=None,
                    valid_to=None,
                    extraction_model="fixture",
                    extraction_prompt_version="fixture",
                    now_fn=lambda: datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                    source_evidence_id=evidence_id,
                )
                assert events == []


def test_broad_no_memory_binding_is_not_overridden_by_narrower_normal(
    postgres_url: str,
) -> None:
    # Strictest mode wins: a broad user-scope no_memory binding is not overridden
    # by a narrower project-scope normal binding.
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        _set_scope_binding(
            client, scope_type="user", scope_key=memory.USER_SUBJECT_KEY, memory_mode="no_memory"
        )
        _set_scope_binding(
            client, scope_type="project", scope_key="project:phoenix", memory_mode="normal"
        )
        with _session_factory(client)() as db:
            with db.begin():
                policy = memory.resolve_memory_policy(
                    db,
                    operation="recall",
                    now=datetime.now(tz=UTC),
                    project_key="project:phoenix",
                )
        assert policy.effective_mode == "no_memory"
        assert policy.allowed is False
        # The most specific carrier of the winning mode is reported as controlling.
        assert policy.controlling_scope_type == "user"


def test_expired_scope_binding_stops_applying(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        _set_scope_binding(
            client,
            scope_type="project",
            scope_key="project:phoenix",
            memory_mode="no_memory",
            expires_at="2020-01-01T00:00:00+00:00",
        )
        with _session_factory(client)() as db:
            with db.begin():
                policy = memory.resolve_memory_policy(
                    db,
                    operation="recall",
                    now=datetime.now(tz=UTC),
                    project_key="project:phoenix",
                )
        # The expired binding does not apply; resolution falls back to the default.
        assert policy.effective_mode == "normal"
        assert policy.allowed is True
        assert policy.controlling_scope_type == "default"


def test_scope_binding_change_writes_memory_version_record(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        _set_scope_binding(
            client, scope_type="project", scope_key="project:phoenix", memory_mode="no_memory"
        )
        with _session_factory(client)() as db:
            binding = db.scalar(
                select(MemoryScopeBindingRecord)
                .where(MemoryScopeBindingRecord.scope_type == "project")
                .limit(1)
            )
            assert binding is not None
            version = db.scalar(
                select(MemoryVersionRecord)
                .where(
                    MemoryVersionRecord.canonical_table == "memory_scope_bindings",
                    MemoryVersionRecord.canonical_id == binding.id,
                )
                .order_by(MemoryVersionRecord.version.desc())
                .limit(1)
            )
            assert version is not None
            assert version.change_type == "created"
            assert isinstance(version.new_state, dict)
            assert version.new_state["memory_mode"] == "no_memory"


@dataclass
class MemoryInspectTurnAdapter:
    """First model turn invokes the cap.memory.inspect callable; the follow-up turn
    emits the final message. The turn runs a callable, so the chat-turn path writes
    a reasoning trace."""

    provider: str = "provider.north-star-memory"
    model: str = "model.north-star-memory"
    calls_made: int = 0

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, history, context_bundle
        self.calls_made += 1
        if self.calls_made == 1:
            calls = [{"name": "memory.inspect", "input": {"section": "all", "limit": 10}}]
        else:
            calls = [
                {"name": "agent.emit_message", "input": {"text": f"inspected::{user_message}"}}
            ]
        return responses_with_run_calls(
            assistant_text=f"inspected::{user_message}",
            calls=calls,
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_north_star_memory_inspect_{self.calls_made}",
        )


def test_turn_that_runs_callables_writes_reasoning_trace(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryInspectTurnAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "inspect my memory please"},
        )
        assert response.status_code == 200

        with _session_factory(client)() as db:
            # The turn ran the cap.memory.inspect callable, so it created an action
            # attempt and the chat-turn path wrote exactly one reasoning trace.
            attempts = db.scalars(
                select(ActionAttemptRecord).where(ActionAttemptRecord.session_id == session_id)
            ).all()
            assert [attempt.capability_id for attempt in attempts] == ["cap.memory.inspect"]
            assert attempts[0].status == "succeeded"
            traces = db.scalars(
                select(MemoryReasoningTraceRecord).where(
                    MemoryReasoningTraceRecord.scope_key == f"session:{session_id}"
                )
            ).all()
            assert len(traces) == 1
            trace = traces[0]
            assert trace.trace_type == "successful_pattern"
            assert trace.outcome == "succeeded"
            assert trace.lifecycle_state == "active"
            assert "cap.memory.inspect" in trace.trace_summary
            # primary_evidence_id is NOT NULL and points at the turn's user evidence.
            evidence = db.get(MemoryEvidenceRecord, trace.primary_evidence_id)
            assert evidence is not None
            assert evidence.source_session_id == session_id
            assert trace.source_turn_id is not None


def test_negative_candidate_flows_through_review_to_active(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        _session_id(client)
        candidate = _candidate(
            client,
            value="do not retry the synchronous import path",
            assertion_type="negative",
            subject_key="repo:ariel",
            predicate="negative.rejected_approach",
            evidence_text="The synchronous import path was rejected after it deadlocked.",
        )
        assert candidate["type"] == "negative"
        # negative.* predicates are coexist, so the candidate is multi-valued and
        # opens no conflict; it flows through the standard review lifecycle.
        assert candidate["is_multi_valued"] is True
        assert candidate["state"] == "candidate"

        approve = client.post(f"/v1/memory/candidates/{candidate['id']}/approve")
        assert approve.status_code == 200

        with _session_factory(client)() as db:
            assertion = db.get(MemoryAssertionRecord, candidate["id"])
            assert assertion is not None
            assert assertion.assertion_type == "negative"
            assert assertion.lifecycle_state == "active"
            assert assertion.is_multi_valued is True
            review = db.scalar(
                select(MemoryReviewRecord)
                .where(MemoryReviewRecord.assertion_id == candidate["id"])
                .order_by(MemoryReviewRecord.created_at.desc())
                .limit(1)
            )
            assert review is not None
            assert review.decision == "approved"


def _seed_reasoning_trace(
    db: Any,
    *,
    trace_id: str,
    trace_type: str,
    task_summary: str,
    trace_summary: str,
    outcome: str,
    session_id: str,
    now: datetime,
) -> None:
    evidence_id = f"mev_{trace_id}"
    db.add(
        MemoryEvidenceRecord(
            id=evidence_id,
            source_turn_id=None,
            source_session_id=session_id,
            actor_id="system",
            content_class="system",
            trust_boundary="system",
            lifecycle_state="available",
            source_uri=None,
            source_artifact_id=None,
            source_text=trace_summary,
            evidence_snippet=trace_summary[:360],
            redaction_posture="none",
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryReasoningTraceRecord(
            id=trace_id,
            trace_type=trace_type,
            scope_key="global",
            task_summary=task_summary,
            trace_summary=trace_summary,
            outcome=outcome,
            primary_evidence_id=evidence_id,
            source_turn_id=None,
            related_entity_ids=[],
            related_assertion_ids=[],
            lifecycle_state="active",
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )


def test_consolidation_promotes_successful_traces_and_failures_to_candidates(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        now = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
        with _session_factory(client)() as db:
            with db.begin():
                # Two repeated successful_pattern traces for the same task become a
                # procedure candidate; the failure trace becomes a negative-memory
                # candidate.
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_ok_a",
                    trace_type="successful_pattern",
                    task_summary="run the database migration",
                    trace_summary="ran migration 0030 and verified the schema",
                    outcome="succeeded",
                    session_id=session_id,
                    now=now,
                )
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_ok_b",
                    trace_type="successful_pattern",
                    task_summary="run the database migration",
                    trace_summary="ran migration 0031 and verified the schema",
                    outcome="succeeded",
                    session_id=session_id,
                    now=now,
                )
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_fail",
                    trace_type="failure",
                    task_summary="patch the worker loop",
                    trace_summary="patching the worker loop in place deadlocked the queue",
                    outcome="failed",
                    session_id=session_id,
                    now=now,
                )

        with _session_factory(client)() as db:
            with db.begin():
                result = memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    source_session_id=session_id,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                )
        assert result["status"] == "completed"
        change_kinds = [change["kind"] for change in result["proposed_changes"]]
        assert "procedure_candidate" in change_kinds
        assert "negative_memory_candidate" in change_kinds

        with _session_factory(client)() as db:
            # The repeated successful traces produced a reviewable procedure candidate.
            procedure = db.scalar(
                select(MemoryProcedureRecord)
                .where(MemoryProcedureRecord.lifecycle_state == "candidate")
                .limit(1)
            )
            assert procedure is not None
            assert procedure.review_state == "needs_operator_review"
            assert procedure.metadata_json["source_reasoning_trace_ids"] == [
                "mrt_ok_a",
                "mrt_ok_b",
            ]
            # The failure trace produced a negative-memory assertion candidate that
            # routes through the standard review lifecycle (never a direct active write).
            negative = db.scalar(
                select(MemoryAssertionRecord)
                .where(MemoryAssertionRecord.assertion_type == "negative")
                .limit(1)
            )
            assert negative is not None
            assert negative.lifecycle_state == "candidate"
            assert negative.predicate == "negative.known_bad_path"
            assert negative.is_multi_valued is True


def _seed_episode(
    db: Any,
    *,
    episode_id: str,
    title: str,
    summary: str,
    outcome: str | None,
    session_id: str,
    now: datetime,
) -> None:
    evidence_id = f"mev_{episode_id}"
    db.add(
        MemoryEvidenceRecord(
            id=evidence_id,
            source_turn_id=None,
            source_session_id=session_id,
            actor_id="system",
            content_class="system",
            trust_boundary="system",
            lifecycle_state="available",
            source_uri=None,
            source_artifact_id=None,
            source_text=summary,
            evidence_snippet=summary[:360],
            redaction_posture="none",
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryEpisodeRecord(
            id=episode_id,
            episode_type="task_event",
            scope_key="global",
            title=title,
            summary=summary,
            outcome=outcome,
            occurred_at=now,
            valid_from=now,
            valid_to=None,
            lifecycle_state="active",
            primary_evidence_id=evidence_id,
            related_entity_ids=[],
            related_assertion_ids=[],
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )


def test_reflection_phase_proposes_synthesized_insight_and_negative_memory_as_candidates(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FO-4. A scope whose episodes and reasoning traces share a pattern that no
    # single record states yields, from one bounded AI judgment, a synthesised
    # insight and a negative-memory item — both routed through the standard
    # candidate -> review lifecycle, never written active.
    _use_fake_memory_models(monkeypatch)
    now = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)

    def _reflection(
        *,
        scope_key: str,
        episodes: Sequence[dict[str, Any]],
        reasoning_traces: Sequence[dict[str, Any]],
        action_traces: Sequence[dict[str, Any]],
        settings: AppSettings,
    ) -> dict[str, Any]:
        del settings, action_traces
        # The reflection sees the whole bounded scope and synthesises across it.
        assert {episode["id"] for episode in episodes} == {"mep_flake_a", "mep_flake_b"}
        assert {trace["id"] for trace in reasoning_traces} == {"mrt_flake_a", "mrt_flake_b"}
        return {
            "proposed_memory": [
                {
                    "kind": "insight",
                    "subject_key": scope_key,
                    "predicate": "domain.invariant",
                    "assertion_type": "domain_concept",
                    "value": "the integration suite is flaky whenever the cache is cold",
                    "confidence": 0.75,
                    "synthesis": "derived from two cold-cache episodes and two retry traces",
                },
                {
                    "kind": "negative",
                    "subject_key": scope_key,
                    "predicate": "negative.known_bad_path",
                    "assertion_type": "negative",
                    "value": "do not run the integration suite before warming the cache",
                    "confidence": 0.7,
                    "synthesis": "the same cold-cache failure recurred across both traces",
                },
            ],
            "rationale": "synthesised a cross-record flakiness invariant",
            "uncertainty": "",
            "confidence": 0.8,
            "model": "fixture-memory-reflector",
            "prompt_version": memory.MEMORY_REFLECTION_PROMPT_VERSION,
            "provider_response_id": "resp_fixture_reflection",
            "parse_status": "parsed",
        }

    monkeypatch.setattr(memory, "_reflect_on_scope_with_model", _reflection)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        with _session_factory(client)() as db:
            with db.begin():
                # No single record states "cold cache causes flakiness"; each one
                # records only one cold-cache run or one retry.
                _seed_episode(
                    db,
                    episode_id="mep_flake_a",
                    title="integration run with a cold cache",
                    summary="ran the integration suite with a cold cache",
                    outcome="three tests failed intermittently",
                    session_id=session_id,
                    now=now,
                )
                _seed_episode(
                    db,
                    episode_id="mep_flake_b",
                    title="another integration run with a cold cache",
                    summary="ran the integration suite again with a cold cache",
                    outcome="two tests failed intermittently",
                    session_id=session_id,
                    now=now,
                )
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_flake_a",
                    trace_type="diagnostic",
                    task_summary="investigate the integration suite",
                    trace_summary="retried the suite and the failures cleared",
                    outcome="succeeded",
                    session_id=session_id,
                    now=now,
                )
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_flake_b",
                    trace_type="diagnostic",
                    task_summary="investigate the integration suite once more",
                    trace_summary="retried the suite a second time and it passed",
                    outcome="succeeded",
                    session_id=session_id,
                    now=now,
                )

        with _session_factory(client)() as db:
            with db.begin():
                result = memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    source_session_id=session_id,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                )
        assert result["status"] == "completed"
        change_kinds = [change["kind"] for change in result["proposed_changes"]]
        assert "reflective_insight_candidate" in change_kinds
        assert "reflective_negative_memory_candidate" in change_kinds

        with _session_factory(client)() as db:
            # The synthesised insight is a candidate assertion, never an active
            # write, and carries a review row through the standard lifecycle.
            insight = db.scalar(
                select(MemoryAssertionRecord).where(
                    MemoryAssertionRecord.predicate == "domain.invariant"
                )
            )
            assert insight is not None
            assert insight.lifecycle_state == "candidate"
            assert insight.extraction_prompt_version == memory.MEMORY_REFLECTION_PROMPT_VERSION
            assert (
                insight.object_value["text"]
                == "the integration suite is flaky whenever the cache is cold"
            )
            insight_review = db.scalar(
                select(MemoryReviewRecord).where(
                    MemoryReviewRecord.assertion_id == insight.id,
                    MemoryReviewRecord.decision == "needs_user_review",
                )
            )
            assert insight_review is not None
            # The synthesised negative memory is likewise only a candidate.
            negative = db.scalar(
                select(MemoryAssertionRecord).where(
                    MemoryAssertionRecord.predicate == "negative.known_bad_path"
                )
            )
            assert negative is not None
            assert negative.lifecycle_state == "candidate"
            assert negative.assertion_type == "negative"

            # The reflection is recorded as an auditable AI-judgment row with
            # full provenance: model, prompt version, provider response id, and
            # the input/selected sources.
            judgment = db.scalar(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "reflective_consolidation"
                )
            )
            assert judgment is not None
            assert judgment.status == "succeeded"
            assert judgment.source_type == "memory_scope"
            assert judgment.source_id == "global"
            assert judgment.model == "fixture-memory-reflector"
            assert judgment.prompt_version == memory.MEMORY_REFLECTION_PROMPT_VERSION
            assert judgment.provider_response_id == "resp_fixture_reflection"
            assert sorted(judgment.input_refs["episode_ids"]) == ["mep_flake_a", "mep_flake_b"]
            assert sorted(judgment.input_refs["reasoning_trace_ids"]) == [
                "mrt_flake_a",
                "mrt_flake_b",
            ]
            assert {entry["assertion_id"] for entry in judgment.selected} == {
                insight.id,
                negative.id,
            }
            assert judgment.omitted == []


def test_reflection_phase_skipped_under_no_memory_mode(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FO-4 reuses the policy consolidate_memory already resolved: under a
    # no_memory scope the whole consolidation, reflection included, is skipped.
    _use_fake_memory_models(monkeypatch)
    now = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)

    def _must_not_run(
        *,
        scope_key: str,
        episodes: Sequence[dict[str, Any]],
        reasoning_traces: Sequence[dict[str, Any]],
        action_traces: Sequence[dict[str, Any]],
        settings: AppSettings,
    ) -> dict[str, Any]:
        raise AssertionError("reflection ran under no_memory mode")

    monkeypatch.setattr(memory, "_reflect_on_scope_with_model", _must_not_run)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        with _session_factory(client)() as db:
            with db.begin():
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_no_memory",
                    trace_type="diagnostic",
                    task_summary="investigate something",
                    trace_summary="looked at the failing job",
                    outcome="succeeded",
                    session_id=session_id,
                    now=now,
                )
        assert (
            client.put(
                f"/v1/sessions/{session_id}/memory-mode", json={"memory_mode": "no_memory"}
            ).status_code
            == 200
        )

        with _session_factory(client)() as db:
            with db.begin():
                result = memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    source_session_id=session_id,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                )
        assert result["status"] == "skipped"
        assert result["memory_policy"]["effective_mode"] == "no_memory"
        with _session_factory(client)() as db:
            # No reflective-consolidation judgment was recorded.
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(AIJudgmentRecord)
                    .where(AIJudgmentRecord.judgment_type == "reflective_consolidation")
                )
                == 0
            )


def test_reflection_dedupes_proposal_overlapping_mechanical_promotion(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FO-4 layers LLM synthesis on top of the mechanical WS-5 promotion; it must
    # not re-propose what the mechanical pass already proposed. A failure trace
    # is mechanically promoted to a negative.known_bad_path candidate; a
    # reflection proposal with the identical (subject, predicate, value) is
    # deduped out, leaving exactly one negative candidate.
    _use_fake_memory_models(monkeypatch)
    now = datetime(2026, 5, 12, 11, 0, tzinfo=UTC)
    duplicate_value = "patching the worker loop in place deadlocked the queue"

    def _reflection(
        *,
        scope_key: str,
        episodes: Sequence[dict[str, Any]],
        reasoning_traces: Sequence[dict[str, Any]],
        action_traces: Sequence[dict[str, Any]],
        settings: AppSettings,
    ) -> dict[str, Any]:
        del episodes, reasoning_traces, action_traces, settings
        return {
            "proposed_memory": [
                {
                    "kind": "negative",
                    "subject_key": scope_key,
                    "predicate": "negative.known_bad_path",
                    "assertion_type": "negative",
                    "value": duplicate_value,
                    "confidence": 0.7,
                    "synthesis": "restates the mechanically promoted failure trace",
                }
            ],
            "rationale": "proposed a negative item that overlaps the mechanical promotion",
            "uncertainty": "",
            "confidence": 0.8,
            "model": "fixture-memory-reflector",
            "prompt_version": memory.MEMORY_REFLECTION_PROMPT_VERSION,
            "provider_response_id": "resp_fixture_reflection_dupe",
            "parse_status": "parsed",
        }

    monkeypatch.setattr(memory, "_reflect_on_scope_with_model", _reflection)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        with _session_factory(client)() as db:
            with db.begin():
                _seed_reasoning_trace(
                    db,
                    trace_id="mrt_dupe_fail",
                    trace_type="failure",
                    task_summary="patch the worker loop",
                    trace_summary=duplicate_value,
                    outcome="failed",
                    session_id=session_id,
                    now=now,
                )

        with _session_factory(client)() as db:
            with db.begin():
                result = memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    source_session_id=session_id,
                    now_fn=lambda: now,
                    new_id_fn=_new_id,
                )
        assert result["status"] == "completed"
        change_kinds = [change["kind"] for change in result["proposed_changes"]]
        # The mechanical promotion proposed the negative candidate; the
        # overlapping reflection proposal was deduped out.
        assert "negative_memory_candidate" in change_kinds
        assert "reflective_negative_memory_candidate" not in change_kinds

        with _session_factory(client)() as db:
            negatives = db.scalars(
                select(MemoryAssertionRecord).where(
                    MemoryAssertionRecord.assertion_type == "negative"
                )
            ).all()
            assert len(negatives) == 1
            judgment = db.scalar(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "reflective_consolidation"
                )
            )
            assert judgment is not None
            assert judgment.selected == []
            assert len(judgment.omitted) == 1
            assert judgment.omitted[0]["predicate"] == "negative.known_bad_path"
            assert judgment.output["deduped_count"] == 1


def _top_rrf_curation(
    *,
    user_message: str,
    history: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    max_selected: int,
    settings: AppSettings,
) -> dict[str, Any]:
    """Curator fixture that selects the highest-rrf_score candidates. It mirrors a
    relevance-aware curator just enough to let a test read which candidate the
    fused pool ranked first."""
    del user_message, history, settings
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate["retrieval_features"]["rrf_score"]),
            str(candidate["id"]),
        ),
    )
    selected = [
        {
            "id": candidate["id"],
            "kind": candidate.get("kind", "semantic_assertion"),
            "rationale": "selected by fused rrf score",
        }
        for candidate in ranked[:max_selected]
    ]
    selected_ids = {item["id"] for item in selected}
    return {
        "selected_memories": selected,
        "omitted_memories": [
            {
                "id": candidate["id"],
                "kind": candidate.get("kind", "semantic_assertion"),
                "reason": "omitted: lower fused rrf score",
            }
            for candidate in candidates
            if candidate["id"] not in selected_ids
        ],
        "rationale": "fixture top-rrf curation",
        "uncertainty": "",
        "confidence": 0.9,
        "model": "fixture-rrf-curator",
        "prompt_version": memory.MEMORY_CURATION_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_rrf_curator",
        "parse_status": "parsed",
    }


def test_hybrid_retrieval_requires_multiple_signals(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Three active assertions. The vector signal ranks A first, C second; the
    # lexical signal ranks B first, C second. Under fused Reciprocal Rank Fusion C
    # accumulates a contribution from both signals and ranks first overall, while A
    # and B each score from one signal only. C is the correct answer, and it is
    # reachable only by the hybrid pool: vector-only retrieval surfaces A and
    # keyword-only retrieval surfaces B.
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _top_rrf_curation)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        ids: dict[str, str] = {}
        for label, value in (
            ("A", "the alpha vector-only landing note"),
            ("B", "the bravo keyword-only landing note"),
            ("C", "the gamma fused landing note"),
        ):
            candidate = _candidate(
                client,
                subject_key="user:default",
                predicate="commitment.todo",
                assertion_type="commitment",
                value=value,
                evidence_text=f"The user recorded the {label} landing note.",
            )
            assert (
                client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
            )
            ids[label] = candidate["id"]

        assertions_ref = ("memory_assertions",)

        def _vector(a_first: bool) -> Any:
            def signal(
                db: Any, *, user_message: str, settings: AppSettings, limit: int
            ) -> tuple[list[tuple[str, str]], dict[str, float]]:
                del db, user_message, settings, limit
                ranked = (
                    [(assertions_ref[0], ids["A"]), (assertions_ref[0], ids["C"])]
                    if a_first
                    else []
                )
                return ranked, {ids["A"]: 0.1, ids["C"]: 0.2}

            return signal

        def _lexical(b_first: bool) -> Any:
            def signal(
                db: Any, *, user_message: str, limit: int
            ) -> tuple[list[tuple[str, str]], dict[str, int]]:
                del db, user_message, limit
                ranked = (
                    [(assertions_ref[0], ids["B"]), (assertions_ref[0], ids["C"])]
                    if b_first
                    else []
                )
                return ranked, {ids["B"]: 1, ids["C"]: 2}

            return signal

        def _recall(*, vector_on: bool, lexical_on: bool) -> dict[str, Any]:
            monkeypatch.setattr(memory, "_vector_signal", _vector(vector_on))
            monkeypatch.setattr(memory, "_lexical_signal", _lexical(lexical_on))
            with _session_factory(client)() as db:
                memory_context, _event = memory.build_memory_context(
                    db,
                    user_message="which landing note is the correct one",
                    max_recalled_assertions=1,
                    settings=_settings(),
                    current_session_id=session_id,
                )
            return memory_context

        # Hybrid: every signal runs. C is in the pool and is the selected answer.
        hybrid = _recall(vector_on=True, lexical_on=True)
        hybrid_candidate_ids = hybrid["recall_window"]["candidate_memory_ids"]
        assert ids["C"] in hybrid_candidate_ids
        assert hybrid["recall_window"]["selected_memory_ids"] == [ids["C"]]

        # Vector-only retrieval surfaces A, never the fused-only answer C.
        vector_only = _recall(vector_on=True, lexical_on=False)
        assert vector_only["recall_window"]["selected_memory_ids"] == [ids["A"]]
        assert vector_only["recall_window"]["selected_memory_ids"] != [ids["C"]]

        # Keyword-only retrieval surfaces B, never the fused-only answer C.
        keyword_only = _recall(vector_on=False, lexical_on=True)
        assert keyword_only["recall_window"]["selected_memory_ids"] == [ids["B"]]
        assert keyword_only["recall_window"]["selected_memory_ids"] != [ids["C"]]


def test_recall_returns_every_memory_kind(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed one memory of every kind, then assert the recall candidate pool and the
    # search results both cover all of them: semantic assertion, episode,
    # reasoning trace, action trace, procedure, project state, negative memory,
    # hot index, topic block, and conflict.
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        now = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)

        semantic = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{semantic['id']}/approve").status_code == 200
        _process_projection(client)

        procedure_candidate = _candidate(
            client,
            subject_key="project:phoenix",
            predicate="procedure.deploy",
            assertion_type="procedure",
            value="Before deploying phoenix run the smoke tests.",
            evidence_text="The user described the phoenix deploy procedure.",
        )
        assert (
            client.post(f"/v1/memory/candidates/{procedure_candidate['id']}/approve").status_code
            == 200
        )
        _process_projection(client)

        negative_candidate = _candidate(
            client,
            subject_key="repo:ariel",
            predicate="negative.rejected_approach",
            assertion_type="negative",
            value="do not retry the synchronous phoenix import path",
            evidence_text="The synchronous phoenix import path was rejected.",
        )
        assert (
            client.post(f"/v1/memory/candidates/{negative_candidate['id']}/approve").status_code
            == 200
        )
        _process_projection(client)

        with _session_factory(client)() as db:
            with db.begin():
                evidence = MemoryEvidenceRecord(
                    id=_new_id("mev"),
                    source_turn_id=None,
                    source_session_id=session_id,
                    actor_id="system",
                    content_class="system",
                    trust_boundary="system",
                    lifecycle_state="available",
                    source_uri=None,
                    source_artifact_id=None,
                    source_text="phoenix kinds evidence",
                    evidence_snippet="phoenix kinds evidence",
                    redaction_posture="none",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
                db.add(evidence)
                db.flush()
                db.add(
                    MemoryEpisodeRecord(
                        id=_new_id("mep"),
                        episode_type="task_event",
                        scope_key="global",
                        title="phoenix launch episode",
                        summary="the phoenix launch review happened",
                        outcome="phoenix review complete",
                        occurred_at=now,
                        valid_from=None,
                        valid_to=None,
                        lifecycle_state="active",
                        primary_evidence_id=evidence.id,
                        related_entity_ids=[],
                        related_assertion_ids=[],
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                memory.record_reasoning_trace(
                    db,
                    scope_key="global",
                    trace_type="diagnostic",
                    task_summary="diagnose the phoenix launch",
                    trace_summary="checked the phoenix launch readiness",
                    outcome="succeeded",
                    primary_evidence_id=evidence.id,
                    source_turn_id=None,
                    now=now,
                    new_id_fn=_new_id,
                )
                db.add(
                    MemoryActionTraceRecord(
                        id=_new_id("mat"),
                        scope_key="global",
                        trace_type="execution",
                        action_attempt_id=None,
                        source_turn_id=None,
                        primary_evidence_id=evidence.id,
                        capability_id="cap.memory.inspect",
                        summary="ran the phoenix launch action",
                        outcome="succeeded",
                        result_refs={},
                        lifecycle_state="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    ProjectStateSnapshotRecord(
                        id=_new_id("pss"),
                        project_key="phoenix",
                        summary="phoenix project state snapshot",
                        state={"status": "active"},
                        lifecycle_state="active",
                        source_assertion_ids=[procedure_candidate["id"]],
                        source_episode_ids=[],
                        source_evidence_ids=[evidence.id],
                        projection_version=MEMORY_PROJECTION_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    MemoryContextBlockRecord(
                        id=_new_id("mcb"),
                        block_type="hot_index",
                        scope_key="global",
                        content="phoenix hot index block",
                        topic_id=None,
                        lifecycle_state="active",
                        source_assertion_ids=[procedure_candidate["id"]],
                        source_episode_ids=[],
                        source_trace_ids=[],
                        source_action_trace_ids=[],
                        source_procedure_ids=[],
                        source_project_state_snapshot_ids=[],
                        source_memory_versions={},
                        source_projection_versions={},
                        projection_version=MEMORY_PROJECTION_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                )
                topic = MemoryTopicRecord(
                    id=_new_id("mtp"),
                    topic_key="phoenix-topic",
                    family="active-projects",
                    scope_key="global",
                    title="phoenix topic",
                    summary="phoenix topic summary",
                    lifecycle_state="active",
                    projection_version=MEMORY_PROJECTION_VERSION,
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
                db.add(topic)
                db.flush()
                db.add(
                    MemoryContextBlockRecord(
                        id=_new_id("mcb"),
                        block_type="topic",
                        scope_key="global",
                        content="phoenix topic block",
                        topic_id=topic.id,
                        lifecycle_state="active",
                        source_assertion_ids=[procedure_candidate["id"]],
                        source_episode_ids=[],
                        source_trace_ids=[],
                        source_action_trace_ids=[],
                        source_procedure_ids=[],
                        source_project_state_snapshot_ids=[],
                        source_memory_versions={},
                        source_projection_versions={},
                        projection_version=MEMORY_PROJECTION_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                )

        # An open conflict on the phoenix deadline gives the conflict kind.
        conflicting = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{conflicting['id']}/approve").status_code == 409

        with _session_factory(client)() as db:
            memory_context, _event = memory.build_memory_context(
                db,
                user_message="phoenix launch deploy review readiness import",
                max_recalled_assertions=40,
                settings=_settings(),
                current_session_id=session_id,
            )
        candidate_kinds = {
            item["kind"] for item in memory_context["recall_window"]["candidate_memories"]
        }
        assert candidate_kinds == {
            "semantic_assertion",
            "episode",
            "reasoning_trace",
            "action_trace",
            "procedure",
            "project_state",
            "negative_memory",
            "hot_index",
            "topic",
            "conflict",
        }

        search_results = client.get(
            "/v1/memory/search",
            params={"q": "phoenix launch deploy review readiness import", "limit": 40},
        ).json()["results"]
        assert {result["kind"] for result in search_results} == candidate_kinds


def test_curation_accounts_for_every_candidate(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Curation must classify every pooled candidate as either selected or omitted:
    # selected + omitted is exactly the candidate pool, with no overlap.
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        for index in range(5):
            candidate = _candidate(
                client,
                subject_key="user:default",
                predicate="commitment.todo",
                assertion_type="commitment",
                value=f"phoenix follow-up task {index}",
                evidence_text=f"The user noted phoenix follow-up task {index}.",
            )
            assert (
                client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
            )

        with _session_factory(client)() as db:
            memory_context, _event = memory.build_memory_context(
                db,
                user_message="phoenix follow-up task",
                max_recalled_assertions=2,
                settings=_settings(),
                current_session_id=session_id,
            )
        recall_window = memory_context["recall_window"]
        pool = set(recall_window["candidate_memory_ids"])
        assert pool
        selected = {item["id"] for item in recall_window["selected_memories"]}
        omitted = {item["id"] for item in recall_window["omitted_memories"]}
        # Every candidate is accounted for, and nothing is both selected and omitted.
        assert selected | omitted == pool
        assert selected & omitted == set()
        assert (
            recall_window["selected_memory_count"] + recall_window["omitted_memory_count"]
            == recall_window["memory_candidate_count"]
        )


def test_supersession_records_invalidated_at_leaving_validity_window_intact(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        # profile.display_name is a supersede-policy predicate: approving a second
        # value supersedes the first on activation, no conflict.
        old_name = _candidate(
            client,
            value="Ada",
            assertion_type="profile",
            subject_key="user:default",
            predicate="profile.display_name",
        )
        assert client.post(f"/v1/memory/candidates/{old_name['id']}/approve").status_code == 200

        with _session_factory(client)() as db:
            active = db.get(MemoryAssertionRecord, old_name["id"])
            assert active is not None
            # An active assertion is current belief: no transaction-time invalidation.
            assert active.lifecycle_state == "active"
            assert active.invalidated_at is None
            valid_from_before = active.valid_from
            valid_to_before = active.valid_to
            assert valid_from_before is not None
            assert valid_to_before is None

        new_name = _candidate(
            client,
            value="Ada Lovelace",
            assertion_type="profile",
            subject_key="user:default",
            predicate="profile.display_name",
        )
        assert client.post(f"/v1/memory/candidates/{new_name['id']}/approve").status_code == 200

        with _session_factory(client)() as db:
            superseded = db.get(MemoryAssertionRecord, old_name["id"])
            replacement = db.get(MemoryAssertionRecord, new_name["id"])
            assert superseded is not None and replacement is not None
            assert superseded.lifecycle_state == "superseded"
            # Supersession stamps transaction-time invalidation at the moment the
            # replacement is activated.
            assert superseded.invalidated_at is not None
            assert superseded.invalidated_at == replacement.valid_from
            # valid_from is real-world validity and is left untouched by the
            # transaction-time stamp; valid_to is the pre-existing supersession
            # behaviour, distinct from invalidated_at.
            assert superseded.valid_from == valid_from_before
            # The freshly activated replacement is current belief: not invalidated.
            assert replacement.lifecycle_state == "active"
            assert replacement.invalidated_at is None


def test_retract_delete_and_stale_each_record_invalidated_at(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        retracted = _candidate(client, value="phoenix ships tomorrow")
        deleted = _candidate(client, value="zebra audit is overdue", subject_key="project:zebra")
        stale = _candidate(
            client, value="notebook migration is planned", subject_key="project:notebook"
        )
        for assertion in (retracted, deleted, stale):
            assert (
                client.post(f"/v1/memory/candidates/{assertion['id']}/approve").status_code == 200
            )

        assert client.post(f"/v1/memory/assertions/{retracted['id']}/retract").status_code == 200
        assert client.delete(f"/v1/memory/assertions/{deleted['id']}").status_code == 200
        assert (
            client.post(
                f"/v1/memory/assertions/{stale['id']}/mark-stale",
                json={"reason": "no reconfirmation before the deadline"},
            ).status_code
            == 200
        )

        with _session_factory(client)() as db:
            # Every transition out of active stamps invalidated_at.
            for assertion_id, expected_state in (
                (retracted["id"], "retracted"),
                (deleted["id"], "deleted"),
                (stale["id"], "stale"),
            ):
                row = db.get(MemoryAssertionRecord, assertion_id)
                assert row is not None
                assert row.lifecycle_state == expected_state
                assert row.invalidated_at is not None
                assert row.invalidated_at == row.updated_at


def test_redacting_evidence_records_invalidated_at_on_the_retracted_assertion(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        # Redacting source evidence cascades its linked active assertion to
        # `retracted`. That is a transition out of `active`, so -- like retract,
        # delete, stale, and supersession -- it must stamp invalidated_at; without
        # it an as-of recall would reconstruct the redacted assertion as
        # believed-active for all time.
        assertion = _candidate(client, value="orion launch secret is code violet")
        assert client.post(f"/v1/memory/candidates/{assertion['id']}/approve").status_code == 200

        with _session_factory(client)() as db:
            evidence_id = db.scalar(
                select(MemoryAssertionEvidenceRecord.evidence_id)
                .where(MemoryAssertionEvidenceRecord.assertion_id == assertion["id"])
                .limit(1)
            )
        assert evidence_id is not None
        assert (
            client.post(
                f"/v1/memory/evidence/{evidence_id}/redact",
                json={"reason": "user requested source redaction"},
            ).status_code
            == 200
        )

        with _session_factory(client)() as db:
            redacted = db.get(MemoryAssertionRecord, assertion["id"])
            assert redacted is not None
            assert redacted.lifecycle_state == "retracted"
            assert redacted.invalidated_at is not None
            assert redacted.invalidated_at == redacted.updated_at


def test_as_of_recall_reconstructs_past_transaction_time_belief_state(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_memory_models(monkeypatch)
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        base = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
        # A fully time-controlled supersession so the as-of windows are exact.
        #   base+0   old proposed
        #   base+1   old activated -> active belief is "Ada"
        #   base+3   new proposed
        #   base+4   new activated -> "Ada" superseded, invalidated_at = base+4
        timeline = iter(
            [
                base,
                base + timedelta(minutes=1),
                base + timedelta(minutes=3),
                base + timedelta(minutes=4),
            ]
        )

        def _next_now() -> datetime:
            return next(timeline)

        with _session_factory(client)() as db:
            with db.begin():
                memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text="the user is called Ada",
                    subject_key="user:default",
                    predicate="profile.display_name",
                    assertion_type="profile",
                    value="Ada",
                    confidence=0.95,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model="fixture",
                    extraction_prompt_version="fixture",
                    now_fn=_next_now,
                    new_id_fn=_new_id,
                )
        old_id = client.get("/v1/memory").json()["candidates"][0]["id"]
        with _session_factory(client)() as db:
            with db.begin():
                memory.approve_candidate(
                    db,
                    assertion_id=old_id,
                    actor_id="system",
                    now_fn=_next_now,
                    new_id_fn=_new_id,
                )
        with _session_factory(client)() as db:
            with db.begin():
                memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text="the user is called Ada Lovelace",
                    subject_key="user:default",
                    predicate="profile.display_name",
                    assertion_type="profile",
                    value="Ada Lovelace",
                    confidence=0.95,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model="fixture",
                    extraction_prompt_version="fixture",
                    now_fn=_next_now,
                    new_id_fn=_new_id,
                )
        new_id = next(
            candidate["id"]
            for candidate in client.get("/v1/memory").json()["candidates"]
            if candidate["id"] != old_id
        )
        with _session_factory(client)() as db:
            with db.begin():
                memory.approve_candidate(
                    db,
                    assertion_id=new_id,
                    actor_id="system",
                    now_fn=_next_now,
                    new_id_fn=_new_id,
                )

        with _session_factory(client)() as db:
            superseded = db.get(MemoryAssertionRecord, old_id)
            assert superseded is not None
            assert superseded.lifecycle_state == "superseded"
            assert superseded.invalidated_at == base + timedelta(minutes=4)

        # As-of before the new value was even proposed: the belief state is the old
        # value alone -- it reappears although it is now superseded -- and the new
        # value is excluded because it did not yet exist.
        with _session_factory(client)() as db:
            past_context, _past_event = memory.build_memory_context(
                db,
                user_message="who is the user",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
                as_of=base + timedelta(minutes=2),
            )
        past_pool = set(past_context["recall_window"]["candidate_memory_ids"])
        assert old_id in past_pool
        assert new_id not in past_pool

        # As-of after the supersession: the old value is invalidated and gone; the
        # current active value is the belief state.
        with _session_factory(client)() as db:
            now_context, _now_event = memory.build_memory_context(
                db,
                user_message="who is the user",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id=session_id,
                as_of=base + timedelta(minutes=5),
            )
        now_pool = set(now_context["recall_window"]["candidate_memory_ids"])
        assert new_id in now_pool
        assert old_id not in now_pool


def test_reactivation_clears_invalidated_at(postgres_url: str) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        # project.deadline is a conflict-policy predicate: a second contradicting
        # candidate enters the conflicted state, which stamps invalidated_at.
        first = _candidate(client, value="phoenix ships tomorrow")
        assert client.post(f"/v1/memory/candidates/{first['id']}/approve").status_code == 200
        second = _candidate(client, value="phoenix ships next week")
        assert client.post(f"/v1/memory/candidates/{second['id']}/approve").status_code == 409

        with _session_factory(client)() as db:
            conflicted = db.get(MemoryAssertionRecord, second["id"])
            assert conflicted is not None
            assert conflicted.lifecycle_state == "conflicted"
            assert conflicted.invalidated_at is not None

        # Retracting the active member leaves the conflicted candidate as the only
        # live member, so the conflict settles by re-activating it. Re-activation
        # restores it to current belief and clears the transaction-time stamp.
        assert client.post(f"/v1/memory/assertions/{first['id']}/retract").status_code == 200
        with _session_factory(client)() as db:
            reactivated = db.get(MemoryAssertionRecord, second["id"])
            assert reactivated is not None
            assert reactivated.lifecycle_state == "active"
            assert reactivated.invalidated_at is None


def test_reconfirmation_reactivates_an_assertion_demoted_by_the_forgetting_pass(
    postgres_url: str,
) -> None:
    adapter = MemoryContextProbeAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _session_id(client)
        # Fully time-controlled instants so the forgetting pass and the
        # re-confirmation are exact: propose and activate a single-valued fact,
        # then consolidate 300 days later -- far past the staleness horizon -- and
        # re-confirm two minutes after that. Each step gets a constant now so the
        # several now_fn calls inside a step (consolidation calls it again through
        # mark_assertion_stale) all agree.
        base = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
        consolidate_at = base + timedelta(days=300)
        reconfirm_at = consolidate_at + timedelta(minutes=2)

        # fact.environment is a supersede-policy (single-valued), decaying
        # predicate. The candidate is proposed with a low confidence so its
        # half-life-decayed value score lands below the forgetting floor.
        with _session_factory(client)() as db:
            with db.begin():
                memory.propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text="The user said the staging database runs postgres 14.",
                    subject_key="fact:staging-db",
                    predicate="fact.environment",
                    assertion_type="fact",
                    value="the staging database runs postgres 14",
                    confidence=0.3,
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model="fixture",
                    extraction_prompt_version="fixture",
                    now_fn=lambda: base,
                    new_id_fn=_new_id,
                )
        seeded_id = client.get("/v1/memory").json()["candidates"][0]["id"]
        with _session_factory(client)() as db:
            with db.begin():
                memory.approve_candidate(
                    db,
                    assertion_id=seeded_id,
                    actor_id="system",
                    now_fn=lambda: base + timedelta(minutes=1),
                    new_id_fn=_new_id,
                )

        # The forgetting pass, run 300 days on, demotes the now low-value fact to
        # `stale`, stamping the transaction-time invalidation.
        with _session_factory(client)() as db:
            with db.begin():
                memory.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    now_fn=lambda: consolidate_at,
                    new_id_fn=_new_id,
                    settings=_settings(),
                )
        with _session_factory(client)() as db:
            demoted = db.get(MemoryAssertionRecord, seeded_id)
            assert demoted is not None
            assert demoted.lifecycle_state == "stale"
            assert demoted.invalidated_at == consolidate_at

        # Re-confirmation: the same fact is stated again and approved. Activation
        # finds the demoted twin of the same value and re-activates it rather than
        # leaving a stale assertion beside a fresh active one, so invalidated_at is
        # cleared and last_verified_at is refreshed.
        reconfirm = _candidate(
            client,
            value="the staging database runs postgres 14",
            assertion_type="fact",
            subject_key="fact:staging-db",
            predicate="fact.environment",
        )
        assert reconfirm["id"] != seeded_id
        with _session_factory(client)() as db:
            with db.begin():
                memory.approve_candidate(
                    db,
                    assertion_id=reconfirm["id"],
                    actor_id="system",
                    now_fn=lambda: reconfirm_at,
                    new_id_fn=_new_id,
                )

        with _session_factory(client)() as db:
            reactivated = db.get(MemoryAssertionRecord, seeded_id)
            assert reactivated is not None
            assert reactivated.lifecycle_state == "active"
            assert reactivated.invalidated_at is None
            assert reactivated.last_verified_at == reconfirm_at
            # The re-confirmation candidate folds into the re-activated twin, so
            # recall carries one active assertion, not a stale/active duplicate.
            folded = db.get(MemoryAssertionRecord, reconfirm["id"])
            assert folded is not None
            assert folded.lifecycle_state == "superseded"
            assert folded.superseded_by_assertion_id == seeded_id
            # The re-activation is audited on a review and a version row.
            review = db.scalar(
                select(MemoryReviewRecord).where(
                    MemoryReviewRecord.assertion_id == seeded_id,
                    MemoryReviewRecord.reason == "reactivated by re-confirmation",
                )
            )
            assert review is not None
            version = db.scalar(
                select(MemoryVersionRecord).where(
                    MemoryVersionRecord.canonical_table == "memory_assertions",
                    MemoryVersionRecord.canonical_id == seeded_id,
                    MemoryVersionRecord.reason == "reactivated by re-confirmation",
                )
            )
            assert version is not None
