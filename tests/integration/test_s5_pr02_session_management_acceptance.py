from __future__ import annotations

import copy
import threading
import time
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, _session_turn_lock_id, create_app
from ariel.config import AppSettings
import ariel.memory as memory
from ariel.memory import MEMORY_PROJECTION_VERSION, process_memory_projection_job
from tests.integration.responses_helpers import responses_message, responses_with_function_calls
from ariel.persistence import AIJudgmentRecord, SessionRecord


_projection_id_counter = count(1)


def _fake_memory_embedding(text: str, *, settings: AppSettings) -> list[float]:
    vector = [0.0] * settings.memory_embedding_dimensions
    lowered = text.lower()
    if "invoice" in lowered or "open" in lowered:
        vector[0] = 1.0
    if not any(vector):
        vector[1] = 1.0
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
            "id": str(candidate["id"]),
            "kind": str(candidate.get("kind") or "semantic_assertion"),
            "rationale": "curator selected for this turn",
        }
        for candidate in candidates[:max_selected]
    ]
    selected_ids = {item["id"] for item in selected}
    omitted = [
        {
            "id": str(candidate["id"]),
            "kind": str(candidate.get("kind") or "semantic_assertion"),
            "reason": "curator omitted",
        }
        for candidate in candidates
        if str(candidate["id"]) not in selected_ids
    ]
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


def _fake_continuity_curation(
    *,
    rotation_reason: str,
    prior_session_id: str,
    new_session_id: str,
    source_turns: Sequence[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    del settings
    source_turn_ids = [turn["turn_id"] for turn in source_turns]
    omitted_turn_refs = (
        [
            {
                "turn_id": source_turn_ids[-1],
                "reason": "AI continuity omitted the least actionable prior turn",
            }
        ]
        if len(source_turn_ids) > 1
        else []
    )
    omitted_turn_ids = {item["turn_id"] for item in omitted_turn_refs}
    preserved_turn_refs = [
        {"turn_id": turn_id, "reason": "AI continuity preserved this turn"}
        for turn_id in source_turn_ids
        if turn_id not in omitted_turn_ids
    ]
    return {
        "summary": f"fixture continuity for {rotation_reason}",
        "preserved_turn_refs": preserved_turn_refs,
        "omitted_turn_refs": omitted_turn_refs,
        "user_commitments": [],
        "assistant_commitments": [],
        "decisions": [{"summary": "fixture continuity decision"}],
        "open_loops": [],
        "unresolved_uncertainty": [],
        "tool_action_outcomes": [],
        "important_omissions": omitted_turn_refs,
        "confidence": 0.86,
        "model": "fixture-continuity-curator",
        "prompt_version": memory.MEMORY_CONTINUITY_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_continuity_curator",
        "parse_status": "parsed",
        "validation_status": "valid",
        "prior_session_id": prior_session_id,
        "new_session_id": new_session_id,
        "source_turn_ids": source_turn_ids,
    }


def _use_fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _fake_memory_curation)
    monkeypatch.setattr(memory, "_curate_rotation_context_with_model", _fake_continuity_curation)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "summary": "missing refs",
            "omitted_turn_refs": [],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 0.8,
        },
        {
            "summary": "missing reason",
            "preserved_turn_refs": [{"turn_id": "trn_1"}],
            "omitted_turn_refs": [{"turn_id": "trn_2", "reason": "omitted"}],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 0.8,
        },
        {
            "summary": "unknown turn",
            "preserved_turn_refs": [{"turn_id": "trn_1", "reason": "kept"}],
            "omitted_turn_refs": [{"turn_id": "trn_missing", "reason": "omitted"}],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 0.8,
        },
        {
            "summary": "duplicate turn",
            "preserved_turn_refs": [{"turn_id": "trn_1", "reason": "kept"}],
            "omitted_turn_refs": [{"turn_id": "trn_1", "reason": "omitted"}],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 0.8,
        },
        {
            "summary": "bad confidence",
            "preserved_turn_refs": [{"turn_id": "trn_1", "reason": "kept"}],
            "omitted_turn_refs": [{"turn_id": "trn_2", "reason": "omitted"}],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 1.2,
        },
    ],
)
def test_s5_pr02_continuity_validation_rejects_partial_or_misaccounted_output(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(memory.AIJudgmentFailure):
        memory.validate_continuity_compaction_payload(
            payload,
            source_turn_ids=["trn_1", "trn_2"],
            model="fixture-continuity-curator",
            provider_response_id="resp_fixture_continuity",
        )


def _process_projection(client: TestClient) -> None:
    processed = process_memory_projection_job(
        session_factory=cast(Any, client.app).state.session_factory,
        settings=AppSettings(),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=lambda prefix: f"{prefix}_test_{next(_projection_id_counter)}",
    )
    assert processed is True


@dataclass
class SessionManagementProbeAdapter:
    provider: str = "provider.s5-pr02"
    model: str = "model.s5-pr02-v1"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)
    history_lengths_by_message: dict[str, int] = field(default_factory=dict)
    message_delays_seconds: dict[str, float] = field(default_factory=dict)
    proposals_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools
        with self._lock:
            self.history_lengths_by_message[user_message] = len(history)
            self.context_bundles.append(copy.deepcopy(context_bundle))

        delay_seconds = float(self.message_delays_seconds.get(user_message, 0.0))
        if delay_seconds > 0:
            time.sleep(delay_seconds)

        proposals = self.proposals_by_message.get(user_message)
        if isinstance(proposals, list):
            return responses_with_function_calls(
                input_items=input_items,
                assistant_text=f"assistant::{user_message}",
                proposals=copy.deepcopy(proposals),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_s5_pr02_123",
                input_tokens=17,
                output_tokens=12,
            )
        return responses_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s5_pr02_123",
            input_tokens=17,
            output_tokens=12,
        )


