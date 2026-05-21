"""Unit tests for ``_action_attempt_observations`` and its capability-specific
projections.

A single ``cap.calendar.list`` event with 30+ attendees plus
``description_blocks`` + ``conference_data`` + ``raw_payload_digest`` +
``provider_evidence_refs`` balloons past 8KB and overflows the 4096-byte
observation cap, leaving the next-round model with a mid-JSON prefix and no
usable substance. The projection trims to model-useful fields BEFORE
serialization. The full payload remains on the ``ActionAttemptRecord`` for
audit.

These tests pin the projection's behaviour:
- a fat calendar event projects down to <2KB and keeps the model-useful fields,
- the projection only applies to ``cap.calendar.list`` (other capabilities
  with the same byte size still truncate),
- the model receives the projected substance, not the truncated prefix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ariel.agent_loop import (
    _MAX_ATTEMPT_OBSERVATION_OUTPUT_BYTES,
    _action_attempt_observations,
    _project_calendar_list_output,
)
from ariel.persistence import ActionAttemptRecord


_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)


def _fat_calendar_event() -> dict[str, Any]:
    """A realistic ``cap.calendar.list`` event payload that overflows 4KB.

    Mirrors the structure of the production smoke that surfaced this bug: 38
    attendees, description_blocks, conference_data, raw_payload_digest. The
    event is 8KB+ encoded; the projection must drop it to <2KB.
    """
    attendees = [
        {
            "email": f"attendee{i}@example.com",
            "display_name": f"Attendee {i} Person",
            "response_status": "needsAction",
            "optional": False,
            "organizer": False,
            "self": False,
        }
        for i in range(38)
    ]
    description_blocks = [
        {
            "block_id": f"calendar:evt_1:description:{i}",
            "kind": "body",
            "text": "x" * 200,
            "digest": "d" * 64,
            "truncated": False,
            "source_mime_type": "text/plain",
            "charset": "utf-8",
        }
        for i in range(3)
    ]
    return {
        "event_id": "evt_career_meeting",
        "calendar_id": "primary",
        "provider_account_id": "acct_google",
        "ical_uid": "evt_career_meeting@google.com",
        "recurring_event_id": "evt_career_recurring",
        "status": "confirmed",
        "summary": "Weekly Career Meeting @ Fractal Tech",
        "description_blocks": description_blocks,
        "organizer": {
            "email": "organizer@fractal.tech",
            "display_name": "Career Lead",
            "self": False,
        },
        "creator": {
            "email": "creator@fractal.tech",
            "display_name": "Creator",
            "self": False,
        },
        "attendees": attendees,
        "raw_payload_digest": "r" * 64,
        "start": {"value": "2026-05-21T14:00:00Z", "timezone": "UTC", "all_day": False},
        "end": {"value": "2026-05-21T15:00:00Z", "timezone": "UTC", "all_day": False},
        "all_day": False,
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TH"],
        "location": "Fractal Tech HQ — Conference Room 3",
        "conference_data": {
            "conference_id": "abc-defg-hij",
            "entry_points": [
                {
                    "entry_point_type": "video",
                    "uri": "https://meet.google.com/abc-defg-hij",
                    "label": "meet.google.com/abc-defg-hij",
                },
                {
                    "entry_point_type": "phone",
                    "uri": "tel:+1-555-555-0000",
                    "label": "+1 (555) 555-0000",
                    "pin": "123456789",
                },
            ],
        },
        "reminders": {"use_default": True, "overrides": []},
        "updated": "2026-05-19T12:00:00Z",
        "etag": "etag_evt_career_meeting",
        "provider_url": "https://calendar.google.com/event?eid=evt_career_meeting",
        "hangout_link": "https://meet.google.com/abc-defg-hij",
        "provider_evidence_refs": [
            {"kind": "calendar_event", "ref": f"google://calendar/primary/evt_career_meeting/{i}"}
            for i in range(3)
        ],
    }


def _calendar_list_output(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "google.calendar.events.v1",
        "status": "succeeded",
        "events": list(events),
        "retrieved_at": "2026-05-20T12:00:00Z",
        "window_start": "2026-05-20T00:00:00Z",
        "window_end": "2026-05-27T00:00:00Z",
    }


def _attempt(*, capability_id: str, execution_output: dict[str, Any]) -> ActionAttemptRecord:
    return ActionAttemptRecord(
        id="aat_test_1",
        session_id="ses_test",
        turn_id="trn_test",
        proposal_index=1,
        capability_id=capability_id,
        capability_version="1.0",
        capability_contract_hash="h" * 64,
        impact_level="read",
        proposed_input={},
        payload_hash="p" * 64,
        policy_decision="allow_inline",
        policy_reason=None,
        status="succeeded",
        approval_required=False,
        execution_output=execution_output,
        execution_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_calendar_list_fat_event_projects_to_model_useful_fields() -> None:
    """A fat ``cap.calendar.list`` event projects to <2KB and keeps the
    model-useful fields: summary, start, end, location, organizer_email,
    attendee_count (count, not list), html_link."""
    fat_payload = _calendar_list_output(_fat_calendar_event())
    raw_size = len(json.dumps(fat_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assert raw_size > _MAX_ATTEMPT_OBSERVATION_OUTPUT_BYTES, (
        f"test premise: raw payload must exceed the 4KB cap (got {raw_size})"
    )

    projected = _project_calendar_list_output(fat_payload)
    projected_size = len(
        json.dumps(projected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert projected_size < 2048, (
        f"projection must fit comfortably under 2KB (got {projected_size})"
    )

    assert projected["schema_version"] == "google.calendar.events.v1"
    assert projected["window_start"] == "2026-05-20T00:00:00Z"
    assert projected["window_end"] == "2026-05-27T00:00:00Z"

    assert len(projected["events"]) == 1
    event = projected["events"][0]
    assert event["summary"] == "Weekly Career Meeting @ Fractal Tech"
    assert event["start"] == {"value": "2026-05-21T14:00:00Z", "timezone": "UTC", "all_day": False}
    assert event["end"] == {"value": "2026-05-21T15:00:00Z", "timezone": "UTC", "all_day": False}
    assert event["location"] == "Fractal Tech HQ — Conference Room 3"
    assert event["organizer_email"] == "organizer@fractal.tech"
    assert event["attendee_count"] == 38
    assert event["html_link"] == "https://calendar.google.com/event?eid=evt_career_meeting"
    assert event["event_id"] == "evt_career_meeting"

    # The bulk fields must NOT survive — that is the whole point of the projection.
    assert "attendees" not in event
    assert "description_blocks" not in event
    assert "conference_data" not in event
    assert "raw_payload_digest" not in event
    assert "provider_evidence_refs" not in event


def test_action_attempt_observations_projects_calendar_list_and_fits_under_cap() -> None:
    """End-to-end through ``_action_attempt_observations``: a fat
    ``cap.calendar.list`` attempt produces a per-attempt entry that carries
    ``execution_output`` (not the truncated prefix), and the model sees the
    summary and organizer email."""
    attempt = _attempt(
        capability_id="cap.calendar.list",
        execution_output=_calendar_list_output(_fat_calendar_event()),
    )
    observations = _action_attempt_observations([attempt])

    assert len(observations) == 1
    obs = observations[0]
    assert obs["capability_id"] == "cap.calendar.list"
    assert obs["status"] == "succeeded"
    assert "execution_output" in obs, (
        f"projection must fit under the cap so the model sees the data, not "
        f"the truncated prefix; got: {sorted(obs.keys())}"
    )
    assert "execution_output_truncated" not in obs
    assert "execution_output_prefix" not in obs

    event = obs["execution_output"]["events"][0]
    assert event["summary"] == "Weekly Career Meeting @ Fractal Tech"
    assert event["organizer_email"] == "organizer@fractal.tech"
    assert event["attendee_count"] == 38


def test_action_attempt_observations_does_not_project_other_capabilities() -> None:
    """The projection only applies to ``cap.calendar.list``. A different
    capability with a fat payload still truncates against the cap — this is
    the gate that prevents the projection from silently swallowing data the
    model needs from other capabilities."""
    fat_unknown_payload = {
        "schema_version": "some.other.v1",
        "events": [_fat_calendar_event()],
        "retrieved_at": "2026-05-20T12:00:00Z",
        "window_start": "2026-05-20T00:00:00Z",
        "window_end": "2026-05-27T00:00:00Z",
    }
    attempt = _attempt(
        capability_id="cap.some.other.fat",
        execution_output=fat_unknown_payload,
    )
    observations = _action_attempt_observations([attempt])

    assert len(observations) == 1
    obs = observations[0]
    assert obs["capability_id"] == "cap.some.other.fat"
    # No projection applies — the full payload exceeds the cap and truncates.
    assert obs.get("execution_output_truncated") is True
    assert "execution_output_prefix" in obs
    assert "execution_output" not in obs


def test_project_calendar_list_output_handles_empty_events() -> None:
    """Projection on an empty events list returns a well-formed empty
    payload; no exceptions, no surprise rewriting of unrelated fields."""
    output = _calendar_list_output()
    projected = _project_calendar_list_output(output)
    assert projected["events"] == []
    assert projected["schema_version"] == "google.calendar.events.v1"


def test_project_calendar_list_output_handles_missing_organizer() -> None:
    """An event without an organizer projects ``organizer_email=None`` rather
    than raising."""
    event = _fat_calendar_event()
    event["organizer"] = None
    projected = _project_calendar_list_output(_calendar_list_output(event))
    assert projected["events"][0]["organizer_email"] is None
