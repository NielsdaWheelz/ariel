from __future__ import annotations

from fastapi.testclient import TestClient

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    drain_task,
    empty_recall_response,
    is_memory_subsystem_call,
    last_user_message,
    responses_run_message,
)


class _EchoAdapter(FakeModelAdapter):
    provider = "provider.idempotency-test"
    model = "model.idempotency-test-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        user_message = last_user_message(request.messages)
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_idempotency_test_123",
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def test_post_message_replay_returns_recorded_response_without_new_turn(
    postgres_url: str,
) -> None:
    adapter = _EchoAdapter()
    with _build_client(postgres_url, adapter) as client:
        body = {"message": "idempotent hello"}
        headers = {"Idempotency-Key": "msg-replay-K"}

        first = client.post("/v1/messages", json=body, headers=headers)
        assert first.status_code == 202
        drain_task(client, first.json()["task_id"])

        replay = client.post("/v1/messages", json=body, headers=headers)
        replay_again = client.post("/v1/messages", json=body, headers=headers)
        assert replay.status_code == replay_again.status_code
        assert replay.json() == replay_again.json()

        timeline = client.get("/v1/events").json()
        assert [turn["user_message"] for turn in timeline["turns"]] == ["idempotent hello"]


def test_post_message_reuse_with_different_body_returns_conflict(postgres_url: str) -> None:
    adapter = _EchoAdapter()
    with _build_client(postgres_url, adapter) as client:
        headers = {"Idempotency-Key": "msg-conflict-K"}

        first = client.post("/v1/messages", json={"message": "body A"}, headers=headers)
        assert first.status_code == 202
        drain_task(client, first.json()["task_id"])

        conflict = client.post("/v1/messages", json={"message": "body B"}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_IDEMPOTENCY_KEY_REUSED"
