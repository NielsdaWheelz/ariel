from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from ariel.app import create_app
from tests.fake_sandbox import FakeSandboxRuntime

LOCAL_AUTH_TOKEN = "test_local_auth_token_0123456789abcdef"


@dataclass
class NoModelAdapter:
    provider: str = "provider.api-auth-test"
    model: str = "model.api-auth-test"

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
        raise AssertionError("API auth tests must not call the model")


def test_local_auth_guards_authority_routes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", LOCAL_AUTH_TOKEN)

    app = create_app(
        database_url=postgres_url,
        model_adapter=NoModelAdapter(),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200

        unauthenticated = client.get("/v1/memory/facts")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "E_LOCAL_AUTH_TOKEN_INVALID"

        rejected = client.get("/v1/memory/facts", headers={"Authorization": "Bearer wrong"})
        assert rejected.status_code == 401

        accepted = client.get(
            "/v1/memory/facts",
            headers={"Authorization": f"Bearer {LOCAL_AUTH_TOKEN}"},
        )
        assert accepted.status_code == 200


def test_provider_callback_auth_is_owned_by_provider_verification(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", LOCAL_AUTH_TOKEN)
    monkeypatch.setenv("ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN", "provider-token")
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", "agency-secret")

    app = create_app(
        database_url=postgres_url,
        model_adapter=NoModelAdapter(),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        response = client.get("/v1/connectors/google/callback")
        assert response.status_code != 401
        assert response.json()["error"]["code"] != "E_LOCAL_AUTH_TOKEN_INVALID"

        provider_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "provider-token",
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
        assert rejected_provider_event.json()["error"]["code"] == "E_PROVIDER_EVENT_TOKEN_INVALID"

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
