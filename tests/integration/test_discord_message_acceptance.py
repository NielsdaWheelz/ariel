from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from fastapi.encoders import jsonable_encoder
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart
from sqlalchemy import text

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    has_tool_returns,
    is_memory_subsystem_call,
    last_user_message,
    post_message_and_drain,
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


def _system_prompt_text(messages: list[ModelMessage]) -> str:
    """Concatenate all SystemPromptPart contents from ``messages`` for substring checks."""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                parts.append(part.content)
    return "\n".join(parts)


def _patch_discord_attachment_download(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    body: bytes,
) -> None:
    class FakeStreamResponse:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"content-length": str(len(body))}

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [body]

    class FakeHttpClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            return None

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, request_url: str) -> FakeStreamResponse:
            assert method == "GET"
            assert request_url == url
            return FakeStreamResponse()

    monkeypatch.setattr("ariel.attachment_content.httpx.Client", FakeHttpClient)


def _post_report_attachment_read_request(
    client: TestClient,
    session_id: str,
    *,
    guild_id: int | None = 123,
) -> Any:
    return post_message_and_drain(
        client,
        session_id,
        message="please summarize this",
        json_extra={
            "discord": {
                "guild_id": guild_id,
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


class DiscordStatusAdapter(FakeModelAdapter):
    provider = "provider.discord-status"
    model = "model.discord-status-v1"

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
            provider_response_id="resp_discord_status_123",
            input_tokens=11,
            output_tokens=7,
        )


class NoVisibleResponseAdapter(FakeModelAdapter):
    provider = "provider.discord"
    model = "model.discord-v1"

    def __init__(self) -> None:
        super().__init__()
        self.messages_seen: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.messages_seen.append(list(request.messages))
        calls = [{"name": "agent.pause_until_input", "input": {}}]
        return responses_with_run_calls(
            calls=calls,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_no_visible_response_123",
            input_tokens=13,
            output_tokens=2,
        )


class CapturingAttachmentAdapter(FakeModelAdapter):
    provider = "provider.attachments"
    model = "model.attachments-v1"

    def __init__(self) -> None:
        super().__init__()
        self.messages_seen: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.messages_seen.append(list(request.messages))
        user_message = last_user_message(request.messages)
        return responses_run_message(
            assistant_text=f"ack::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_attachment_acceptance_123",
            input_tokens=5,
            output_tokens=3,
        )


class AttachmentReadAdapter(FakeModelAdapter):
    provider = "provider.attachment-read"
    model = "model.attachment-read-v1"

    def __init__(self) -> None:
        super().__init__()
        self.messages_seen: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        if has_tool_returns(request.messages):
            return responses_run_message(
                assistant_text="attachment content: quarterly revenue increased [1]",
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_attachment_final_123",
                input_tokens=7,
                output_tokens=5,
            )
        self.messages_seen.append(list(request.messages))
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
        assert turn_data["surface_action_lifecycle"] == []

        turn_started = next(
            event for event in turn_data["events"] if event["event_type"] == "evt.turn.started"
        )
        assert turn_started["payload"]["discord"]["channel_name"] == "ops"
        system_text = _system_prompt_text(adapter.messages_seen[0])
        assert "discord context:" in system_text
        assert "filename=note.txt" in system_text
        assert "attachment_ref=discord:161718" in system_text
        assert "url=" not in system_text
        assert "https://cdn.discordapp.com/attachments/note.txt" not in system_text
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

        discord_messages = client.get("/v1/discord-messages", params={"limit": 1})
        assert discord_messages.status_code == 200
        discord_message_payload = discord_messages.json()["discord_messages"][0]
        assert discord_message_payload["id"] == discord_message["id"]
        assert discord_message_payload["message_id"] == "101112"

        discord_events = client.get(f"/v1/discord-messages/{discord_message_payload['id']}/events")
        assert discord_events.status_code == 200
        discord_events_payload = discord_events.json()
        assert discord_events_payload["discord_message_id"] == discord_message["id"]
        assert len(discord_events_payload["events"]) == 1
        assert discord_events_payload["events"][0]["event_type"] == "created"


def test_discord_attachment_only_message_does_not_blind_read_or_expose_raw_url(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    adapter = NoVisibleResponseAdapter()
    raw_url = "https://cdn.discordapp.com/attachments/photo.png"
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client,
            session_id,
            message="What would you like me to do with the attachment(s)?",
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
                            "filename": "photo.png",
                            "content_type": "image/png",
                            "size_bytes": 2048,
                            "attachment_ref": "discord:131415",
                            "download_url": raw_url,
                        }
                    ],
                }
            },
        )

        assert turn.assistant_message == ""
        assert turn.status == "completed"

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["surface_action_lifecycle"] == []

        model_payload = json.dumps(jsonable_encoder(adapter.messages_seen), sort_keys=True)
        assert "attachment_ref=discord:131415" in model_payload
        assert "filename=photo.png" in model_payload
        assert raw_url not in model_payload
        assert "download_url" not in model_payload
        assert "url=" not in model_payload

        durable_payload = json.dumps(turn_data, sort_keys=True)
        assert raw_url not in durable_payload
        assert "download_url" not in durable_payload

        with cast(Any, client.app).state.session_factory() as db:
            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_sources) AS sources, "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()
        assert persisted_content_counts == (1, 0, 0)
        assert not (tmp_path / "attachments").exists()


