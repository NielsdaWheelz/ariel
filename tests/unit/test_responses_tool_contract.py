from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from ariel.action_runtime import (
    RuntimeProvenance,
    _FunctionCallProcessingContext,
    process_one_call,
)
from ariel.capability_registry import (
    RUN_CALLABLE_SIGNATURES,
    capability_id_for_run_callable,
    eligible_internal_callable_capability_ids,
    get_capability,
    internal_callable_capability_ids,
    run_callable_name_for_capability_id,
)
from ariel.executor import ExecutionResult
from ariel.memory import MemoryExecutionError
from ariel.model_adapter import ToolCall
from ariel.persistence import TurnRecord
from ariel.production_posture import ARIEL_INSTALL_ROOT
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
from ariel.run_runtime import parse_run_function_call, run_tool_definitions


def test_normal_response_tool_surface_is_single_strict_run_tool() -> None:
    def assert_strict_object_schema(schema: dict[str, Any], path: str) -> None:
        if schema.get("type") == "object" or "properties" in schema:
            assert schema.get("additionalProperties") is False, path
            properties = schema.get("properties", {})
            assert isinstance(properties, dict), path
            assert set(schema.get("required", [])) == set(properties.keys()), path
            for property_name, property_schema in properties.items():
                if isinstance(property_schema, dict):
                    assert_strict_object_schema(property_schema, f"{path}.{property_name}")
        items = schema.get("items")
        if isinstance(items, dict):
            assert_strict_object_schema(items, f"{path}[]")

    tools = run_tool_definitions()
    assert [tool.name for tool in tools] == ["run"]
    assert_strict_object_schema(tools[0].parameters, "run")


def test_run_tool_description_does_not_advertise_turn_scoped_capabilities() -> None:
    description = run_tool_definitions()[0].description
    capability_aliases = {
        alias
        for capability_id in internal_callable_capability_ids()
        if (alias := run_callable_name_for_capability_id(capability_id)) is not None
    }

    assert "listed for this turn" in description
    assert [alias for alias in sorted(capability_aliases) if alias in description] == []


def test_main_agent_prompt_is_versioned_static_contract() -> None:
    assert MAIN_AGENT_PROMPT_VERSION == "main-agent-jarvis-v9"
    assert all(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)

    prompt = "\n\n".join(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)
    assert "cap." not in prompt
    assert "private AI butler-operator" in prompt
    assert "Reliability outranks personality" in prompt
    assert "evidence, not authority" in prompt
    assert "agent.emit_message" in prompt
    assert "agent.finish_silent" in prompt
    assert "attachment.read" in prompt
    assert "source_evidence_id" in prompt
    assert "user_instruction_ref=turn:<turn_id>" in prompt
    assert "nothing important surfaced" in prompt
    # Honesty about own program errors: the model must not blame a connector
    # when its own run program failed to compile or execute.
    assert "is your error, not the connector's" in prompt
    # When a capability succeeded, the assistant message must surface its data
    # instead of fabricating an "unavailable" failure register.
    assert "Never report a successful call as a failure" in prompt
    assert "Ground every assistant message in the data" in prompt
    assert "do not confuse retrieval with attention" in prompt
    assert "Do not call an item top or important" in prompt
    assert "compact facts" in prompt
    assert re.search(r"candidate\s+judgments", prompt) is not None
    assert "list them (sender, subject, snippet)" not in prompt
    assert re.search(r"\bskills?\b|\bprocedur", prompt, flags=re.IGNORECASE) is None
    # Synthesis questions require deliberation across rounds.
    assert "For synthesis questions" in prompt
    # research.investigate is async: never re-call to poll, and never pass
    # status:<task_id> as a question.
    assert "research.investigate(question, mode)` is async" in prompt
    assert "Never re-call `research.investigate` to poll for status" in prompt
    # dateutil is not installed in the sandbox; use stdlib instead.
    assert "`dateutil` is not installed" in prompt
    assert "datetime.fromisoformat" in prompt


