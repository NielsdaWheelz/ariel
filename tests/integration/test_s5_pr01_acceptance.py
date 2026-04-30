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
    MemoryAssertionRecord,
    MemoryEmbeddingProjectionRecord,
    MemorySalienceRecord,
    SessionRecord,
    SessionRotationRecord,
)
from tests.integration.responses_helpers import responses_message


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
            assertions = memory_context.get("assertions")
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


def _latest_turn(client: TestClient, session_id: str) -> dict[str, Any]:
    timeline = client.get(f"/v1/sessions/{session_id}/events")
    assert timeline.status_code == 200
    turns = timeline.json()["turns"]
    assert turns
    return turns[-1]


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _recalled_assertion_ids(context_bundle: dict[str, Any]) -> list[str]:
    memory_context = context_bundle.get("memory_context")
    if not isinstance(memory_context, dict):
        return []
    assertions = memory_context.get("assertions")
    if not isinstance(assertions, list):
        return []
    ids: list[str] = []
    for assertion in assertions:
        if isinstance(assertion, dict) and isinstance(assertion.get("assertion_id"), str):
            ids.append(assertion["assertion_id"])
    return ids


def _recalled_values(context_bundle: dict[str, Any]) -> list[str]:
    memory_context = context_bundle.get("memory_context")
    if not isinstance(memory_context, dict):
        return []
    assertions = memory_context.get("assertions")
    if not isinstance(assertions, list):
        return []
    values: list[str] = []
    for assertion in assertions:
        if isinstance(assertion, dict) and isinstance(assertion.get("value"), str):
            values.append(assertion["value"])
    return values


def _memory_assertions(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/v1/memory")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assertions = payload["assertions"]
    assert isinstance(assertions, list)
    return assertions


def test_s5_pr01_candidate_memory_requires_review_before_cross_session_recall(
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
        event_types = _event_types(_latest_turn(client, first_session_id))
        assert "evt.memory.candidate_proposed" in event_types
        assert "evt.memory.review_required" in event_types

        projection_before = client.get("/v1/memory")
        assert projection_before.status_code == 200
        candidates = projection_before.json()["candidates"]
        assert len(candidates) == 1
        assert candidates[0]["lifecycle_state"] == "candidate"
        assert candidates[0]["predicate"] == "preference.general"

        rotate_first = client.post(
            "/v1/sessions/rotate",
            headers={"Idempotency-Key": "rotate-candidate-1"},
        )
        assert rotate_first.status_code == 200
        second_session_id = rotate_first.json()["session"]["id"]

        recall_before_approval = client.post(
            f"/v1/sessions/{second_session_id}/message",
            json={"message": "what notebooks do i like?"},
        )
        assert recall_before_approval.status_code == 200
        assert all(
            "matte black notebooks" not in value
            for value in _recalled_values(adapter.context_bundles[-1])
        )

        approve = client.post(f"/v1/memory/candidates/{candidates[0]['assertion_id']}/approve")
        assert approve.status_code == 200
        approved_assertion = next(
            item
            for item in approve.json()["assertions"]
            if item["assertion_id"] == candidates[0]["assertion_id"]
        )
        assert approved_assertion["lifecycle_state"] == "active"

        rotate_second = client.post(
            "/v1/sessions/rotate",
            headers={"Idempotency-Key": "rotate-candidate-2"},
        )
        assert rotate_second.status_code == 200
        third_session_id = rotate_second.json()["session"]["id"]

        recall_after_approval = client.post(
            f"/v1/sessions/{third_session_id}/message",
            json={"message": "what notebooks do i like?"},
        )
        assert recall_after_approval.status_code == 200
        assert any(
            "matte black notebooks" in value
            for value in _recalled_values(adapter.context_bundles[-1])
        )


def test_s5_pr01_correction_and_retraction_apply_immediately(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        remember = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "remember preference coffee = pour-over coffee"},
        )
        assert remember.status_code == 200

        recall_before = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer?"},
        )
        assert recall_before.status_code == 200
        assert any(
            "pour-over coffee" in value for value in _recalled_values(adapter.context_bundles[-1])
        )

        correction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "correct preference coffee = espresso"},
        )
        assert correction.status_code == 200
        corrected_events = _event_types(_latest_turn(client, session_id))
        assert "evt.memory.assertion_superseded" in corrected_events
        assert "evt.memory.assertion_activated" in corrected_events

        recall_after_correction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer now?"},
        )
        assert recall_after_correction.status_code == 200
        values_after_correction = _recalled_values(adapter.context_bundles[-1])
        assert any("espresso" in value for value in values_after_correction)
        assert all("pour-over coffee" not in value for value in values_after_correction)

        retraction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "forget preference coffee"},
        )
        assert retraction.status_code == 200
        assert "evt.memory.assertion_retracted" in _event_types(_latest_turn(client, session_id))

        recall_after_retraction = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what coffee do i prefer now?"},
        )
        assert recall_after_retraction.status_code == 200
        values_after_retraction = _recalled_values(adapter.context_bundles[-1])
        assert all("espresso" not in value for value in values_after_retraction)
        assert all("pour-over coffee" not in value for value in values_after_retraction)

        coffee_assertion = next(
            item for item in _memory_assertions(client) if item["predicate"] == "preference.coffee"
        )
        assert coffee_assertion["lifecycle_state"] == "retracted"


