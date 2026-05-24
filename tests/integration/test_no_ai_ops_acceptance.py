from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import text

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from ariel.persistence import JobRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
)


class NoAiOpsAdapter(FakeModelAdapter):
    provider = "provider.no-ai-ops"
    model = "model.no-ai-ops-v1"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.calls += 1
        raise AssertionError("no-AI ops must not call the model adapter")


@dataclass(frozen=True)
class CaptureStorageRow:
    turn_id: str
    effective_session_id: str
    normalized_turn_input: str


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _turn_count(client: TestClient) -> int:
    with _session_factory(client)() as db:
        with db.begin():
            result = db.execute(text("SELECT COUNT(*) AS count FROM turns")).mappings().one()
            return int(result["count"])


def _capture_count(client: TestClient) -> int:
    with _session_factory(client)() as db:
        with db.begin():
            result = db.execute(text("SELECT COUNT(*) AS count FROM captures")).mappings().one()
            return int(result["count"])


def _capture_columns(client: TestClient) -> set[str]:
    with _session_factory(client)() as db:
        with db.begin():
            rows = db.execute(
                text(
                    "SELECT column_name "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'captures'"
                )
            ).all()
    return {str(row.column_name) for row in rows}


def _capture_storage_row(client: TestClient, capture_id: str) -> CaptureStorageRow:
    with _session_factory(client)() as db:
        with db.begin():
            row = (
                db.execute(
                    text(
                        "SELECT turn_id, effective_session_id, normalized_turn_input "
                        "FROM captures WHERE id = :capture_id"
                    ),
                    {"capture_id": capture_id},
                )
                .mappings()
                .one()
            )
    return CaptureStorageRow(
        turn_id=str(row["turn_id"]),
        effective_session_id=str(row["effective_session_id"]),
        normalized_turn_input=str(row["normalized_turn_input"]),
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
        assert capture["idempotency_key"] == "capture-record-001"
        assert isinstance(capture["effective_session_id"], str)
        assert isinstance(capture["turn_id"], str)
        assert "terminal_state" not in capture
        assert "ingest_failure" not in capture

        row = _capture_storage_row(client, capture["id"])
        assert row.turn_id == capture["turn_id"]
        assert row.effective_session_id == capture["effective_session_id"]
        assert "capture ingress" in row.normalized_turn_input
        capture_columns = _capture_columns(client)
        assert "original_payload" not in capture_columns
        assert "terminal_state" not in capture_columns
        assert "ingest_error_code" not in capture_columns

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


def test_capture_record_supports_shared_content_without_model(postgres_url: str) -> None:
    adapter = NoAiOpsAdapter()
    with _build_client(postgres_url, adapter) as client:
        response = client.post(
            "/v1/captures/record",
            json={
                "kind": "shared_content",
                "shared_content": {
                    "text": "link preview text",
                    "urls": ["https://example.com/brief"],
                },
                "note": "read later",
                "source": {"app": "mobile", "url": "https://example.com/share"},
            },
        )

        assert response.status_code == 200
        capture = response.json()["capture"]
        assert capture["kind"] == "shared_content"
        row = _capture_storage_row(client, capture["id"])
        assert "capture_kind: shared_content" in row.normalized_turn_input
        assert "shared_source_text:" in row.normalized_turn_input
        assert "https://example.com/brief" in row.normalized_turn_input
        assert _turn_count(client) == 1
        assert adapter.calls == 0


def test_capture_record_rejects_loose_payload_shapes_without_side_effects(
    postgres_url: str,
) -> None:
    adapter = NoAiOpsAdapter()
    invalid_payloads = [
        {"kind": "TEXT", "text": "save this"},
        {"kind": "text", "text": "save this", "url": ""},
        {"kind": "url", "url": "https://example.com", "text": ""},
        {
            "kind": "shared_content",
            "shared_content": {"text": "preview"},
            "text": "",
        },
        {"kind": "text", "text": "save this", "note": ""},
        {"kind": "text", "text": "save this", "source": {"app": ""}},
        {
            "kind": "shared_content",
            "shared_content": {
                "urls": ["https://example.com/a", "https://example.com/a"],
            },
        },
    ]
    with _build_client(postgres_url, adapter) as client:
        for payload in invalid_payloads:
            response = client.post("/v1/captures/record", json=payload)
            assert response.status_code == 422
            assert response.json()["ok"] is False

        assert _turn_count(client) == 0
        assert _capture_count(client) == 0
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
