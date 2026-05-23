from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from ariel.model_adapter import ModelAdapter
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_message,
    responses_run_message,
    responses_with_run_calls,
)
from tests.fake_sandbox import FakeSandboxRuntime


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


@dataclass
class DiscordStatusAdapter:
    provider: str = "provider.discord-status"
    model: str = "model.discord-status-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, history, context_bundle
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_discord_status_123",
            input_tokens=11,
            output_tokens=7,
        )


@dataclass
class NoVisibleResponseAdapter:
    provider: str = "provider.discord"
    model: str = "model.discord-v1"
    input_items: list[list[dict[str, Any]]] = field(default_factory=list)
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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, history, user_message
        self.input_items.append(input_items)
        self.context_bundles.append(context_bundle)
        calls = [{"name": "agent.pause_until_input", "input": {}}]
        return responses_with_run_calls(
            calls=calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_no_visible_response_123",
            input_tokens=13,
            output_tokens=2,
        )


@dataclass
class CapturingAttachmentAdapter:
    provider: str = "provider.attachments"
    model: str = "model.attachments-v1"
    input_items: list[list[dict[str, Any]]] = field(default_factory=list)
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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, history
        self.input_items.append(input_items)
        self.context_bundles.append(context_bundle)
        return responses_run_message(
            assistant_text=f"ack::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_attachment_acceptance_123",
            input_tokens=5,
            output_tokens=3,
        )


@dataclass
class AttachmentReadAdapter:
    provider: str = "provider.attachment-read"
    model: str = "model.attachment-read-v1"
    input_items: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history
        if context_bundle.get("origin") == "tool_result_interpretation":
            interpreter_input = json.loads(
                next(
                    item["content"]
                    for item in input_items
                    if item.get("role") == "user" and isinstance(item.get("content"), str)
                )
            )
            selected_output_refs = [
                output["output_ref"]
                for output in interpreter_input["audited_tool_outputs"]
                if isinstance(output, dict) and isinstance(output.get("output_ref"), str)
            ]
            return responses_message(
                assistant_text=json.dumps(
                    {
                        "findings": ["attachment output requires interpreted answer context"],
                        "contradictions": [],
                        "uncertainty": [],
                        "selected_output_refs": selected_output_refs,
                        "omitted_output_refs": [],
                        "citation_refs": interpreter_input["citation_refs"],
                        "artifact_refs": interpreter_input["artifact_refs"],
                        "recommended_next_evidence": [],
                        "confidence": 0.91,
                    },
                    sort_keys=True,
                ),
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_attachment_interpreter_123",
                input_tokens=7,
                output_tokens=5,
            )
        if any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            return responses_run_message(
                assistant_text="attachment content: quarterly revenue increased [1]",
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_attachment_final_123",
                input_tokens=7,
                output_tokens=5,
            )
        self.input_items.append(input_items)
        return responses_with_run_calls(
            calls=[
                {
                    "name": "attachment.read",
                    "input": {"attachment_ref": "discord:131415", "intent": "summarize"},
                }
            ],
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_attachment_read_123",
            input_tokens=7,
            output_tokens=5,
        )


def test_no_visible_response_operation_completes_turn_without_visible_reply(
    postgres_url: str,
) -> None:
    adapter = NoVisibleResponseAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        turn = post_message_and_drain(
            client,
            session_id,
            message="noted",
            json_extra={
                "discord": {
                    "guild_id": 123,
                    "guild_name": "Home",
                    "channel_id": 456,
                    "channel_name": "ops",
                    "channel_type": "text",
                    "thread_id": 789,
                    "thread_name": "deploy",
                    "parent_channel_id": 456,
                    "parent_channel_name": "ops",
                    "message_id": 101112,
                    "message_url": "https://discord.com/channels/123/456/101112",
                    "author_id": 131415,
                    "author_name": "owner",
                    "reply_to_message_id": None,
                    "mentioned_bot": False,
                    "attachments": [
                        {
                            "source": "discord",
                            "source_attachment_id": 161718,
                            "filename": "note.txt",
                            "content_type": "text/plain",
                            "size_bytes": 12,
                            "attachment_ref": "discord:161718",
                            "download_url": "https://cdn.discordapp.com/attachments/note.txt",
                        }
                    ],
                }
            },
        )

        assert turn.assistant_message == ""
        assert turn.status == "completed"

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["assistant_message"] == ""

        turn_started = next(
            event for event in turn_data["events"] if event["event_type"] == "evt.turn.started"
        )
        assert turn_started["payload"]["discord"]["channel_name"] == "ops"
        assert adapter.context_bundles[0]["discord_context"]["message_id"] == 101112
        assert any(
            item.get("role") == "system"
            and isinstance(item.get("content"), str)
            and "discord context:" in item["content"]
            and "filename=note.txt" in item["content"]
            and "attachment_ref=discord:161718" in item["content"]
            and "url=" not in item["content"]
            and "https://cdn.discordapp.com/attachments/note.txt" not in item["content"]
            for item in adapter.input_items[0]
        )
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                discord_message = (
                    db.execute(
                        text(
                            "SELECT id, message_id, title, summary, "
                            "source_uri, metadata FROM discord_messages "
                            "WHERE message_id = '101112'"
                        )
                    )
                    .mappings()
                    .one()
                )
                discord_message_event = (
                    db.execute(
                        text(
                            "SELECT id, discord_message_id, dedupe_key, event_type, payload "
                            "FROM discord_message_events "
                            "WHERE discord_message_id = :discord_message_id"
                        ),
                        {"discord_message_id": discord_message["id"]},
                    )
                    .mappings()
                    .one()
                )
        assert discord_message["title"] == "Discord message in #ops"
        assert discord_message["summary"] == "noted"
        assert discord_message["source_uri"] == "https://discord.com/channels/123/456/101112"
        assert discord_message["metadata"]["channel_id"] == 456
        assert discord_message["metadata"]["author_id"] == 131415
        assert discord_message_event["dedupe_key"] == "discord:message:101112:ingested"
        assert discord_message_event["event_type"] == "created"
        assert discord_message_event["payload"]["message_id"] == "101112"
        assert discord_message_event["payload"]["message"] == "noted"


