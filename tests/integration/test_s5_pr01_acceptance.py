from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
import ariel.memory as memory
from ariel.memory import MEMORY_PROJECTION_VERSION, process_memory_projection_job
from ariel.persistence import (
    AIJudgmentRecord,
    BackgroundTaskRecord,
    MemoryAssertionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEventRecord,
    MemoryKeywordProjectionRecord,
    MemorySalienceRecord,
)
from tests.integration.responses_helpers import responses_run_message
from tests.fake_sandbox import FakeSandboxRuntime


_projection_id_counter = count(1)


def _fake_memory_embedding(text: str, *, settings: AppSettings) -> list[float]:
    vector = [0.0] * settings.memory_embedding_dimensions
    lowered = text.lower()
    for index, words in (
        (0, ("notebook", "notebooks")),
        (1, ("apollo",)),
        (2, ("milestone", "latest", "status", "state")),
        (3, ("risk", "vendor", "latency")),
        (4, ("archive", "drive")),
        (5, ("invoice", "open")),
        (6, ("coffee", "espresso", "pour-over")),
    ):
        if any(word in lowered for word in words):
            vector[index] = 1.0
    if not any(vector):
        vector[7] = 1.0
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
    selected: list[dict[str, str]] = []
    omitted: list[dict[str, str]] = []
    for candidate in candidates:
        memory_id = candidate["id"]
        value = str(candidate.get("value", ""))
        kind = str(candidate.get("kind") or "semantic_assertion")
        if "delivery risk" not in value and len(selected) < max_selected:
            selected.append(
                {
                    "id": memory_id,
                    "kind": kind,
                    "rationale": "curator selected for this turn",
                }
            )
        else:
            omitted.append({"id": memory_id, "kind": kind, "reason": "curator omitted"})
    return {
        "selected_memories": selected,
        "omitted_memories": omitted,
        "rationale": "fixture curator decision",
        "uncertainty": "",
        "confidence": 0.9,
        "model": "fixture-memory-curator",
        "prompt_version": memory.MEMORY_CURATION_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_memory_curator",
        "parse_status": "parsed",
    }


def _use_fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _fake_memory_curation)


def _process_projection(client: TestClient) -> None:
    processed = process_memory_projection_job(
        session_factory=cast(Any, client.app).state.session_factory,
        settings=AppSettings(),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=lambda prefix: f"{prefix}_test_{next(_projection_id_counter)}",
    )
    assert processed is True


@dataclass
class MemoryProbeAdapter:
    provider: str = "provider.s5-pr01"
    model: str = "model.s5-pr01-v1"
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
        snapshot = copy.deepcopy(context_bundle)
        self.context_bundles.append(snapshot)

        fragments: list[str] = []
        memory_context = snapshot.get("memory_context")
        if isinstance(memory_context, dict):
            assertions = memory_context.get("semantic_assertions")
            if isinstance(assertions, list):
                for assertion in assertions:
                    if not isinstance(assertion, dict):
                        continue
                    subject_key = assertion.get("subject_key")
                    predicate = assertion.get("predicate")
                    value = assertion.get("value")
                    if (
                        isinstance(subject_key, str)
                        and isinstance(predicate, str)
                        and isinstance(value, str)
                    ):
                        fragments.append(f"{subject_key}::{predicate}::{value}")

        assistant_text = (
            "recalled::" + " | ".join(fragments) if fragments else f"assistant::{user_message}"
        )
        return responses_run_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s5_pr01_123",
            input_tokens=19,
            output_tokens=14,
        )