def test_discord_dm_attachment_only_message_does_not_blind_read_or_expose_raw_url(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    adapter = NoVisibleResponseAdapter()
    raw_url = "https://cdn.discordapp.com/attachments/photo.png"
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client,
            session_id,
            message="What would you like me to do with the attachment(s)?",
            json_extra={
                "discord": {
                    "guild_id": None,
                    "channel_id": 456,
                    "message_id": 789,
                    "author_id": 101112,
                    "mentioned_bot": False,
                    "attachments": [
                        {
                            "source": "discord",
                            "source_attachment_id": 131415,
                            "filename": "photo.png",
                            "content_type": "image/png",
                            "size_bytes": 2048,
                            "attachment_ref": "discord:131415",
                            "download_url": raw_url,
                        }
                    ],
                }
            },
        )

        assert turn.assistant_message == ""
        assert turn.status == "completed"

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        model_payload = json.dumps(jsonable_encoder(adapter.messages_seen), sort_keys=True)
        durable_payload = json.dumps(turn_data, sort_keys=True)
        assert "attachment_ref=discord:131415" in model_payload
        assert "filename=photo.png" in model_payload
        assert raw_url not in model_payload
        assert "download_url" not in model_payload
        assert raw_url not in durable_payload
        assert "download_url" not in durable_payload

        with cast(Any, client.app).state.session_factory() as db:
            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_sources) AS sources, "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()
        assert persisted_content_counts == (1, 0, 0)
        assert not (tmp_path / "attachments").exists()


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

    model_payload = json.dumps(jsonable_encoder(adapter.messages_seen), sort_keys=True)
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
    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "disabled")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    _patch_discord_attachment_download(
        monkeypatch,
        url="https://cdn.discordapp.com/attachments/report.txt",
        body=b"quarterly revenue increased",
    )

    adapter = AttachmentReadAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = _post_report_attachment_read_request(client, session_id)

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


def test_discord_attachment_read_fail_closed_returns_typed_scan_failure(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "fail_closed")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    _patch_discord_attachment_download(
        monkeypatch,
        url="https://cdn.discordapp.com/attachments/report.txt",
        body=b"quarterly revenue increased",
    )

    adapter = AttachmentReadAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        _post_report_attachment_read_request(client, session_id)

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        lifecycle = turn_data["surface_action_lifecycle"]
        assert lifecycle[0]["proposal"]["capability_id"] == "cap.attachment.read"
        execution = lifecycle[0]["execution"]
        assert execution["status"] == "succeeded"
        output = execution["output"]
        assert output["read_outcome"]["status"] == "scan_failed"
        assert output["read_outcome"]["reason_code"] == "scan_failed"
        assert isinstance(output["read_outcome"]["recovery"], str)
        assert output["read_outcome"]["recovery"].strip()
        assert output["attachment_ref"] == "discord:131415"
        assert output["filename"] == "report.txt"
        assert output["modality"] == "text"
        assert output["blocks"] == []
        assert output["results"] == []

        durable_payload = json.dumps(turn_data, sort_keys=True)
        model_payload = json.dumps(jsonable_encoder(adapter.messages_seen), sort_keys=True)
        assert "attachment_ref=discord:131415" in model_payload
        assert "https://cdn.discordapp.com/attachments/report.txt" not in model_payload
        assert "download_url" not in model_payload
        assert "https://cdn.discordapp.com/attachments/report.txt" not in durable_payload
        assert "download_url" not in durable_payload

        with cast(Any, client.app).state.session_factory() as db:
            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_sources) AS sources, "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()
        assert persisted_content_counts == (1, 0, 0)
        assert not (tmp_path / "attachments").exists()


def test_discord_dm_attachment_read_fail_closed_returns_typed_scan_failure(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "fail_closed")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    _patch_discord_attachment_download(
        monkeypatch,
        url="https://cdn.discordapp.com/attachments/report.txt",
        body=b"quarterly revenue increased",
    )

    adapter = AttachmentReadAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        _post_report_attachment_read_request(client, session_id, guild_id=None)

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        execution = turn_data["surface_action_lifecycle"][0]["execution"]
        output = execution["output"]
        assert execution["status"] == "succeeded"
        assert output["read_outcome"]["status"] == "scan_failed"
        assert output["read_outcome"]["reason_code"] == "scan_failed"
        assert output["attachment_ref"] == "discord:131415"
        assert output["filename"] == "report.txt"
        assert output["blocks"] == []

        durable_payload = json.dumps(turn_data, sort_keys=True)
        model_payload = json.dumps(jsonable_encoder(adapter.messages_seen), sort_keys=True)
        assert "attachment_ref=discord:131415" in model_payload
        assert "https://cdn.discordapp.com/attachments/report.txt" not in model_payload
        assert "download_url" not in model_payload
        assert "https://cdn.discordapp.com/attachments/report.txt" not in durable_payload
        assert "download_url" not in durable_payload

        with cast(Any, client.app).state.session_factory() as db:
            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_sources) AS sources, "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()
        assert persisted_content_counts == (1, 0, 0)
        assert not (tmp_path / "attachments").exists()


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