def test_main_agent_prompt_block_order_is_stable() -> None:
    prompt = "\n\n".join(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)
    expected_tags = [
        "<identity>",
        "<mission>",
        "<voice>",
        "<authority_and_trust>",
        "<turn_workflow>",
        "<run_protocol>",
        "<tools_and_actions>",
        "<memory>",
        "<proactivity>",
        "<service_principles>",
        "<communication>",
        "<failure_handling>",
        "<safety_overrides>",
        "<exemplars>",
        "<self_check>",
    ]

    positions = [prompt.index(tag) for tag in expected_tags]
    assert positions == sorted(positions)


def _run_call(arguments: dict[str, Any], *, name: str = "run") -> ToolCall:
    return ToolCall(call_id="call_test", name=name, arguments=arguments)


def test_run_protocol_requires_exactly_one_run_call() -> None:
    assert parse_run_function_call([]) == (
        None,
        "run_protocol_requires_exactly_one_tool_call",
    )
    assert parse_run_function_call([_run_call({}), _run_call({})]) == (
        None,
        "run_protocol_requires_exactly_one_tool_call",
    )
    assert parse_run_function_call([_run_call({}, name="not_the_run_tool")]) == (
        None,
        "run_protocol_requires_run_tool",
    )


def test_run_callable_aliases_are_unique_and_deliberate() -> None:
    aliases: dict[str, str] = {}
    for capability_id in internal_callable_capability_ids():
        alias = run_callable_name_for_capability_id(capability_id)
        assert alias is not None, capability_id
        assert not alias.startswith("cap.")
        assert alias not in aliases, alias
        aliases[alias] = capability_id
        assert capability_id_for_run_callable(alias) == capability_id

    assert capability_id_for_run_callable("discord.no_response") is None
    assert capability_id_for_run_callable("memory.forget_all") is None


def test_every_run_callable_alias_has_model_facing_signature() -> None:
    missing: list[tuple[str, str]] = []
    for capability_id in internal_callable_capability_ids():
        alias = run_callable_name_for_capability_id(capability_id)
        assert alias is not None, capability_id
        if not RUN_CALLABLE_SIGNATURES.get(alias, "").strip():
            missing.append((alias, capability_id))

    assert missing == []


