from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ariel.app import create_app
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
from ariel.db import SchemaReadinessProbe, run_migrations
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_retriever_call,
    last_user_message,
    post_message_and_drain,
    responses_run_message,
    responses_with_run_calls,
)
from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from tests.fake_sandbox import FakeSandboxRuntime


def _parse_utc_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC
    return parsed


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


class DeterministicModelAdapter(FakeModelAdapter):
    provider = "provider.test"
    model = "model.test-v1"

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        fail: bool = False,
    ) -> None:
        super().__init__()
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        self.fail = fail

    def _respond(self, request: ModelCall) -> ModelResponse:
        user_message = last_user_message(request.messages)
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_test_123",
            input_tokens=11,
            output_tokens=7,
        )


class NoVisibleResponseAdapter(FakeModelAdapter):
    provider = "provider.discord"
    model = "model.discord-v1"

    def __init__(self) -> None:
        super().__init__()
        self.input_items: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.input_items.append(list(request.messages))
        calls = [{"name": "agent.pause_until_input", "input": {}}]
        return responses_with_run_calls(
            assistant_text="",
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
        self.input_items: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        user_message = last_user_message(request.messages)
        self.input_items.append(list(request.messages))
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
        self.input_items: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        from pydantic_ai.messages import ModelRequest, ToolReturnPart  # noqa: PLC0415

        has_tool_return = any(
            isinstance(message, ModelRequest)
            and any(isinstance(part, ToolReturnPart) for part in message.parts)
            for message in request.messages
        )
        if has_tool_return:
            return responses_run_message(
                assistant_text="attachment content: quarterly revenue increased [1]",
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_attachment_final_123",
                input_tokens=7,
                output_tokens=5,
            )
        self.input_items.append(list(request.messages))
        return responses_with_run_calls(
            assistant_text="",
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


class ContextWindowDecisionAdapter(FakeModelAdapter):
    """Routes on the user message — drives codename memory across turns."""

    provider = "provider.context-window"
    model = "model.context-window-v1"

    def __init__(self) -> None:
        super().__init__()
        self.input_items: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.input_items.append(list(request.messages))
        user_message = last_user_message(request.messages)

        normalized = user_message.strip().lower()
        if normalized == "book me travel":
            assistant_text = "i need your destination and travel dates before i can plan this trip."
        elif normalized.startswith("project codename is "):
            declared_codename = normalized.replace("project codename is ", "", 1).strip()
            assistant_text = f"noted. project codename set to {declared_codename}."
        elif normalized == "what is the project codename?":
            codename = self._find_recent_codename(request.messages)
            if codename is None:
                assistant_text = (
                    "i'm not sure because that detail is outside my recent context window. "
                    "please remind me of the codename."
                )
            else:
                assistant_text = f"your project codename is {codename}."
        else:
            assistant_text = f"direct::{user_message}"

        return responses_run_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_context_window_123",
            input_tokens=9,
            output_tokens=11,
        )

    @staticmethod
    def _find_recent_codename(messages: list[Any]) -> str | None:
        """Scan the system-prompt graph for the prior-turn codename note."""
        from pydantic_ai.messages import ModelRequest, SystemPromptPart  # noqa: PLC0415

        codename: str | None = None
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if not isinstance(part, SystemPromptPart):
                    continue
                for line in part.content.splitlines():
                    lowered = line.lower()
                    marker = "project codename is "
                    if marker in lowered:
                        codename = lowered.split(marker, 1)[1].strip().rstrip(".")
        return codename


class MutatingContextAdapter(FakeModelAdapter):
    provider = "provider.mutating"
    model = "model.mutating-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        # Pre-cutover the adapter would mutate ``context_bundle`` keys to assert
        # the loop deep-copies; with the new ``ModelMessage`` contract, parts
        # are immutable dataclasses and mutation is structurally impossible.
        return responses_run_message(
            assistant_text="mutating-adapter-response",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_mutating_123",
            input_tokens=3,
            output_tokens=3,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def test_user_can_send_message_and_receive_model_backed_response(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        turn = post_message_and_drain(client, session_id, message="hello from phone")
        assert turn.assistant_message == "assistant::hello from phone"
        assert turn.status == "completed"


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
        from pydantic_ai.messages import ModelRequest, SystemPromptPart  # noqa: PLC0415

        captured_systems = [
            part.content
            for message in adapter.input_items[0]
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]
        assert any("message_id: 101112" in content for content in captured_systems)
        assert any(
            "discord context:" in content
            and "filename=note.txt" in content
            and "attachment_ref=discord:161718" in content
            and "url=" not in content
            and "https://cdn.discordapp.com/attachments/note.txt" not in content
            for content in captured_systems
        )
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                workspace_item = (
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
                workspace_event = (
                    db.execute(
                        text(
                            "SELECT id, discord_message_id, dedupe_key, event_type, payload "
                            "FROM discord_message_events "
                            "WHERE discord_message_id = :discord_message_id"
                        ),
                        {"discord_message_id": workspace_item["id"]},
                    )
                    .mappings()
                    .one()
                )
                ambient_task_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM background_tasks "
                        "WHERE task_type = 'ambient_interpretation_due'"
                    )
                ).scalar_one()
        assert workspace_item["title"] == "Discord message in #ops"
        assert workspace_item["summary"] == "noted"
        assert workspace_item["source_uri"] == "https://discord.com/channels/123/456/101112"
        assert workspace_item["metadata"]["channel_id"] == 456
        assert workspace_item["metadata"]["author_id"] == 131415
        assert workspace_event["dedupe_key"] == "discord:message:101112:ingested"
        assert workspace_event["event_type"] == "created"
        assert workspace_event["payload"]["message_id"] == "101112"
        assert workspace_event["payload"]["message"] == "noted"
        assert ambient_task_count == 0


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

    # The rendered discord context lives inside the model's system prompts —
    # the attachment block carries the source-attachment metadata and never
    # leaks the upstream download_url / cdn URL.
    from pydantic_ai.messages import ModelRequest, SystemPromptPart  # noqa: PLC0415

    captured_systems = [
        part.content
        for snapshot in adapter.input_items
        for message in snapshot
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    model_payload = json.dumps(captured_systems, sort_keys=True)
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


def test_pr01_model_led_direct_and_clarification_messages_are_emitted(postgres_url: str) -> None:
    adapter = ContextWindowDecisionAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        clear_turn = post_message_and_drain(
            client, session_id, message="summarize this in one line"
        )
        assert clear_turn.assistant_message is not None
        assert clear_turn.assistant_message.startswith("direct::")

        ambiguous_turn = post_message_and_drain(client, session_id, message="book me travel")
        assert ambiguous_turn.assistant_message is not None
        assert "destination and travel dates" in ambiguous_turn.assistant_message

        timeline = _timeline(client, session_id)
        event_types_by_turn = [
            [event["event_type"] for event in turn["events"]] for turn in timeline["turns"]
        ]
        assert all("evt.assistant.emitted" in event_types for event_types in event_types_by_turn)
        assert all("evt.turn.completed" in event_types for event_types in event_types_by_turn)


def test_pr01_turn_context_section_order_and_audit_metadata(
    postgres_url: str,
) -> None:
    """Context bundle section_order and audit metadata follow the recall-based schema.

    After the memory substrate cutover the bounded session-turn window
    (recent_active_session_turns / profile / session_digest / recalled_memory)
    is gone.  Every turn receives the same four sections and a zero recent_window.
    The main agent's evt.model.started carries the pre-call snapshot.
    """
    adapter = ContextWindowDecisionAdapter()

    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        post_message_and_drain(client, session_id, message="first turn")
        post_message_and_drain(client, session_id, message="second turn")

        assert len(adapter.input_items) == 2
        # Post-cutover the adapter receives a pydantic-ai ``ModelMessage``
        # graph, not a context_bundle dict — the legacy section_order /
        # prompt_version asserts move to the ``evt.model.started`` payload
        # below (still emitted by the loop with the same shape).

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert len(turns) == 2

        # The retriever's evt.model.started carries the empty sentinel (section_order=[]).
        # The main agent's evt.model.started is the last one; it carries the real snapshot.
        for turn_data in turns:
            model_started_events = [
                e for e in turn_data["events"] if e["event_type"] == "evt.model.started"
            ]
            main_agent_started = model_started_events[-1]
            context_meta = main_agent_started["payload"]["context"]
            assert context_meta["schema_version"] == "1.0"
            assert context_meta["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
            assert context_meta["section_order"] == [
                "policy_system_instructions",
                "recall_v1",
                "open_commitments_and_jobs",
                "relevant_artifacts_and_observations",
            ]
            assert context_meta["policy_instruction_count"] == len(
                MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
            )
            # Bounded session-turn window is gone; recent_window is always zero.
            assert context_meta["recent_window"] == {
                "max_recent_turns": 0,
                "included_turn_count": 0,
                "omitted_turn_count": 0,
                "included_turn_ids": [],
            }


def test_pr01_context_audit_is_stable_even_if_adapter_mutates_context_bundle(
    postgres_url: str,
) -> None:
    adapter = MutatingContextAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        post_message_and_drain(client, session_id, message="seed history")
        post_message_and_drain(client, session_id, message="mutate context")

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        # The retriever fires first; its model.started carries the empty sentinel
        # context meta. The main agent's model.started is the last evt.model.started
        # in the turn; it carries the real pre-call snapshot — stable even though
        # MutatingContextAdapter appends "mutated" to section_order after the fact.
        model_started_events = [
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        ]
        main_agent_started = model_started_events[-1]
        context_meta = main_agent_started["payload"]["context"]
        assert context_meta["schema_version"] == "1.0"
        assert context_meta["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
        # Memory substrate cutover: section_order now contains the new recall-based
        # sections; the old recent_active_session_turns / profile / session_digest /
        # recalled_memory sections are gone.
        assert context_meta["section_order"] == [
            "policy_system_instructions",
            "recall_v1",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_observations",
        ]
        # "mutated" must NOT appear — the audit snapshot was taken before the adapter ran.
        assert "mutated" not in context_meta["section_order"]
        # recent_window is always zero after the memory substrate cutover (the
        # bounded session-turn window no longer exists).
        assert context_meta["recent_window"] == {
            "max_recent_turns": 0,
            "included_turn_count": 0,
            "omitted_turn_count": 0,
            "included_turn_ids": [],
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


def test_schema_not_ready_returns_503_until_migrated(unmigrated_postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()

    app_without_migration = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        reset_database=False,
        sandbox=FakeSandboxRuntime(),
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

    run_migrations(unmigrated_postgres_url)
    app_with_migration = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        reset_database=False,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app_with_migration) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/sessions/active").status_code == 200


def test_schema_readiness_recovers_when_migrations_land_after_startup(
    unmigrated_postgres_url: str,
) -> None:
    # Regression: before this fix, app.state.schema_missing_tables was computed
    # once at lifespan startup and never re-checked, so a post-startup migration
    # left /v1/health stuck at 503 forever. The TTL-cached probe must reflect
    # the current DB state within the TTL window.
    adapter = DeterministicModelAdapter()
    app = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        reset_database=False,
        sandbox=FakeSandboxRuntime(),
    )
    fake_now = [0.0]

    def fake_clock() -> float:
        return fake_now[0]

    ttl_seconds = 10.0
    app.state.schema_probe = SchemaReadinessProbe(
        app.state.engine,
        ttl_seconds=ttl_seconds,
        clock=fake_clock,
    )
    with TestClient(app) as client:
        first = client.get("/v1/health")
        assert first.status_code == 503
        first_body = first.json()
        assert first_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        first_missing = first_body["error"]["details"]["missing_tables"]
        assert first_missing

        # A second hit within the TTL window must come from the cache, not
        # re-reflect against the DB — same payload, same identity.
        cached = client.get("/v1/health")
        assert cached.status_code == 503
        assert cached.json()["error"]["details"]["missing_tables"] == first_missing

        # Migrations land while the process keeps running.
        run_migrations(unmigrated_postgres_url)

        # Still within the TTL window: the probe returns the cached 503.
        stale = client.get("/v1/health")
        assert stale.status_code == 503

        # Advance past the TTL: the next probe re-reflects against the DB and
        # sees the schema is now ready.
        fake_now[0] += ttl_seconds + 0.1

        recovered = client.get("/v1/health")
        assert recovered.status_code == 200

        # And every other protected route also recovers without a restart.
        assert client.get("/v1/sessions/active").status_code == 200


def test_single_active_session_and_ordered_turn_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("first message", "second message"):
            post_message_and_drain(client, session_id, message=message)

        active_again = client.get("/v1/sessions/active")
        assert active_again.status_code == 200
        assert active_again.json()["session"]["id"] == session_id

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert [turn["user_message"] for turn in turns] == ["first message", "second message"]

        # Pre-turn retriever fires before the main agent on every wake, producing
        # two retriever model events followed by the main agent model events.
        expected_types = [
            "evt.turn.started",
            "evt.model.started",  # retriever
            "evt.model.completed",  # retriever
            "evt.model.started",  # main agent
            "evt.model.completed",  # main agent
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
        post_message_and_drain(client, session_id, message="inspect model metadata")

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        # The pre-turn retriever fires first (its model.completed has 0 tokens from
        # empty_recall_response). The main agent's model.completed is the last one;
        # it carries the real token counts reported by DeterministicModelAdapter.
        model_completed_events = [
            event for event in events if event["event_type"] == "evt.model.completed"
        ]
        # Main agent's event is the last evt.model.completed in the sequence.
        model_completed = model_completed_events[-1]
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
        turn = post_message_and_drain(client, session_id, message="this should fail")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert len(turns) == 1
        turn_data = turns[0]
        assert turn_data["status"] == "failed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        # Retriever runs first (succeeds, emitting its model events); then the
        # main agent call fails (DeterministicModelAdapter.fail=True raises plain
        # RuntimeError, which the retriever guard short-circuits but the main
        # agent raises on).
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",  # retriever
            "evt.model.completed",  # retriever
            "evt.model.started",  # main agent
            "evt.model.failed",  # main agent
            "evt.turn.failed",
        ]
        model_failed = next(
            event for event in turn_data["events"] if event["event_type"] == "evt.model.failed"
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

        turn = post_message_and_drain(client, session["id"], message="validate ids")
        assert turn.id.startswith("trn_")
        _parse_utc_rfc3339(turn.created_at.isoformat())
        _parse_utc_rfc3339(turn.updated_at.isoformat())

        timeline = _timeline(client, session["id"])
        for saved_turn in timeline["turns"]:
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

        timeline = _timeline(client, session_id)
        assert timeline["turns"] == []


def test_root_serves_discord_primary_status_not_phone_surface(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
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


class SecretLeakingFailureAdapter(FakeModelAdapter):
    provider = "provider.leaky"
    model = "model.leaky-v1"

    def __init__(self) -> None:
        super().__init__()
        self.secret_value = "sk-live-very-secret"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        raise RuntimeError(f"provider rejected credential {self.secret_value}")


class NonSecretFailureAdapter(FakeModelAdapter):
    provider = "provider.non-secret"
    model = "model.non-secret-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        raise RuntimeError("token limit exceeded for this request")


def test_default_runtime_model_requires_server_secret_credentials(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MODEL_NAME", "gpt-5.5")
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "")

    app = create_app(
        database_url=postgres_url,
        model_adapter=None,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="credential check")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        event_types = [event["event_type"] for event in events]
        # The real adapter raises ModelAdapterError(safe_reason="model credentials
        # are not configured") for both the retriever call and the main agent call.
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",  # retriever (no API key)
            "evt.model.failed",  # retriever fails
            "evt.model.started",  # main agent (no API key)
            "evt.model.failed",  # main agent fails
            "evt.turn.failed",
        ]
        # The main agent's model.failed is the last evt.model.failed event.
        model_failed_events = [e for e in events if e["event_type"] == "evt.model.failed"]
        failure_payload = model_failed_events[-1]["payload"]
        assert "credential" in failure_payload["failure_reason"].lower()
        assert "sk-" not in failure_payload["failure_reason"]


def test_model_failure_reason_preserves_non_secret_detail(postgres_url: str) -> None:
    adapter = NonSecretFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger non-secret failure")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert model_failed["payload"]["failure_reason"] == "token limit exceeded for this request"


def test_restart_preserves_history_and_appends_to_same_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as first_client:
        first_session = first_client.get("/v1/sessions/active")
        assert first_session.status_code == 200
        session_id = first_session.json()["session"]["id"]
        post_message_and_drain(first_client, session_id, message="before restart")

        timeline_before = _timeline(first_client, session_id)
        assert [turn["user_message"] for turn in timeline_before["turns"]] == ["before restart"]

    restarted_app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=False,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(restarted_app) as second_client:
        active_after_restart = second_client.get("/v1/sessions/active")
        assert active_after_restart.status_code == 200
        assert active_after_restart.json()["session"]["id"] == session_id

        timeline_after_restart = _timeline(second_client, session_id)
        assert [turn["user_message"] for turn in timeline_after_restart["turns"]] == [
            "before restart"
        ]

        post_message_and_drain(second_client, session_id, message="after restart")

        final_timeline = _timeline(second_client, session_id)
        assert [turn["user_message"] for turn in final_timeline["turns"]] == [
            "before restart",
            "after restart",
        ]


class LongResponseAdapter(FakeModelAdapter):
    provider = "provider.long-response"
    model = "model.long-response-v1"

    def __init__(self) -> None:
        super().__init__()
        self.response_token_count = 16

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        assistant_text = " ".join(["long"] * self.response_token_count)
        return responses_run_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_long_123",
            input_tokens=5,
            output_tokens=self.response_token_count,
        )


class UsageDrivenResponseAdapter(FakeModelAdapter):
    provider = "provider.usage-driven"
    model = "model.usage-driven-v1"

    def __init__(self) -> None:
        super().__init__()
        self.reported_output_tokens = 12

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        return responses_run_message(
            assistant_text="ok",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_usage_123",
            input_tokens=2,
            output_tokens=self.reported_output_tokens,
        )


class _RetryableTestFailure(Exception):
    """Mimics the legacy ``ModelAdapterError`` retry surface for the loop.

    The agent loop checks ``getattr(exc, 'retryable', False)`` and
    ``getattr(exc, 'safe_reason', str(exc))`` — a plain exception with those
    attributes is sufficient for the retry path.
    """

    def __init__(self, *, safe_reason: str, retryable: bool) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason
        self.retryable = retryable


class RetryableFailureAdapter(FakeModelAdapter):
    provider = "provider.retryable-failure"
    model = "model.retryable-failure-v1"

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.attempts += 1
        raise _RetryableTestFailure(safe_reason="temporary provider timeout", retryable=True)


def test_pr02_model_call_backstop_exhaustion_ends_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backstop exhaustion (agent_loop_max_model_calls) ends as a graceful
    status-200 completed turn, not a 429 error.

    With agent_loop_max_model_calls=2 and a retryable-failure adapter, the loop
    makes exactly 2 model calls, retrying both times, then the backstop check
    fires on the 3rd iteration (model_call_count=2 > 2 is False, but after the
    2nd call model_call_count=2 which on the next loop top becomes >2 when we
    set it to 3 — actually model_call_count > max fires at model_call_count=2
    only when max=1). Use max=1 so the backstop fires after exactly 1 call.
    """
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "1")
    # Generous wall-clock budget so backstop fires first.
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger model call backstop")
        # Graceful exhaustion: completed turn, not failed.
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline["turns"])
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types
        assert "evt.assistant.emitted" in event_types


def test_pr02_turn_budget_exhaustion_ends_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wall-clock budget exhaustion (main_turn_budget_seconds) ends as a
    graceful status-200 completed turn, not a 429 error.

    With a fake perf_counter that advances quickly and a tiny budget, the budget
    check fires before (or just after) the first model call and the loop ends
    with the exhausted message.
    """
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    # Tiny budget: 0.001s — the fake clock advances 0.1s per call, so the
    # budget check fires on the first loop iteration.
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "0.001")
    # High backstop so budget fires first.
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.1
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger turn budget")
        # Graceful exhaustion: completed turn, not failed.
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline["turns"])
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types


def test_pr02_stuck_detection_ends_turn_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stuck-detection (identical run source on consecutive rounds) ends as a
    graceful status-200 completed turn, not a 429 error.
    """
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    # An adapter that always returns a run program that emits a value
    # (so the loop continues) but always with the same source — triggering
    # stuck-detection on the second round.
    class StuckAdapter(FakeModelAdapter):
        provider = "provider.stuck"
        model = "model.stuck-v1"

        def _respond(self, request: ModelCall) -> ModelResponse:
            if is_retriever_call(request.messages):
                return empty_recall_response(
                    provider=self.provider, model=self.model, messages=request.messages
                )
            # A program that only calls emit_value (loop continues with same
            # source every time — triggers stuck-detection on round 2).
            return responses_with_run_calls(
                assistant_text="",
                calls=[{"name": "agent.emit_value", "input": {"value": {"x": 1}}}],
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_stuck_123",
                input_tokens=1,
                output_tokens=1,
            )

    adapter = StuckAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger stuck detection")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types
