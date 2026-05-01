from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
import ariel.memory as memory
from ariel.memory import MEMORY_PROJECTION_VERSION, process_memory_projection_job
from ariel.persistence import (
    BackgroundTaskRecord,
    MemoryAssertionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryKeywordProjectionRecord,
    MemorySalienceRecord,
)
from tests.integration.responses_helpers import responses_message


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


def _use_fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)


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
        return responses_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s5_pr01_123",
            input_tokens=19,
            output_tokens=14,
        )


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter, *, reset_database: bool) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=reset_database,
    )
    return TestClient(app)


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
    assert payload["schema_version"] == "memory.sota.v1"
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
        assert "evt.memory.evidence_recorded" in event_types
        assert "evt.memory.extraction_queued" in event_types
        assert "evt.memory.candidate_proposed" not in event_types

        with cast(Any, client.app).state.session_factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(BackgroundTaskRecord)
                    .where(BackgroundTaskRecord.task_type == "memory_extract_turn")
                )
                == 1
            )


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
            assert keyword.weighted_terms


def test_s5_pr01_recall_is_bounded_deterministic_and_reports_omissions(
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
        first_ids = [
            item["id"]
            for item in adapter.context_bundles[-1]["memory_context"]["semantic_assertions"]
        ]

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "again, what is the latest apollo project status?"},
        )
        assert second.status_code == 200
        second_ids = [
            item["id"]
            for item in adapter.context_bundles[-1]["memory_context"]["semantic_assertions"]
        ]

        assert first_ids == second_ids
        assert len(first_ids) == 2
        first_reasons = [
            item["rank_reason"]
            for item in adapter.context_bundles[-2]["memory_context"]["semantic_assertions"]
        ]
        assert any("semantic_vector" in reason for reason in first_reasons)
        assert any("keyword" in reason for reason in first_reasons)

        recalled_event_payload = next(
            event["payload"]
            for event in _latest_turn(client, session_id)["events"]
            if event["event_type"] == "evt.memory.recalled"
        )
        assert recalled_event_payload["max_recalled_items"] == 2
        assert recalled_event_payload["included_memory_count"] == 2
        assert recalled_event_payload["omitted_memory_count"] >= 1
        assert any(
            item["reason"] == "top_k_bounded" for item in recalled_event_payload["omitted_memories"]
        )
