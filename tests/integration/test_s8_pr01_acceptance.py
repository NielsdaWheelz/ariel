from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, ModelAdapterError, create_app
from tests.integration.responses_helpers import responses_message


@dataclass
class CaptureProbeAdapter:
    provider: str = "provider.s8-pr01"
    model: str = "model.s8-pr01-v1"
    seen_user_messages: list[str] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history, context_bundle
        self.seen_user_messages.append(user_message)
        return responses_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s8_pr01_123",
            input_tokens=21,
            output_tokens=13,
        )


@dataclass
class CaptureFailingAdapter:
    provider: str = "provider.s8-pr01"
    model: str = "model.s8-pr01-failing"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, user_message, history, context_bundle
        raise ModelAdapterError(
            safe_reason="forced non-retryable model failure for capture acceptance test",
            status_code=503,
            code="E_MODEL_PROVIDER_DOWN",
            message="model provider unavailable",
            retryable=False,
        )


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _turn_count(client: TestClient) -> int:
    with cast(Any, client.app).state.session_factory() as db:
        with db.begin():
            result = db.execute(text("SELECT COUNT(*) AS count FROM turns")).mappings().one()
            return int(result["count"])


def _capture_row(client: TestClient, capture_id: str) -> dict[str, Any]:
    with cast(Any, client.app).state.session_factory() as db:
        with db.begin():
            row = (
                db.execute(
                    text(
                        "SELECT id, capture_kind, terminal_state, effective_session_id, turn_id, "
                        "idempotency_key, request_hash, original_payload, normalized_turn_input, "
                        "ingest_error_code, status_code "
                        "FROM captures WHERE id = :capture_id"
                    ),
                    {"capture_id": capture_id},
                )
                .mappings()
                .first()
            )
            assert row is not None
            return dict(row)


def _capture_ids_for_idempotency_key(client: TestClient, idempotency_key: str) -> list[str]:
    with cast(Any, client.app).state.session_factory() as db:
        with db.begin():
            rows = (
                db.execute(
                    text(
                        "SELECT id FROM captures "
                        "WHERE idempotency_key = :idempotency_key "
                        "ORDER BY created_at ASC"
                    ),
                    {"idempotency_key": idempotency_key},
                )
                .mappings()
                .all()
            )
            return [str(row["id"]) for row in rows]


def test_s8_pr01_text_capture_creates_durable_capture_and_surfaces_as_normal_turn(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-text-001"},
            json={
                "kind": "text",
                "text": "kickoff notes: discuss timeline and dependencies",
                "note": "summarize the core blockers",
                "source": {"app": "ios.share", "title": "kickoff notes"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"ok", "capture", "session", "turn", "assistant"}
        assert body["ok"] is True

        capture = body["capture"]
        assert set(capture.keys()) == {
            "id",
            "kind",
            "terminal_state",
            "effective_session_id",
            "turn_id",
            "idempotency_key",
            "ingest_failure",
            "created_at",
            "updated_at",
        }
        assert capture["id"].startswith("cpt_")
        assert capture["kind"] == "text"
        assert capture["terminal_state"] == "turn_created"
        assert capture["turn_id"] == body["turn"]["id"]
        assert capture["effective_session_id"] == body["session"]["id"]
        assert capture["ingest_failure"] is None

        timeline = client.get(f"/v1/sessions/{body['session']['id']}/events")
        assert timeline.status_code == 200
        assert [turn["id"] for turn in timeline.json()["turns"]] == [body["turn"]["id"]]

        row = _capture_row(client, capture["id"])
        assert row["capture_kind"] == "text"
        assert row["terminal_state"] == "turn_created"
        assert row["turn_id"] == body["turn"]["id"]
        assert row["effective_session_id"] == body["session"]["id"]
        assert row["idempotency_key"] == "cap-idem-text-001"
        assert isinstance(row["request_hash"], str) and row["request_hash"]
        assert row["status_code"] == 200
        assert row["ingest_error_code"] is None
        assert isinstance(row["normalized_turn_input"], str) and row["normalized_turn_input"]
        assert "capture ingress" in row["normalized_turn_input"].lower()


def test_s8_pr01_capture_idempotency_replays_same_outcome_and_blocks_payload_conflicts(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-001"},
            json={"kind": "url", "url": "https://example.com/research/article"},
        )
        assert first.status_code == 200
        first_payload = first.json()

        replay = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-001"},
            json={"kind": "url", "url": "https://example.com/research/article"},
        )
        assert replay.status_code == 200
        replay_payload = replay.json()
        assert replay_payload["capture"]["id"] == first_payload["capture"]["id"]
        assert replay_payload["turn"]["id"] == first_payload["turn"]["id"]

        conflict = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-001"},
            json={"kind": "url", "url": "https://example.com/research/different"},
        )
        assert conflict.status_code == 409
        conflict_payload = conflict.json()
        assert conflict_payload["error"]["code"] == "E_IDEMPOTENCY_KEY_REUSED"

        assert _turn_count(client) == 1
        assert len(_capture_ids_for_idempotency_key(client, "cap-idem-001")) == 1


