"""S5 PR02 session-management acceptance tests.

Pure session management: message idempotency, auto-rotation thresholds, the
event-timeline cursor, the turn lock, and the context-bundle constitution.

The agent-loop cutover (P1) made ``post_message`` async: it enqueues a
``user_message`` background task and returns ``202 {"status": "accepted",
"task_id": ...}``. Tests drain the enqueued task via ``post_message_and_drain``
or ``drain_task`` before asserting outcomes. Idempotency lives in
``enqueue_background_task``'s key dedup: a duplicate key returns the existing
task (same ``task_id``), regardless of payload, while the task is still
pending; there is no 409 conflict.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from ariel.app import ModelAdapter, create_app
from ariel.persistence import SessionRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    drain_task,
    empty_recall_response,
    is_retriever_call,
    post_message_and_drain,
    responses_run_message,
    responses_with_run_calls,
)


@dataclass
class SessionManagementProbeAdapter:
    provider: str = "provider.s5-pr02"
    model: str = "model.s5-pr02-v1"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)
    history_lengths_by_message: dict[str, int] = field(default_factory=dict)
    message_delays_seconds: dict[str, float] = field(default_factory=dict)
    run_calls_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
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
        if is_retriever_call(input_items):
            return empty_recall_response(provider=self.provider, model=self.model)
        assert [tool.get("name") for tool in tools] == ["run"]
        with self._lock:
            self.context_bundles.append(copy.deepcopy(context_bundle))
            self.history_lengths_by_message[user_message] = len(history)
        run_calls = self.run_calls_by_message.get(user_message)
        if isinstance(run_calls, list):
            if any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            ):
                run_calls = [
                    {"name": "agent.emit_message", "input": {"text": f"assistant::{user_message}"}}
                ]
            if not run_calls:
                run_calls = [
                    {"name": "agent.emit_message", "input": {"text": f"assistant::{user_message}"}}
                ]
            return responses_with_run_calls(
                assistant_text=f"assistant::{user_message}",
                calls=copy.deepcopy(run_calls),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_s5_pr02_123",
                input_tokens=17,
                output_tokens=12,
            )
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s5_pr02_123",
            input_tokens=17,
            output_tokens=12,
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
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _timeline(client: TestClient, session_id: str, *, after: str | None = None) -> dict[str, Any]:
    params = {"after": after} if after is not None else None
    timeline = client.get(f"/v1/sessions/{session_id}/events", params=params)
    assert timeline.status_code == 200
    return timeline.json()


def test_s5_pr02_message_idempotency_key_replays_same_task_id(
    postgres_url: str,
) -> None:
    """A duplicate Idempotency-Key with the same payload returns 202 with the
    same task_id while the task is still pending. A duplicate key with a
    different payload also returns 202 with the same task_id — enqueue deduplicates
    by key regardless of payload. After the task is drained (deleted), only one turn
    is recorded."""
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)

        first = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = planning kickoff on monday"},
        )
        assert first.status_code == 202
        first_task_id = first.json()["task_id"]

        # Replay while task is still pending — same task_id returned.
        replay = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = planning kickoff on monday"},
        )
        assert replay.status_code == 202
        assert replay.json()["task_id"] == first_task_id

        # Different payload, same key — still deduplicates to the same task.
        conflict = client.post(
            f"/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": "msg-idem-001"},
            json={"message": "remember project phoenix = kickoff on tuesday instead"},
        )
        assert conflict.status_code == 202
        assert conflict.json()["task_id"] == first_task_id

        # Drain the original task so the turn is committed.
        drain_task(client, first_task_id)

        timeline = _timeline(client, session_id)
        assert len(timeline["turns"]) == 1


def test_s5_pr02_idempotency_replay_survives_auto_rotation_when_retrying_new_session_id(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        post_message_and_drain(client, initial_session_id, message="seed prior turn")

        # This message triggers rotation; turn is recorded in the new session.
        triggering_resp = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            headers={"Idempotency-Key": "msg-idem-rotate-001"},
            json={"message": "second turn triggers threshold rotation"},
        )
        assert triggering_resp.status_code == 202
        triggering_task_id = triggering_resp.json()["task_id"]

        # Replay on the same session while the task is still pending — same task_id.
        replay_before_drain = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            headers={"Idempotency-Key": "msg-idem-rotate-001"},
            json={"message": "second turn triggers threshold rotation"},
        )
        assert replay_before_drain.status_code == 202
        assert replay_before_drain.json()["task_id"] == triggering_task_id

        # Drain the task; rotation happens inside _wake.
        drain_task(client, triggering_task_id)

        # After rotation the new active session holds the triggering turn.
        rotated_session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        assert rotated_session_id != initial_session_id

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
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    adapter = SessionManagementProbeAdapter()

    with _build_client(postgres_url, adapter, reset_database=True) as client:
        initial_session_id = _session_id(client)
        post_message_and_drain(client, initial_session_id, message="first message in session")

        # Second message triggers rotation; turn.session_id is the new session.
        second_turn = post_message_and_drain(
            client, initial_session_id, message="second message should trigger auto rotate"
        )
        rotated_session_id = second_turn.session_id
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


def test_s5_pr02_timeline_supports_after_cursor_for_incremental_sync(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SessionManagementProbeAdapter()
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="first timeline turn")
        post_message_and_drain(client, session_id, message="second timeline turn")

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
    first_message = "search memory for cursor regression"
    adapter = SessionManagementProbeAdapter(
        run_calls_by_message={
            first_message: [
                {
                    "name": "memory.recall",
                    "input": {"query": "cursor regression", "limit": 1},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter, reset_database=True) as client:
        session_id = _session_id(client)
        first_turn = post_message_and_drain(client, session_id, message=first_message)
        post_message_and_drain(client, session_id, message="plain follow-up turn")

        full = _timeline(client, session_id)
        assert len(full["turns"]) == 2
        first_turn_data = full["turns"][0]
        assert first_turn_data["id"] == first_turn.id
        assert first_turn_data["surface_action_lifecycle"]
        cursor_event_id = first_turn_data["events"][-1]["id"]

        delta = _timeline(client, session_id, after=cursor_event_id)
        assert len(delta["turns"]) == 1
        assert delta["turns"][0]["id"] == full["turns"][1]["id"]
        assert all(turn["id"] != first_turn.id for turn in delta["turns"])
        assert all(turn["events"] for turn in delta["turns"])