@dataclass
class NoOpOverBudgetCompactionAdapter:
    calls: list[dict[str, int]] = field(default_factory=list)

    def compact(
        self,
        *,
        context_bundle: dict[str, Any],
        user_message: str,
        estimated_context_tokens: int,
        max_context_tokens: int,
    ) -> dict[str, Any] | None:
        del context_bundle, user_message
        self.calls.append(
            {
                "estimated_context_tokens": estimated_context_tokens,
                "max_context_tokens": max_context_tokens,
            }
        )
        return None


@dataclass
class LastNDeterministicCompactionAdapter:
    calls: list[dict[str, int]] = field(default_factory=list)

    def compact(
        self,
        *,
        context_bundle: dict[str, Any],
        user_message: str,
        estimated_context_tokens: int,
        max_context_tokens: int,
    ) -> dict[str, Any] | None:
        del user_message
        self.calls.append(
            {
                "estimated_context_tokens": estimated_context_tokens,
                "max_context_tokens": max_context_tokens,
            }
        )
        recent_turns = context_bundle.get("recent_active_session_turns")
        recent_turn = recent_turns[-1] if isinstance(recent_turns, list) and recent_turns else {}
        turn_id = recent_turn.get("turn_id") if isinstance(recent_turn, dict) else None
        compacted = copy.deepcopy(context_bundle)
        compacted["recent_active_session_turns"] = (
            [
                {
                    "turn_id": turn_id,
                    "user_message": "deterministic last-N summary",
                    "assistant_message": "",
                    "status": recent_turn.get("status", "completed")
                    if isinstance(recent_turn, dict)
                    else "completed",
                }
            ]
            if isinstance(turn_id, str)
            else []
        )
        compacted["recent_window"] = {
            "max_recent_turns": 1,
            "included_turn_count": len(compacted["recent_active_session_turns"]),
            "omitted_turn_count": max(0, len(recent_turns) - 1)
            if isinstance(recent_turns, list)
            else 0,
            "included_turn_ids": [turn_id] if isinstance(turn_id, str) else [],
            "compacted_by": "deterministic_last_n_summary",
        }
        return compacted


