from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, ModelAdapterError, create_app
from tests.integration.responses_helpers import responses_message, responses_with_function_calls
from ariel.db import run_migrations
from ariel.google_connector import GOOGLE_CONNECTOR_ID
from ariel.persistence import GoogleConnectorRecord, WorkCommitmentRecord, WorkFollowUpLoopRecord


def _parse_utc_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC
    return parsed


@dataclass
class DeterministicModelAdapter:
    provider: str = "provider.test"
    model: str = "model.test-v1"
    fail: bool = False

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
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return responses_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_test_123",
            input_tokens=11,
            output_tokens=7,
        )


@dataclass
class DiscordNoResponseAdapter:
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
        del tools, history, user_message
        self.input_items.append(input_items)
        self.context_bundles.append(context_bundle)
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text="",
            proposals=[
                {
                    "capability_id": "cap.discord.no_response",
                    "input": {"reason": "nothing useful to add"},
                    "influenced_by_untrusted_content": False,
                }
            ],
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_discord_no_response_123",
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
        del tools, history
        self.input_items.append(input_items)
        self.context_bundles.append(context_bundle)
        return responses_message(
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
            return responses_message(
                assistant_text="attachment content: quarterly revenue increased [1]",
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_attachment_final_123",
                input_tokens=7,
                output_tokens=5,
            )
        self.input_items.append(input_items)
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text="",
            proposals=[
                {
                    "capability_id": "cap.attachment.read",
                    "input": {"attachment_ref": "discord:131415", "intent": "summarize"},
                    "influenced_by_untrusted_content": False,
                }
            ],
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_attachment_read_123",
            input_tokens=7,
            output_tokens=5,
        )


@dataclass
class ContextWindowDecisionAdapter:
    provider: str = "provider.context-window"
    model: str = "model.context-window-v1"
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
        del tools, history
        self.context_bundles.append(context_bundle)

        normalized = user_message.strip().lower()
        if normalized == "book me travel":
            assistant_text = "i need your destination and travel dates before i can plan this trip."
        elif normalized.startswith("project codename is "):
            declared_codename = normalized.replace("project codename is ", "", 1).strip()
            assistant_text = f"noted. project codename set to {declared_codename}."
        elif normalized == "what is the project codename?":
            codename = self._find_recent_codename(context_bundle)
            if codename is None:
                assistant_text = (
                    "i'm not sure because that detail is outside my recent context window. "
                    "please remind me of the codename."
                )
            else:
                assistant_text = f"your project codename is {codename}."
        else:
            assistant_text = f"direct::{user_message}"

        return responses_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_context_window_123",
            input_tokens=9,
            output_tokens=11,
        )

    def _find_recent_codename(self, context_bundle: dict[str, Any]) -> str | None:
        recent_turns = context_bundle.get("recent_active_session_turns")
        if not isinstance(recent_turns, list):
            return None
        for turn in reversed(recent_turns):
            if not isinstance(turn, dict):
                continue
            prior_user_message = turn.get("user_message")
            if not isinstance(prior_user_message, str):
                continue
            normalized = prior_user_message.strip().lower()
            if normalized.startswith("project codename is "):
                return normalized.replace("project codename is ", "", 1).strip()
        return None


@dataclass
class MutatingContextAdapter:
    provider: str = "provider.mutating"
    model: str = "model.mutating-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, user_message, history
        section_order = context_bundle.get("section_order")
        if isinstance(section_order, list):
            section_order.append("mutated")
        recent_window = context_bundle.get("recent_window")
        if isinstance(recent_window, dict):
            recent_window["included_turn_count"] = 999
            recent_window["included_turn_ids"] = ["mutated"]

        return responses_message(
            assistant_text="mutating-adapter-response",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_mutating_123",
            input_tokens=3,
            output_tokens=3,
        )


def _strategy_response(
    *,
    provider: str,
    model: str,
    selected_capability_ids: list[str] | None = None,
) -> dict[str, Any]:
    return responses_message(
        assistant_text=json.dumps(
            {
                "decision": "selected_tools" if selected_capability_ids else "no_tools",
                "selected_capability_ids": selected_capability_ids or [],
                "rationale": "test strategy",
                "unavailable_reason": None,
                "confidence": 1.0,
            },
            sort_keys=True,
        ),
        provider=provider,
        model=model,
        provider_response_id="resp_tool_strategy_123",
        input_tokens=3,
        output_tokens=2,
    )


@dataclass
class StrategyAwareTestAdapter:
    inner: ModelAdapter
    selected_capability_ids: list[str]
    provider: str = ""
    model: str = ""

    def __post_init__(self) -> None:
        self.provider = str(getattr(self.inner, "provider", "provider.test"))
        self.model = str(getattr(self.inner, "model", "model.test"))

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if context_bundle.get("origin") == "tool_strategy":
            del input_items, tools, user_message, history
            return _strategy_response(
                provider=self.provider,
                model=self.model,
                selected_capability_ids=self.selected_capability_ids,
            )
        return self.inner.create_response(
            input_items=input_items,
            tools=tools,
            user_message=user_message,
            history=history,
            context_bundle=context_bundle,
        )


def _wrap_strategy_adapter(adapter: ModelAdapter) -> StrategyAwareTestAdapter:
    selected_capability_ids: list[str] = []
    if isinstance(adapter, DiscordNoResponseAdapter):
        selected_capability_ids = ["cap.discord.no_response"]
    elif isinstance(adapter, AttachmentReadAdapter):
        selected_capability_ids = ["cap.attachment.read"]
    return StrategyAwareTestAdapter(
        inner=adapter,
        selected_capability_ids=selected_capability_ids,
    )


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def fresh_postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=_wrap_strategy_adapter(adapter),
        reset_database=True,
    )
    return TestClient(app)