def test_s8_pr01_capture_idempotency_survives_auto_rotation_between_retries(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "1")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "999999")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "999999")

    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        initial_session_id = _session_id(client)
        seeded = client.post(
            f"/v1/sessions/{initial_session_id}/message",
            json={"message": "seed one existing turn"},
        )
        assert seeded.status_code == 200

        first_capture = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-rotate-001"},
            json={"kind": "text", "text": "capture after threshold turn"},
        )
        assert first_capture.status_code == 200
        first_payload = first_capture.json()
        rotated_session_id = first_payload["session"]["id"]
        assert rotated_session_id != initial_session_id

        replay_capture = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-idem-rotate-001"},
            json={"kind": "text", "text": "capture after threshold turn"},
        )
        assert replay_capture.status_code == 200
        replay_payload = replay_capture.json()
        assert replay_payload["capture"]["id"] == first_payload["capture"]["id"]
        assert replay_payload["turn"]["id"] == first_payload["turn"]["id"]
        assert replay_payload["session"]["id"] == rotated_session_id

        timeline = client.get(f"/v1/sessions/{rotated_session_id}/events")
        assert timeline.status_code == 200
        assert [turn["id"] for turn in timeline.json()["turns"]] == [first_payload["turn"]["id"]]


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_code"),
    [
        ({"kind": "audio", "text": "unsupported"}, 422, "E_CAPTURE_KIND_UNSUPPORTED"),
        ({"kind": "url", "url": "ftp://example.com/private"}, 422, "E_CAPTURE_URL_INVALID"),
        ({"kind": "text", "text": "x" * 12001}, 413, "E_CAPTURE_TEXT_TOO_LARGE"),
        (
            {"kind": "text", "text": "valid text", "source": {"unsupported": "field"}},
            422,
            "E_CAPTURE_SOURCE_INVALID",
        ),
    ],
)
def test_s8_pr01_invalid_or_oversize_captures_are_durable_ingest_failures(
    postgres_url: str,
    payload: dict[str, Any],
    expected_status: int,
    expected_code: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": f"cap-invalid-{expected_code}"},
            json=payload,
        )
        assert response.status_code == expected_status
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == expected_code
        assert set(body.keys()) == {"ok", "capture", "error"}

        capture = body["capture"]
        assert capture["id"].startswith("cpt_")
        assert capture["terminal_state"] == "ingest_failed"
        assert capture["turn_id"] is None
        assert capture["ingest_failure"]["code"] == expected_code
        assert capture["effective_session_id"] is None

        row = _capture_row(client, capture["id"])
        assert row["terminal_state"] == "ingest_failed"
        assert row["turn_id"] is None
        assert row["ingest_error_code"] == expected_code
        assert row["status_code"] == expected_status

        assert _turn_count(client) == 0


def test_s8_pr01_invalid_capture_payload_must_be_json_object_and_is_durable_failure(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-invalid-nonobject-001"},
            json=["not", "an", "object"],
        )
        assert response.status_code == 422
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_CAPTURE_PAYLOAD_INVALID"
        assert body["capture"]["kind"] == "unknown"
        assert body["capture"]["terminal_state"] == "ingest_failed"
        assert body["capture"]["turn_id"] is None

        row = _capture_row(client, body["capture"]["id"])
        assert row["capture_kind"] == "unknown"
        assert row["terminal_state"] == "ingest_failed"
        assert row["turn_id"] is None
        assert row["status_code"] == 422
        assert _turn_count(client) == 0


def test_s8_pr01_ingest_failure_idempotency_replays_same_capture_outcome(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        request_payload = {"kind": "audio", "text": "unsupported"}
        first = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-ingest-failure-idem-001"},
            json=request_payload,
        )
        assert first.status_code == 422
        first_payload = first.json()

        replay = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-ingest-failure-idem-001"},
            json=request_payload,
        )
        assert replay.status_code == 422
        replay_payload = replay.json()
        assert replay_payload["capture"]["id"] == first_payload["capture"]["id"]
        assert replay_payload["error"]["code"] == "E_CAPTURE_KIND_UNSUPPORTED"
        assert _turn_count(client) == 0
        assert len(_capture_ids_for_idempotency_key(client, "cap-ingest-failure-idem-001")) == 1


def test_s8_pr01_capture_without_idempotency_key_creates_distinct_captures(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post(
            "/v1/captures",
            json={"kind": "url", "url": "https://example.com/without-idempotency"},
        )
        second = client.post(
            "/v1/captures",
            json={"kind": "url", "url": "https://example.com/without-idempotency"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_payload = first.json()
        second_payload = second.json()
        assert first_payload["capture"]["id"] != second_payload["capture"]["id"]
        assert first_payload["turn"]["id"] != second_payload["turn"]["id"]
        assert _turn_count(client) == 2


def test_s8_pr01_turn_failure_after_ingest_is_durable_and_typed(
    postgres_url: str,
) -> None:
    adapter = CaptureFailingAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "cap-turn-failure-001"},
            json={"kind": "text", "text": "capture that triggers model failure"},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_PROVIDER_DOWN"

        capture = body["capture"]
        assert capture["terminal_state"] == "turn_created"
        assert isinstance(capture["turn_id"], str) and capture["turn_id"]
        assert isinstance(capture["effective_session_id"], str) and capture["effective_session_id"]
        assert capture["ingest_failure"] is None

        row = _capture_row(client, capture["id"])
        assert row["terminal_state"] == "turn_created"
        assert row["turn_id"] == capture["turn_id"]
        assert row["effective_session_id"] == capture["effective_session_id"]
        assert row["status_code"] == 503


def test_s8_pr01_bare_text_capture_is_observe_first_and_not_direct_memory_command(
    postgres_url: str,
) -> None:
    adapter = CaptureProbeAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures",
            json={"kind": "text", "text": "remember project phoenix = ship tomorrow"},
        )
        assert response.status_code == 200
        payload = response.json()

        memory_projection = client.get("/v1/memory")
        assert memory_projection.status_code == 200
        memory_payload = memory_projection.json()
        assert memory_payload["assertions"] == []
        assert memory_payload["candidates"] == []

        event_types = [event["event_type"] for event in payload["turn"]["events"]]
        assert "evt.memory.candidate_proposed" not in event_types
        assert "evt.memory.assertion_activated" not in event_types
        assert adapter.seen_user_messages
        assert not adapter.seen_user_messages[0].strip().lower().startswith("remember ")