@dataclass
class ValidAICompactionAdapter:
    model: str = "fixture-context-compactor"
    provider_response_id: str = "resp_fixture_context_compactor"
    calls: list[dict[str, int]] = field(default_factory=list)

    def compact(
        self,
        *,
        context_bundle: dict[str, Any],
        user_message: str,
        estimated_context_tokens: int,
        max_context_tokens: int,
    ) -> dict[str, Any] | None:
        del user_message
        self.calls.append(
            {
                "estimated_context_tokens": estimated_context_tokens,
                "max_context_tokens": max_context_tokens,
            }
        )
        compacted = copy.deepcopy(context_bundle)
        source_turn_ids = [
            turn["turn_id"]
            for turn in context_bundle.get("recent_active_session_turns", [])
            if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)
        ]
        compacted["recent_active_session_turns"] = []
        compacted["continuity_compaction"] = {
            "summary": "fixture AI context compaction",
            "provider_response_id": self.provider_response_id,
            "source_turn_ids": source_turn_ids,
            "preserved_turn_refs": [],
            "omitted_turn_refs": [
                {"turn_id": turn_id, "reason": "AI omitted this turn for context budget"}
                for turn_id in source_turn_ids
            ],
            "user_commitments": [],
            "assistant_commitments": [],
            "decisions": [],
            "open_loops": [],
            "tool_action_outcomes": [],
            "unresolved_uncertainty": [],
            "important_omissions": [],
            "confidence": 0.91,
        }
        compacted["recent_window"] = {
            "max_recent_turns": 0,
            "included_turn_count": 0,
            "omitted_turn_count": len(source_turn_ids),
            "included_turn_ids": [],
            "omitted_turns": compacted["continuity_compaction"]["omitted_turn_refs"],
            "compacted_by": "ai_context_compaction",
        }
        return compacted


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(
    postgres_url: str,
    adapter: ModelAdapter,
    *,
    reset_database: bool,
    context_compaction_adapter: Any | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        context_compaction_adapter=context_compaction_adapter,
        reset_database=reset_database,
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _timeline(client: TestClient, session_id: str, *, after: str | None = None) -> dict[str, Any]:
    params = {"after": after} if after is not None else None
    timeline = client.get(f"/v1/sessions/{session_id}/events", params=params)
    assert timeline.status_code == 200
    return timeline.json()


def _project_state(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/v1/memory/project-state")
    assert response.status_code == 200
    return response.json()["project_state"]


def test_s5_pr02_message_idempotency_key_replays_same_turn_and_blocks_conflicting_payload(
    postgres_url: str,
) -> None:
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = planning kickoff on monday"},
        )
        assert first.status_code == 200
        first_turn_id = first.json()["turn"]["id"]

        replay = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = planning kickoff on monday"},
        )
        assert replay.status_code == 200
        assert replay.json()["turn"]["id"] == first_turn_id

        timeline = _timeline(client, session_id)
        assert len(timeline["turns"]) == 1

        conflict = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = kickoff on tuesday instead"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_IDEMPOTENCY_KEY_REUSED"


def test_s5_pr02_idempotency_replay_survives_auto_rotation_when_retrying_new_session_id(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        assert (
            client.post(
                f"/v1/sessions/{initial_session_id}/message",
                json={"message": "seed prior turn"},
            ).status_code
            == 200
        )

        triggering = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            headers={"Idempotency-Key": "msg-idem-rotate-001"},
            json={"message": "second turn triggers threshold rotation"},
        )
        assert triggering.status_code == 200
        triggering_turn_id = triggering.json()["turn"]["id"]
        rotated_session_id = triggering.json()["session"]["id"]
        assert rotated_session_id != initial_session_id

        replay_on_old_session = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            headers={"Idempotency-Key": "msg-idem-rotate-001"},
            json={"message": "second turn triggers threshold rotation"},
        )
        assert replay_on_old_session.status_code == 200
        assert replay_on_old_session.json()["turn"]["id"] == triggering_turn_id
        assert replay_on_old_session.json()["session"]["id"] == rotated_session_id

        replay_on_new_session = client.post(
            f"/v1/sessions/{rotated_session_id}/message",
            headers={"Idempotency-Key": "msg-idem-rotate-001"},
            json={"message": "second turn triggers threshold rotation"},
        )
        assert replay_on_new_session.status_code == 200
        assert replay_on_new_session.json()["turn"]["id"] == triggering_turn_id
        assert replay_on_new_session.json()["session"]["id"] == rotated_session_id

        timeline_rotated = _timeline(client, rotated_session_id)
        assert len(timeline_rotated["turns"]) == 1


def test_s5_pr02_rotate_rejects_overlong_idempotency_key_with_typed_validation_error(
    postgres_url: str,
) -> None:
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        response = client.post("/v1/sessions/rotate", headers={"Idempotency-Key": "k" * 129})
        assert response.status_code == 422
        payload = response.json()["error"]
        assert payload["code"] == "E_IDEMPOTENCY_KEY_INVALID"
        assert payload["message"] == "idempotency key is invalid"
        assert payload["retryable"] is False
        assert payload["details"]["max_length"] == 128