def test_user_can_send_message_and_receive_model_backed_response(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        active = client.get("/v1/sessions/active")
        assert active.status_code == 200
        session_id = active.json()["session"]["id"]

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "hello from phone"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["assistant"]["message"] == "assistant::hello from phone"
        assert body["assistant"]["silent"] is False
        assert body["turn"]["status"] == "completed"


def test_discord_no_response_tool_completes_turn_without_visible_reply(
    postgres_url: str,
) -> None:
    adapter = DiscordNoResponseAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "noted",
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
                },
            },
        )

        body = response.json()
        assert response.status_code == 200
        assert body["assistant"]["message"] == ""
        assert body["assistant"]["silent"] is True
        assert body["turn"]["assistant_message"] == ""
        lifecycle = body["turn"]["surface_action_lifecycle"]
        assert lifecycle[0]["proposal"]["capability_id"] == "cap.discord.no_response"
        assert lifecycle[0]["execution"]["status"] == "succeeded"
        turn_started = [
            event for event in body["turn"]["events"] if event["event_type"] == "evt.turn.started"
        ][0]
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
                workspace_item = (
                    db.execute(
                        text(
                            "SELECT id, provider, item_type, external_id, title, summary, "
                            "source_uri, metadata FROM workspace_items "
                            "WHERE provider = 'discord' AND item_type = 'discord_message' "
                            "AND external_id = '101112'"
                        )
                    )
                    .mappings()
                    .one()
                )
                workspace_event = (
                    db.execute(
                        text(
                            "SELECT id, workspace_item_id, dedupe_key, event_type, payload "
                            "FROM workspace_item_events "
                            "WHERE workspace_item_id = :workspace_item_id"
                        ),
                        {"workspace_item_id": workspace_item["id"]},
                    )
                    .mappings()
                    .one()
                )
                task = (
                    db.execute(
                        text(
                            "SELECT task_type, payload FROM background_tasks "
                            "WHERE task_type = 'ambient_interpretation_due' "
                            "AND payload ->> 'workspace_item_event_id' = :event_id"
                        ),
                        {"event_id": workspace_event["id"]},
                    )
                    .mappings()
                    .one()
                )
        assert workspace_item["provider"] == "discord"
        assert workspace_item["item_type"] == "discord_message"
        assert workspace_item["title"] == "Discord message in #ops"
        assert workspace_item["summary"] == "noted"
        assert workspace_item["source_uri"] == "https://discord.com/channels/123/456/101112"
        assert workspace_item["metadata"]["channel_id"] == 456
        assert workspace_item["metadata"]["author_id"] == 131415
        assert workspace_event["dedupe_key"] == "discord:message:101112:ingested"
        assert workspace_event["event_type"] == "created"
        assert workspace_event["payload"]["message_id"] == "101112"
        assert workspace_event["payload"]["message"] == "noted"
        assert task["task_type"] == "ambient_interpretation_due"
        assert task["payload"]["workspace_item_event_id"] == workspace_event["id"]


def test_discord_attachment_content_is_referenced_without_raw_cdn_url(
    postgres_url: str,
) -> None:
    adapter = CapturingAttachmentAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "please summarize this",
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
                },
            },
        )

        assert response.status_code == 200
        body = response.json()

    assert body["ok"] is True

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
    durable_payload = json.dumps(body, sort_keys=True)
    assert "attachment_ref=discord:131415" in model_payload
    assert "filename=quarterly.pdf" in model_payload
    assert "url=" not in model_payload
    assert "download_url" not in model_payload
    assert "https://cdn.discordapp.com/attachments/raw.pdf" not in model_payload
    assert "https://cdn.discordapp.com/attachments/raw.pdf" not in durable_payload


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
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "please summarize this",
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
                },
            },
        )

        assert response.status_code == 200
        body = response.json()

    assert body["assistant"]["message"] == "attachment content: quarterly revenue increased [1]"
    assert body["assistant"]["sources"][0]["title"] == "report.txt"
    lifecycle = body["turn"]["surface_action_lifecycle"]
    assert lifecycle[0]["proposal"]["capability_id"] == "cap.attachment.read"
    assert lifecycle[0]["execution"]["output"]["blocks"] == [
        {"kind": "text", "text": "quarterly revenue increased"}
    ]
    durable_payload = json.dumps(body, sort_keys=True)
    assert "https://cdn.discordapp.com/attachments/report.txt" not in durable_payload
    assert "download_url" not in durable_payload