def test_run_callable_signatures_match_validators_for_common_capabilities() -> None:
    """Each high-traffic capability's signature string must list every key its
    input validator accepts, by exact name. The model sees this signature both
    in the per-turn callable list and (on schema_invalid) as the ``expected``
    field of the rejection payload — those are the only sources from which it
    learns the right keyword names. Drift between signature and validator makes
    the model invent argument names and waste rounds on rejected calls.

    Each entry pairs a syscall alias with (a) the set of arg names that must
    appear in its signature string, and (b) a single payload that the
    validator must accept. Both halves are asserted: the signature is what
    the model reads; the payload-acceptance check confirms the documented
    shape is the one the validator implements.
    """

    cases: list[tuple[str, str, set[str], dict[str, Any]]] = [
        (
            "search.web",
            "cap.search.web",
            {"query"},
            {"query": "best espresso machines 2026"},
        ),
        (
            "web.extract",
            "cap.web.extract",
            {"url"},
            {"url": "https://example.com/article"},
        ),
        (
            "calendar.list",
            "cap.calendar.list",
            {"window_start", "window_end"},
            {
                "window_start": "2026-05-20T00:00:00Z",
                "window_end": "2026-05-21T00:00:00Z",
            },
        ),
        (
            "calendar.list_calendars",
            "cap.calendar.list_calendars",
            set(),
            {},
        ),
        (
            "calendar.propose_slots",
            "cap.calendar.propose_slots",
            {
                "window_start",
                "window_end",
                "duration_minutes",
                "attendees",
                "timezone",
                "source_evidence_ids",
                "quoted_content_caveat",
                "participants",
                "proposed_windows",
                "timezone_evidence",
                "constraints",
            },
            {
                "window_start": "2026-05-20T00:00:00Z",
                "window_end": "2026-05-21T00:00:00Z",
                "duration_minutes": 30,
                "attendees": ["niels@example.com"],
                "timezone": "UTC",
                "source_evidence_ids": [],
                "quoted_content_caveat": False,
                "participants": ["Niels"],
                "proposed_windows": [],
                "timezone_evidence": {
                    "source": None,
                    "rationale": None,
                    "confidence": None,
                },
                "constraints": {
                    "hard": [],
                    "soft": [],
                    "attendee_notes": [],
                },
            },
        ),
        (
            "calendar.create_event",
            "cap.calendar.create_event",
            {
                "title",
                "start_time",
                "end_time",
                "idempotency_key",
                "user_instruction_ref",
            },
            {
                "title": "Manual smoke hold",
                "start_time": "2026-05-20T09:00:00Z",
                "end_time": "2026-05-20T09:30:00Z",
                "attendees": [],
                "idempotency_key": "smoke-cal-create",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "calendar.update_event",
            "cap.calendar.update_event",
            {"event_id", "title", "idempotency_key", "user_instruction_ref"},
            {
                "event_id": "evt_123",
                "title": "Manual smoke hold, updated",
                "idempotency_key": "smoke-cal-update",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "calendar.respond_to_event",
            "cap.calendar.respond_to_event",
            {
                "event_id",
                "attendee_email",
                "response_status",
                "idempotency_key",
                "user_instruction_ref",
            },
            {
                "event_id": "evt_123",
                "attendee_email": "niels@example.com",
                "response_status": "tentative",
                "idempotency_key": "smoke-cal-respond",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.search",
            "cap.email.search",
            {"query"},
            {"query": "from:stripe@example.com newer_than:7d"},
        ),
        (
            "email.read",
            "cap.email.read",
            {"message_id", "thread_id", "mode"},
            {"message_id": "msg_123", "thread_id": "thr_456", "mode": "message"},
        ),
        (
            "drive.search",
            "cap.drive.search",
            {"query"},
            {"query": "Q2 retainer"},
        ),
        (
            "drive.read",
            "cap.drive.read",
            {"file_id"},
            {"file_id": "1AbCdEf"},
        ),
        (
            "drive.share",
            "cap.drive.share",
            {
                "file_id",
                "grantee_email",
                "role",
                "idempotency_key",
                "user_instruction_ref",
            },
            {
                "file_id": "1AbCdEf",
                "grantee_email": "niels@example.com",
                "role": "reader",
                "idempotency_key": "smoke-drive-share",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.draft",
            "cap.email.draft",
            {"to", "subject", "body", "idempotency_key", "user_instruction_ref"},
            {
                "to": ["niels@example.com"],
                "subject": "Manual smoke draft",
                "body": "Draft body",
                "idempotency_key": "smoke-email-draft",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.send",
            "cap.email.send",
            {"to", "subject", "body", "idempotency_key", "user_instruction_ref"},
            {
                "to": ["niels@example.com"],
                "subject": "Manual smoke send",
                "body": "Send body",
                "idempotency_key": "smoke-email-send",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.archive",
            "cap.email.archive",
            {"message_ids", "idempotency_key", "user_instruction_ref"},
            {
                "message_ids": ["msg_123"],
                "idempotency_key": "smoke-email-archive",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.trash",
            "cap.email.trash",
            {"message_ids", "idempotency_key", "user_instruction_ref"},
            {
                "message_ids": ["msg_123"],
                "idempotency_key": "smoke-email-trash",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.labels.modify",
            "cap.email.labels.modify",
            {
                "message_ids",
                "add_labels",
                "remove_labels",
                "idempotency_key",
                "user_instruction_ref",
            },
            {
                "message_ids": ["msg_123"],
                "add_labels": ["ManualSmoke"],
                "remove_labels": [],
                "idempotency_key": "smoke-email-labels",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "email.undo",
            "cap.email.undo",
            {"undo_token", "idempotency_key", "user_instruction_ref"},
            {
                "undo_token": "undo_123",
                "idempotency_key": "smoke-email-undo",
                "user_instruction_ref": "turn:test",
            },
        ),
        (
            "maps.search_places",
            "cap.maps.search_places",
            {"query", "location_context", "radius_meters"},
            {"query": "coffee", "location_context": "Berlin", "radius_meters": 2000},
        ),
        (
            "maps.directions",
            "cap.maps.directions",
            {"origin", "destination", "travel_mode", "waypoints", "optimize_order"},
            {
                "origin": "Brandenburger Tor",
                "destination": "Berliner Hauptbahnhof",
                "travel_mode": "walking",
                "waypoints": [],
                "optimize_order": False,
            },
        ),
        (
            "weather.forecast",
            "cap.weather.forecast",
            {"location", "timeframe"},
            {"location": "Berlin", "timeframe": "today"},
        ),
        (
            "attachment.read",
            "cap.attachment.read",
            {"attachment_ref", "intent"},
            {"attachment_ref": "discord:777", "intent": "summarize"},
        ),
        (
            "proactive.schedule",
            "cap.proactive.schedule",
            {"when", "note"},
            {"when": "2026-05-21T09:00:00Z", "note": "remind about offsite"},
        ),
        (
            "memory.recall",
            "cap.memory.recall",
            {"query"},
            {"query": "project phoenix"},
        ),
        (
            "memory.remember",
            "cap.memory.remember",
            {"note"},
            {"note": "the user prefers tea"},
        ),
        (
            "memory.search",
            "cap.memory.search",
            {"query", "limit", "since", "kinds"},
            {
                "query": "career meeting",
                "limit": 3,
                "since": "2026-05-20T12:00:00Z",
                "kinds": ["user_message", "note_create"],
            },
        ),
        (
            "memory.read",
            "cap.memory.read",
            {"id"},
            {"id": "mem_123"},
        ),
        (
            "memory.note.create",
            "cap.memory.note.create",
            {"content"},
            {"content": "new note"},
        ),
        (
            "memory.note.edit",
            "cap.memory.note.edit",
            {"id", "content"},
            {"id": "note_123", "content": "updated note"},
        ),
        (
            "memory.note.delete",
            "cap.memory.note.delete",
            {"id"},
            {"id": "note_123"},
        ),
        (
            "research.investigate",
            "cap.research.investigate",
            {"question", "mode"},
            {"question": "What is the state of fusion in 2026?", "mode": "web"},
        ),
        (
            "agency.run",
            "cap.agency.run",
            {
                "repo_root",
                "name",
                "prompt",
                "base_branch",
                "runner",
                "runner_args",
                "env",
                "no_include_untracked",
            },
            {
                "repo_root": ARIEL_INSTALL_ROOT,
                "name": "Manual smoke survey",
                "prompt": "Inspect only.",
                "base_branch": "main",
                "runner": "codex",
                "runner_args": [],
                "env": [],
                "no_include_untracked": True,
            },
        ),
        (
            "agency.status",
            "cap.agency.status",
            {"job_id", "repo_id", "task_id"},
            {"job_id": "job_123"},
        ),
        (
            "agency.artifacts",
            "cap.agency.artifacts",
            {"job_id", "repo_id", "task_id"},
            {"job_id": "job_123"},
        ),
        (
            "agency.request_pr",
            "cap.agency.request_pr",
            {
                "job_id",
                "repo_id",
                "task_id",
                "invocation_id",
                "worktree_id",
            },
            {"job_id": "job_123"},
        ),
    ]

    for alias, capability_id, required_names, payload in cases:
        signature = RUN_CALLABLE_SIGNATURES[alias]
        for name in required_names:
            assert name in signature, (
                f"signature for {alias} is missing the {name!r} arg name; "
                f"the model needs to see it to pass the validator. signature={signature!r}"
            )
        capability = get_capability(capability_id)
        assert capability is not None, capability_id
        normalized, error = capability.validate_input(payload)
        assert error is None, (
            f"validator for {capability_id} rejected the payload documented in "
            f"its signature ({payload!r}); signature/validator have drifted. "
            f"signature={signature!r} error={error!r}"
        )
        assert normalized is not None


def test_memory_search_validator_defaults_match_signature() -> None:
    capability = get_capability("cap.memory.search")
    assert capability is not None

    normalized, error = capability.validate_input({"query": "  career meeting  "})

    assert error is None
    assert normalized == {
        "query": "career meeting",
        "limit": 24,
        "since": None,
        "kinds": None,
    }


def test_agency_run_validator_accepts_its_normalized_payload_for_approval_replay() -> None:
    capability = get_capability("cap.agency.run")
    assert capability is not None

    normalized, error = capability.validate_input(
        {
            "repo_root": ARIEL_INSTALL_ROOT,
            "name": "Replayable Agency run",
            "prompt": "Inspect only.",
            "base_branch": " main ",
            "runner": " codex ",
            "runner_args": [" --sandbox=workspace-write "],
            "env": [{"name": " SMOKE_FLAG ", "value": " enabled "}],
            "no_include_untracked": True,
        }
    )
    assert error is None
    assert normalized is not None

    replayed, replay_error = capability.validate_input(normalized)

    assert replay_error is None
    assert replayed == normalized
    assert capability.validate_input(
        {
            "repo_root": ARIEL_INSTALL_ROOT,
            "name": "Replayable Agency run",
            "prompt": "Inspect only.",
            "env": {"SMOKE_FLAG": "enabled"},
        }
    ) == (None, "schema_invalid")


def test_run_callable_signatures_warn_about_email_read_invented_nulls() -> None:
    """email.read fails when both ids are null. The signature must spell out
    that the model has to fill at least one with a value from a prior
    ``email.search`` result; an all-null identifier payload is always
    ``schema_invalid``."""

    signature = RUN_CALLABLE_SIGNATURES["email.read"]
    assert "non-null" in signature.lower() or "from a prior" in signature.lower(), signature

    capability = get_capability("cap.email.read")
    assert capability is not None
    _, error = capability.validate_input({"message_id": None, "thread_id": None, "mode": "message"})
    assert error == "schema_invalid"


def test_web_extract_signature_names_exact_runtime_output_shape() -> None:
    signature = RUN_CALLABLE_SIGNATURES["web.extract"]

    assert "..." not in signature
    for field_name in {
        "url",
        "status",
        "extract_outcome",
        "reason_code",
        "recovery",
        "document",
        "canonical_source",
        "resolved_url",
        "retrieved_at",
        "published_at",
        "language",
        "content_chars",
        "content_blocks",
        "provider",
        "endpoint",
        "attempt_count",
    }:
        assert field_name in signature


def test_memory_remember_signature_names_runtime_output_shape() -> None:
    signature = RUN_CALLABLE_SIGNATURES["memory.remember"]

    assert "'status': 'queued'" in signature
    assert "encode_id" in signature
    assert "task_id" not in signature


def test_memory_and_scheduler_signatures_name_runtime_outputs() -> None:
    assert "'status': 'recalled'" in RUN_CALLABLE_SIGNATURES["memory.recall"]
    assert "recall" in RUN_CALLABLE_SIGNATURES["memory.recall"]

    memory_read_signature = RUN_CALLABLE_SIGNATURES["memory.read"]
    for field_name in {
        "'status'",
        "'found'",
        "'not_found'",
        "turn_id",
        "taint",
    }:
        assert field_name in memory_read_signature

    assert "'created'" in RUN_CALLABLE_SIGNATURES["memory.note.create"]
    assert "'edited'" in RUN_CALLABLE_SIGNATURES["memory.note.edit"]
    assert "'deleted'" in RUN_CALLABLE_SIGNATURES["memory.note.delete"]
    assert "run_after" in RUN_CALLABLE_SIGNATURES["proactive.schedule"]


def test_sandbox_level_syscall_signatures_match_runtime_callbacks() -> None:
    assert RUN_CALLABLE_SIGNATURES["agent.finish_silent"] == "(reason: str = '') -> None"
    assert RUN_CALLABLE_SIGNATURES["agent.emit_done"] == "(summary: str = '') -> None"
    assert RUN_CALLABLE_SIGNATURES["scratch.set"] == "(key: str, value: JSONValue) -> None"
    assert RUN_CALLABLE_SIGNATURES["scratch.get"] == "(key: str) -> JSONValue"


def test_drive_contract_metadata_uses_google_drive_schema_family() -> None:
    drive_search = get_capability("cap.drive.search")
    drive_read = get_capability("cap.drive.read")

    assert drive_search is not None
    assert drive_read is not None
    assert drive_search.contract_metadata["output_schema"] == "google_drive_search_results_v1"
    assert drive_read.contract_metadata["output_schema"] == "google_drive_read_result_v1"


def test_drive_read_signature_is_read_native_not_search_style() -> None:
    signature = RUN_CALLABLE_SIGNATURES["drive.read"]

    assert "results" not in signature
    for field_name in {
        "title",
        "source",
        "published_at",
        "content_excerpt",
        "read_outcome",
        "reason_code",
        "recovery",
    }:
        assert field_name in signature


def test_agency_capabilities_become_eligible_when_runtime_is_bound() -> None:
    """The agency.* family is gated by ``runtime_bindings.agency`` after the app
    has proven configured repo roots plus a reachable daemon. When the binding
    flips on, all four agency caps become eligible. When off, none are."""

    on = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {"agency": True},
            }
        )
    )
    assert {
        "cap.agency.run",
        "cap.agency.status",
        "cap.agency.artifacts",
        "cap.agency.request_pr",
    }.issubset(on)

    off = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {"agency": False},
            }
        )
    )
    assert not any(cap_id.startswith("cap.agency.") for cap_id in off)


def test_agency_request_pr_does_not_expose_operator_override_knobs() -> None:
    signature = RUN_CALLABLE_SIGNATURES["agency.request_pr"]
    assert "allow_dirty" not in signature
    assert "force_with_lease" not in signature

    capability = get_capability("cap.agency.request_pr")
    assert capability is not None
    assert capability.validate_input({"job_id": "job_123", "allow_dirty": True}) == (
        None,
        "schema_invalid",
    )
    assert capability.validate_input({"job_id": "job_123", "force_with_lease": True}) == (
        None,
        "schema_invalid",
    )


def test_google_capabilities_require_connection_and_granted_scopes() -> None:
    missing_scope = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "google": {
                    "connected": True,
                    "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                },
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {},
            }
        )
    )
    assert "cap.email.read" in missing_scope
    assert "cap.email.send" not in missing_scope

    disconnected = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "google": {
                    "connected": False,
                    "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                },
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {},
            }
        )
    )
    assert "cap.email.read" not in disconnected


