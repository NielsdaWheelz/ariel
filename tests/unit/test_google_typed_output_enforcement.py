from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from ariel.google_connector import GoogleConnectorRuntime


@dataclass
class _FakeGoogleProvider:
    output: dict[str, Any]

    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output

    def calendar_propose_slots(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        del access_token, normalized_input, attendee_intersection_enabled
        return self.output

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output

    def calendar_update_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output

    def calendar_respond_to_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        return self.output


def _runtime(output: dict[str, Any]) -> GoogleConnectorRuntime:
    return GoogleConnectorRuntime(
        oauth_client=cast(Any, object()),
        workspace_provider=cast(Any, _FakeGoogleProvider(output)),
        redirect_uri="https://app.example.test/oauth/google/callback",
        oauth_state_ttl_seconds=300,
        encryption_secret="test-secret",
        encryption_key_version="v1",
    )


def _execute(
    capability_id: str, output: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, str | None]:
    result = _runtime(output).execute_provider_capability(
        capability_id=capability_id,
        normalized_input={
            "query": "invoice",
            "message_id": "msg_1",
            "event_id": "evt_1",
            "attendee_email": "user@example.com",
            "response_status": "accepted",
            "window_start": "2026-03-04T00:00:00Z",
            "window_end": "2026-03-05T00:00:00Z",
            "duration_minutes": 30,
        },
        access_token="tok_live",
        granted_scopes=set(),
    )
    return result.status, result.output, result.error


def _calendar_events_output(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "google.calendar.events.v1",
        "events": [event],
        "retrieved_at": "2026-03-03T12:00:00Z",
        "window_start": "2026-03-04T00:00:00Z",
        "window_end": "2026-03-05T00:00:00Z",
    }


def _calendar_event() -> dict[str, Any]:
    return {
        "event_id": "evt_1",
        "calendar_id": "primary",
        "provider_account_id": "acct_google",
        "ical_uid": "evt_1@google.com",
        "recurring_event_id": None,
        "status": "confirmed",
        "summary": "Risk review",
        "description_blocks": [],
        "organizer": None,
        "creator": None,
        "attendees": [],
        "raw_payload_digest": "d" * 64,
        "start": {"value": "2026-03-04T10:00:00Z", "timezone": "UTC", "all_day": False},
        "end": {"value": "2026-03-04T10:30:00Z", "timezone": "UTC", "all_day": False},
        "all_day": False,
        "recurrence": [],
        "location": None,
        "conference_data": None,
        "reminders": None,
        "updated": "2026-03-03T12:00:00Z",
        "etag": "etag_evt_1",
        "provider_url": "https://calendar.google.com/event?eid=evt_1",
        "hangout_link": None,
    }


@pytest.mark.parametrize(
    "capability_id",
    [
        "cap.calendar.list",
        "cap.calendar.propose_slots",
        "cap.calendar.create_event",
        "cap.calendar.update_event",
        "cap.calendar.respond_to_event",
        "cap.email.search",
        "cap.email.read",
    ],
)
def test_google_cutover_read_outputs_reject_legacy_results_shape(capability_id: str) -> None:
    status, output, error = _execute(
        capability_id,
        {
            "results": [
                {
                    "title": "legacy result",
                    "source": "google://legacy",
                    "snippet": "old output shape",
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


@pytest.mark.parametrize(
    ("capability_id", "typed_output"),
    [
        (
            "cap.calendar.list",
            {
                "schema_version": "google.calendar.events.v1",
                "events": [
                    {
                        "event_id": "evt_1",
                        "calendar_id": "primary",
                        "provider_account_id": "acct_google",
                        "ical_uid": "evt_1@google.com",
                        "recurring_event_id": None,
                        "status": "confirmed",
                        "summary": "Risk review",
                        "description_blocks": [],
                        "organizer": None,
                        "creator": None,
                        "attendees": [],
                        "raw_payload_digest": "d" * 64,
                        "start": {
                            "value": "2026-03-04T10:00:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "end": {
                            "value": "2026-03-04T10:30:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "all_day": False,
                        "recurrence": [],
                        "location": None,
                        "conference_data": None,
                        "reminders": None,
                        "updated": "2026-03-03T12:00:00Z",
                        "etag": "etag_evt_1",
                        "provider_url": "https://calendar.google.com/event?eid=evt_1",
                        "hangout_link": None,
                    }
                ],
                "retrieved_at": "2026-03-03T12:00:00Z",
                "window_start": "2026-03-04T00:00:00Z",
                "window_end": "2026-03-05T00:00:00Z",
            },
        ),
        (
            "cap.calendar.propose_slots",
            {
                "schema_version": "google.calendar.slot_options.v1",
                "slots": [
                    {
                        "slot_id": "slot_1",
                        "start": {
                            "value": "2026-03-04T10:00:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "end": {
                            "value": "2026-03-04T10:30:00Z",
                            "timezone": "UTC",
                            "all_day": False,
                        },
                        "availability_scope": "all_attendees",
                        "partial": False,
                    }
                ],
                "retrieved_at": "2026-03-03T12:00:00Z",
                "window_start": "2026-03-04T00:00:00Z",
                "window_end": "2026-03-05T00:00:00Z",
                "duration_minutes": 30,
                "attendees_considered": ["teammate@example.com"],
                "availability_scope": "all_attendees",
                "partial": False,
                "partial_reason": None,
                "timezone": "UTC",
                "source_evidence_refs": [],
                "constraints_used": {},
                "freebusy_diagnostics": [],
                "no_slots_reason": None,
            },
        ),
        (
            "cap.calendar.create_event",
            {
                "schema_version": "google.calendar.create_result.v1",
                "status": "created",
                "event_id": "evt_1",
                "calendar_id": "primary",
                "provider_event_ref": "calendar://evt_1",
                "etag": "etag_evt_1",
                "updated": "2026-03-03T12:00:00Z",
                "ical_uid": "evt_1@google.com",
                "provider_status": "confirmed",
                "executed_at": "2026-03-03T12:00:01Z",
            },
        ),
        (
            "cap.calendar.update_event",
            {
                "schema_version": "google.calendar.update_result.v1",
                "status": "updated",
                "event_id": "evt_1",
                "calendar_id": "primary",
                "provider_event_ref": "calendar://evt_1",
                "etag": "etag_evt_1",
                "updated": "2026-03-03T12:00:00Z",
                "ical_uid": "evt_1@google.com",
                "provider_status": "confirmed",
                "executed_at": "2026-03-03T12:00:01Z",
            },
        ),
        (
            "cap.calendar.respond_to_event",
            {
                "schema_version": "google.calendar.response_result.v1",
                "status": "responded",
                "event_id": "evt_1",
                "calendar_id": "primary",
                "response_status": "accepted",
                "provider_event_ref": "calendar://evt_1",
                "etag": "etag_evt_1",
                "updated": "2026-03-03T12:00:00Z",
                "ical_uid": "evt_1@google.com",
                "provider_status": "confirmed",
                "executed_at": "2026-03-03T12:00:01Z",
            },
        ),
        (
            "cap.email.search",
            {
                "schema_version": "google.gmail.message_refs.v1",
                "messages": [
                    {
                        "message_id": "msg_1",
                        "thread_id": "thr_1",
                        "history_id": "hist_1",
                        "subject": "Invoice",
                        "subject_key": "invoice",
                        "sender": {"email": "sender@example.com"},
                        "recipients": [{"email": "user@example.com"}],
                        "internal_date": "2026-03-03T11:00:00Z",
                        "label_ids": ["INBOX"],
                        "direction": "received",
                        "preview": "invoice due",
                        "provider_url": "https://mail.google.com/mail/u/0/#all/msg_1",
                        "evidence_status": "needs_read",
                    }
                ],
                "retrieved_at": "2026-03-03T12:00:00Z",
            },
        ),
        (
            "cap.email.read",
            {
                "schema_version": "google.gmail.message_evidence.v1",
                "message": {"message_id": "msg_1", "thread_id": "thr_1"},
                "evidence": {
                    "source_kind": "gmail_message",
                    "message_id": "msg_1",
                    "thread_id": "thr_1",
                    "body_digest": "b" * 64,
                    "blocks": [
                        {
                            "block_id": "gmail:msg_1:body:0",
                            "kind": "body",
                            "text": "payment confirmed",
                            "digest": "c" * 64,
                        }
                    ],
                },
                "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
                "retrieved_at": "2026-03-03T12:00:00Z",
            },
        ),
    ],
)
def test_google_cutover_read_outputs_accept_typed_shapes(
    capability_id: str,
    typed_output: dict[str, Any],
) -> None:
    status, output, error = _execute(capability_id, typed_output)

    assert status == "succeeded"
    assert output == typed_output
    assert error is None


def test_google_cutover_gmail_search_accepts_message_refs_that_need_read() -> None:
    status, output, error = _execute(
        "cap.email.search",
        {
            "schema_version": "google.gmail.message_refs.v1",
            "messages": [
                {
                    "message_id": "msg_1",
                    "thread_id": "thr_1",
                    "history_id": "hist_1",
                    "subject": "Invoice",
                    "subject_key": "invoice",
                    "sender": {"email": "sender@example.com"},
                    "recipients": [{"email": "user@example.com"}],
                    "internal_date": "2026-03-03T11:00:00Z",
                    "label_ids": ["INBOX"],
                    "direction": "received",
                    "preview": "invoice due",
                    "provider_url": "https://mail.google.com/mail/u/0/#all/msg_1",
                    "evidence_status": "needs_read",
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "succeeded"
    assert output is not None
    assert error is None


def test_google_cutover_gmail_search_rejects_thin_message_refs() -> None:
    status, output, error = _execute(
        "cap.email.search",
        {
            "schema_version": "google.gmail.message_refs.v1",
            "messages": [
                {
                    "message_id": "msg_1",
                    "thread_id": "thr_1",
                    "provider_url": "https://mail.google.com/mail/u/0/#all/msg_1",
                    "evidence_status": "needs_read",
                }
            ],
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_gmail_read_rejects_unbounded_message_body_fields() -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "body": {"text": "full body should only appear in evidence blocks"},
            },
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "body_digest": "b" * 64,
                "blocks": [
                    {
                        "block_id": "gmail:msg_1:body:0",
                        "kind": "body",
                        "text": "payment confirmed",
                        "digest": "c" * 64,
                    }
                ],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_gmail_read_rejects_unknown_raw_body_fields() -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "raw_body": "private body text",
            },
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "body_digest": "b" * 64,
                "blocks": [
                    {
                        "block_id": "gmail:msg_1:body:0",
                        "kind": "body",
                        "text": "payment confirmed",
                        "digest": "c" * 64,
                    }
                ],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_invalid_non_ok_gmail_read_returns_no_provider_payload() -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "raw_body": "private body text",
            },
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "blocks": [],
                "raw_body": "private body text",
            },
            "read_outcome": {
                "status": "body_too_large",
                "reason_code": "gmail_body_too_large",
                "recovery": "Use narrower context.",
            },
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_calendar_list_rejects_raw_description_fields() -> None:
    event = _calendar_event()
    event["raw_description"] = "private calendar description"

    status, output, error = _execute("cap.calendar.list", _calendar_events_output(event))

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_calendar_list_rejects_unbounded_description_blocks() -> None:
    event = _calendar_event()
    event["description_blocks"] = [
        {
            "block_id": "calendar:evt_1:description:0",
            "kind": "body",
            "text": "x" * 2001,
            "digest": "d" * 64,
            "truncated": False,
            "source_mime_type": "text/plain",
            "charset": "utf-8",
        }
    ]

    status, output, error = _execute("cap.calendar.list", _calendar_events_output(event))

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_gmail_read_accepts_typed_non_ok_body_read() -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {"message_id": "msg_1", "thread_id": "thr_1"},
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "blocks": [],
                "decode_notes": ["body too large in text/plain; skipped"],
            },
            "read_outcome": {
                "status": "body_too_large",
                "reason_code": "gmail_body_too_large",
                "recovery": "Use narrower message context.",
            },
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "succeeded"
    assert output is not None
    assert output["read_outcome"]["status"] == "body_too_large"
    assert error is None


@pytest.mark.parametrize(
    ("message", "evidence_thread_id"),
    [
        ({"message_id": "msg_1"}, "thr_1"),
        ({"message_id": "msg_1", "thread_id": ""}, ""),
        ({"message_id": "msg_1", "thread_id": "thr_1"}, None),
        ({"message_id": "msg_1", "thread_id": "thr_1"}, "other_thr"),
    ],
)
def test_google_cutover_gmail_read_requires_message_thread_id(
    message: dict[str, Any],
    evidence_thread_id: str | None,
) -> None:
    evidence = {
        "source_kind": "gmail_message",
        "message_id": "msg_1",
        "body_digest": "b" * 64,
        "blocks": [
            {
                "block_id": "gmail:msg_1:body:0",
                "kind": "body",
                "text": "payment confirmed",
                "digest": "c" * 64,
            }
        ],
    }
    if evidence_thread_id is not None:
        evidence["thread_id"] = evidence_thread_id

    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": message,
            "evidence": evidence,
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


def test_google_cutover_gmail_read_accepts_thread_evidence_shape() -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "thread",
            "thread": {"thread_id": "thr_1", "message_count": 1},
            "messages": [{"message_id": "msg_1", "thread_id": "thr_1"}],
            "evidence": {
                "source_kind": "gmail_thread",
                "thread_id": "thr_1",
                "body_digest": "b" * 64,
                "blocks": [
                    {
                        "block_id": "gmail:msg_1:body:0",
                        "kind": "body",
                        "text": "payment confirmed",
                        "digest": "c" * 64,
                    }
                ],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "succeeded"
    assert output is not None
    assert error is None


@pytest.mark.parametrize(
    "evidence",
    [
        {
            "source_kind": "gmail_message",
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "blocks": [
                {
                    "block_id": "gmail:msg_1:body:0",
                    "kind": "body",
                    "text": "payment confirmed",
                    "digest": "c" * 64,
                }
            ],
        },
        {
            "source_kind": "gmail_message",
            "message_id": "other_msg",
            "thread_id": "thr_1",
            "body_digest": "b" * 64,
            "blocks": [
                {
                    "block_id": "gmail:msg_1:body:0",
                    "kind": "body",
                    "text": "payment confirmed",
                    "digest": "c" * 64,
                }
            ],
        },
        {
            "source_kind": "gmail_message",
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "body_digest": "b" * 64,
            "blocks": [],
        },
        {
            "source_kind": "gmail_message",
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "body_digest": "b" * 64,
            "blocks": [
                {
                    "block_id": "gmail:msg_1:body:0",
                    "kind": "body",
                    "text": "payment confirmed",
                }
            ],
        },
    ],
)
def test_google_cutover_gmail_read_requires_persistable_body_evidence(
    evidence: dict[str, Any],
) -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "message": {"message_id": "msg_1", "thread_id": "thr_1"},
            "evidence": evidence,
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {
                "block_id": f"gmail:msg_1:body:{index}",
                "kind": "body",
                "text": "payment confirmed",
                "digest": "c" * 64,
            }
            for index in range(13)
        ],
        [
            {
                "block_id": "gmail:msg_1:body:0",
                "kind": "body",
                "text": "x" * 2001,
                "digest": "c" * 64,
            }
        ],
    ],
)
def test_google_cutover_gmail_read_rejects_unbounded_evidence_blocks(
    blocks: list[dict[str, Any]],
) -> None:
    status, output, error = _execute(
        "cap.email.read",
        {
            "schema_version": "google.gmail.message_evidence.v1",
            "message": {"message_id": "msg_1", "thread_id": "thr_1"},
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "body_digest": "b" * 64,
                "blocks": blocks,
            },
            "retrieved_at": "2026-03-03T12:00:00Z",
        },
    )

    assert status == "failed"
    assert output is None
    assert error == "invalid_provider_output"