def test_large_attachment_read_writes_tool_result_interpretation_judgment(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    large_text = "quarterly revenue increased " * 320

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-length": str(len(large_text.encode()))}

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [large_text.encode()]

    class FakeHttpClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            return None

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeStreamResponse:
            assert method == "GET"
            assert url == "https://cdn.discordapp.com/attachments/large-report.txt"
            return FakeStreamResponse()

    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "disabled")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    monkeypatch.setattr("ariel.attachment_content.httpx.Client", FakeHttpClient)

    adapter = AttachmentReadAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "please summarize this long report",
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
                            "filename": "large-report.txt",
                            "content_type": "text/plain",
                            "size_bytes": len(large_text.encode()),
                            "attachment_ref": "discord:131415",
                            "download_url": (
                                "https://cdn.discordapp.com/attachments/large-report.txt"
                            ),
                        }
                    ],
                },
            },
        )
        assert response.status_code == 200
        turn_id = response.json()["turn"]["id"]
        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        event = next(
            event
            for event in timeline.json()["turns"][0]["events"]
            if event["event_type"] == "evt.ai_judgment.completed"
            and event["payload"].get("judgment_type") == "tool_result_interpretation"
        )
        assert event["payload"]["response_output_shape"] == {
            "output_type": "list",
            "output_count": 1,
            "text_present": True,
        }

        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                judgment = (
                    db.execute(
                        text(
                            "SELECT status, model, prompt_version, provider_response_id, "
                            "parse_status, validation_status, selected, output "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'tool_result_interpretation' "
                            "AND source_id = :turn_id "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"turn_id": turn_id},
                    )
                    .mappings()
                    .one()
                )

    assert judgment["status"] == "succeeded"
    assert judgment["model"] == "model.attachment-read-v1"
    assert judgment["prompt_version"] == "tool-result-interpretation-v1"
    assert judgment["provider_response_id"] == "resp_attachment_interpreter_123"
    assert judgment["parse_status"] == "parsed"
    assert judgment["validation_status"] == "valid"
    selected_refs = [
        item["output_ref"]
        for item in judgment["selected"]
        if isinstance(item, dict) and isinstance(item.get("output_ref"), str)
    ]
    assert selected_refs
    assert judgment["output"]["selected_output_refs"] == selected_refs
    assert judgment["output"]["provider"] == "provider.attachment-read"
    assert judgment["output"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 5,
        "total_tokens": 12,
    }


def test_invalid_tool_result_interpreter_output_preserves_failure_provenance(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    large_text = "quarterly revenue increased " * 320

    class InvalidInterpreterAdapter(AttachmentReadAdapter):
        def create_response(
            self,
            *,
            input_items: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            user_message: str,
            history: list[dict[str, Any]],
            context_bundle: dict[str, Any],
        ) -> dict[str, Any]:
            if context_bundle.get("origin") == "tool_result_interpretation":
                del input_items, tools, user_message, history
                return responses_message(
                    assistant_text="{not json",
                    provider=self.provider,
                    model=self.model,
                    provider_response_id="resp_attachment_interpreter_invalid",
                    input_tokens=17,
                    output_tokens=11,
                )
            return super().create_response(
                input_items=input_items,
                tools=tools,
                user_message=user_message,
                history=history,
                context_bundle=context_bundle,
            )

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-length": str(len(large_text.encode()))}

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [large_text.encode()]

    class FakeHttpClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            return None

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeStreamResponse:
            assert method == "GET"
            assert url == "https://cdn.discordapp.com/attachments/large-report.txt"
            return FakeStreamResponse()

    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "disabled")
    monkeypatch.setenv("ARIEL_ATTACHMENT_BLOB_STORE_PATH", str(tmp_path / "attachments"))
    monkeypatch.setattr("ariel.attachment_content.httpx.Client", FakeHttpClient)

    with _build_client(postgres_url, InvalidInterpreterAdapter()) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "please summarize this long report",
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
                            "filename": "large-report.txt",
                            "content_type": "text/plain",
                            "size_bytes": len(large_text.encode()),
                            "attachment_ref": "discord:131415",
                            "download_url": (
                                "https://cdn.discordapp.com/attachments/large-report.txt"
                            ),
                        }
                    ],
                },
            },
        )
        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "E_AI_JUDGMENT_INVALID_JSON"
        assert body["error"]["details"]["parse_status"] == "invalid_json"
        assert body["error"]["details"]["validation_status"] == "not_validated"
        assert body["error"]["details"]["provider"] == "provider.attachment-read"
        assert body["error"]["details"]["model"] == "model.attachment-read-v1"
        assert body["error"]["details"]["usage"] == {
            "input_tokens": 17,
            "output_tokens": 11,
            "total_tokens": 28,
        }
        assert body["error"]["details"]["provider_response_id"] == (
            "resp_attachment_interpreter_invalid"
        )

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        event_types = [event["event_type"] for event in turn["events"]]
        assert "evt.assistant.emitted" not in event_types
        failure_event = next(
            event
            for event in turn["events"]
            if event["payload"].get("judgment_type") == "tool_result_interpretation"
        )
        assert failure_event["payload"]["failure_code"] == "E_AI_JUDGMENT_INVALID_JSON"
        assert failure_event["payload"]["parse_status"] == "invalid_json"
        assert failure_event["payload"]["validation_status"] == "not_validated"
        assert failure_event["payload"]["provider_response_id"] == (
            "resp_attachment_interpreter_invalid"
        )
        turn_id = turn["id"]

        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                judgment = (
                    db.execute(
                        text(
                            "SELECT status, model, provider_response_id, parse_status, "
                            "validation_status, failure_code, output "
                            "FROM ai_judgments "
                            "WHERE judgment_type = 'tool_result_interpretation' "
                            "AND source_id = :turn_id "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"turn_id": turn_id},
                    )
                    .mappings()
                    .one()
                )

    assert judgment["status"] == "failed"
    assert judgment["model"] == "model.attachment-read-v1"
    assert judgment["provider_response_id"] == "resp_attachment_interpreter_invalid"
    assert judgment["parse_status"] == "invalid_json"
    assert judgment["validation_status"] == "not_validated"
    assert judgment["failure_code"] == "E_AI_JUDGMENT_INVALID_JSON"
    assert judgment["output"]["provider"] == "provider.attachment-read"
    assert judgment["output"]["usage"] == {
        "input_tokens": 17,
        "output_tokens": 11,
        "total_tokens": 28,
    }