def test_s5_pr02_rotation_follows_turn_count_threshold_with_typed_reason(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()

    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        assert (
            client.post(
                f"/v1/sessions/{initial_session_id}/message",
                json={"message": "first message in session"},
            ).status_code
            == 200
        )

        second = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            json={"message": "second message should trigger auto rotate"},
        )
        assert second.status_code == 200
        rotated_session_id = second.json()["session"]["id"]
        assert rotated_session_id != initial_session_id

        rotations = client.get("/v1/sessions/rotations", params={"limit": 20})
        assert rotations.status_code == 200
        rotation_row = next(
            row
            for row in rotations.json()["rotations"]
            if row["rotated_from_session_id"] == initial_session_id
            and row["rotated_to_session_id"] == rotated_session_id
        )
        assert rotation_row["reason"] == "threshold_turn_count"
        trigger_snapshot = rotation_row["trigger_snapshot"]
        assert trigger_snapshot["prior_turn_count"] >= 1
        assert trigger_snapshot["thresholds"]["max_turns"] == 1

        with cast(Any, client.app).state.session_factory() as db:
            closed_prior = db.scalar(
                select(SessionRecord).where(SessionRecord.id == initial_session_id).limit(1)
            )
            assert closed_prior is not None
            assert closed_prior.lifecycle_state == "closed"
            assert closed_prior.is_active is False

            active_new = db.scalar(
                select(SessionRecord).where(SessionRecord.id == rotated_session_id).limit(1)
            )
            assert active_new is not None
            assert active_new.lifecycle_state == "active"
            assert active_new.is_active is True


def test_s5_pr02_empty_session_rotation_writes_no_candidates_ai_judgment(
    postgres_url: str,
) -> None:
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        response = client.post("/v1/sessions/rotate")

        assert response.status_code == 200, response.text
        payload = response.json()
        rotation_id = payload["rotation"]["rotation_id"]
        rotated_session_id = payload["session"]["id"]
        assert rotated_session_id != session_id

        with cast(Any, client.app).state.session_factory() as db:
            judgment = db.scalar(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "continuity_compaction",
                    AIJudgmentRecord.source_type == "session_rotation",
                    AIJudgmentRecord.source_id == rotation_id,
                )
                .limit(1)
            )
            assert judgment is not None
            assert judgment.status == "succeeded"
            assert judgment.provider_response_id is None
            assert judgment.parse_status == "not_required_no_candidates"
            assert judgment.validation_status == "not_validated"
            assert judgment.selected == []
            assert judgment.omitted == []

        continuity_row = next(
            row
            for row in _project_state(client)
            if row["project_key"] == "session_continuity"
            and row["state"].get("rotation_id") == rotation_id
        )
        assert continuity_row["state"]["ai_judgment_id"] == judgment.id
        assert continuity_row["state"]["source_turn_ids"] == []
        assert continuity_row["state"]["parse_status"] == "not_required_no_candidates"
        assert continuity_row["state"]["validation_status"] == "not_validated"


def test_s5_pr02_rotation_continuity_failure_is_typed_audited_and_stops_rotation(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_continuity_curation(
        *,
        rotation_reason: str,
        prior_session_id: str,
        new_session_id: str,
        source_turns: Sequence[dict[str, Any]],
        settings: AppSettings,
    ) -> dict[str, Any]:
        del rotation_reason, prior_session_id, new_session_id, source_turns, settings
        raise memory.AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="fixture continuity curation invalid",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id="resp_fixture_continuity_invalid",
        )

    monkeypatch.setattr(memory, "_curate_rotation_context_with_model", failing_continuity_curation)
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()

    with _build_client(
        postgres_url,
        adapter,
        reset_database=True,
        raise_server_exceptions=False,
    ) as client:
        session_id = _session_id(client)
        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "seed turn before rotation failure"},
        )
        assert first.status_code == 200

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger failed rotation continuity"},
        )

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "E_AI_JUDGMENT_SCHEMA"
        assert error["details"]["judgment_type"] == "continuity_compaction"
        assert _session_id(client) == session_id
        rotations = client.get("/v1/sessions/rotations", params={"limit": 20})
        assert rotations.status_code == 200
        assert rotations.json()["rotations"] == []

        with cast(Any, client.app).state.session_factory() as db:
            judgment = db.scalar(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "continuity_compaction",
                    AIJudgmentRecord.source_type == "session_rotation",
                    AIJudgmentRecord.status == "failed",
                )
                .limit(1)
            )
            assert judgment is not None
            assert judgment.provider_response_id == "resp_fixture_continuity_invalid"
            assert judgment.failure_code == "E_AI_JUDGMENT_SCHEMA"
            assert judgment.parse_status == "schema_invalid"
            assert judgment.validation_status == "invalid"
            assert judgment.input_refs["prior_session_id"] == session_id


