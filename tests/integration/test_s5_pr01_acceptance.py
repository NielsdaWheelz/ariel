from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.persistence import (
    MemoryItemRecord,
    MemoryRevisionRecord,
    SessionRecord,
    SessionRotationRecord,
)


@dataclass
class MemoryProbeAdapter:
    provider: str = "provider.s5-pr01"
    model: str = "model.s5-pr01-v1"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history
        snapshot = copy.deepcopy(context_bundle)
        self.context_bundles.append(snapshot)

        recalled = snapshot.get("durable_memory_recall")
        recalled_fragments: list[str] = []
        if isinstance(recalled, list):
            for item in recalled:
                if not isinstance(item, dict):
                    continue
                memory_class = item.get("memory_class")
                key = item.get("key")
                value = item.get("value")
                if isinstance(memory_class, str) and isinstance(key, str) and isinstance(value, str):
                    recalled_fragments.append(f"{memory_class}::{key}::{value}")

        assistant_text = (
            "recalled::" + " | ".join(recalled_fragments)
            if recalled_fragments
            else f"assistant::{user_message}"
        )
        return {
            "assistant_text": assistant_text,
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 19, "completion_tokens": 14, "total_tokens": 33},
            "provider_response_id": "resp_s5_pr01_123",
        }


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter, *, reset_database: bool) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=reset_database,
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _turn_ids(client: TestClient, session_id: str) -> list[str]:
    timeline = client.get(f"/v1/sessions/{session_id}/events")
    assert timeline.status_code == 200
    return [turn["id"] for turn in timeline.json()["turns"]]


def _latest_turn(client: TestClient, session_id: str) -> dict[str, Any]:
    timeline = client.get(f"/v1/sessions/{session_id}/events")
    assert timeline.status_code == 200
    turns = timeline.json()["turns"]
    assert turns
    return turns[-1]


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _recalled_ids_from_context(context_bundle: dict[str, Any]) -> list[str]:
    recalled = context_bundle.get("durable_memory_recall")
    if not isinstance(recalled, list):
        return []
    recalled_ids: list[str] = []
    for item in recalled:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("memory_id")
        if isinstance(memory_id, str):
            recalled_ids.append(memory_id)
    return recalled_ids


def _recalled_values_from_context(context_bundle: dict[str, Any]) -> list[str]:
    recalled = context_bundle.get("durable_memory_recall")
    if not isinstance(recalled, list):
        return []
    recalled_values: list[str] = []
    for item in recalled:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, str):
            recalled_values.append(value)
    return recalled_values


def _memory_projection_items(client: TestClient) -> list[dict[str, Any]]:
    projection = client.get("/v1/memory")
    assert projection.status_code == 200
    payload = projection.json()
    assert payload["ok"] is True
    items = payload["items"]
    assert isinstance(items, list)
    return items


def test_s5_pr01_candidate_memory_requires_explicit_promotion_before_cross_session_recall(
    postgres_url: str,
) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        first_session_id = _session_id(client)
        candidate_turn = client.post(
            f"/v1/sessions/{first_session_id}/message",
            json={"message": "i like matte black notebooks"},
        )
        assert candidate_turn.status_code == 200
        assert "evt.memory.candidate_proposed" in _event_types(_latest_turn(client, first_session_id))

        projection_before = _memory_projection_items(client)
        notebook_candidate = next(
            item
            for item in projection_before
            if item["memory_key"] == "preference:notebook_style"
        )
        assert notebook_candidate["lifecycle_state"] == "candidate"

        rotate_first = client.post(
            "/v1/sessions/rotate",
            headers={"Idempotency-Key": "rotate-candidate-1"},
        )
        assert rotate_first.status_code == 200
        second_session_id = rotate_first.json()["session"]["id"]
        assert second_session_id != first_session_id

        recall_before_promotion = client.post(
            f"/v1/sessions/{second_session_id}/message",
            json={"message": "what notebook style do i like?"},
        )
        assert recall_before_promotion.status_code == 200
        recalled_values_before = _recalled_values_from_context(adapter.context_bundles[-1])
        assert all("matte black notebooks" not in value for value in recalled_values_before)

        promotion_turn = client.post(
            f"/v1/sessions/{second_session_id}/message",
            json={"message": "remember preference:notebook_style=matte black notebooks"},
        )
        assert promotion_turn.status_code == 200
        promoted_events = _event_types(_latest_turn(client, second_session_id))
        assert "evt.memory.promoted" in promoted_events

        projection_after = _memory_projection_items(client)
        notebook_item = next(
            item for item in projection_after if item["memory_key"] == "preference:notebook_style"
        )
        assert notebook_item["lifecycle_state"] == "validated"

        rotate_second = client.post(
            "/v1/sessions/rotate",
            headers={"Idempotency-Key": "rotate-candidate-2"},
        )
        assert rotate_second.status_code == 200
        third_session_id = rotate_second.json()["session"]["id"]

        recall_after_promotion = client.post(
            f"/v1/sessions/{third_session_id}/message",
            json={"message": "what notebook style do i like?"},
        )
        assert recall_after_promotion.status_code == 200
        recalled_values_after = _recalled_values_from_context(adapter.context_bundles[-1])
        assert any("matte black notebooks" in value for value in recalled_values_after)