def test_discord_turn_context_includes_bounded_same_channel_history(
    postgres_url: str,
) -> None:
    adapter = DiscordNoResponseAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "channel note one",
                "discord": {
                    "guild_id": 123,
                    "channel_id": 456,
                    "message_id": 1001,
                    "author_id": 131415,
                    "mentioned_bot": False,
                    "attachments": [],
                },
            },
        )
        assert first.status_code == 200

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={
                "message": "what was the note?",
                "discord": {
                    "guild_id": 123,
                    "channel_id": 456,
                    "message_id": 1002,
                    "author_id": 131415,
                    "mentioned_bot": False,
                    "attachments": [],
                },
            },
        )
        assert second.status_code == 200

        channel_turns = adapter.context_bundles[1]["discord_channel_recent_turns"]
        assert channel_turns == [
            {
                "turn_id": first.json()["turn"]["id"],
                "message_id": 1001,
                "user_message": "channel note one",
                "assistant_message": "",
                "status": "completed",
            }
        ]
        assert any(
            item.get("role") == "system"
            and isinstance(item.get("content"), str)
            and "recent Discord channel context:" in item["content"]
            and "message_id=1001 user=channel note one" in item["content"]
            for item in adapter.input_items[1]
        )


def test_pr01_model_led_direct_and_clarification_messages_are_emitted(postgres_url: str) -> None:
    adapter = ContextWindowDecisionAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        clear_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "summarize this in one line"},
        )
        assert clear_turn.status_code == 200
        assert clear_turn.json()["assistant"]["message"].startswith("direct::")

        ambiguous_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "book me travel"},
        )
        assert ambiguous_turn.status_code == 200
        assert "destination and travel dates" in ambiguous_turn.json()["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        event_types_by_turn = [
            [event["event_type"] for event in turn["events"]] for turn in timeline.json()["turns"]
        ]
        assert all("evt.assistant.emitted" in event_types for event_types in event_types_by_turn)
        assert all("evt.turn.completed" in event_types for event_types in event_types_by_turn)


