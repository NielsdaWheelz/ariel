from __future__ import annotations

import base64
from typing import Any

from ariel.google_workspace_normalization import (
    normalize_calendar_event,
    normalize_gmail_message,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _part(
    mime_type: str,
    text: str | bytes,
    *,
    headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload = text if isinstance(text, bytes) else text.encode("utf-8")
    return {
        "mimeType": mime_type,
        "headers": headers or [{"name": "Content-Type", "value": mime_type}],
        "body": {"data": _b64url(payload), "size": len(payload)},
    }


def test_gmail_multipart_alternative_prefers_plain_text_and_uses_stable_blocks() -> None:
    payload = {
        "id": "msg_1",
        "threadId": "thread_1",
        "historyId": "77",
        "internalDate": "179000",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Re: Launch Plan"},
                {"name": "From", "value": "Ada Lovelace <ada@example.com>"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Message-ID", "value": "<rfc-1@example.com>"},
            ],
            "parts": [
                _part("text/plain", "Plain body\n\n> quoted context\n-- \nAda"),
                _part("text/html", "<p>HTML body</p><script>bad()</script>"),
            ],
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")
    repeated = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.message_id == "msg_1"
    assert normalized.subject_key == "launch plan"
    assert normalized.sender is not None
    assert normalized.sender.email == "ada@example.com"
    assert normalized.body.preferred_mime_type == "text/plain"
    assert "Plain body" in normalized.body.text
    assert "HTML body" not in normalized.body.text
    assert [block.kind for block in normalized.body.blocks] == ["body", "quote", "signature"]
    assert [block.block_id for block in normalized.body.blocks] == [
        block.block_id for block in repeated.body.blocks
    ]


def test_gmail_nested_multipart_decodes_inner_plain_text_and_forwarded_marker() -> None:
    payload = {
        "id": "msg_nested",
        "threadId": "thread_nested",
        "labelIds": ["SENT"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Fwd: Notes"}],
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "mimeType": "multipart/alternative",
                            "parts": [
                                _part(
                                    "text/plain",
                                    "Current note\n\n---------- Forwarded message ---------\nOld note",
                                )
                            ],
                        }
                    ],
                }
            ],
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.direction == "sent"
    assert normalized.body.preferred_mime_type == "text/plain"
    assert [block.kind for block in normalized.body.blocks] == ["body", "forwarded"]
    assert normalized.body.blocks[1].text.endswith("Old note")


def test_gmail_html_only_uses_sanitized_html_text() -> None:
    payload = {
        "id": "msg_html",
        "threadId": "thread_html",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
            "body": {
                "data": _b64url(
                    b"<html><body><p>Hello <b>team</b></p><script>x()</script></body></html>"
                )
            },
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.preferred_mime_type == "text/html"
    assert normalized.body.text == "Hello team"
    assert "script" not in normalized.body.text
    assert normalized.body.html_text == "Hello team"


def test_gmail_html_nested_hidden_content_stays_omitted_after_inner_close() -> None:
    payload = {
        "id": "msg_hidden",
        "threadId": "thread_hidden",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
            "body": {
                "data": _b64url(
                    b"<p>Visible before</p>"
                    b"<div style='display:none'>hidden <span>nested tag</span>"
                    b"<br><span hidden>nested hidden tag</span> still hidden</div>"
                    b"<p>Visible after</p>"
                )
            },
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == "Visible before\n\nVisible after"
    assert "still hidden" not in normalized.body.text
    assert "nested tag" not in normalized.body.text
    assert "nested hidden tag" not in normalized.body.text
    assert normalized.body.html_security["hidden_text_count"] == 4
    assert normalized.body.html_security["conversion_notes"] == ["hidden_html_text_omitted"]


def test_gmail_html_hidden_content_ignores_unmatched_inner_close() -> None:
    payload = {
        "id": "msg_hidden_malformed",
        "threadId": "thread_hidden_malformed",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
            "body": {
                "data": _b64url(
                    b"<p>Visible before</p>"
                    b"<div style='display:none'>hidden </span>still hidden</div>"
                    b"<p>Visible after</p>"
                )
            },
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == "Visible before\n\nVisible after"
    assert "still hidden" not in normalized.body.text
    assert normalized.body.html_security["hidden_text_count"] == 2


def test_gmail_requires_non_empty_thread_id() -> None:
    payload = {
        "id": "msg_no_thread",
        "threadId": " ",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64url(b"Body")},
        },
    }

    try:
        normalize_gmail_message(payload, provider_account_id="acct_1")
    except ValueError as exc:
        assert str(exc) == "gmail_thread_id_missing"
    else:
        raise AssertionError("expected missing Gmail thread id to fail normalization")


def test_gmail_attachment_metadata_is_preserved_without_body_prompting() -> None:
    payload = {
        "id": "msg_attach",
        "threadId": "thread_attach",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                _part("text/plain", "See attached."),
                {
                    "mimeType": "application/pdf",
                    "filename": "contract.pdf",
                    "headers": [
                        {
                            "name": "Content-Disposition",
                            "value": "attachment; filename=contract.pdf",
                        },
                        {"name": "Content-ID", "value": "<cid-1>"},
                    ],
                    "body": {"attachmentId": "att_1", "size": 12345},
                },
            ],
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == "See attached."
    assert len(normalized.attachments) == 1
    attachment = normalized.attachments[0]
    assert attachment.attachment_id == "att_1"
    assert attachment.filename == "contract.pdf"
    assert attachment.mime_type == "application/pdf"
    assert attachment.size == 12345
    assert attachment.content_id == "cid-1"
    assert attachment.inline is False


def test_gmail_malformed_base64_records_decode_note_and_skips_body() -> None:
    payload = {
        "id": "msg_bad",
        "threadId": "thread_bad",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": "not-valid$$"},
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == ""
    assert normalized.body.blocks == ()
    assert normalized.body.decode_notes == ("invalid base64url body data in text/plain",)


def test_gmail_body_blocks_are_bounded_and_mark_truncation() -> None:
    payload = {
        "id": "msg_long",
        "threadId": "thread_long",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64url(("a" * 25000).encode("utf-8"))},
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert len(normalized.body.blocks) == 12
    assert normalized.body.truncated is True
    assert normalized.body.blocks[-1].truncated is True
    assert all(len(block.text) <= 2000 for block in normalized.body.blocks)


def test_gmail_oversized_text_part_records_typed_decode_note_without_body() -> None:
    payload = {
        "id": "msg_too_large",
        "threadId": "thread_too_large",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64url(("a" * 262145).encode("utf-8")), "size": 262145},
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == ""
    assert normalized.body.blocks == ()
    assert normalized.body.decode_notes == ("body too large in text/plain; skipped",)


def test_gmail_non_utf_charset_is_decoded() -> None:
    payload = {
        "id": "msg_latin1",
        "threadId": "thread_latin1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
            "body": {"data": _b64url("Olá, amanhã".encode("iso-8859-1"))},
        },
    }

    normalized = normalize_gmail_message(payload, provider_account_id="acct_1")

    assert normalized.body.text == "Olá, amanhã"
    assert normalized.body.blocks[0].charset == "iso-8859-1"


def test_calendar_event_normalization_preserves_typed_event_fields() -> None:
    event = {
        "id": "evt_1",
        "iCalUID": "ical_1@example.com",
        "recurringEventId": "series_1",
        "status": "confirmed",
        "summary": "Planning",
        "description": "<p>Bring roadmap.</p>",
        "organizer": {"email": "lead@example.com", "displayName": "Lead"},
        "creator": {"email": "creator@example.com", "self": True},
        "attendees": [
            {
                "email": "user@example.com",
                "displayName": "User",
                "responseStatus": "accepted",
                "self": True,
            },
            {
                "email": "guest@example.com",
                "responseStatus": "needsAction",
                "optional": True,
            },
        ],
        "start": {"dateTime": "2026-06-01T10:00:00-07:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-06-01T10:30:00-07:00", "timeZone": "America/Los_Angeles"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2"],
        "location": "Room 1",
        "conferenceData": {"conferenceId": "meet_1"},
        "reminders": {"useDefault": True},
        "updated": "2026-05-31T09:00:00Z",
        "etag": '"etag_1"',
        "htmlLink": "https://calendar.google.com/event?eid=evt_1",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
    }

    normalized = normalize_calendar_event(
        event,
        provider_account_id="acct_1",
        calendar_id="primary",
    )

    assert normalized.provider_account_id == "acct_1"
    assert normalized.event_id == "evt_1"
    assert normalized.status == "confirmed"
    assert normalized.description_blocks[0].text == "Bring roadmap."
    assert normalized.organizer is not None
    assert normalized.organizer.email == "lead@example.com"
    assert normalized.attendees[0].response_status == "accepted"
    assert normalized.attendees[1].optional is True
    assert normalized.start.value == "2026-06-01T10:00:00-07:00"
    assert normalized.start.timezone == "America/Los_Angeles"
    assert normalized.all_day is False
    assert normalized.recurrence == ("RRULE:FREQ=WEEKLY;COUNT=2",)
    assert normalized.provider_url == "https://calendar.google.com/event?eid=evt_1"
    assert normalized.etag == '"etag_1"'
    assert len(normalized.raw_payload_digest) == 64