def _build_client(
    postgres_url: str,
    adapter: ModelAdapter,
    *,
    reset_database: bool,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=reset_database,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _session_id(client: TestClient) -> str:
    response = client.get("/v1/sessions/active")
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _latest_turn(client: TestClient, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}/events")
    assert response.status_code == 200
    turns = response.json()["turns"]
    assert turns
    return turns[-1]


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _memory(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/memory")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["schema_version"] == "memory.sota.v2"
    return payload


def _candidate(
    client: TestClient,
    *,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    evidence_text: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/memory/candidates",
        json={
            "subject_key": subject_key,
            "predicate": predicate,
            "assertion_type": assertion_type,
            "value": value,
            "evidence_text": evidence_text,
            "confidence": 0.92,
        },
    )
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert candidates
    return candidates[0]


def _recalled_values(context_bundle: dict[str, Any]) -> list[str]:
    memory_context = context_bundle.get("memory_context")
    if not isinstance(memory_context, dict):
        return []
    assertions = memory_context.get("semantic_assertions")
    if not isinstance(assertions, list):
        return []
    return [
        assertion["value"]
        for assertion in assertions
        if isinstance(assertion, dict) and isinstance(assertion.get("value"), str)
    ]


def test_s5_pr01_turns_record_evidence_and_queue_extraction_without_command_parser(
    postgres_url: str,
) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember preference coffee = pour-over"},
        )
        assert response.status_code == 200

        payload = _memory(client)
        assert payload["active_assertions"] == []
        assert payload["candidates"] == []
        assert payload["evidence"]

        event_types = _event_types(_latest_turn(client, session_id))
        assert "evt.memory.extraction_queued" in event_types
        assert "evt.memory.candidate_proposed" not in event_types
        # Memory lifecycle events are not in the turn EventRecord stream; they
        # land in the non-turn-scoped memory_events log.
        assert "evt.memory.evidence_recorded" not in event_types

        with cast(Any, client.app).state.session_factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(BackgroundTaskRecord)
                    .where(BackgroundTaskRecord.task_type == "memory_extract_turn")
                )
                == 1
            )
            memory_event_rows = db.scalars(
                select(MemoryEventRecord).where(MemoryEventRecord.entry_path == "turn")
            ).all()
            assert {row.event_type for row in memory_event_rows} == {"evt.memory.evidence_recorded"}
            assert all(row.source_turn_id is not None for row in memory_event_rows)


def test_s5_pr01_reviewed_candidate_is_recalled_with_evidence_snippet(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        candidate = _candidate(
            client,
            subject_key="user:default",
            predicate="preference.notebook_style",
            assertion_type="preference",
            value="matte black notebooks",
            evidence_text="The user said they prefer matte black notebooks.",
        )
        assert candidate["state"] == "candidate"
        assert candidate["evidence_refs"][0]["snippet"]

        recall_before = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what notebooks do i like?"},
        )
        assert recall_before.status_code == 200
        assert "matte black notebooks" not in _recalled_values(adapter.context_bundles[-1])

        approve = client.post(f"/v1/memory/candidates/{candidate['id']}/approve")
        assert approve.status_code == 200
        active = approve.json()["active_assertions"]
        assert [item["value"] for item in active] == ["matte black notebooks"]
        assert active[0]["evidence_refs"][0]["snippet"]
        _process_projection(client)

        recall_after = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what notebooks do i like?"},
        )
        assert recall_after.status_code == 200
        assert "matte black notebooks" in _recalled_values(adapter.context_bundles[-1])


def test_s5_pr01_correction_retraction_and_projection_invalidation(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        candidate = _candidate(
            client,
            subject_key="user:default",
            predicate="preference.coffee",
            assertion_type="preference",
            value="pour-over coffee",
            evidence_text="The user prefers pour-over coffee.",
        )
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        correction = client.post(
            f"/v1/memory/assertions/{candidate['id']}/correct",
            json={"value": "espresso"},
        )
        assert correction.status_code == 200
        active = correction.json()["active_assertions"]
        assert [item["value"] for item in active] == ["espresso"]
        _process_projection(client)

        with cast(Any, client.app).state.session_factory() as db:
            rows = db.scalars(
                select(MemoryAssertionRecord)
                .where(MemoryAssertionRecord.predicate == "preference.coffee")
                .order_by(MemoryAssertionRecord.created_at.asc(), MemoryAssertionRecord.id.asc())
            ).all()
            assert [row.lifecycle_state for row in rows] == ["superseded", "active"]
            assert rows[0].superseded_by_assertion_id == rows[1].id
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProjectionRecord)
                    .where(MemoryEmbeddingProjectionRecord.assertion_id == rows[0].id)
                )
                == 0
            )
            replacement_embedding = db.scalar(
                select(MemoryEmbeddingProjectionRecord).where(
                    MemoryEmbeddingProjectionRecord.assertion_id == rows[1].id,
                    MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                )
            )
            replacement_keyword = db.scalar(
                select(MemoryKeywordProjectionRecord).where(
                    MemoryKeywordProjectionRecord.canonical_id == rows[1].id,
                    MemoryKeywordProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                )
            )
            assert replacement_embedding is not None
            assert replacement_embedding.embedding_provider == "openai"
            assert replacement_embedding.embedding_model == "text-embedding-3-small"
            assert (
                replacement_embedding.embedding_dimensions
                == AppSettings().memory_embedding_dimensions
            )
            assert len(replacement_embedding.embedding) == AppSettings().memory_embedding_dimensions
            assert replacement_keyword is not None
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemorySalienceRecord)
                    .where(MemorySalienceRecord.assertion_id == rows[1].id)
                )
                == 1
            )

        retraction = client.post(f"/v1/memory/assertions/{active[0]['id']}/retract")
        assert retraction.status_code == 200
        assert retraction.json()["active_assertions"] == []