def test_pr01_turn_context_is_bounded_ordered_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "1")
    adapter = ContextWindowDecisionAdapter()

    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        turn_1 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "project codename is aurora"},
        )
        assert turn_1.status_code == 200

        turn_2 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the project codename?"},
        )
        assert turn_2.status_code == 200
        assert "aurora" in turn_2.json()["assistant"]["message"]

        turn_3 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "let's move on"},
        )
        assert turn_3.status_code == 200

        turn_4 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the project codename?"},
        )
        assert turn_4.status_code == 200
        assert "outside my recent context window" in turn_4.json()["assistant"]["message"]

        assert len(adapter.context_bundles) == 4
        for context_bundle in adapter.context_bundles:
            assert context_bundle["section_order"] == [
                "policy_system_instructions",
                "recent_active_session_turns",
                "memory_context",
                "open_commitments_and_jobs",
                "relevant_artifacts_and_observations",
            ]

        second_turn_context = adapter.context_bundles[1]
        assert [
            turn["user_message"] for turn in second_turn_context["recent_active_session_turns"]
        ] == ["project codename is aurora"]

        fourth_turn_context = adapter.context_bundles[3]
        assert [
            turn["user_message"] for turn in fourth_turn_context["recent_active_session_turns"]
        ] == ["let's move on"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 4

        model_started_second_turn = next(
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        )
        second_context_meta = model_started_second_turn["payload"]["context"]
        assert second_context_meta["schema_version"] == "1.0"
        assert second_context_meta["section_order"] == [
            "policy_system_instructions",
            "recent_active_session_turns",
            "memory_context",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_observations",
        ]
        assert second_context_meta["policy_instruction_count"] >= 1
        assert second_context_meta["recent_window"] == {
            "max_recent_turns": 1,
            "included_turn_count": 1,
            "omitted_turn_count": 0,
            "included_turn_ids": [turns[0]["id"]],
        }

        model_started_fourth_turn = next(
            event for event in turns[3]["events"] if event["event_type"] == "evt.model.started"
        )
        fourth_context_meta = model_started_fourth_turn["payload"]["context"]
        assert fourth_context_meta["schema_version"] == "1.0"
        assert fourth_context_meta["recent_window"]["max_recent_turns"] == 1
        assert fourth_context_meta["recent_window"]["included_turn_count"] == 1
        assert fourth_context_meta["recent_window"]["omitted_turn_count"] == 2
        assert fourth_context_meta["recent_window"]["included_turn_ids"] == [turns[2]["id"]]


def test_pr01_context_includes_open_google_commitments_and_due_follow_up_loops(
    postgres_url: str,
) -> None:
    adapter = CapturingAttachmentAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                db.add(
                    GoogleConnectorRecord(
                        id=GOOGLE_CONNECTOR_ID,
                        provider="google",
                        status="connected",
                        account_subject="acct_google",
                        account_email="user@example.com",
                        granted_scopes=[],
                        access_token_enc=None,
                        refresh_token_enc=None,
                        access_token_expires_at=None,
                        token_obtained_at=None,
                        encryption_key_version="v1",
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkCommitmentRecord(
                        id="wcm_context_open",
                        provider="google",
                        provider_account_id="acct_google",
                        owner="user",
                        requester_person_id=None,
                        counterparty_person_id=None,
                        thread_id=None,
                        dedupe_digest="wkc_relevant",
                        action_text="Send the invoice pack to Dana before the Friday review.",
                        action_category="deliverable",
                        due_start=datetime(2026, 5, 15, 9, 30, tzinfo=UTC),
                        due_end=None,
                        timezone="UTC",
                        priority="high",
                        confidence=0.91,
                        lifecycle_state="active",
                        review_state="approved",
                        resolution_evidence_id=None,
                        superseded_by_commitment_id=None,
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkCommitmentRecord(
                        id="wcm_context_other_account",
                        provider="google",
                        provider_account_id="acct_other",
                        owner="user",
                        requester_person_id=None,
                        counterparty_person_id=None,
                        thread_id=None,
                        dedupe_digest="wkc_other_account",
                        action_text="Other account work should stay out of this context.",
                        action_category="deliverable",
                        due_start=datetime(2026, 5, 13, 13, 30, tzinfo=UTC),
                        due_end=None,
                        timezone="UTC",
                        priority="critical",
                        confidence=0.9,
                        lifecycle_state="active",
                        review_state="approved",
                        resolution_evidence_id=None,
                        superseded_by_commitment_id=None,
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkCommitmentRecord(
                        id="wcm_context_candidate",
                        provider="google",
                        provider_account_id="acct_google",
                        owner="user",
                        requester_person_id=None,
                        counterparty_person_id=None,
                        thread_id=None,
                        dedupe_digest="wkc_candidate",
                        action_text="Candidate work should render only as review.",
                        action_category="deliverable",
                        due_start=datetime(2026, 5, 13, 13, 30, tzinfo=UTC),
                        due_end=None,
                        timezone="UTC",
                        priority="normal",
                        confidence=0.7,
                        lifecycle_state="candidate",
                        review_state="review_required",
                        resolution_evidence_id=None,
                        superseded_by_commitment_id=None,
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkCommitmentRecord(
                        id="wcm_context_done",
                        provider="google",
                        provider_account_id="acct_google",
                        owner="user",
                        requester_person_id=None,
                        counterparty_person_id=None,
                        thread_id=None,
                        dedupe_digest="wkc_resolved",
                        action_text="Already resolved work should stay out of context.",
                        action_category="deliverable",
                        due_start=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
                        due_end=None,
                        timezone="UTC",
                        priority="normal",
                        confidence=0.9,
                        lifecycle_state="resolved",
                        review_state="approved",
                        resolution_evidence_id=None,
                        superseded_by_commitment_id=None,
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkFollowUpLoopRecord(
                        id="wfl_context_terminal",
                        commitment_id="wcm_context_done",
                        thread_id=None,
                        loop_kind="due_date",
                        state="active",
                        version=1,
                        next_check_at=datetime(2020, 1, 1, tzinfo=UTC),
                        next_notification_at=None,
                        stale_after=None,
                        last_evaluated_evidence_id=None,
                        snoozed_until=None,
                        last_feedback=None,
                        policy_version="test-policy",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkFollowUpLoopRecord(
                        id="wfl_context_due",
                        commitment_id="wcm_context_open",
                        thread_id=None,
                        loop_kind="due_date",
                        state="active",
                        version=1,
                        next_check_at=datetime(2020, 1, 1, tzinfo=UTC),
                        next_notification_at=None,
                        stale_after=None,
                        last_evaluated_evidence_id=None,
                        snoozed_until=None,
                        last_feedback=None,
                        policy_version="test-policy",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkFollowUpLoopRecord(
                        id="wfl_context_other_account",
                        commitment_id="wcm_context_other_account",
                        thread_id=None,
                        loop_kind="due_date",
                        state="active",
                        version=1,
                        next_check_at=datetime(2020, 1, 1, tzinfo=UTC),
                        next_notification_at=None,
                        stale_after=None,
                        last_evaluated_evidence_id=None,
                        snoozed_until=None,
                        last_feedback=None,
                        policy_version="test-policy",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    WorkFollowUpLoopRecord(
                        id="wfl_context_future",
                        commitment_id="wcm_context_open",
                        thread_id=None,
                        loop_kind="due_date",
                        state="active",
                        version=1,
                        next_check_at=datetime(2099, 1, 1, tzinfo=UTC),
                        next_notification_at=None,
                        stale_after=None,
                        last_evaluated_evidence_id=None,
                        snoozed_until=None,
                        last_feedback=None,
                        policy_version="test-policy",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what work needs attention?"},
        )

        assert response.status_code == 200
        work_context = adapter.context_bundles[0]["open_commitments_and_jobs"]
        assert work_context["provider_account_id"] == "acct_google"
        assert work_context["open_commitments"] == [
            {
                "id": "wcm_context_open",
                "provider": "google",
                "owner": "user",
                "action_text": "Send the invoice pack to Dana before the Friday review.",
                "action_category": "deliverable",
                "due_start": "2026-05-15T09:30:00Z",
                "due_end": None,
                "timezone": "UTC",
                "priority": "high",
                "lifecycle_state": "active",
                "review_state": "approved",
                "thread_id": None,
                "source_refs": [],
            }
        ]
        assert work_context["commitment_review_prompts"] == [
            {
                "id": "wcm_context_candidate",
                "provider": "google",
                "owner": "user",
                "action_text": "Candidate work should render only as review.",
                "action_category": "deliverable",
                "due_start": "2026-05-13T13:30:00Z",
                "due_end": None,
                "timezone": "UTC",
                "priority": "normal",
                "lifecycle_state": "candidate",
                "review_state": "review_required",
                "thread_id": None,
                "source_refs": [],
            }
        ]
        assert work_context["due_follow_up_loops"][0]["id"] == "wfl_context_due"
        assert work_context["due_follow_up_loops"][0]["commitment_action_text"] == (
            "Send the invoice pack to Dana before the Friday review."
        )
        assert "wfl_context_future" not in {
            loop["id"] for loop in work_context["due_follow_up_loops"]
        }
        assert "wfl_context_terminal" not in {
            loop["id"] for loop in work_context["due_follow_up_loops"]
        }
        assert "wfl_context_other_account" not in {
            loop["id"] for loop in work_context["due_follow_up_loops"]
        }

        rendered_context = "\n".join(
            item["content"]
            for item in adapter.input_items[0]
            if item.get("role") == "system" and isinstance(item.get("content"), str)
        )
        assert "open work commitments:" in rendered_context
        assert "wcm_context_open: high: active: Send the invoice pack" in rendered_context
        assert "due follow-up loops:" in rendered_context
        assert "wfl_context_due: due_date: active" in rendered_context
        assert "work commitments needing review:" in rendered_context
        assert "wcm_context_candidate: candidate: review_required" in rendered_context
        assert "wcm_context_done" not in rendered_context
        assert "wcm_context_other_account" not in rendered_context
        assert "wfl_context_future" not in rendered_context


def test_pr01_context_audit_is_stable_even_if_adapter_mutates_context_bundle(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "1")
    adapter = MutatingContextAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "seed history"},
        )
        assert first.status_code == 200

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "mutate context"},
        )
        assert second.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        model_started_second_turn = next(
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        )
        context_meta = model_started_second_turn["payload"]["context"]
        assert context_meta["schema_version"] == "1.0"
        assert context_meta["section_order"] == [
            "policy_system_instructions",
            "recent_active_session_turns",
            "memory_context",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_observations",
        ]
        assert context_meta["recent_window"] == {
            "max_recent_turns": 1,
            "included_turn_count": 1,
            "omitted_turn_count": 0,
            "included_turn_ids": [turns[0]["id"]],
        }


def test_create_session_endpoint_reuses_single_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post("/v1/sessions")
        second = client.post("/v1/sessions")
        active = client.get("/v1/sessions/active")

        assert first.status_code == 200
        assert second.status_code == 200
        assert active.status_code == 200

        first_id = first.json()["session"]["id"]
        assert second.json()["session"]["id"] == first_id
        assert active.json()["session"]["id"] == first_id


def test_schema_not_ready_returns_503_until_migrated(fresh_postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()

    app_without_migration = create_app(
        database_url=fresh_postgres_url,
        model_adapter=adapter,
        reset_database=False,
    )
    with TestClient(app_without_migration) as client:
        health = client.get("/v1/health")
        assert health.status_code == 503
        health_body = health.json()
        assert health_body["ok"] is False
        assert health_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        assert "missing_tables" in health_body["error"]["details"]

        active = client.get("/v1/sessions/active")
        assert active.status_code == 503
        active_body = active.json()
        assert active_body["ok"] is False
        assert active_body["error"]["code"] == "E_SCHEMA_NOT_READY"

    run_migrations(fresh_postgres_url)
    app_with_migration = create_app(
        database_url=fresh_postgres_url,
        model_adapter=adapter,
        reset_database=False,
    )
    with TestClient(app_with_migration) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/sessions/active").status_code == 200


def test_single_active_session_and_ordered_turn_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("first message", "second message"):
            send = client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": message},
            )
            assert send.status_code == 200

        active_again = client.get("/v1/sessions/active")
        assert active_again.status_code == 200
        assert active_again.json()["session"]["id"] == session_id

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert [turn["user_message"] for turn in turns] == ["first message", "second message"]

        expected_types = [
            "evt.turn.started",
            "evt.ai_judgment.completed",
            "evt.model.started",
            "evt.model.completed",
            "evt.memory.evidence_recorded",
            "evt.memory.evidence_recorded",
            "evt.memory.extraction_queued",
            "evt.assistant.emitted",
            "evt.turn.completed",
        ]
        for turn in turns:
            assert [event["event_type"] for event in turn["events"]] == expected_types
            assert [event["sequence"] for event in turn["events"]] == list(
                range(1, len(expected_types) + 1)
            )

        first_turn_ts = _parse_utc_rfc3339(turns[0]["created_at"])
        second_turn_ts = _parse_utc_rfc3339(turns[1]["created_at"])
        assert first_turn_ts <= second_turn_ts


def test_model_timeline_includes_identity_duration_and_usage(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(provider="provider.alpha", model="alpha-mini")
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "inspect model metadata"},
        )
        assert send.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_completed = next(
            event for event in events if event["event_type"] == "evt.model.completed"
        )
        payload = model_completed["payload"]
        assert payload["provider"] == "provider.alpha"
        assert payload["model"] == "alpha-mini"
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0
        assert payload["usage"]["input_tokens"] == 11
        assert payload["usage"]["output_tokens"] == 7
        assert payload["usage"]["total_tokens"] == 18


def test_model_failure_is_auditable_and_turn_terminates_failed(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(fail=True)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "this should fail"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"
        assert body["error"]["retryable"] is True

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 1
        turn = turns[0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.ai_judgment.completed",
            "evt.model.started",
            "evt.model.failed",
            "evt.turn.failed",
        ]
        model_failed = next(
            event for event in turn["events"] if event["event_type"] == "evt.model.failed"
        )
        assert "failure_reason" in model_failed["payload"]
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in turns)


def test_ids_timestamps_and_error_envelope_follow_constitution(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        active = client.get("/v1/sessions/active")
        assert active.status_code == 200
        session = active.json()["session"]
        assert session["id"].startswith("ses_")
        _parse_utc_rfc3339(session["created_at"])
        _parse_utc_rfc3339(session["updated_at"])

        send = client.post(
            f"/v1/sessions/{session['id']}/message",
            json={"message": "validate ids"},
        )
        assert send.status_code == 200
        turn = send.json()["turn"]
        assert turn["id"].startswith("trn_")
        _parse_utc_rfc3339(turn["created_at"])
        _parse_utc_rfc3339(turn["updated_at"])

        timeline = client.get(f"/v1/sessions/{session['id']}/events")
        for saved_turn in timeline.json()["turns"]:
            _parse_utc_rfc3339(saved_turn["created_at"])
            _parse_utc_rfc3339(saved_turn["updated_at"])
            for event in saved_turn["events"]:
                _parse_utc_rfc3339(event["created_at"])

        missing = client.post(
            "/v1/sessions/ses_01JZZZZZZZZZZZZZZZZZZZZZZZ/message",
            json={"message": "missing"},
        )
        assert missing.status_code == 404
        error = missing.json()
        assert error["ok"] is False
        assert error["error"]["code"] == "E_SESSION_NOT_FOUND"
        assert isinstance(error["error"]["details"], dict)
        assert error["error"]["retryable"] is False


def test_whitespace_only_message_is_rejected_with_standard_error(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        invalid = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "   "},
        )
        assert invalid.status_code == 422
        body = invalid.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_VALIDATION"
        assert body["error"]["retryable"] is False

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        assert timeline.json()["turns"] == []


def test_root_serves_discord_primary_status_not_phone_surface(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("msg-a", "msg-b"):
            sent = client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": message},
            )
            assert sent.status_code == 200

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

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        turns = timeline.json()["turns"]
        assert [turn["user_message"] for turn in turns] == ["msg-a", "msg-b"]
        assert turns[0]["events"][0]["event_type"] == "evt.turn.started"
        assert turns[1]["events"][0]["event_type"] == "evt.turn.started"


@dataclass
class SecretLeakingFailureAdapter:
    provider: str = "provider.leaky"
    model: str = "model.leaky-v1"
    secret_value: str = "sk-live-very-secret"

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
        raise RuntimeError(f"provider rejected credential {self.secret_value}")


@dataclass
class NonSecretFailureAdapter:
    provider: str = "provider.non-secret"
    model: str = "model.non-secret-v1"

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
        raise RuntimeError("token limit exceeded for this request")


def test_default_runtime_model_requires_server_secret_credentials(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MODEL_NAME", "gpt-5.5")
    # Force empty key so this assertion is stable even if local .env files exist.
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "")

    app = create_app(
        database_url=postgres_url,
        model_adapter=None,
        reset_database=True,
    )
    with TestClient(app) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "credential check"},
        )
        assert send.status_code == 503
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_CREDENTIALS"
        assert body["error"]["retryable"] is False
        assert "credential" in body["error"]["message"].lower()
        assert "sk-" not in str(body)

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        event_types = [event["event_type"] for event in events]
        assert event_types == [
            "evt.turn.started",
            "evt.ai_judgment.failed",
            "evt.turn.failed",
        ]
        failure_payload = next(
            event["payload"] for event in events if event["event_type"] == "evt.ai_judgment.failed"
        )
        assert "credential" in failure_payload["failure_reason"].lower()
        assert "sk-" not in failure_payload["failure_reason"]


def test_model_failure_reason_is_redacted_for_secret_like_exceptions(postgres_url: str) -> None:
    adapter = SecretLeakingFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger redaction"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert adapter.secret_value not in model_failed["payload"]["failure_reason"]
        assert "RuntimeError" in model_failed["payload"]["failure_reason"]


def test_model_failure_reason_preserves_non_secret_detail(postgres_url: str) -> None:
    adapter = NonSecretFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger non-secret failure"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert model_failed["payload"]["failure_reason"] == "token limit exceeded for this request"


def test_restart_preserves_history_and_appends_to_same_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as first_client:
        first_session = first_client.get("/v1/sessions/active")
        assert first_session.status_code == 200
        session_id = first_session.json()["session"]["id"]
        first_send = first_client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "before restart"},
        )
        assert first_send.status_code == 200

        timeline_before = first_client.get(f"/v1/sessions/{session_id}/events")
        assert timeline_before.status_code == 200
        assert [turn["user_message"] for turn in timeline_before.json()["turns"]] == [
            "before restart"
        ]

    restarted_app = create_app(
        database_url=postgres_url,
        model_adapter=_wrap_strategy_adapter(adapter),
        reset_database=False,
    )
    with TestClient(restarted_app) as second_client:
        active_after_restart = second_client.get("/v1/sessions/active")
        assert active_after_restart.status_code == 200
        assert active_after_restart.json()["session"]["id"] == session_id

        timeline_after_restart = second_client.get(f"/v1/sessions/{session_id}/events")
        assert timeline_after_restart.status_code == 200
        assert [turn["user_message"] for turn in timeline_after_restart.json()["turns"]] == [
            "before restart"
        ]

        second_send = second_client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "after restart"},
        )
        assert second_send.status_code == 200

        final_timeline = second_client.get(f"/v1/sessions/{session_id}/events")
        assert final_timeline.status_code == 200
        assert [turn["user_message"] for turn in final_timeline.json()["turns"]] == [
            "before restart",
            "after restart",
        ]


