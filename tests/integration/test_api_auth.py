from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import time
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from ariel.model_adapter import ModelCall, ModelResponse
from ariel.persistence import ProviderWatchChannelRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
)

LOCAL_AUTH_TOKEN = "test_local_auth_token_0123456789abcdef"


class NoModelAdapter(FakeModelAdapter):
    provider = "provider.api-auth-test"
    model = "model.api-auth-test"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        raise AssertionError("API auth tests must not call the model")


def test_local_auth_guards_authority_routes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", LOCAL_AUTH_TOKEN)

    app = create_test_app(
        database_url=postgres_url,
        model_adapter=NoModelAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200

        unauthenticated = client.get("/v1/memory/log")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "E_LOCAL_AUTH_TOKEN_INVALID"

        rejected = client.get("/v1/memory/log", headers={"Authorization": "Bearer wrong"})
        assert rejected.status_code == 401

        accepted = client.get(
            "/v1/memory/log",
            headers={"Authorization": f"Bearer {LOCAL_AUTH_TOKEN}"},
        )
        assert accepted.status_code == 200


def test_provider_callback_auth_is_owned_by_provider_verification(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", LOCAL_AUTH_TOKEN)
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", "agency-secret")

    app = create_test_app(
        database_url=postgres_url,
        model_adapter=NoModelAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        response = client.get("/v1/connectors/google/callback")
        assert response.status_code != 401
        assert response.json()["error"]["code"] != "E_LOCAL_AUTH_TOKEN_INVALID"

        now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                db.add(
                    ProviderWatchChannelRecord(
                        id="wch_chan_1",
                        provider="google",
                        resource_type="calendar",
                        resource_id="primary",
                        channel_id="chan_1",
                        channel_token="channel-token",
                        provider_resource_id="res_chan_1",
                        cursor_seed=None,
                        status="active",
                        expires_at=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
                        created_at=now,
                        updated_at=now,
                    )
                )

        provider_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "channel-token",
                "X-Goog-Channel-ID": "chan_1",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
            },
            json={},
        )
        assert provider_event.status_code == 202

        rejected_provider_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "wrong",
                "X-Goog-Channel-ID": "chan_2",
                "X-Goog-Message-Number": "2",
                "X-Goog-Resource-State": "exists",
            },
            json={},
        )
        assert rejected_provider_event.status_code == 401
        assert rejected_provider_event.json()["error"]["code"] == "E_PROVIDER_EVENT_CHANNEL_INVALID"

        body = json.dumps(
            {
                "source": "agency-test",
                "event_id": "evt_1",
                "event_type": "heartbeat",
                "payload": {"status": "ok"},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            b"agency-secret",
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        agency_event = client.post(
            "/v1/agency/events",
            headers={
                "X-Ariel-Agency-Timestamp": timestamp,
                "X-Ariel-Agency-Signature": signature,
                "content-type": "application/json",
            },
            content=body,
        )
        assert agency_event.status_code == 202