def test_attachment_capability_requires_attachment_reference() -> None:
    without_attachment = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {},
            }
        )
    )
    with_attachment = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 1},
                "runtime_bindings": {},
            }
        )
    )

    assert "cap.attachment.read" not in without_attachment
    assert "cap.attachment.read" in with_attachment


def test_provider_capabilities_follow_runtime_bindings() -> None:
    off = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {
                    "web_extract": False,
                    "search_web": False,
                    "maps": False,
                    "weather": False,
                },
            }
        )
    )
    on = set(
        eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {
                    "web_extract": True,
                    "search_web": True,
                    "maps": True,
                    "weather": True,
                },
            }
        )
    )

    gated_capabilities = {
        "cap.web.extract",
        "cap.search.web",
        "cap.maps.directions",
        "cap.maps.search_places",
        "cap.weather.forecast",
    }
    assert gated_capabilities.isdisjoint(off)
    assert gated_capabilities.issubset(on)


def test_run_source_is_a_python_program_string() -> None:
    """A valid run call carries a Python-program source string; it is not parsed
    as a flat-JSON call list. ``parse_run_function_call`` validates only the
    tool-call envelope and the source-size budget; the program itself runs in
    the sandbox."""

    source = (
        "results = memory.recall(query='project phoenix')\n"
        "agent.emit_message(text='Found ' + str(len(results['facts'])) + ' memories.')\n"
    )
    parsed_source, error = parse_run_function_call([_run_call({"source": source})])
    assert error is None
    assert parsed_source == source.strip()