@dataclass
class LongResponseAdapter:
    provider: str = "provider.long-response"
    model: str = "model.long-response-v1"
    response_token_count: int = 16

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
        assistant_text = " ".join(["long"] * self.response_token_count)
        return responses_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_long_123",
            input_tokens=5,
            output_tokens=self.response_token_count,
        )


@dataclass
class UsageDrivenResponseAdapter:
    provider: str = "provider.usage-driven"
    model: str = "model.usage-driven-v1"
    reported_output_tokens: int = 12

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
        return responses_message(
            assistant_text="ok",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_usage_123",
            input_tokens=2,
            output_tokens=self.reported_output_tokens,
        )


@dataclass
class RetryableFailureAdapter:
    provider: str = "provider.retryable-failure"
    model: str = "model.retryable-failure-v1"
    attempts: int = 0

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
        self.attempts += 1
        raise ModelAdapterError(
            safe_reason="temporary provider timeout",
            status_code=502,
            code="E_MODEL_FAILURE",
            message="model provider request failed",
            retryable=True,
        )


def test_pr02_context_budget_exhaustion_returns_bounded_failure_with_audit_details(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "1")

    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "hello from bounded context"},
        )
        assert send.status_code == 503
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"].startswith("E_AI_JUDGMENT_")
        assert "continuity" in body["error"]["message"].lower()
        assert body["error"]["details"]["judgment_type"] == "continuity_compaction"
        assert body["error"]["details"]["session_id"] == session_id

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 1
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in turns)

        turn = turns[0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert "evt.turn.started" in event_types
        assert "evt.ai_judgment.failed" in event_types
        assert "evt.turn.failed" in event_types
        assert "evt.turn.limit_reached" not in event_types
        assert "evt.assistant.emitted" not in event_types


def test_pr02_response_budget_exhaustion_is_emitted_before_terminal_failed(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "3")

    adapter = LongResponseAdapter(response_token_count=8)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger response budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "response budget" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "response_tokens"
        assert limit_details["unit"] == "tokens"
        assert limit_details["limit"] == 3
        assert limit_details["measured"] > 3
        assert body["error"]["details"]["applied_limits"]["max_response_tokens"] == 3

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        assert not any(
            saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"]
        )
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.ai_judgment.completed",
            "evt.model.started",
            "evt.model.completed",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]
        model_completed = next(
            event for event in turn["events"] if event["event_type"] == "evt.model.completed"
        )
        assert model_completed["payload"]["provider"] == adapter.provider
        assert model_completed["payload"]["model"] == adapter.model