def test_s5_pr01_correction_and_retraction_apply_immediately(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        remember = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember preference:coffee=pour-over coffee"},
        )
        assert remember.status_code == 200

        recall_before = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer?"},
        )
        assert recall_before.status_code == 200
        values_before = _recalled_values_from_context(adapter.context_bundles[-1])
        assert any("pour-over coffee" in value for value in values_before)

        correction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "correct preference:coffee=espresso"},
        )
        assert correction.status_code == 200
        corrected_events = _event_types(_latest_turn(client, session_id))
        assert "evt.memory.corrected" in corrected_events

        recall_after_correction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer now?"},
        )
        assert recall_after_correction.status_code == 200
        values_after_correction = _recalled_values_from_context(adapter.context_bundles[-1])
        assert any("espresso" in value for value in values_after_correction)
        assert all("pour-over coffee" not in value for value in values_after_correction)

        retraction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "forget preference:coffee"},
        )
        assert retraction.status_code == 200
        retracted_events = _event_types(_latest_turn(client, session_id))
        assert "evt.memory.retracted" in retracted_events

        recall_after_retraction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer now?"},
        )
        assert recall_after_retraction.status_code == 200
        values_after_retraction = _recalled_values_from_context(adapter.context_bundles[-1])
        assert all("espresso" not in value for value in values_after_retraction)
        assert all("pour-over coffee" not in value for value in values_after_retraction)

        projection = _memory_projection_items(client)
        coffee_item = next(item for item in projection if item["memory_key"] == "preference:coffee")
        assert coffee_item["lifecycle_state"] == "retracted"


def test_s5_pr01_replacing_active_memory_revision_marks_prior_revision_superseded(
    postgres_url: str,
) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)

        candidate = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "i like matte black notebooks"},
        )
        assert candidate.status_code == 200
        assert "evt.memory.candidate_proposed" in _event_types(_latest_turn(client, session_id))

        promotion = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember preference:notebook_style=matte black notebooks"},
        )
        assert promotion.status_code == 200
        assert "evt.memory.promoted" in _event_types(_latest_turn(client, session_id))

        correction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "correct preference:notebook_style=dot grid notebooks"},
        )
        assert correction.status_code == 200
        assert "evt.memory.corrected" in _event_types(_latest_turn(client, session_id))

        overwrite = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember preference:notebook_style=spiral notebooks"},
        )
        assert overwrite.status_code == 200
        assert "evt.memory.captured" in _event_types(_latest_turn(client, session_id))

        retract = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "forget preference:notebook_style"},
        )
        assert retract.status_code == 200
        assert "evt.memory.retracted" in _event_types(_latest_turn(client, session_id))

        projection = _memory_projection_items(client)
        notebook_item = next(item for item in projection if item["memory_key"] == "preference:notebook_style")
        assert notebook_item["lifecycle_state"] == "retracted"
        assert notebook_item["revision_count"] == 5

        with cast(Any, client.app).state.session_factory() as db:
            item_row = db.scalar(
                select(MemoryItemRecord).where(MemoryItemRecord.memory_key == "preference:notebook_style").limit(1)
            )
            assert item_row is not None
            revisions = db.scalars(
                select(MemoryRevisionRecord)
                .where(MemoryRevisionRecord.memory_item_id == item_row.id)
                .order_by(MemoryRevisionRecord.created_at.asc(), MemoryRevisionRecord.id.asc())
            ).all()
            assert len(revisions) == 5
            assert [revision.lifecycle_state for revision in revisions] == [
                "superseded",
                "superseded",
                "superseded",
                "superseded",
                "retracted",
            ]
            assert [revision.value for revision in revisions] == [
                "matte black notebooks",
                "matte black notebooks",
                "dot grid notebooks",
                "spiral notebooks",
                None,
            ]
            assert item_row.active_revision_id == revisions[-1].id