def test_run_source_rejects_blank_and_oversized_programs() -> None:
    assert parse_run_function_call([_run_call({"source": "   "})]) == (
        None,
        "run_source_empty",
    )
    assert parse_run_function_call([_run_call({"source": "x" * 20001})]) == (
        None,
        "run_source_too_large",
    )


def test_action_runtime_has_no_deterministic_tool_result_synthesizer() -> None:
    source = (Path(__file__).parents[2] / "src/ariel/action_runtime.py").read_text()

    assert "_synthesize_" not in source
    assert "build_assistant_action_appendix" not in source
    assert "attachment content:" not in source


def _run_one_call(
    *,
    db: Session,
    function_call_raw: dict[str, Any],
    turn: TurnRecord,
    now: datetime,
    new_id_fn: Any,
    add_event: Any,
    runtime_provenance: RuntimeProvenance | None,
    attachment_runtime: Any | None = None,
    session_factory: Any | None = None,
    settings: Any | None = None,
    sandbox: Any | None = None,
    model_adapter: Any | None = None,
    allowed_capability_ids: set[str],
) -> _FunctionCallProcessingContext:
    """Run one capability syscall through ``process_one_call``.

    This is the per-call lifecycle a ``run`` program's syscalls dispatch
    through; the run-program host path drives the same function. The tests use
    it directly to assert the per-call rails (turn scope, execution, taint).
    """

    ctx = _FunctionCallProcessingContext()
    process_one_call(
        ctx=ctx,
        function_call_index=1,
        function_call_raw=function_call_raw,
        db=db,
        session_factory=session_factory,
        turn=turn,
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=add_event,
        now_fn=lambda: now,
        new_id_fn=new_id_fn,
        runtime_provenance=runtime_provenance,
        google_runtime=None,
        execute_google_reads_outside_transaction=False,
        agency_runtime=None,
        attachment_runtime=attachment_runtime,
        allowed_capability_id_set=allowed_capability_ids,
        settings=settings,
        sandbox=sandbox,
        model_adapter=model_adapter,
    )
    return ctx