def test_s5_pr01_projections_are_vector_and_keyword_not_legacy_terms(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        candidate = _candidate(
            client,
            subject_key="project:apollo",
            predicate="project.state",
            assertion_type="project_state",
            value="apollo milestone is in may",
            evidence_text="Apollo project milestone is in May.",
        )
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        with cast(Any, client.app).state.session_factory() as db:
            embedding = db.scalar(
                select(MemoryEmbeddingProjectionRecord).where(
                    MemoryEmbeddingProjectionRecord.assertion_id == candidate["id"]
                )
            )
            keyword = db.scalar(
                select(MemoryKeywordProjectionRecord).where(
                    MemoryKeywordProjectionRecord.canonical_id == candidate["id"]
                )
            )
            assert embedding is not None
            assert keyword is not None
            assert embedding.projection_version == MEMORY_PROJECTION_VERSION
            assert embedding.embedding_provider == "openai"
            assert embedding.embedding_model == "text-embedding-3-small"
            assert embedding.embedding_dimensions == AppSettings().memory_embedding_dimensions
            assert len(embedding.embedding) == AppSettings().memory_embedding_dimensions
            assert len(embedding.embedding) != 64
            assert (
                len([float(item) for item in embedding.embedding])
                == AppSettings().memory_embedding_dimensions
            )
            assert keyword.projection_version == MEMORY_PROJECTION_VERSION
            assert keyword.search_document


def test_s5_pr01_recall_uses_ai_curation_and_reports_omissions(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECALLED_ASSERTIONS", "2")
    _use_fake_embeddings(monkeypatch)
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        for predicate, value in (
            ("project.state", "apollo milestone in may"),
            ("project.risk", "apollo delivery risk is vendor latency"),
            ("project.archive", "apollo archive doc is in drive"),
        ):
            candidate = _candidate(
                client,
                subject_key="project:apollo",
                predicate=predicate,
                assertion_type="project_state",
                value=value,
                evidence_text=value,
            )
            assert (
                client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
            )
            _process_projection(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the latest apollo project status?"},
        )
        assert first.status_code == 200
        first_turn_id = first.json()["turn"]["id"]
        first_ids = [
            item["id"]
            for item in adapter.context_bundles[-1]["memory_context"]["semantic_assertions"]
        ]

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "again, what is the latest apollo project status?"},
        )
        assert second.status_code == 200
        second_turn_id = second.json()["turn"]["id"]
        second_ids = [
            item["id"]
            for item in adapter.context_bundles[-1]["memory_context"]["semantic_assertions"]
        ]

        assert first_ids == second_ids
        assert len(first_ids) == 2
        first_assertions = adapter.context_bundles[-2]["memory_context"]["semantic_assertions"]
        assert {item["predicate"] for item in first_assertions} == {
            "project.archive",
            "project.state",
        }
        assert all("rank_reason" not in item for item in first_assertions)
        assert all("rank_score" not in item for item in first_assertions)

        recalled_event_payload = next(
            event["payload"]
            for event in _latest_turn(client, session_id)["events"]
            if event["event_type"] == "evt.memory.curated"
        )
        assert recalled_event_payload["max_selected_memories"] == 2
        assert recalled_event_payload["selected_memory_count"] == 2
        assert recalled_event_payload["curation_model"] == "fixture-memory-curator"
        assert recalled_event_payload["selected_memories"] == [
            {
                "id": first_ids[0],
                "kind": "semantic_assertion",
                "rationale": "curator selected for this turn",
            },
            {
                "id": first_ids[1],
                "kind": "semantic_assertion",
                "rationale": "curator selected for this turn",
            },
        ]
        assert recalled_event_payload["omitted_memory_count"] >= 1
        assert any(
            item["reason"] == "curator omitted"
            for item in recalled_event_payload["omitted_memories"]
        )
        first_candidate = next(
            item
            for item in recalled_event_payload["candidate_memories"]
            if item["kind"] == "semantic_assertion"
        )
        assert first_candidate["lifecycle_state"] == "active"
        assert first_candidate["trust_boundary"]
        assert first_candidate["taint"]["provenance_status"]
        assert first_candidate["projection_version"] == MEMORY_PROJECTION_VERSION
        # The RRF pipeline attaches a feature vector, not a transport_order.
        features = first_candidate["retrieval_features"]
        assert features["rrf_score"] > 0.0
        assert isinstance(features["signal_ranks"], dict) and features["signal_ranks"]
        assert "effective_confidence" in features
        assert "conflict_status" in first_candidate

        with cast(Any, client.app).state.session_factory() as db:
            judgments = db.scalars(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "memory_curation",
                    AIJudgmentRecord.source_id.in_([first_turn_id, second_turn_id]),
                )
                .order_by(AIJudgmentRecord.created_at.asc())
            ).all()
            assert [judgment.source_id for judgment in judgments] == [
                first_turn_id,
                second_turn_id,
            ]
            assert all(judgment.status == "succeeded" for judgment in judgments)
            assert all(
                judgment.provider_response_id == "resp_fixture_memory_curator"
                for judgment in judgments
            )
            assert all(judgment.parse_status == "parsed" for judgment in judgments)
            assert all(judgment.validation_status == "valid" for judgment in judgments)
            assert all(judgment.selected for judgment in judgments)
            assert all(judgment.output["recall_window"]["curation_model"] for judgment in judgments)
            assert all(judgment.input_refs["candidate_memories"] for judgment in judgments)