def test_s5_pr02_context_pressure_rotation_persists_ai_continuity_with_sources_and_omissions(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()

    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        for message in (
            "first continuity source turn with a user commitment",
            "second continuity source turn with less useful detail",
        ):
            response = client.post(
                f"/v1/sessions/{initial_session_id}/message",
                json={"message": message},
            )
            assert response.status_code == 200

        source_turn_ids = [turn["id"] for turn in _timeline(client, initial_session_id)["turns"]]
        assert len(source_turn_ids) == 2

        cast(Any, client.app).state.auto_rotate_context_pressure_tokens = 1
        triggering = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            json={"message": "trigger context pressure continuity curation"},
        )
        assert triggering.status_code == 200
        rotated_session_id = triggering.json()["session"]["id"]
        assert rotated_session_id != initial_session_id

        rotations = client.get("/v1/sessions/rotations", params={"limit": 20})
        assert rotations.status_code == 200
        rotation_row = next(
            row
            for row in rotations.json()["rotations"]
            if row["rotated_from_session_id"] == initial_session_id
            and row["rotated_to_session_id"] == rotated_session_id
        )
        assert rotation_row["reason"] == "threshold_context_pressure"
        assert rotation_row["trigger_snapshot"]["prior_turn_count"] == 2

        continuity_row = next(
            row
            for row in _project_state(client)
            if row["project_key"] == "session_continuity"
            and row["state"].get("prior_session_id") == initial_session_id
            and row["state"].get("new_session_id") == rotated_session_id
        )
        continuity_state = continuity_row["state"]
        expected_omissions = [
            {
                "turn_id": source_turn_ids[-1],
                "reason": "AI continuity omitted the least actionable prior turn",
            }
        ]
        assert continuity_state["source_turn_ids"] == source_turn_ids
        assert continuity_state["preserved_turn_refs"] == [
            {
                "turn_id": source_turn_ids[0],
                "reason": "AI continuity preserved this turn",
            }
        ]
        assert continuity_state["omitted_turn_refs"] == expected_omissions
        assert continuity_state["model"] == "fixture-continuity-curator"
        assert continuity_state["provider_response_id"] == "resp_fixture_continuity_curator"
        assert continuity_state["prompt_version"] == memory.MEMORY_CONTINUITY_PROMPT_VERSION
        assert continuity_state["parse_status"] == "parsed"
        assert continuity_state["validation_status"] == "valid"
        assert continuity_state["confidence"] == 0.86

        with cast(Any, client.app).state.session_factory() as db:
            rotation_judgment = db.scalar(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "continuity_compaction",
                    AIJudgmentRecord.source_type == "session_rotation",
                    AIJudgmentRecord.source_id == rotation_row["rotation_id"],
                )
                .limit(1)
            )
            assert rotation_judgment is not None
            assert rotation_judgment.status == "succeeded"
            assert rotation_judgment.model == "fixture-continuity-curator"
            assert rotation_judgment.provider_response_id == "resp_fixture_continuity_curator"
            assert rotation_judgment.prompt_version == memory.MEMORY_CONTINUITY_PROMPT_VERSION
            assert rotation_judgment.parse_status == "parsed"
            assert rotation_judgment.validation_status == "valid"
            assert rotation_judgment.input_refs["source_turn_ids"] == source_turn_ids
            assert rotation_judgment.output["continuity_compaction"]["source_turn_ids"] == (
                source_turn_ids
            )
            assert (
                rotation_judgment.output["continuity_compaction"]["provider_response_id"]
                == "resp_fixture_continuity_curator"
            )

        continuity_context = next(
            item
            for item in adapter.context_bundles[-1]["memory_context"]["project_state"]
            if item["project_key"] == "session_continuity"
            and item["state"].get("prior_session_id") == initial_session_id
        )
        assert continuity_context["state"]["source_turn_ids"] == source_turn_ids
        assert continuity_context["state"]["omitted_turn_refs"] == expected_omissions