def test_s5_pr01_replacing_active_assertion_supersedes_prior_assertions(
    postgres_url: str,
) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)

        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "remember preference notebook_style = matte black notebooks"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "correct preference notebook_style = dot grid notebooks"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "remember preference notebook_style = spiral notebooks"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "forget preference notebook_style"},
            ).status_code
            == 200
        )

        notebook_assertion = next(
            item
            for item in _memory_assertions(client)
            if item["predicate"] == "preference.notebook_style"
        )
        assert notebook_assertion["lifecycle_state"] == "retracted"

        with cast(Any, client.app).state.session_factory() as db:
            rows = db.scalars(
                select(MemoryAssertionRecord)
                .where(MemoryAssertionRecord.predicate == "preference.notebook_style")
                .order_by(MemoryAssertionRecord.created_at.asc(), MemoryAssertionRecord.id.asc())
            ).all()
            assert len(rows) == 3
            assert [row.lifecycle_state for row in rows] == [
                "superseded",
                "superseded",
                "retracted",
            ]
            assert [row.object_value["text"] for row in rows] == [
                "matte black notebooks",
                "dot grid notebooks",
                "spiral notebooks",
            ]
            assert rows[0].superseded_by_assertion_id == rows[1].id
            assert rows[1].superseded_by_assertion_id == rows[2].id
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProjectionRecord)
                    .where(
                        MemoryEmbeddingProjectionRecord.assertion_id.in_([row.id for row in rows])
                    )
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemorySalienceRecord)
                    .where(MemorySalienceRecord.assertion_id.in_([row.id for row in rows]))
                )
                == 0
            )


def test_s5_pr01_rotation_is_idempotent_by_key_and_auditable(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        seeded = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            json={
                "message": "remember commitment legal_review = review legal terms before signing"
            },
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
                select(func.count())
                .select_from(SessionRecord)
                .where(SessionRecord.is_active.is_(True))
            )
            assert active_count == 1
            rotation_count = db.scalar(
                select(func.count())
                .select_from(SessionRotationRecord)
                .where(SessionRotationRecord.idempotency_key == "rotate-audit-001")
            )
            assert rotation_count == 1


def test_s5_pr01_projection_is_derived_from_active_assertion(postgres_url: str) -> None:
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "remember fact timezone = pst"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "remember fact timezone = utc"},
            ).status_code
            == 200
        )

        assertions = [
            item for item in _memory_assertions(client) if item["predicate"] == "profile.timezone"
        ]
        assert len(assertions) == 1
        assert assertions[0]["value"] == "utc"
        assert assertions[0]["lifecycle_state"] == "active"

        with cast(Any, client.app).state.session_factory() as db:
            rows = db.scalars(
                select(MemoryAssertionRecord)
                .where(MemoryAssertionRecord.predicate == "profile.timezone")
                .order_by(MemoryAssertionRecord.created_at.asc(), MemoryAssertionRecord.id.asc())
            ).all()
            assert len(rows) == 2
            assert [row.lifecycle_state for row in rows] == ["superseded", "active"]
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProjectionRecord)
                    .where(MemoryEmbeddingProjectionRecord.assertion_id == rows[0].id)
                )
                == 0
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProjectionRecord)
                    .where(MemoryEmbeddingProjectionRecord.assertion_id == rows[1].id)
                )
                == 1
            )


def test_s5_pr01_recall_is_bounded_deterministic_and_emits_skip_reasons(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECALLED_ASSERTIONS", "2")
    adapter = MemoryProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "remember project apollo = apollo milestone in may"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={
                    "message": "remember project apollo_risk = apollo delivery risk is vendor latency"
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={
                    "message": "remember project apollo_archive = apollo archive doc is in drive"
                },
            ).status_code
            == 200
        )

        first_query = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the latest apollo project status?"},
        )
        assert first_query.status_code == 200
        first_recall_ids = _recalled_assertion_ids(adapter.context_bundles[-1])

        second_query = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "again, what is the latest apollo project status?"},
        )
        assert second_query.status_code == 200
        second_recall_ids = _recalled_assertion_ids(adapter.context_bundles[-1])

        assert first_recall_ids == second_recall_ids
        assert len(first_recall_ids) == 2

        recalled_event_payload = next(
            event["payload"]
            for event in _latest_turn(client, session_id)["events"]
            if event["event_type"] == "evt.memory.recalled"
        )
        assert recalled_event_payload["max_recalled_assertions"] == 2
        assert recalled_event_payload["included_assertion_count"] == 2
        assert recalled_event_payload["omitted_assertion_count"] >= 1
        assert any(
            item["reason"] == "top_k_bounded"
            for item in recalled_event_payload["omitted_assertions"]
        )
