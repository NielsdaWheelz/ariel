from __future__ import annotations

import json
from typing import Any, cast

from fastapi.testclient import TestClient

from ariel.action_runtime import process_action_execution_task
from ariel.app import _new_id, _utcnow
from ariel.google_connector import GoogleConnectorRuntime


def responses_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            }
        ],
    }


def responses_run_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    return responses_with_run_calls(
        assistant_text=assistant_text,
        calls=[{"name": "agent.emit_message", "input": {"text": assistant_text}}],
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def responses_with_run_calls(
    *,
    assistant_text: str,
    calls: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    del assistant_text
    if not calls:
        raise AssertionError("responses_with_run_calls requires at least one run call")
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "function_call",
                "id": "fc_run_test",
                "call_id": "call_run_test",
                "name": "run",
                "arguments": json.dumps(
                    {"source": json.dumps({"calls": calls}, sort_keys=True)},
                    sort_keys=True,
                ),
                "status": "completed",
            }
        ],
    }


def process_queued_action_execution(client: TestClient, approval_payload: dict[str, Any]) -> bool:
    action_attempt_id = approval_payload.get("action_attempt_id")
    if not isinstance(action_attempt_id, str):
        raise AssertionError("approval response did not include action_attempt_id")
    app_state = cast(Any, client.app).state
    return process_action_execution_task(
        session_factory=app_state.session_factory,
        action_attempt_id=action_attempt_id,
        google_runtime=GoogleConnectorRuntime(
            oauth_client=app_state.google_oauth_client,
            workspace_provider=app_state.google_workspace_provider,
            redirect_uri=str(app_state.google_oauth_redirect_uri),
            oauth_state_ttl_seconds=int(app_state.google_oauth_state_ttl_seconds),
            encryption_secret=str(app_state.connector_encryption_secret),
            encryption_key_version=str(app_state.connector_encryption_key_version),
            encryption_keys=(
                str(app_state.connector_encryption_keys)
                if app_state.connector_encryption_keys is not None
                else None
            ),
        ),
        agency_runtime=None,
        now_fn=_utcnow,
        new_id_fn=_new_id,
        memory_import_cutover_enabled=False,
    )
