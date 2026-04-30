from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.persistence import JobRecord


@dataclass
class NoAiOpsAdapter:
    provider: str = "provider.no-ai-ops"
    model: str = "model.no-ai-ops-v1"
    calls: int = 0

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
        self.calls += 1
        raise AssertionError("no-AI ops must not call the model adapter")


@dataclass(frozen=True)
class CaptureStorageRow:
    terminal_state: str
    turn_id: str | None
    effective_session_id: str | None
    status_code: int
    normalized_turn_input: str | None


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


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _turn_count(client: TestClient) -> int:
    with _session_factory(client)() as db:
        with db.begin():
            result = db.execute(text("SELECT COUNT(*) AS count FROM turns")).mappings().one()
            return int(result["count"])


def _capture_storage_row(client: TestClient, capture_id: str) -> CaptureStorageRow:
    with _session_factory(client)() as db:
        with db.begin():
            row = (
                db.execute(
                    text(
                        "SELECT terminal_state, turn_id, effective_session_id, status_code, "
                        "normalized_turn_input "
                        "FROM captures WHERE id = :capture_id"
                    ),
                    {"capture_id": capture_id},
                )
                .mappings()
                .one()
            )
    return CaptureStorageRow(
        terminal_state=str(row["terminal_state"]),
        turn_id=cast(str | None, row["turn_id"]),
        effective_session_id=cast(str | None, row["effective_session_id"]),
        status_code=int(row["status_code"]),
        normalized_turn_input=cast(str | None, row["normalized_turn_input"]),
    )


def test_jobs_endpoint_lists_recent_jobs_deterministically(postgres_url: str) -> None:
    adapter = NoAiOpsAdapter()
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    with _build_client(postgres_url, adapter) as client:
        with _session_factory(client)() as db:
            with db.begin():
                db.add_all(
                    [
                        JobRecord(
                            id="job_001",
                            source="agency.local",
                            external_job_id="external-001",
                            title="First tied job",
                            status="running",
                            summary="First tied summary.",
                            latest_payload={"rank": 1},
                            created_at=now - timedelta(minutes=10),
                            updated_at=now,
                        ),
                        JobRecord(
                            id="job_002",
                            source="agency.local",
                            external_job_id="external-002",
                            title="Second tied job",
                            status="queued",
                            summary="Second tied summary.",
                            latest_payload={"rank": 2},
                            created_at=now - timedelta(minutes=5),
                            updated_at=now,
                        ),
                        JobRecord(
                            id="job_000",
                            source="agency.local",
                            external_job_id="external-000",
                            title="Older job",
                            status="succeeded",
                            summary="Older summary.",
                            latest_payload={"rank": 0},
                            created_at=now - timedelta(hours=1),
                            updated_at=now - timedelta(minutes=1),
                        ),
                    ],
                )

        response = client.get("/v1/jobs", params={"limit": 2})
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert [job["id"] for job in payload["jobs"]] == ["job_002", "job_001"]
        assert payload["jobs"][0]["status"] == "queued"

        detail = client.get("/v1/jobs/job_001")
        assert detail.status_code == 200
        assert detail.json()["job"]["id"] == "job_001"
        assert adapter.calls == 0


def test_capture_record_creates_durable_capture_without_model(postgres_url: str) -> None:
    adapter = NoAiOpsAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post(
            "/v1/captures/record",
            headers={"Idempotency-Key": "capture-record-001"},
            json={
                "kind": "text",
                "text": "capture this deterministic note",
                "note": "store only",
                "source": {"app": "discord", "title": "slash capture"},
            },
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert set(first_payload.keys()) == {"ok", "capture"}
        assert first_payload["ok"] is True

        capture = first_payload["capture"]
        assert capture["id"].startswith("cpt_")
        assert capture["kind"] == "text"
        assert capture["terminal_state"] == "turn_created"
        assert capture["idempotency_key"] == "capture-record-001"
        assert capture["ingest_failure"] is None
        assert isinstance(capture["effective_session_id"], str)
        assert isinstance(capture["turn_id"], str)

        row = _capture_storage_row(client, capture["id"])
        assert row.terminal_state == "turn_created"
        assert row.turn_id == capture["turn_id"]
        assert row.effective_session_id == capture["effective_session_id"]
        assert row.status_code == 200
        assert row.normalized_turn_input is not None
        assert "capture ingress" in row.normalized_turn_input

        timeline = client.get(f"/v1/sessions/{capture['effective_session_id']}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["id"] == capture["turn_id"]
        assert turn["assistant_message"] is None
        assert [event["event_type"] for event in turn["events"]] == [
            "evt.turn.started",
            "evt.turn.completed",
        ]

        replay = client.post(
            "/v1/captures/record",
            headers={"Idempotency-Key": "capture-record-001"},
            json={
                "kind": "text",
                "text": "capture this deterministic note",
                "note": "store only",
                "source": {"app": "discord", "title": "slash capture"},
            },
        )
        assert replay.status_code == 200
        assert replay.json()["capture"]["id"] == capture["id"]
        assert _turn_count(client) == 1
        assert adapter.calls == 0


def test_capture_record_idempotency_blocks_payload_conflicts(postgres_url: str) -> None:
    adapter = NoAiOpsAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post(
            "/v1/captures/record",
            headers={"Idempotency-Key": "capture-record-conflict-001"},
            json={"kind": "url", "url": "https://example.com/first"},
        )
        assert first.status_code == 200

        conflict = client.post(
            "/v1/captures/record",
            headers={"Idempotency-Key": "capture-record-conflict-001"},
            json={"kind": "url", "url": "https://example.com/second"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_IDEMPOTENCY_KEY_REUSED"
        assert _turn_count(client) == 1
        assert adapter.calls == 0