def test_process_one_call_default_denies_without_turn_scope() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    events: list[tuple[str, dict[str, Any]]] = []

    class Db:
        def add(self, record: Any) -> None:
            raise AssertionError(f"unscoped tool created a record: {record!r}")

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    turn = TurnRecord(
        id="trn_1",
        user_message="quiet",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.unscoped.no_response",
            "input": {"reason": "nothing useful to add"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        runtime_provenance=RuntimeProvenance(status="clean"),
        allowed_capability_ids=set(),
    )

    assert ctx.created_action_attempts == []
    function_call_output = ctx.function_call_outputs[0]
    assert function_call_output["type"] == "function_call_output"
    assert function_call_output["call_id"] == "call_1"
    assert json.loads(function_call_output["output"]) == {
        "status": "denied",
        "capability_id": "cap.unscoped.no_response",
        "error": "tool_not_in_turn_scope",
    }
    assert events == [
        (
            "evt.action.call_denied",
            {
                "call_index": 1,
                "call_id": "call_1",
                "tool_name": "cap.unscoped.no_response",
                "capability_id": "cap.unscoped.no_response",
                "reason": "tool_not_in_turn_scope",
            },
        )
    ]


def test_process_one_call_denies_unscoped_tools() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    class Db:
        def add(self, record: Any) -> None:
            raise AssertionError(f"unscoped tool created a record: {record!r}")

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    turn = TurnRecord(
        id="trn_1",
        user_message="echo",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.unscoped.no_response",
            "input": {"reason": "nothing useful to add"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        add_event=lambda _event_type, _payload: None,
        runtime_provenance=None,
        allowed_capability_ids=set(),
    )

    assert ctx.created_action_attempts == []
    assert json.loads(ctx.function_call_outputs[0]["output"]) == {
        "status": "denied",
        "capability_id": "cap.unscoped.no_response",
        "error": "tool_not_in_turn_scope",
    }


def test_process_one_call_maps_expected_memory_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    events: list[tuple[str, dict[str, Any]]] = []

    class Db:
        def add(self, record: Any) -> None:
            return None

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    def fail_memory(**_: Any) -> dict[str, Any]:
        raise MemoryExecutionError("memory_recall_model_failed")

    monkeypatch.setattr("ariel.action_runtime._execute_memory_capability", fail_memory)
    turn = TurnRecord(
        id="trn_1",
        user_message="search memory",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.memory.search",
            "input": {"query": "project phoenix"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        runtime_provenance=RuntimeProvenance(status="clean"),
        session_factory=object(),
        settings=object(),
        allowed_capability_ids={"cap.memory.search"},
    )

    attempt = ctx.created_action_attempts[0]
    assert attempt.status == "failed"
    assert attempt.execution_error == "memory_recall_model_failed"
    assert json.loads(ctx.function_call_outputs[0]["output"]) == {
        "status": "failed",
        "capability_id": "cap.memory.search",
        "error": "memory_recall_model_failed",
    }
    assert events[-1] == (
        "evt.action.execution.failed",
        {
            "action_attempt_id": "aat_1",
            "error": "memory_recall_model_failed",
            "output": None,
        },
    )


def test_process_one_call_propagates_unexpected_memory_execution_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    class Db:
        def add(self, record: Any) -> None:
            return None

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    def fail_memory(**_: Any) -> dict[str, Any]:
        raise RuntimeError("memory substrate defect")

    monkeypatch.setattr("ariel.action_runtime._execute_memory_capability", fail_memory)
    turn = TurnRecord(
        id="trn_1",
        user_message="search memory",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    with pytest.raises(RuntimeError, match="memory substrate defect"):
        _run_one_call(
            db=cast(Session, Db()),
            function_call_raw={
                "call_id": "call_1",
                "capability_id": "cap.memory.search",
                "input": {"query": "project phoenix"},
            },
            turn=turn,
            now=fixed_now,
            new_id_fn=lambda prefix: f"{prefix}_1",
            add_event=lambda _event_type, _payload: None,
            runtime_provenance=RuntimeProvenance(status="clean"),
            session_factory=object(),
            settings=object(),
            allowed_capability_ids={"cap.memory.search"},
        )


def test_process_one_call_executes_attachment_read_runtime() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    events: list[tuple[str, dict[str, Any]]] = []
    id_counts: dict[str, int] = {}

    class Db:
        def add(self, record: Any) -> None:
            return None

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    class AttachmentRuntime:
        def execute_read(self, **_: Any) -> ExecutionResult:
            return ExecutionResult(
                status="succeeded",
                output={
                    "attachment_ref": "discord:777",
                    "filename": "report.txt",
                    "retrieved_at": "2026-04-27T12:00:00Z",
                    "modality": "text",
                    "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
                    "blocks": [{"kind": "text", "text": "quarterly revenue increased"}],
                    "results": [
                        {
                            "title": "report.txt",
                            "source": "discord://channel/1/message/2/attachment/777",
                            "snippet": "quarterly revenue increased",
                            "published_at": None,
                        }
                    ],
                    "runtime_provenance": {
                        "status": "tainted",
                        "evidence": [
                            {
                                "kind": "attachment_content_read",
                                "attachment_ref": "discord:777",
                                "filename": "report.txt",
                                "modality": "text",
                            }
                        ],
                    },
                },
                error=None,
            )

    def new_id(prefix: str) -> str:
        id_counts[prefix] = id_counts.get(prefix, 0) + 1
        return f"{prefix}_{id_counts[prefix]}"

    turn = TurnRecord(
        id="trn_1",
        user_message="read the attachment",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.attachment.read",
            "input": {"attachment_ref": "discord:777", "intent": "summarize"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=new_id,
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        runtime_provenance=RuntimeProvenance(status="clean"),
        attachment_runtime=cast(Any, AttachmentRuntime()),
        allowed_capability_ids={"cap.attachment.read"},
    )

    assert ctx.created_action_attempts[0].capability_id == "cap.attachment.read"
    assert ctx.created_action_attempts[0].status == "succeeded"
    assert json.loads(ctx.function_call_outputs[0]["output"])["output"]["blocks"] == [
        {"kind": "text", "text": "quarterly revenue increased"}
    ]
    # The attachment read returned untrusted-influenced content; process_one_call
    # records the tainted provenance so a later same-program syscall sees it.
    assert ctx.result_runtime_provenance == RuntimeProvenance(
        status="tainted",
        evidence=(
            {
                "kind": "attachment_content_read",
                "attachment_ref": "discord:777",
                "filename": "report.txt",
                "modality": "text",
            },
        ),
    )