def test_s5_pr01_rotation_is_idempotent_by_key_and_auditable(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        seeded = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            json={"message": "remember commitment:legal_review=review legal terms before signing"},
        )
        assert seeded.status_code == 200

        first = client.post("/v1/sessions/rotate", headers={"Idempotency-Key": "rotate-audit-001"})
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["rotation"]["idempotency_key"] == "rotate-audit-001"
        assert first_payload["rotation"]["idempotent_replay"] is False

        second = client.post("/v1/sessions/rotate", headers={"Idempotency-Key": "rotate-audit-001"})
        assert second.status_code == 200
        second_payload = second.json()
        assert second_payload["rotation"]["idempotent_replay"] is True
        assert second_payload["rotation"]["rotation_id"] == first_payload["rotation"]["rotation_id"]
        assert second_payload["session"]["id"] == first_payload["session"]["id"]

        rotations = client.get("/v1/sessions/rotations", params={"limit": 20})
        assert rotations.status_code == 200
        rows = rotations.json()["rotations"]
        assert [row["idempotency_key"] for row in rows].count("rotate-audit-001") == 1
        rotation_row = next(row for row in rows if row["idempotency_key"] == "rotate-audit-001")
        assert rotation_row["reason"] == "user_initiated"
        assert rotation_row["rotated_from_session_id"] == initial_session_id

        with cast(Any, client.app).state.session_factory() as db:
            active_count = db.scalar(
                select(func.count()).select_from(SessionRecord).where(SessionRecord.is_active.is_(True))
            )
            assert active_count == 1
            rotation_count = db.scalar(
                select(func.count())
                .select_from(SessionRotationRecord)
                .where(SessionRotationRecord.idempotency_key == "rotate-audit-001")
            )
            assert rotation_count == 1


def test_s5_pr01_projection_is_derived_from_active_revision(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember profile:timezone=pst"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember profile:timezone=utc"},
        )
        assert second.status_code == 200

        items = _memory_projection_items(client)
        timezone_item = next(item for item in items if item["memory_key"] == "profile:timezone")
        assert timezone_item["value"] == "utc"
        assert timezone_item["lifecycle_state"] == "validated"
        assert timezone_item["revision_count"] == 2

        with cast(Any, client.app).state.session_factory() as db:
            item_row = db.scalar(
                select(MemoryItemRecord).where(MemoryItemRecord.memory_key == "profile:timezone").limit(1)
            )
            assert item_row is not None
            revisions = db.scalars(
                select(MemoryRevisionRecord)
                .where(MemoryRevisionRecord.memory_item_id == item_row.id)
                .order_by(MemoryRevisionRecord.created_at.asc(), MemoryRevisionRecord.id.asc())
            ).all()
            assert len(revisions) == 2
            assert item_row.active_revision_id == revisions[-1].id


def test_s5_pr01_recall_is_bounded_deterministic_and_emits_skip_reasons(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECALLED_MEMORIES", "2")
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        assert client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember project:apollo=apollo milestone in may"},
        ).status_code == 200
        assert client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember project:apollo_risk=apollo delivery risk is vendor latency"},
        ).status_code == 200
        assert client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember project:apollo_archive=apollo archive doc is in drive"},
        ).status_code == 200

        first_query = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the latest apollo status?"},
        )
        assert first_query.status_code == 200
        first_recall_ids = _recalled_ids_from_context(adapter.context_bundles[-1])

        second_query = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "again, what is the latest apollo status?"},
        )
        assert second_query.status_code == 200
        second_recall_ids = _recalled_ids_from_context(adapter.context_bundles[-1])

        assert first_recall_ids == second_recall_ids
        assert len(first_recall_ids) == 2

        recalled_event_payload = next(
            event["payload"]
            for event in _latest_turn(client, session_id)["events"]
            if event["event_type"] == "evt.memory.recalled"
        )
        assert recalled_event_payload["max_recalled_memories"] == 2
        assert recalled_event_payload["included_memory_count"] == 2
        assert recalled_event_payload["omitted_memory_count"] >= 1
        excluded = recalled_event_payload["excluded_memories"]
        assert any(item["reason"] == "top_k_bounded" for item in excluded)