def test_s5_pr02_context_pressure_compaction_preserves_provider_response_id(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()
    compaction_adapter = ValidAICompactionAdapter()

    with _build_client(
        postgres_url,
        adapter,
        reset_database=True,
        context_compaction_adapter=compaction_adapter,
    ) as client:
        session_id = _session_id(client)
        seed = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "seed context " + ("detail " * 300)},
        )
        assert seed.status_code == 200

        cast(Any, client.app).state.max_context_tokens = 500
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "compress context"},
        )

        assert response.status_code == 200, response.text
        assert compaction_adapter.calls
        turn_id = response.json()["turn"]["id"]
        with cast(Any, client.app).state.session_factory() as db:
            judgment = db.scalar(
                select(AIJudgmentRecord)
                .where(
                    AIJudgmentRecord.judgment_type == "continuity_compaction",
                    AIJudgmentRecord.source_type == "turn",
                    AIJudgmentRecord.source_id == turn_id,
                )
                .limit(1)
            )
            assert judgment is not None
            assert judgment.status == "succeeded"
            assert judgment.model == "fixture-context-compactor"
            assert judgment.provider_response_id == "resp_fixture_context_compactor"
            assert judgment.output["continuity_compaction"]["provider_response_id"] == (
                "resp_fixture_context_compactor"
            )


def test_s5_pr02_over_budget_compaction_noop_is_ai_judgment_failure_not_budget_summary(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()
    compaction_adapter = NoOpOverBudgetCompactionAdapter()

    with _build_client(
        postgres_url,
        adapter,
        reset_database=True,
        context_compaction_adapter=compaction_adapter,
        raise_server_exceptions=False,
    ) as client:
        session_id = _session_id(client)
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "force context pressure without continuity output"},
        )

        assert compaction_adapter.calls
        assert compaction_adapter.calls[-1]["estimated_context_tokens"] > 1
        assert response.headers["content-type"].startswith("application/json")
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"].startswith("E_AI_JUDGMENT_")
        assert error["details"]["judgment_type"] == "continuity_compaction"
        assert error["details"]["prompt_version"] == memory.MEMORY_CONTINUITY_PROMPT_VERSION
        assert isinstance(error["details"]["turn_id"], str)
        assert adapter.context_bundles == []

        latest_turn = _timeline(client, session_id)["turns"][-1]
        assert latest_turn["id"] == error["details"]["turn_id"]
        assert not any(
            event["event_type"] == "evt.turn.limit_reached"
            and event["payload"].get("limit", {}).get("budget") == "context_tokens"
            for event in latest_turn["events"]
        )
        assert any(
            event["payload"].get("judgment_type") == "continuity_compaction"
            and str(event["payload"].get("failure_code", "")).startswith("E_AI_JUDGMENT_")
            for event in latest_turn["events"]
        )


def test_s5_pr02_last_n_compaction_output_is_ai_judgment_failure_not_last_n_summary(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")
    adapter = SessionManagementProbeAdapter()
    compaction_adapter = LastNDeterministicCompactionAdapter()

    with _build_client(
        postgres_url,
        adapter,
        reset_database=True,
        context_compaction_adapter=compaction_adapter,
        raise_server_exceptions=False,
    ) as client:
        session_id = _session_id(client)
        seed = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "seed context " + ("detail " * 2500)},
        )
        assert seed.status_code == 200
        prior_model_call_count = len(adapter.context_bundles)

        cast(Any, client.app).state.max_context_tokens = 500
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "force deterministic last-N compaction output"},
        )

        assert compaction_adapter.calls
        assert compaction_adapter.calls[-1]["estimated_context_tokens"] > 500
        assert response.headers["content-type"].startswith("application/json")
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"].startswith("E_AI_JUDGMENT_")
        assert error["details"]["judgment_type"] == "continuity_compaction"
        assert error["details"]["prompt_version"] == memory.MEMORY_CONTINUITY_PROMPT_VERSION
        assert len(adapter.context_bundles) == prior_model_call_count

        latest_turn = _timeline(client, session_id)["turns"][-1]
        assert latest_turn["id"] == error["details"]["turn_id"]
        assert any(
            event["payload"].get("judgment_type") == "continuity_compaction"
            and str(event["payload"].get("failure_code", "")).startswith("E_AI_JUDGMENT_")
            for event in latest_turn["events"]
        )