def test_pr02_response_budget_uses_reported_output_tokens_when_present(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "5")

    adapter = UsageDrivenResponseAdapter(reported_output_tokens=9)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger usage-driven response budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "response_tokens"
        assert limit_details["measured"] == 9
        assert limit_details["limit"] == 5

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.ai_judgment.completed",
            "evt.model.started",
            "evt.model.completed",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]


def test_pr02_model_attempt_budget_exhaustion_uses_ai_judgment_budget_error(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_MODEL_ATTEMPTS", "2")
    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger model attempt budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_AI_JUDGMENT_BUDGET"
        assert body["error"]["details"]["judgment_type"] == "model_output"
        assert body["error"]["details"]["attempt"] == 2
        assert body["error"]["details"]["max_model_attempts"] == 2

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        events = turn["events"]
        assert len([event for event in events if event["event_type"] == "evt.model.started"]) == 2
        assert len([event for event in events if event["event_type"] == "evt.model.failed"]) == 2
        event_types = [event["event_type"] for event in events]
        assert event_types[-2:] == ["evt.ai_judgment.failed", "evt.turn.failed"]
        assert "evt.assistant.emitted" not in event_types
        model_output_failure = next(
            event for event in events if event["payload"].get("judgment_type") == "model_output"
        )
        assert model_output_failure["payload"]["failure_code"] == "E_AI_JUDGMENT_BUDGET"
        assert not any(
            saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"]
        )


def test_pr02_wall_time_budget_takes_precedence_if_multiple_limits_exhaust(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_MODEL_ATTEMPTS", "1")
    monkeypatch.setenv("ARIEL_MAX_TURN_WALL_TIME_MS", "20")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.03
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger competing limits"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "turn_wall_time_ms"
        assert limit_details["measured"] > limit_details["limit"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.ai_judgment.completed",
            "evt.model.started",
            "evt.model.failed",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]


def test_pr02_wall_time_budget_exhaustion_is_bounded_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_TURN_WALL_TIME_MS", "20")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.03
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger wall-time budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "time budget" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "turn_wall_time_ms"
        assert limit_details["unit"] == "ms"
        assert limit_details["limit"] == 20
        assert limit_details["measured"] > 20
        assert body["error"]["details"]["applied_limits"]["max_turn_wall_time_ms"] == 20

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types[-2:] == ["evt.assistant.emitted", "evt.turn.failed"]
        assert turn["status"] == "failed"
        assert not any(
            saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"]
        )