def test_s5_pr01_invalid_memory_curation_fails_as_typed_audited_turn_failure(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_memory_curation(
        *,
        user_message: str,
        history: Sequence[dict[str, Any]],
        candidates: Sequence[dict[str, Any]],
        max_selected: int,
        settings: AppSettings,
    ) -> dict[str, Any]:
        del user_message, history, candidates, max_selected, settings
        raise RuntimeError("memory curation JSON missing required fields")

    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", invalid_memory_curation)
    adapter = MemoryProbeAdapter()
    with _build_client(
        postgres_url,
        adapter,
        reset_database=True,
        raise_server_exceptions=False,
    ) as client:
        session_id = _session_id(client)
        candidate = _candidate(
            client,
            subject_key="project:curation-cutover",
            predicate="project.state",
            assertion_type="project_state",
            value="curation cutover candidate should require AI selection",
            evidence_text="The curation cutover candidate should require AI selection.",
        )
        assert client.post(f"/v1/memory/candidates/{candidate['id']}/approve").status_code == 200
        _process_projection(client)

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "recall the curation cutover project state"},
        )

        assert response.headers["content-type"].startswith("application/json")
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "E_AI_JUDGMENT_SCHEMA"
        assert error["retryable"] is True
        assert error["details"]["judgment_type"] == "memory_curation"
        assert error["details"]["prompt_version"] == memory.MEMORY_CURATION_PROMPT_VERSION
        assert isinstance(error["details"]["turn_id"], str)

        latest_turn = _latest_turn(client, session_id)
        assert latest_turn["id"] == error["details"]["turn_id"]
        assert "evt.turn.started" in _event_types(latest_turn)
        assert "evt.assistant.emitted" not in _event_types(latest_turn)
        assert adapter.context_bundles == []

        audit_events = [
            event
            for event in latest_turn["events"]
            if event["payload"].get("judgment_type") == "memory_curation"
        ]
        assert audit_events
        failure_payload = audit_events[-1]["payload"]
        assert failure_payload["failure_code"] == "E_AI_JUDGMENT_SCHEMA"
        assert failure_payload["prompt_version"] == memory.MEMORY_CURATION_PROMPT_VERSION
        assert failure_payload["source_id"] == latest_turn["id"]
        assert failure_payload["parse_status"] == "parsed"
        assert failure_payload["validation_status"] == "invalid"
        assert candidate["id"] in repr(failure_payload["input_refs"])

        with cast(Any, client.app).state.session_factory() as db:
            judgment = db.scalar(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "memory_curation",
                    AIJudgmentRecord.source_id == latest_turn["id"],
                )
                .limit(1)
            )
            assert judgment is not None
            assert judgment.status == "failed"
            assert judgment.failure_code == "E_AI_JUDGMENT_SCHEMA"
            assert judgment.parse_status == "parsed"
            assert judgment.validation_status == "invalid"
            assert candidate["id"] in repr(judgment.input_refs)