def test_discord_attachment_content_is_referenced_without_raw_cdn_url(
    postgres_url: str,
) -> None:
    adapter = CapturingAttachmentAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client,
            session_id,
            message="please summarize this",
            json_extra={
                "discord": {
                    "guild_id": 123,
                    "channel_id": 456,
                    "message_id": 789,
                    "author_id": 101112,
                    "mentioned_bot": False,
                    "attachments": [
                        {
                            "source": "discord",
                            "source_attachment_id": 131415,
                            "filename": "quarterly.pdf",
                            "content_type": "application/pdf",
                            "size_bytes": 2048,
                            "attachment_ref": "discord:131415",
                            "download_url": "https://cdn.discordapp.com/attachments/raw.pdf",
                        }
                    ],
                }
            },
        )
        assert turn.status == "completed"

    context_attachment = adapter.context_bundles[0]["discord_context"]["attachments"][0]
    assert context_attachment == {
        "source": "discord",
        "source_attachment_id": 131415,
        "filename": "quarterly.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "attachment_ref": "discord:131415",
    }

    model_payload = json.dumps(adapter.input_items, sort_keys=True)
    assert "attachment_ref=discord:131415" in model_payload
    assert "filename=quarterly.pdf" in model_payload
    assert "url=" not in model_payload
    assert "download_url" not in model_payload
    assert "https://cdn.discordapp.com/attachments/raw.pdf" not in model_payload


def test_discord_attachment_read_tool_reads_text_attachment(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-length": "28"}

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [b"quarterly revenue increased"]

    class FakeHttpClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            return None

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeStreamResponse:
            assert method == "GET"
            assert url == "https://cdn.discordapp.com/attachments/report.txt"
            return FakeStreamResponse()

    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "disabled")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    monkeypatch.setattr("ariel.attachment_content.httpx.Client", FakeHttpClient)

    adapter = AttachmentReadAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client,
            session_id,
            message="please summarize this",
            json_extra={
                "discord": {
                    "guild_id": 123,
                    "channel_id": 456,
                    "message_id": 789,
                    "author_id": 101112,
                    "mentioned_bot": False,
                    "attachments": [
                        {
                            "source": "discord",
                            "source_attachment_id": 131415,
                            "filename": "report.txt",
                            "content_type": "text/plain",
                            "size_bytes": 28,
                            "attachment_ref": "discord:131415",
                            "download_url": "https://cdn.discordapp.com/attachments/report.txt",
                        }
                    ],
                }
            },
        )

        assert turn.assistant_message == "attachment content: quarterly revenue increased [1]"

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        lifecycle = turn_data["surface_action_lifecycle"]
        assert lifecycle[0]["proposal"]["capability_id"] == "cap.attachment.read"
        assert lifecycle[0]["execution"]["output"]["blocks"] == [
            {"kind": "text", "text": "quarterly revenue increased"}
        ]

        durable_payload = json.dumps(turn_data, sort_keys=True)
        assert "https://cdn.discordapp.com/attachments/report.txt" not in durable_payload
        assert "download_url" not in durable_payload


def test_root_serves_discord_primary_status_not_phone_surface(postgres_url: str) -> None:
    adapter = DiscordStatusAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("msg-a", "msg-b"):
            post_message_and_drain(client, session_id, message=message)

        surface = client.get("/")
        assert surface.status_code == 200
        assert surface.headers["content-type"].startswith("application/json")
        root_payload = surface.json()
        assert root_payload["ok"] is True
        assert root_payload["surface"] == "discord"
        assert root_payload["api"]["active_session"] == "/v1/sessions/active"
        assert "Discord" in root_payload["message"]
        assert "chat-form" not in surface.text
        assert "/v1/sessions/${sessionId}/events" not in surface.text

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert [turn["user_message"] for turn in turns] == ["msg-a", "msg-b"]
        assert turns[0]["events"][0]["event_type"] == "evt.turn.started"
        assert turns[1]["events"][0]["event_type"] == "evt.turn.started"