def test_s5_pr02_context_bundle_follows_constitution_section_order_and_includes_required_sections(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        candidate = client.post(
            "/v1/memory/candidates",
            json={
                "subject_key": "user:default",
                "predicate": "commitment.invoice",
                "assertion_type": "commitment",
                "value": "send invoice before friday",
                "evidence_text": "The user committed to send invoice before Friday.",
                "confidence": 1.0,
            },
        )
        assert candidate.status_code == 200
        candidate_id = candidate.json()["candidates"][0]["id"]
        assert client.post(f"/v1/memory/candidates/{candidate_id}/approve").status_code == 200
        _process_projection(client)
        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is still open?"},
        )
        assert second.status_code == 200

        bundle = adapter.context_bundles[-1]
        assert bundle["section_order"] == [
            "policy_system_instructions",
            "recent_active_session_turns",
            "memory_context",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_observations",
        ]

        memory_context = bundle["memory_context"]
        assert isinstance(memory_context, dict)
        assert memory_context["projection_version"] == MEMORY_PROJECTION_VERSION
        assert (
            memory_context["projection_health"]["projection_version"] == MEMORY_PROJECTION_VERSION
        )
        assert isinstance(memory_context["commitments_and_decisions"], list)
        assert memory_context["commitments_and_decisions"]

        commitments_jobs = bundle["open_commitments_and_jobs"]
        assert isinstance(commitments_jobs, dict)
        assert isinstance(commitments_jobs["open_jobs"], list)

        observations = bundle["relevant_artifacts_and_observations"]
        assert isinstance(observations, dict)
        assert isinstance(observations["artifacts"], list)
        assert isinstance(observations["proactive_observations"], list)


def test_s5_pr02_timeline_supports_after_cursor_for_incremental_sync(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "first timeline turn"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": "second timeline turn"},
            ).status_code
            == 200
        )

        full = _timeline(client, session_id)
        assert len(full["turns"]) == 2
        first_turn = full["turns"][0]
        cursor_event_id = first_turn["events"][-1]["id"]
        first_turn_event_ids = {event["id"] for event in first_turn["events"]}

        delta = _timeline(client, session_id, after=cursor_event_id)
        assert len(delta["turns"]) == 1
        assert delta["turns"][0]["id"] == full["turns"][1]["id"]
        assert all(
            event["id"] not in first_turn_event_ids
            for turn in delta["turns"]
            for event in turn["events"]
        )

        missing = client.get(
            f"/v1/sessions/{session_id}/events",
            params={"after": "evn_missing_cursor"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "E_EVENT_CURSOR_NOT_FOUND"


def test_s5_pr02_timeline_after_cursor_omits_turns_with_action_attempts_and_no_new_events(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_embeddings(monkeypatch)
    first_message = "propose an unavailable capability"
    adapter = SessionManagementProbeAdapter(
        proposals_by_message={
            first_message: [
                {"capability_id": "cap.unavailable.demo", "input": {"subject": "cursor regression"}}
            ]
        }
    )
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": first_message},
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "plain follow-up turn"},
        )
        assert second.status_code == 200

        full = _timeline(client, session_id)
        assert len(full["turns"]) == 2
        first_turn = full["turns"][0]
        assert first_turn["surface_action_lifecycle"]
        cursor_event_id = first_turn["events"][-1]["id"]

        delta = _timeline(client, session_id, after=cursor_event_id)
        assert len(delta["turns"]) == 1
        assert delta["turns"][0]["id"] == full["turns"][1]["id"]
        assert all(turn["id"] != first_turn["id"] for turn in delta["turns"])
        assert all(turn["events"] for turn in delta["turns"])


def test_s5_pr02_session_turn_lock_blocks_parallel_writes_to_same_session(
    postgres_url: str,
) -> None:
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        lock_id = _session_turn_lock_id(session_id)

        with cast(Any, client.app).state.session_factory() as db_one:
            with db_one.begin():
                db_one.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})

                with cast(Any, client.app).state.session_factory() as db_two:
                    with db_two.begin():
                        acquired = db_two.scalar(
                            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                            {"lock_id": lock_id},
                        )
                        assert acquired is False
