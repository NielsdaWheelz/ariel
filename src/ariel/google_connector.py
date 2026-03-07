from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
import json
import hashlib
import hmac
import secrets
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariel.persistence import (
    GoogleConnectorEventRecord,
    GoogleConnectorRecord,
    GoogleOAuthStateRecord,
    to_rfc3339,
)
from ariel.redaction import redact_json_value, safe_failure_reason


GOOGLE_CONNECTOR_ID = "con_google"
GOOGLE_PROVIDER = "google"

GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_DRIVE_METADATA_READ_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
GOOGLE_DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DRIVE_SHARE_SCOPE = "https://www.googleapis.com/auth/drive"

GOOGLE_READ_CAPABILITY_SCOPES: dict[str, set[str]] = {
    "cap.calendar.list": {GOOGLE_CALENDAR_READ_SCOPE},
    "cap.calendar.propose_slots": {GOOGLE_CALENDAR_READ_SCOPE},
    "cap.email.search": {GOOGLE_GMAIL_READ_SCOPE},
    "cap.email.read": {GOOGLE_GMAIL_READ_SCOPE},
    "cap.drive.search": {GOOGLE_DRIVE_METADATA_READ_SCOPE},
    "cap.drive.read": {GOOGLE_DRIVE_READ_SCOPE},
}
GOOGLE_READ_CAPABILITY_IDS = frozenset(GOOGLE_READ_CAPABILITY_SCOPES.keys())
GOOGLE_WRITE_CAPABILITY_SCOPES: dict[str, set[str]] = {
    "cap.calendar.create_event": {GOOGLE_CALENDAR_WRITE_SCOPE},
    "cap.email.draft": {GOOGLE_GMAIL_COMPOSE_SCOPE},
    "cap.email.send": {GOOGLE_GMAIL_SEND_SCOPE},
    "cap.drive.share": {GOOGLE_DRIVE_SHARE_SCOPE},
}
GOOGLE_WRITE_CAPABILITY_IDS = frozenset(GOOGLE_WRITE_CAPABILITY_SCOPES.keys())
GOOGLE_CAPABILITY_SCOPES: dict[str, set[str]] = {
    **GOOGLE_READ_CAPABILITY_SCOPES,
    **GOOGLE_WRITE_CAPABILITY_SCOPES,
}
GOOGLE_CAPABILITY_IDS = frozenset(GOOGLE_CAPABILITY_SCOPES.keys())
GOOGLE_RECONNECT_INTENT_EXTRA_SCOPES: dict[str, set[str]] = {
    "cap.calendar.propose_slots": {GOOGLE_CALENDAR_FREEBUSY_SCOPE},
}

_GOOGLE_MINIMUM_READ_SCOPES = {GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE}
_READINESS_BLOCKING_FAILURE_CODES = {
    "consent_required",
    "scope_missing",
    "access_revoked",
}
_READINESS_TRANSIENT_FAILURE_CODES = {"token_expired"}

TypedAuthFailureClass = Literal[
    "not_connected",
    "consent_required",
    "scope_missing",
    "token_expired",
    "access_revoked",
]

_AUTH_FAILURE_RECOVERY: dict[TypedAuthFailureClass, str] = {
    "not_connected": "Connect Google to continue.",
    "consent_required": "Reconnect Google and grant the requested scope.",
    "scope_missing": "Reconnect Google and re-consent to required scopes.",
    "token_expired": "Retry once; if it still fails, reconnect Google.",
    "access_revoked": "Reconnect Google from scratch.",
}

_GOOGLE_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_GOOGLE_RESULTS = 5
_MAX_DRIVE_READ_BYTES = 131072
_MAX_DRIVE_READ_CHARS = 2000
_DRIVE_NATIVE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_DRIVE_PLAIN_TEXT_EXPORT_MIME_TYPE = "text/plain"
_DRIVE_TEXT_LIKE_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/ld+json",
    "application/rtf",
    "application/xml",
}


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class GoogleConnectorError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any],
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TypedAuthFailure:
    failure_class: TypedAuthFailureClass
    recovery: str


@dataclass(frozen=True, slots=True)
class GoogleCapabilityExecutionResult:
    status: Literal["succeeded", "failed"]
    output: dict[str, Any] | None
    auth_failure: TypedAuthFailure | None
    error: str | None


class GoogleOAuthClient(Protocol):
    def build_authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        scopes: list[str],
        redirect_uri: str,
        prompt_consent: bool,
    ) -> str: ...

    def exchange_code_for_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        state: str,
    ) -> dict[str, Any]: ...

    def refresh_access_token(self, *, refresh_token: str) -> dict[str, Any]: ...

    def revoke_token(self, *, token: str) -> None: ...


class GoogleWorkspaceProvider(Protocol):
    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def calendar_propose_slots(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]: ...

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def email_create_draft(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def email_send(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def drive_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def drive_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    def drive_share(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(slots=True, frozen=True)
class DefaultGoogleOAuthClient:
    client_id: str | None
    client_secret: str | None
    authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"
    revoke_url: str = "https://oauth2.googleapis.com/revoke"
    userinfo_url: str = "https://www.googleapis.com/oauth2/v3/userinfo"
    timeout_seconds: float = 10.0

    def build_authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        scopes: list[str],
        redirect_uri: str,
        prompt_consent: bool,
    ) -> str:
        if self.client_id is None or not self.client_id.strip():
            raise RuntimeError("google oauth client id is not configured")
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent" if prompt_consent else "select_account",
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    def exchange_code_for_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        state: str,
    ) -> dict[str, Any]:
        del state
        if self.client_id is None or self.client_secret is None:
            raise RuntimeError("google oauth credentials are not configured")
        try:
            token_response = httpx.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("oauth token exchange timeout") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("oauth token exchange network failure") from exc

        if token_response.status_code >= 400:
            if token_response.status_code in {400, 401, 403}:
                detail = safe_failure_reason(
                    token_response.text,
                    fallback=f"oauth token exchange rejected ({token_response.status_code})",
                )
                raise RuntimeError(detail)
            raise RuntimeError(f"oauth token exchange failed ({token_response.status_code})")

        payload = token_response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("oauth token exchange returned invalid payload")
        access_token_raw = payload.get("access_token")
        refresh_token_raw = payload.get("refresh_token")
        if not isinstance(access_token_raw, str) or not access_token_raw.strip():
            raise RuntimeError("oauth token exchange missing access token")
        access_token = access_token_raw.strip()
        refresh_token = (
            refresh_token_raw.strip()
            if isinstance(refresh_token_raw, str) and refresh_token_raw.strip()
            else ""
        )
        scope_value = payload.get("scope")
        granted_scopes = _normalize_scope_list(scope_value)
        expires_in_raw = payload.get("expires_in")
        expires_in_seconds = expires_in_raw if isinstance(expires_in_raw, int) else 3600
        if expires_in_seconds <= 0:
            expires_in_seconds = 3600

        account_subject = "unknown-subject"
        account_email = "unknown-email"
        try:
            profile_response = httpx.get(
                self.userinfo_url,
                headers={"authorization": f"Bearer {access_token}"},
                timeout=self.timeout_seconds,
            )
            if profile_response.status_code < 400:
                profile_payload = profile_response.json()
                if isinstance(profile_payload, dict):
                    subject = profile_payload.get("sub")
                    email = profile_payload.get("email")
                    if isinstance(subject, str) and subject.strip():
                        account_subject = subject.strip()
                    if isinstance(email, str) and email.strip():
                        account_email = email.strip()
        except httpx.HTTPError:
            pass

        return {
            "account_subject": account_subject,
            "account_email": account_email,
            "granted_scopes": granted_scopes,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in_seconds": expires_in_seconds,
        }

    def refresh_access_token(self, *, refresh_token: str) -> dict[str, Any]:
        if self.client_id is None or self.client_secret is None:
            raise RuntimeError("google oauth credentials are not configured")
        try:
            token_response = httpx.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("oauth refresh timeout") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("oauth refresh network failure") from exc
        if token_response.status_code >= 400:
            detail = safe_failure_reason(
                token_response.text,
                fallback=f"oauth refresh failed ({token_response.status_code})",
            )
            raise RuntimeError(detail)
        payload = token_response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("oauth refresh returned invalid payload")
        access_token_raw = payload.get("access_token")
        if not isinstance(access_token_raw, str) or not access_token_raw.strip():
            raise RuntimeError("oauth refresh missing access token")
        refreshed_refresh_token_raw = payload.get("refresh_token")
        expires_in_raw = payload.get("expires_in")
        expires_in_seconds = expires_in_raw if isinstance(expires_in_raw, int) else 3600
        if expires_in_seconds <= 0:
            expires_in_seconds = 3600
        return {
            "access_token": access_token_raw.strip(),
            "refresh_token": (
                refreshed_refresh_token_raw.strip()
                if isinstance(refreshed_refresh_token_raw, str) and refreshed_refresh_token_raw.strip()
                else refresh_token
            ),
            "expires_in_seconds": expires_in_seconds,
        }

    def revoke_token(self, *, token: str) -> None:
        if not token.strip():
            return
        try:
            response = httpx.post(
                self.revoke_url,
                data={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError:
            return
        if response.status_code >= 400:
            return


@dataclass(slots=True, frozen=True)
class DefaultGoogleWorkspaceProvider:
    calendar_api_base_url: str = "https://www.googleapis.com/calendar/v3"
    gmail_api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    drive_api_base_url: str = "https://www.googleapis.com/drive/v3"
    timeout_seconds: float = 10.0
    max_attempts: int = 2

    def _authorized_headers(self, *, access_token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
        }

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = max(1, self.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.request(
                    method,
                    url,
                    headers=self._authorized_headers(access_token=access_token),
                    params=params,
                    json=json_payload,
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    continue
                raise RuntimeError("google_upstream_timeout") from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    continue
                raise RuntimeError("google_upstream_network_failure") from exc

            status_code = response.status_code
            if status_code in _GOOGLE_TRANSIENT_STATUS_CODES and attempt < attempts:
                continue
            if status_code == 401:
                raise RuntimeError("token_expired")
            if status_code == 403:
                if _is_google_scope_failure(response):
                    raise RuntimeError("insufficient_permissions")
                raise RuntimeError("google_forbidden")
            if status_code in _GOOGLE_TRANSIENT_STATUS_CODES:
                raise RuntimeError(f"google_upstream_{status_code}")
            if status_code == 404:
                raise RuntimeError("resource_not_found")
            if status_code >= 400:
                raise RuntimeError(f"google_request_failed:{status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("google_invalid_payload") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("google_invalid_payload")
            return payload
        raise RuntimeError("google_request_unreachable")

    def _request_text(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        attempts = max(1, self.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.request(
                    method,
                    url,
                    headers=self._authorized_headers(access_token=access_token),
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    continue
                raise RuntimeError("google_upstream_timeout") from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    continue
                raise RuntimeError("google_upstream_network_failure") from exc

            status_code = response.status_code
            if status_code in _GOOGLE_TRANSIENT_STATUS_CODES and attempt < attempts:
                continue
            if status_code == 401:
                raise RuntimeError("token_expired")
            if status_code == 403:
                if _is_google_scope_failure(response):
                    raise RuntimeError("insufficient_permissions")
                raise RuntimeError("google_forbidden")
            if status_code in _GOOGLE_TRANSIENT_STATUS_CODES:
                raise RuntimeError(f"google_upstream_{status_code}")
            if status_code == 404:
                raise RuntimeError("resource_not_found")
            if status_code >= 400:
                raise RuntimeError(f"google_request_failed:{status_code}")
            return response.text
        raise RuntimeError("google_request_unreachable")

    def _calendar_events(
        self,
        *,
        access_token: str,
        window_start: str,
        window_end: str,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            method="GET",
            url=f"{self.calendar_api_base_url}/calendars/primary/events",
            access_token=access_token,
            params={
                "timeMin": window_start,
                "timeMax": window_end,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 50,
            },
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        return [item for item in raw_items if isinstance(item, dict)]
    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        window_start = str(normalized_input["window_start"])
        window_end = str(normalized_input["window_end"])
        items = self._calendar_events(
            access_token=access_token,
            window_start=window_start,
            window_end=window_end,
        )
        results: list[dict[str, Any]] = []
        for item in items:
            summary_raw = item.get("summary")
            summary = summary_raw.strip() if isinstance(summary_raw, str) and summary_raw.strip() else "event"
            source_raw = item.get("htmlLink")
            source = (
                source_raw.strip()
                if isinstance(source_raw, str) and source_raw.strip()
                else f"calendar://{item.get('id', 'event')}"
            )
            start_dt = _parse_google_event_time(item.get("start"))
            end_dt = _parse_google_event_time(item.get("end"))
            if start_dt is None or end_dt is None:
                snippet = summary
            else:
                snippet = f"{to_rfc3339(start_dt)} to {to_rfc3339(end_dt)} {summary}"
            results.append(
                {
                    "title": summary,
                    "source": source,
                    "snippet": snippet,
                    "published_at": _normalize_google_timestamp(item.get("updated")),
                }
            )
            if len(results) >= _MAX_GOOGLE_RESULTS:
                break
        return {
            "results": results,
            "retrieved_at": to_rfc3339(_utcnow()),
            "window_start": window_start,
            "window_end": window_end,
        }

    def calendar_propose_slots(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        attendee_intersection_enabled: bool,
    ) -> dict[str, Any]:
        window_start = _parse_rfc3339(normalized_input.get("window_start"))
        window_end = _parse_rfc3339(normalized_input.get("window_end"))
        if window_start is None or window_end is None or window_end <= window_start:
            raise RuntimeError("schema_invalid")

        duration_raw = normalized_input.get("duration_minutes")
        duration_minutes = duration_raw if isinstance(duration_raw, int) else 30
        if duration_minutes <= 0:
            raise RuntimeError("schema_invalid")

        attendees_raw = normalized_input.get("attendees", [])
        attendees = (
            [
                attendee.strip().lower()
                for attendee in attendees_raw
                if isinstance(attendee, str) and attendee.strip()
            ]
            if isinstance(attendees_raw, list)
            else []
        )

        attendee_recovery_hint: str | None = None
        if attendees and attendee_intersection_enabled:
            busy_intervals = self._freebusy_intervals(
                access_token=access_token,
                window_start=window_start,
                window_end=window_end,
                attendees=attendees,
            )
            attendee_intersection_used = True
        else:
            busy_intervals = self._primary_busy_intervals(
                access_token=access_token,
                window_start=window_start,
                window_end=window_end,
            )
            attendee_intersection_used = not bool(attendees)
            if attendees:
                attendee_recovery_hint = (
                    "Reconnect Google and grant attendee free/busy scope to include "
                    "attendee intersection."
                )

        slots = _propose_slots_from_busy_intervals(
            window_start=window_start,
            window_end=window_end,
            duration=timedelta(minutes=duration_minutes),
            busy_intervals=busy_intervals,
        )
        results: list[dict[str, Any]] = [
            {
                "title": f"slot option {index}",
                "source": "calendar://availability",
                "snippet": (
                    f"{to_rfc3339(slot_start)} to {to_rfc3339(slot_end)}"
                    + (
                        " works for all attendees"
                        if attendee_intersection_used
                        else " available on your calendar only"
                    )
                ),
                "published_at": None,
            }
            for index, (slot_start, slot_end) in enumerate(slots, start=1)
        ]
        if not results:
            results = [
                {
                    "title": "no slots available",
                    "source": "calendar://availability",
                    "snippet": "No matching availability was found in the requested window.",
                    "published_at": None,
                }
            ]
        return {
            "results": results,
            "retrieved_at": to_rfc3339(_utcnow()),
            "attendees_considered": attendees,
            "attendee_intersection_used": attendee_intersection_used,
            "attendee_recovery_hint": attendee_recovery_hint,
        }

    def _primary_busy_intervals(
        self,
        *,
        access_token: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        items = self._calendar_events(
            access_token=access_token,
            window_start=to_rfc3339(window_start),
            window_end=to_rfc3339(window_end),
        )
        intervals: list[tuple[datetime, datetime]] = []
        for item in items:
            start_dt = _parse_google_event_time(item.get("start"))
            end_dt = _parse_google_event_time(item.get("end"))
            if start_dt is None or end_dt is None or end_dt <= start_dt:
                continue
            intervals.append((start_dt, end_dt))
        return _merge_intervals(intervals)

    def _freebusy_intervals(
        self,
        *,
        access_token: str,
        window_start: datetime,
        window_end: datetime,
        attendees: list[str],
    ) -> list[tuple[datetime, datetime]]:
        payload = self._request_json(
            method="POST",
            url=f"{self.calendar_api_base_url}/freeBusy",
            access_token=access_token,
            json_payload={
                "timeMin": to_rfc3339(window_start),
                "timeMax": to_rfc3339(window_end),
                "items": [{"id": "primary"}] + [{"id": attendee} for attendee in attendees],
            },
        )
        calendars_payload = payload.get("calendars")
        if not isinstance(calendars_payload, dict):
            return []
        intervals: list[tuple[datetime, datetime]] = []
        for calendar_state in calendars_payload.values():
            if not isinstance(calendar_state, dict):
                continue
            busy_payload = calendar_state.get("busy")
            if not isinstance(busy_payload, list):
                continue
            for busy_entry in busy_payload:
                if not isinstance(busy_entry, dict):
                    continue
                busy_start = _parse_rfc3339(busy_entry.get("start"))
                busy_end = _parse_rfc3339(busy_entry.get("end"))
                if busy_start is None or busy_end is None or busy_end <= busy_start:
                    continue
                intervals.append((busy_start, busy_end))
        return _merge_intervals(intervals)

    def email_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(normalized_input["query"])
        payload = self._request_json(
            method="GET",
            url=f"{self.gmail_api_base_url}/users/me/messages",
            access_token=access_token,
            params={"q": query, "maxResults": _MAX_GOOGLE_RESULTS},
        )
        raw_messages = payload.get("messages")
        messages = [item for item in raw_messages if isinstance(item, dict)] if isinstance(raw_messages, list) else []
        results: list[dict[str, Any]] = []
        for message in messages[:_MAX_GOOGLE_RESULTS]:
            message_id_raw = message.get("id")
            if not isinstance(message_id_raw, str) or not message_id_raw.strip():
                continue
            message_id = message_id_raw.strip()
            message_payload = self._request_json(
                method="GET",
                url=f"{self.gmail_api_base_url}/users/me/messages/{message_id}",
                access_token=access_token,
                params={
                    "format": "metadata",
                    "metadataHeaders": ["Subject", "From", "Date"],
                },
            )
            subject = _gmail_header_value(message_payload, "Subject") or f"message {message_id}"
            sender = _gmail_header_value(message_payload, "From") or "unknown sender"
            snippet_raw = message_payload.get("snippet")
            snippet = (
                f"{sender} - {snippet_raw.strip()}"
                if isinstance(snippet_raw, str) and snippet_raw.strip()
                else sender
            )
            results.append(
                {
                    "title": subject,
                    "source": f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
                    "snippet": snippet,
                    "published_at": _gmail_internal_date_timestamp(message_payload),
                }
            )
        return {
            "results": results,
            "retrieved_at": to_rfc3339(_utcnow()),
        }

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        message_id = str(normalized_input["message_id"])
        payload = self._request_json(
            method="GET",
            url=f"{self.gmail_api_base_url}/users/me/messages/{message_id}",
            access_token=access_token,
            params={"format": "full"},
        )
        subject = _gmail_header_value(payload, "Subject") or f"email {message_id}"
        snippet_raw = payload.get("snippet")
        snippet = (
            snippet_raw.strip() if isinstance(snippet_raw, str) and snippet_raw.strip() else "(no preview)"
        )
        return {
            "results": [
                {
                    "title": subject,
                    "source": f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
                    "snippet": snippet,
                    "published_at": _gmail_internal_date_timestamp(payload),
                }
            ],
            "retrieved_at": to_rfc3339(_utcnow()),
        }

    def _compose_raw_email_message(self, *, normalized_input: dict[str, Any]) -> str:
        message = EmailMessage()
        to_recipients = normalized_input.get("to", [])
        cc_recipients = normalized_input.get("cc", [])
        bcc_recipients = normalized_input.get("bcc", [])
        if isinstance(to_recipients, list) and to_recipients:
            message["To"] = ", ".join(str(item) for item in to_recipients)
        if isinstance(cc_recipients, list) and cc_recipients:
            message["Cc"] = ", ".join(str(item) for item in cc_recipients)
        if isinstance(bcc_recipients, list) and bcc_recipients:
            message["Bcc"] = ", ".join(str(item) for item in bcc_recipients)
        message["Subject"] = str(normalized_input["subject"])
        message.set_content(str(normalized_input["body"]))
        return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    def calendar_create_event(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": str(normalized_input["title"]),
            "start": {"dateTime": str(normalized_input["start_time"])},
            "end": {"dateTime": str(normalized_input["end_time"])},
        }
        description_raw = normalized_input.get("description")
        if isinstance(description_raw, str) and description_raw.strip():
            payload["description"] = description_raw.strip()
        location_raw = normalized_input.get("location")
        if isinstance(location_raw, str) and location_raw.strip():
            payload["location"] = location_raw.strip()
        attendees_raw = normalized_input.get("attendees", [])
        if isinstance(attendees_raw, list) and attendees_raw:
            payload["attendees"] = [{"email": attendee} for attendee in attendees_raw]

        created_payload = self._request_json(
            method="POST",
            url=f"{self.calendar_api_base_url}/calendars/primary/events",
            access_token=access_token,
            json_payload=payload,
        )
        event_id_raw = created_payload.get("id")
        event_id = event_id_raw.strip() if isinstance(event_id_raw, str) and event_id_raw.strip() else "unknown"
        source_raw = created_payload.get("htmlLink")
        source = (
            source_raw.strip()
            if isinstance(source_raw, str) and source_raw.strip()
            else f"calendar://{event_id}"
        )
        return {
            "status": "created",
            "event_id": event_id,
            "title": str(normalized_input["title"]),
            "start_time": str(normalized_input["start_time"]),
            "end_time": str(normalized_input["end_time"]),
            "provider_event_ref": source,
        }

    def email_create_draft(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        raw_message = self._compose_raw_email_message(normalized_input=normalized_input)
        payload = self._request_json(
            method="POST",
            url=f"{self.gmail_api_base_url}/users/me/drafts",
            access_token=access_token,
            json_payload={"message": {"raw": raw_message}},
        )
        draft_id_raw = payload.get("id")
        if not isinstance(draft_id_raw, str) or not draft_id_raw.strip():
            draft_id_raw = (
                payload.get("message", {}).get("id")
                if isinstance(payload.get("message"), dict)
                else None
            )
        draft_id = (
            draft_id_raw.strip()
            if isinstance(draft_id_raw, str) and draft_id_raw.strip()
            else None
        )
        return {
            "provider_draft_ref": f"gmail://draft/{draft_id}" if draft_id is not None else None,
        }

    def email_send(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        raw_message = self._compose_raw_email_message(normalized_input=normalized_input)
        payload = self._request_json(
            method="POST",
            url=f"{self.gmail_api_base_url}/users/me/messages/send",
            access_token=access_token,
            json_payload={"raw": raw_message},
        )
        message_id_raw = payload.get("id")
        message_id = (
            message_id_raw.strip()
            if isinstance(message_id_raw, str) and message_id_raw.strip()
            else "unknown"
        )
        return {
            "status": "sent",
            "message_id": message_id,
            "provider_message_ref": f"gmail://sent/{message_id}",
            "to": normalized_input.get("to", []),
            "subject": normalized_input.get("subject"),
        }

    def _drive_file_source(self, *, file_id: str, metadata: dict[str, Any]) -> str:
        source_raw = metadata.get("webViewLink")
        if isinstance(source_raw, str) and source_raw.strip():
            return source_raw.strip()
        return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view"

    def _drive_size_bytes(self, raw_size: Any) -> int | None:
        if isinstance(raw_size, int):
            return raw_size if raw_size >= 0 else None
        if isinstance(raw_size, str) and raw_size.strip():
            try:
                parsed = int(raw_size.strip())
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        return None

    def _drive_metadata_snippet(self, metadata: dict[str, Any]) -> str:
        parts: list[str] = []
        mime_type_raw = metadata.get("mimeType")
        if isinstance(mime_type_raw, str) and mime_type_raw.strip():
            parts.append(f"mime_type={mime_type_raw.strip()}")
        owner_value = None
        owners_raw = metadata.get("owners")
        if isinstance(owners_raw, list):
            for owner in owners_raw:
                if not isinstance(owner, dict):
                    continue
                owner_raw = owner.get("emailAddress") or owner.get("displayName")
                if isinstance(owner_raw, str) and owner_raw.strip():
                    owner_value = owner_raw.strip()
                    break
        if owner_value is not None:
            parts.append(f"owner={owner_value}")
        modified_raw = _normalize_google_timestamp(metadata.get("modifiedTime"))
        if modified_raw is not None:
            parts.append(f"modified={modified_raw}")
        size_bytes = self._drive_size_bytes(metadata.get("size"))
        if size_bytes is not None:
            parts.append(f"size_bytes={size_bytes}")
        if parts:
            return " ".join(parts)
        return "drive file metadata available"

    def _drive_read_outcome_output(
        self,
        *,
        file_id: str,
        title: str,
        source: str,
        published_at: str | None,
        status: Literal["unsupported", "too_large", "unavailable"],
        reason_code: str,
        recovery: str,
        snippet: str,
    ) -> dict[str, Any]:
        return {
            "file_id": file_id,
            "retrieved_at": to_rfc3339(_utcnow()),
            "content_excerpt": "",
            "truncated": False,
            "read_outcome": {
                "status": status,
                "reason_code": reason_code,
                "recovery": recovery,
            },
            "results": [
                {
                    "title": title,
                    "source": source,
                    "snippet": snippet,
                    "published_at": published_at,
                }
            ],
        }

    def drive_search(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(normalized_input["query"]).strip()
        escaped_query = query.replace("\\", "\\\\").replace("'", "\\'")
        drive_query = (
            f"(name contains '{escaped_query}' or fullText contains '{escaped_query}') "
            "and trashed = false"
        )
        payload = self._request_json(
            method="GET",
            url=f"{self.drive_api_base_url}/files",
            access_token=access_token,
            params={
                "q": drive_query,
                "pageSize": _MAX_GOOGLE_RESULTS,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "files(id,name,mimeType,modifiedTime,webViewLink,size,"
                    "owners(displayName,emailAddress))"
                ),
            },
        )
        raw_files = payload.get("files")
        files = [item for item in raw_files if isinstance(item, dict)] if isinstance(raw_files, list) else []
        results: list[dict[str, Any]] = []
        for item in files[:_MAX_GOOGLE_RESULTS]:
            file_id_raw = item.get("id")
            file_id = file_id_raw.strip() if isinstance(file_id_raw, str) and file_id_raw.strip() else "unknown"
            title_raw = item.get("name")
            title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else f"file {file_id}"
            results.append(
                {
                    "title": title,
                    "source": self._drive_file_source(file_id=file_id, metadata=item),
                    "snippet": self._drive_metadata_snippet(item),
                    "published_at": _normalize_google_timestamp(item.get("modifiedTime")),
                }
            )
        return {
            "query": query,
            "retrieved_at": to_rfc3339(_utcnow()),
            "results": results,
        }

    def drive_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        file_id = str(normalized_input["file_id"]).strip()
        metadata_url = f"{self.drive_api_base_url}/files/{quote(file_id, safe='')}"
        try:
            metadata = self._request_json(
                method="GET",
                url=metadata_url,
                access_token=access_token,
                params={
                    "fields": "id,name,mimeType,modifiedTime,webViewLink,size,owners(displayName,emailAddress)",
                    "supportsAllDrives": "true",
                },
            )
        except RuntimeError as exc:
            if safe_failure_reason(str(exc), fallback="drive_read_unavailable").lower() == "resource_not_found":
                fallback_source = f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view"
                return self._drive_read_outcome_output(
                    file_id=file_id,
                    title=f"Drive file {file_id}",
                    source=fallback_source,
                    published_at=None,
                    status="unavailable",
                    reason_code="drive_read_unavailable",
                    recovery="Verify file access and file ID, then retry.",
                    snippet="File is unavailable. Verify file access and file ID, then retry.",
                )
            raise

        title_raw = metadata.get("name")
        title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else f"Drive file {file_id}"
        source = self._drive_file_source(file_id=file_id, metadata=metadata)
        published_at = _normalize_google_timestamp(metadata.get("modifiedTime"))
        mime_type_raw = metadata.get("mimeType")
        mime_type = mime_type_raw.strip() if isinstance(mime_type_raw, str) and mime_type_raw.strip() else ""
        size_bytes = self._drive_size_bytes(metadata.get("size"))
        if size_bytes is not None and size_bytes > _MAX_DRIVE_READ_BYTES:
            return self._drive_read_outcome_output(
                file_id=file_id,
                title=title,
                source=source,
                published_at=published_at,
                status="too_large",
                reason_code="drive_read_too_large",
                recovery="Open the file and request a smaller section, then retry.",
                snippet="File exceeds read budget. Request a smaller section and retry.",
            )

        try:
            if mime_type == _DRIVE_NATIVE_DOC_MIME_TYPE:
                content_text = self._request_text(
                    method="GET",
                    url=f"{self.drive_api_base_url}/files/{quote(file_id, safe='')}/export",
                    access_token=access_token,
                    params={
                        "mimeType": _DRIVE_PLAIN_TEXT_EXPORT_MIME_TYPE,
                        "supportsAllDrives": "true",
                    },
                )
            elif mime_type in _DRIVE_TEXT_LIKE_MIME_TYPES or mime_type.startswith("text/"):
                content_text = self._request_text(
                    method="GET",
                    url=f"{self.drive_api_base_url}/files/{quote(file_id, safe='')}",
                    access_token=access_token,
                    params={"alt": "media", "supportsAllDrives": "true"},
                )
            else:
                return self._drive_read_outcome_output(
                    file_id=file_id,
                    title=title,
                    source=source,
                    published_at=published_at,
                    status="unsupported",
                    reason_code="drive_read_unsupported",
                    recovery="Export this file to Google Docs or plain text, then retry.",
                    snippet=(
                        "Unsupported content format. Export this file to Google Docs or plain text, "
                        "then retry."
                    ),
                )
        except RuntimeError as exc:
            if safe_failure_reason(str(exc), fallback="drive_read_unavailable").lower() == "resource_not_found":
                return self._drive_read_outcome_output(
                    file_id=file_id,
                    title=title,
                    source=source,
                    published_at=published_at,
                    status="unavailable",
                    reason_code="drive_read_unavailable",
                    recovery="Verify file access and file ID, then retry.",
                    snippet="File is unavailable. Verify file access and file ID, then retry.",
                )
            raise

        normalized_content = " ".join(content_text.split())
        if not normalized_content:
            normalized_content = "(no readable text content found)"
        truncated = len(normalized_content) > _MAX_DRIVE_READ_CHARS
        content_excerpt = normalized_content[:_MAX_DRIVE_READ_CHARS].rstrip()
        if truncated:
            content_excerpt = f"{content_excerpt}..."
        return {
            "file_id": file_id,
            "retrieved_at": to_rfc3339(_utcnow()),
            "content_excerpt": content_excerpt,
            "truncated": truncated,
            "read_outcome": {
                "status": "ok",
                "reason_code": None,
                "recovery": None,
            },
            "results": [
                {
                    "title": title,
                    "source": source,
                    "snippet": content_excerpt,
                    "published_at": published_at,
                }
            ],
        }

    def drive_share(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        file_id = str(normalized_input["file_id"]).strip()
        grantee_email = str(normalized_input["grantee_email"]).strip().lower()
        role = str(normalized_input["role"]).strip().lower()
        payload = self._request_json(
            method="POST",
            url=f"{self.drive_api_base_url}/files/{quote(file_id, safe='')}/permissions",
            access_token=access_token,
            params={"sendNotificationEmail": "true", "supportsAllDrives": "true"},
            json_payload={
                "type": "user",
                "emailAddress": grantee_email,
                "role": role,
            },
        )
        permission_id_raw = payload.get("id")
        permission_id = (
            permission_id_raw.strip()
            if isinstance(permission_id_raw, str) and permission_id_raw.strip()
            else "unknown"
        )
        return {
            "status": "shared",
            "file_id": file_id,
            "grantee_email": grantee_email,
            "role": role,
            "permission_id": permission_id,
        }


def _parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_google_event_time(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    date_time_raw = value.get("dateTime")
    parsed_date_time = _parse_rfc3339(date_time_raw)
    if parsed_date_time is not None:
        return parsed_date_time
    date_raw = value.get("date")
    if not isinstance(date_raw, str) or not date_raw.strip():
        return None
    return _parse_rfc3339(f"{date_raw.strip()}T00:00:00Z")


def _normalize_google_timestamp(value: Any) -> str | None:
    parsed = _parse_rfc3339(value)
    if parsed is None:
        return None
    return to_rfc3339(parsed)


def _google_error_payload(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error_payload = payload.get("error")
    return error_payload if isinstance(error_payload, dict) else None


def _is_google_scope_failure(response: httpx.Response) -> bool:
    error_payload = _google_error_payload(response)
    if error_payload is None:
        return False

    message_raw = error_payload.get("message")
    if isinstance(message_raw, str) and message_raw.strip():
        normalized_message = message_raw.strip().lower()
        if "authentication scope" in normalized_message:
            return True
        if normalized_message in {"insufficient permission", "insufficient permissions"}:
            return True

    errors_raw = error_payload.get("errors")
    if not isinstance(errors_raw, list):
        return False
    for error_entry in errors_raw:
        if not isinstance(error_entry, dict):
            continue
        reason_raw = error_entry.get("reason")
        if not isinstance(reason_raw, str) or not reason_raw.strip():
            continue
        normalized_reason = reason_raw.strip().lower()
        if normalized_reason in {"insufficientpermissions", "insufficient_permissions"}:
            return True
    return False


def _google_error_reason(response: httpx.Response) -> str:
    error_payload = _google_error_payload(response)
    if error_payload is None:
        return safe_failure_reason(response.text, fallback="google_request_failed")

    message_raw = error_payload.get("message")
    if isinstance(message_raw, str) and message_raw.strip():
        return message_raw.strip()
    errors_raw = error_payload.get("errors")
    if isinstance(errors_raw, list):
        for error_entry in errors_raw:
            if not isinstance(error_entry, dict):
                continue
            reason_raw = error_entry.get("reason")
            if isinstance(reason_raw, str) and reason_raw.strip():
                return reason_raw.strip()
    return safe_failure_reason(response.text, fallback="google_request_failed")


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _slot_overlaps_busy(
    *,
    slot_start: datetime,
    slot_end: datetime,
    busy_intervals: list[tuple[datetime, datetime]],
) -> bool:
    for busy_start, busy_end in busy_intervals:
        if slot_start < busy_end and slot_end > busy_start:
            return True
    return False


def _propose_slots_from_busy_intervals(
    *,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    busy_intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    slots: list[tuple[datetime, datetime]] = []
    pointer = window_start
    step = timedelta(minutes=30)
    merged_busy = _merge_intervals(busy_intervals)
    while pointer + duration <= window_end and len(slots) < _MAX_GOOGLE_RESULTS:
        slot_end = pointer + duration
        if not _slot_overlaps_busy(
            slot_start=pointer,
            slot_end=slot_end,
            busy_intervals=merged_busy,
        ):
            slots.append((pointer, slot_end))
        pointer += step
    return slots


def _gmail_header_value(payload: dict[str, Any], header_name: str) -> str | None:
    payload_root = payload.get("payload")
    if not isinstance(payload_root, dict):
        return None
    headers_raw = payload_root.get("headers")
    if not isinstance(headers_raw, list):
        return None
    for header in headers_raw:
        if not isinstance(header, dict):
            continue
        name_raw = header.get("name")
        value_raw = header.get("value")
        if not isinstance(name_raw, str) or not isinstance(value_raw, str):
            continue
        if name_raw.strip().lower() == header_name.lower():
            value = value_raw.strip()
            if value:
                return value
    return None


def _gmail_internal_date_timestamp(payload: dict[str, Any]) -> str | None:
    internal_date_raw = payload.get("internalDate")
    if not isinstance(internal_date_raw, str) or not internal_date_raw.strip():
        return None
    try:
        millis = int(internal_date_raw.strip())
    except ValueError:
        return None
    if millis < 0:
        return None
    parsed = datetime.fromtimestamp(millis / 1000, tz=UTC)
    return to_rfc3339(parsed)


def _normalize_scope_list(raw_scopes: Any) -> list[str]:
    if isinstance(raw_scopes, str):
        candidates = raw_scopes.split()
    elif isinstance(raw_scopes, list):
        candidates = [entry for entry in raw_scopes if isinstance(entry, str)]
    else:
        candidates = []
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        scope = candidate.strip()
        if not scope or scope in seen:
            continue
        seen.add(scope)
        normalized.append(scope)
    return normalized


def _normalize_capability_intent(capability_intent: str | None) -> str | None:
    if capability_intent is None:
        return None
    normalized = capability_intent.strip()
    if not normalized:
        return None
    return normalized


def _resolve_reconnect_scopes(
    *,
    granted_scopes: list[str],
    capability_intent: str | None,
) -> tuple[list[str], str | None]:
    requested_scopes = set(_normalize_scope_list(granted_scopes))
    if not requested_scopes:
        requested_scopes = set(_GOOGLE_MINIMUM_READ_SCOPES)
    normalized_intent = _normalize_capability_intent(capability_intent)
    if normalized_intent is not None:
        required_scopes = GOOGLE_CAPABILITY_SCOPES.get(normalized_intent)
        if required_scopes is None:
            msg = "unsupported capability intent"
            raise RuntimeError(msg)
        requested_scopes.update(required_scopes)
        requested_scopes.update(GOOGLE_RECONNECT_INTENT_EXTRA_SCOPES.get(normalized_intent, set()))
    return sorted(requested_scopes), normalized_intent


def _classify_google_provider_failure(error_reason: str) -> str | None:
    normalized = error_reason.strip().lower()
    if not normalized:
        return None
    if normalized == "google_upstream_timeout":
        return "provider_timeout"
    if normalized == "google_upstream_network_failure":
        return "provider_network_failure"
    if normalized == "google_upstream_429":
        return "provider_rate_limited"
    if normalized.startswith("google_upstream_5"):
        return "provider_upstream_failure"
    if normalized == "google_forbidden":
        return "provider_permission_denied"
    if normalized.startswith("google_request_failed:4"):
        return "provider_request_rejected"
    if normalized == "resource_not_found":
        return "resource_unavailable"
    if normalized == "google_invalid_payload":
        return "provider_invalid_payload"
    if normalized == "google_request_unreachable":
        return "provider_unreachable"
    return None


def _canonical_draft_output(
    *,
    normalized_input: dict[str, Any],
    provider_projection: dict[str, Any],
) -> dict[str, Any]:
    provider_ref_raw = provider_projection.get("provider_draft_ref")
    provider_ref = (
        provider_ref_raw.strip()
        if isinstance(provider_ref_raw, str) and provider_ref_raw.strip()
        else None
    )
    return {
        "status": "drafted_not_sent",
        "delivery_state": "draft_only",
        "sent": False,
        "draft": {
            "to": normalized_input.get("to", []),
            "cc": normalized_input.get("cc", []),
            "bcc": normalized_input.get("bcc", []),
            "subject": normalized_input.get("subject"),
            "body": normalized_input.get("body"),
        },
        "provider_draft_ref": provider_ref,
    }


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _derive_secret_bytes(secret: str) -> bytes:
    normalized = secret.strip()
    if not normalized:
        normalized = "dev-local-connector-secret"
    return hashlib.sha256(normalized.encode("utf-8")).digest()


def _parse_connector_key_entries(configured_keys: str) -> dict[str, str]:
    normalized = configured_keys.strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
    except ValueError:
        entries: dict[str, str] = {}
        for raw_item in normalized.split(","):
            item = raw_item.strip()
            if not item:
                continue
            version, sep, key_value = item.partition(":")
            if not sep:
                msg = "connector_encryption_keys entry must be version:key"
                raise RuntimeError(msg)
            version_normalized = version.strip()
            key_normalized = key_value.strip()
            if not version_normalized or not key_normalized:
                msg = "connector_encryption_keys entry must not be blank"
                raise RuntimeError(msg)
            entries[version_normalized] = key_normalized
        return entries
    if not isinstance(payload, dict):
        msg = "connector_encryption_keys must be JSON object or version:key list"
        raise RuntimeError(msg)
    entries = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            msg = "connector_encryption_keys JSON must map string versions to string keys"
            raise RuntimeError(msg)
        key_normalized = key.strip()
        value_normalized = value.strip()
        if not key_normalized or not value_normalized:
            msg = "connector_encryption_keys JSON entries must not be blank"
            raise RuntimeError(msg)
        entries[key_normalized] = value_normalized
    return entries


def _decode_aead_key(raw_value: str) -> bytes:
    try:
        decoded = _urlsafe_b64decode(raw_value.strip())
    except Exception as exc:
        raise RuntimeError("connector encryption key must be base64url encoded") from exc
    if len(decoded) not in {16, 24, 32}:
        msg = "connector encryption key length must be 16, 24, or 32 bytes"
        raise RuntimeError(msg)
    return decoded


@dataclass(slots=True, frozen=True)
class ConnectorTokenCipher:
    active_key_version: str
    keys_by_version: dict[str, bytes]
    legacy_secret: str | None = None
    allow_legacy_key_alias: bool = False

    def __post_init__(self) -> None:
        active = self.active_key_version.strip()
        if not active:
            raise RuntimeError("active_key_version must not be blank")
        if active not in self.keys_by_version:
            raise RuntimeError("active_key_version is missing from keys_by_version")
        copied: dict[str, bytes] = {}
        for version, key_bytes in self.keys_by_version.items():
            if not version.strip():
                raise RuntimeError("key version must not be blank")
            if len(key_bytes) not in {16, 24, 32}:
                raise RuntimeError("aead key length must be 16, 24, or 32 bytes")
            copied[version] = bytes(key_bytes)
        object.__setattr__(self, "active_key_version", active)
        object.__setattr__(self, "keys_by_version", copied)

    @classmethod
    def from_config(
        cls,
        *,
        active_key_version: str,
        configured_keys: str | None,
        fallback_secret: str,
    ) -> ConnectorTokenCipher:
        keys: dict[str, bytes] = {}
        if configured_keys is not None:
            entries = _parse_connector_key_entries(configured_keys)
            keys = {
                version: _decode_aead_key(raw_key)
                for version, raw_key in entries.items()
            }
        has_configured_keyring = bool(keys)
        active = active_key_version.strip() or "v1"
        if not keys:
            keys[active] = _derive_secret_bytes(fallback_secret)
        if active not in keys:
            msg = "active connector encryption key version is missing from configured keyring"
            raise RuntimeError(msg)
        return cls(
            active_key_version=active,
            keys_by_version=keys,
            legacy_secret=fallback_secret,
            allow_legacy_key_alias=not has_configured_keyring,
        )

    def encrypt(self, plaintext: str) -> str:
        key_bytes = self.keys_by_version[self.active_key_version]
        nonce = secrets.token_bytes(12)
        aad = f"ariel.connector.google:{self.active_key_version}".encode("utf-8")
        cipher = AESGCM(key_bytes)
        ciphertext = cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return (
            f"aeadv1:{self.active_key_version}:"
            f"{_urlsafe_b64encode(nonce)}:{_urlsafe_b64encode(ciphertext)}"
        )

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext.startswith("aeadv1:"):
            try:
                _, version, nonce_b64, payload_b64 = ciphertext.split(":", maxsplit=3)
            except ValueError as exc:
                raise RuntimeError("encrypted value is malformed") from exc
            key_bytes = self.keys_by_version.get(version)
            if (
                key_bytes is None
                and self.allow_legacy_key_alias
                and self.legacy_secret is not None
            ):
                # Compatibility path for single-secret environments during key-version relabeling.
                key_bytes = _derive_secret_bytes(self.legacy_secret)
            if key_bytes is None:
                raise RuntimeError("unknown encryption key version")
            nonce = _urlsafe_b64decode(nonce_b64)
            if len(nonce) != 12:
                raise RuntimeError("encrypted value nonce is invalid")
            payload = _urlsafe_b64decode(payload_b64)
            aad = f"ariel.connector.google:{version}".encode("utf-8")
            try:
                plaintext = AESGCM(key_bytes).decrypt(nonce, payload, aad)
            except InvalidTag as exc:
                raise RuntimeError("encrypted value integrity check failed") from exc
            return plaintext.decode("utf-8")
        if self.legacy_secret is None:
            raise RuntimeError("legacy secret not configured")
        return _decrypt_secret_legacy(ciphertext=ciphertext, secret=self.legacy_secret)


def _decrypt_secret_legacy(*, ciphertext: str, secret: str) -> str:
    prefix, sep, encoded = ciphertext.partition(":")
    if not sep or not prefix.strip():
        raise RuntimeError("encrypted value is malformed")
    payload = _urlsafe_b64decode(encoded)
    if len(payload) < 16 + 32:
        raise RuntimeError("encrypted value length is invalid")
    secret_bytes = _derive_secret_bytes(secret)
    nonce = payload[:16]
    mac = payload[-32:]
    body = payload[16:-32]
    expected_mac = hmac.new(secret_bytes, nonce + body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise RuntimeError("encrypted value integrity check failed")
    # Legacy stream-cipher compatibility for pre-hardening ciphertext.
    chunks: list[bytes] = []
    counter = 0
    total = 0
    while total < len(body):
        digest = hashlib.sha256(secret_bytes + nonce + counter.to_bytes(4, "big")).digest()
        chunks.append(digest)
        total += len(digest)
        counter += 1
    stream = b"".join(chunks)[: len(body)]
    plaintext_bytes = bytes(a ^ b for a, b in zip(body, stream, strict=False))
    return plaintext_bytes.decode("utf-8")


def _encrypt_secret(
    *,
    plaintext: str,
    secret: str,
    key_version: str,
    encryption_keys: str | None = None,
) -> str:
    cipher = ConnectorTokenCipher.from_config(
        active_key_version=key_version,
        configured_keys=encryption_keys,
        fallback_secret=secret,
    )
    return cipher.encrypt(plaintext)


def _decrypt_secret(
    *,
    ciphertext: str,
    secret: str,
    expected_key_version: str,
    encryption_keys: str | None = None,
) -> str:
    cipher = ConnectorTokenCipher.from_config(
        active_key_version=expected_key_version,
        configured_keys=encryption_keys,
        fallback_secret=secret,
    )
    return cipher.decrypt(ciphertext)


def _pkce_verifier() -> str:
    raw = secrets.token_urlsafe(64)
    return raw[:96]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _urlsafe_b64encode(digest)


def _readiness_failure_kind(error_code: str | None) -> Literal["none", "blocking", "transient", "other"]:
    if error_code is None:
        return "none"
    normalized = error_code.strip().lower()
    if not normalized:
        return "none"
    if normalized in _READINESS_BLOCKING_FAILURE_CODES:
        return "blocking"
    if normalized in _READINESS_TRANSIENT_FAILURE_CODES:
        return "transient"
    return "other"


def _is_blocking_readiness_failure(error_code: str | None) -> bool:
    return _readiness_failure_kind(error_code) == "blocking"


def _set_connector_error(
    *,
    connector: GoogleConnectorRecord,
    error_code: str,
    now_fn: Callable[[], datetime],
    preserve_existing_blocking: bool = True,
) -> None:
    normalized_error_code = error_code.strip()
    if not normalized_error_code:
        return
    if preserve_existing_blocking and _is_blocking_readiness_failure(
        connector.last_error_code
    ) and not _is_blocking_readiness_failure(normalized_error_code):
        connector.updated_at = now_fn()
        return
    connector.last_error_code = normalized_error_code
    connector.last_error_at = now_fn()
    connector.updated_at = now_fn()


def _clear_connector_error(
    *,
    connector: GoogleConnectorRecord,
    now_fn: Callable[[], datetime],
    preserve_existing_blocking: bool,
) -> None:
    if preserve_existing_blocking and _is_blocking_readiness_failure(connector.last_error_code):
        connector.updated_at = now_fn()
        return
    connector.last_error_code = None
    connector.last_error_at = None
    connector.updated_at = now_fn()


def _readiness(connector: GoogleConnectorRecord | None) -> str:
    if connector is None:
        return "not_connected"
    if connector.status == "not_connected":
        return "not_connected"
    if connector.status != "connected":
        return "reconnect_required"
    if _is_blocking_readiness_failure(connector.last_error_code):
        return "reconnect_required"
    granted_scopes = set(_normalize_scope_list(connector.granted_scopes))
    if not _GOOGLE_MINIMUM_READ_SCOPES.issubset(granted_scopes):
        return "reconnect_required"
    if connector.access_token_enc is None:
        return "reconnect_required"
    return "connected"


def _connector_payload(connector: GoogleConnectorRecord | None) -> dict[str, Any]:
    if connector is None:
        return {
            "id": GOOGLE_CONNECTOR_ID,
            "provider": GOOGLE_PROVIDER,
            "status": "not_connected",
            "readiness": "not_connected",
            "account_subject": None,
            "account_email": None,
            "granted_scopes": [],
            "access_token_expires_at": None,
            "token_obtained_at": None,
            "last_error_code": None,
            "last_error_at": None,
        }
    scopes = _normalize_scope_list(connector.granted_scopes)
    return {
        "id": connector.id,
        "provider": connector.provider,
        "status": connector.status,
        "readiness": _readiness(connector),
        "account_subject": connector.account_subject,
        "account_email": connector.account_email,
        "granted_scopes": scopes,
        "access_token_expires_at": (
            to_rfc3339(connector.access_token_expires_at)
            if connector.access_token_expires_at is not None
            else None
        ),
        "token_obtained_at": (
            to_rfc3339(connector.token_obtained_at) if connector.token_obtained_at is not None else None
        ),
        "last_error_code": connector.last_error_code,
        "last_error_at": (
            to_rfc3339(connector.last_error_at) if connector.last_error_at is not None else None
        ),
    }


def _append_connector_event(
    *,
    db: Session,
    connector_id: str,
    event_type: str,
    payload_data: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    event = GoogleConnectorEventRecord(
        id=new_id_fn("gce"),
        connector_id=connector_id,
        event_type=event_type,
        payload=redact_json_value(payload_data),
        created_at=now_fn(),
    )
    db.add(event)


def _connector_event_payload(connector: GoogleConnectorRecord, *, scopes: list[str]) -> dict[str, Any]:
    return {
        "connector_id": connector.id,
        "provider": connector.provider,
        "account_subject": connector.account_subject,
        "account_email": connector.account_email,
        "granted_scopes": scopes,
    }


@dataclass(slots=True)
class GoogleConnectorRuntime:
    oauth_client: GoogleOAuthClient
    workspace_provider: GoogleWorkspaceProvider
    redirect_uri: str
    oauth_state_ttl_seconds: int
    encryption_secret: str
    encryption_key_version: str
    encryption_keys: str | None = None

    def _connector_for_update(
        self,
        *,
        db: Session,
    ) -> GoogleConnectorRecord | None:
        return db.scalar(
            select(GoogleConnectorRecord)
            .where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID)
            .with_for_update()
            .limit(1)
        )

    def _ensure_connector(
        self,
        *,
        db: Session,
        now_fn: Callable[[], datetime],
    ) -> GoogleConnectorRecord:
        existing = self._connector_for_update(db=db)
        if existing is not None:
            return existing
        now = now_fn()
        connector = GoogleConnectorRecord(
            id=GOOGLE_CONNECTOR_ID,
            provider=GOOGLE_PROVIDER,
            status="not_connected",
            account_subject=None,
            account_email=None,
            granted_scopes=[],
            access_token_enc=None,
            refresh_token_enc=None,
            access_token_expires_at=None,
            token_obtained_at=None,
            encryption_key_version=self.encryption_key_version,
            last_error_code=None,
            last_error_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(connector)
        db.flush()
        return connector

    def status_payload(
        self,
        *,
        db: Session,
        now_fn: Callable[[], datetime],
    ) -> dict[str, Any]:
        connector = self._ensure_connector(db=db, now_fn=now_fn)
        return _connector_payload(connector)

    def start_oauth(
        self,
        *,
        db: Session,
        reconnect: bool,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
        capability_intent: str | None = None,
    ) -> dict[str, Any]:
        connector = self._ensure_connector(db=db, now_fn=now_fn)
        if reconnect:
            try:
                requested_scopes, normalized_capability_intent = _resolve_reconnect_scopes(
                    granted_scopes=_normalize_scope_list(connector.granted_scopes),
                    capability_intent=capability_intent,
                )
            except RuntimeError as exc:
                raise GoogleConnectorError(
                    status_code=400,
                    code="E_CONNECTOR_RECONNECT_INVALID_INTENT",
                    message="google reconnect capability intent is invalid",
                    details={"reason": safe_failure_reason(str(exc), fallback="invalid_capability_intent")},
                    retryable=False,
                ) from exc
        else:
            requested_scopes = sorted(_GOOGLE_MINIMUM_READ_SCOPES)
            normalized_capability_intent = None
        state_handle = f"st_{secrets.token_urlsafe(24)}"
        verifier = _pkce_verifier()
        challenge = _pkce_challenge(verifier)
        now = now_fn()
        expires_at = now + timedelta(seconds=max(30, self.oauth_state_ttl_seconds))
        state_record = GoogleOAuthStateRecord(
            id=new_id_fn("gos"),
            state_handle=state_handle,
            flow="reconnect" if reconnect else "connect",
            requested_scopes=requested_scopes,
            pkce_verifier_enc=_encrypt_secret(
                plaintext=verifier,
                secret=self.encryption_secret,
                key_version=self.encryption_key_version,
                encryption_keys=self.encryption_keys,
            ),
            pkce_challenge=challenge,
            redirect_uri=self.redirect_uri,
            expires_at=expires_at,
            consumed_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(state_record)
        db.flush()

        event_type = (
            "evt.connector.google.reconnect.started"
            if reconnect
            else "evt.connector.google.connect.started"
        )
        started_event_payload = {
            **_connector_event_payload(connector, scopes=requested_scopes),
            "requested_scopes": requested_scopes,
            "state_expires_at": to_rfc3339(expires_at),
        }
        if normalized_capability_intent is not None:
            started_event_payload["capability_intent"] = normalized_capability_intent
        _append_connector_event(
            db=db,
            connector_id=connector.id,
            event_type=event_type,
            payload_data=started_event_payload,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )

        try:
            authorization_url = self.oauth_client.build_authorization_url(
                state=state_handle,
                code_challenge=challenge,
                scopes=requested_scopes,
                redirect_uri=self.redirect_uri,
                prompt_consent=reconnect,
            )
        except Exception as exc:
            reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
            _set_connector_error(
                connector=connector,
                error_code="oauth_start_failed",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            failed_event_type = (
                "evt.connector.google.reconnect.failed"
                if reconnect
                else "evt.connector.google.connect.failed"
            )
            failed_payload = {
                **_connector_event_payload(connector, scopes=requested_scopes),
                "requested_scopes": requested_scopes,
                "failure_reason": reason,
            }
            if normalized_capability_intent is not None:
                failed_payload["capability_intent"] = normalized_capability_intent
            _append_connector_event(
                db=db,
                connector_id=connector.id,
                event_type=failed_event_type,
                payload_data=failed_payload,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            raise GoogleConnectorError(
                status_code=503,
                code="E_CONNECTOR_START_FAILED",
                message="google connector start failed",
                details={"reason": reason},
                retryable=True,
            ) from exc

        return {
            "connector": _connector_payload(connector),
            "oauth": {
                "authorization_url": authorization_url,
                "state": state_handle,
                "expires_at": to_rfc3339(expires_at),
                "requested_scopes": requested_scopes,
                "capability_intent": normalized_capability_intent,
            },
        }

    def _callback_invalid(
        self,
        *,
        db: Session,
        connector: GoogleConnectorRecord,
        flow: str,
        reason: str,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> GoogleConnectorError:
        _set_connector_error(
            connector=connector,
            error_code=reason,
            now_fn=now_fn,
            preserve_existing_blocking=True,
        )
        failed_event_type = (
            "evt.connector.google.reconnect.failed" if flow == "reconnect" else "evt.connector.google.connect.failed"
        )
        _append_connector_event(
            db=db,
            connector_id=connector.id,
            event_type=failed_event_type,
            payload_data={
                **_connector_event_payload(
                    connector,
                    scopes=_normalize_scope_list(connector.granted_scopes),
                ),
                "failure_reason": reason,
            },
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        return GoogleConnectorError(
            status_code=400,
            code="E_CONNECTOR_CALLBACK_INVALID",
            message="google connector callback was rejected",
            details={"reason": reason},
            retryable=False,
        )

    def complete_oauth_callback(
        self,
        *,
        db: Session,
        state: str | None,
        code: str | None,
        error: str | None,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> dict[str, Any]:
        connector = self._ensure_connector(db=db, now_fn=now_fn)
        if state is None or not state.strip():
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow="connect",
                reason="missing_state",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )

        state_record = db.scalar(
            select(GoogleOAuthStateRecord)
            .where(GoogleOAuthStateRecord.state_handle == state.strip())
            .with_for_update()
            .limit(1)
        )
        flow = state_record.flow if state_record is not None else "connect"
        if state_record is None:
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="invalid_state",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )

        if state_record.consumed_at is not None:
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="state_replayed",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        now = now_fn()
        if state_record.expires_at < now:
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="state_expired",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        if error is not None and error.strip():
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason=safe_failure_reason(error, fallback="oauth_provider_error"),
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        if code is None or not code.strip():
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="missing_code",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )

        state_record.consumed_at = now
        state_record.updated_at = now
        try:
            verifier = _decrypt_secret(
                ciphertext=state_record.pkce_verifier_enc,
                secret=self.encryption_secret,
                expected_key_version=self.encryption_key_version,
                encryption_keys=self.encryption_keys,
            )
        except Exception as exc:
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason=safe_failure_reason(
                    str(exc),
                    fallback=f"unexpected {exc.__class__.__name__}",
                ),
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            ) from exc

        try:
            token_payload = self.oauth_client.exchange_code_for_tokens(
                code=code.strip(),
                code_verifier=verifier,
                redirect_uri=state_record.redirect_uri,
                state=state_record.state_handle,
            )
        except Exception as exc:
            reason = safe_failure_reason(
                str(exc),
                fallback=f"unexpected {exc.__class__.__name__}",
            )
            failed_event_type = (
                "evt.connector.google.reconnect.failed"
                if flow == "reconnect"
                else "evt.connector.google.connect.failed"
            )
            connector.status = "error"
            _set_connector_error(
                connector=connector,
                error_code="oauth_exchange_failed",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            _append_connector_event(
                db=db,
                connector_id=connector.id,
                event_type=failed_event_type,
                payload_data={
                    **_connector_event_payload(
                        connector,
                        scopes=_normalize_scope_list(connector.granted_scopes),
                    ),
                    "failure_reason": reason,
                },
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            raise GoogleConnectorError(
                status_code=502,
                code="E_CONNECTOR_CALLBACK_FAILED",
                message="google connector callback failed",
                details={"reason": reason},
                retryable=True,
            ) from exc

        account_subject_raw = token_payload.get("account_subject")
        account_email_raw = token_payload.get("account_email")
        granted_scopes = _normalize_scope_list(token_payload.get("granted_scopes"))
        access_token_raw = token_payload.get("access_token")
        refresh_token_raw = token_payload.get("refresh_token")
        expires_in_raw = token_payload.get("expires_in_seconds")
        expires_in_seconds = expires_in_raw if isinstance(expires_in_raw, int) else 3600
        if expires_in_seconds <= 0:
            expires_in_seconds = 0
        if (
            not isinstance(account_subject_raw, str)
            or not account_subject_raw.strip()
            or not isinstance(account_email_raw, str)
            or not account_email_raw.strip()
            or not isinstance(access_token_raw, str)
            or not access_token_raw.strip()
        ):
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="oauth_payload_invalid",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        refresh_token = (
            refresh_token_raw.strip()
            if isinstance(refresh_token_raw, str) and refresh_token_raw.strip()
            else None
        )
        if refresh_token is None and connector.refresh_token_enc is not None:
            refresh_token = _decrypt_secret(
                ciphertext=connector.refresh_token_enc,
                secret=self.encryption_secret,
                expected_key_version=connector.encryption_key_version,
                encryption_keys=self.encryption_keys,
            )
        if refresh_token is None:
            raise self._callback_invalid(
                db=db,
                connector=connector,
                flow=flow,
                reason="missing_refresh_token",
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )

        connector.status = "connected"
        connector.account_subject = account_subject_raw.strip()
        connector.account_email = account_email_raw.strip()
        connector.granted_scopes = granted_scopes
        connector.access_token_enc = _encrypt_secret(
            plaintext=access_token_raw.strip(),
            secret=self.encryption_secret,
            key_version=self.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        connector.refresh_token_enc = _encrypt_secret(
            plaintext=refresh_token,
            secret=self.encryption_secret,
            key_version=self.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        connector.access_token_expires_at = now + timedelta(seconds=expires_in_seconds)
        connector.token_obtained_at = now
        connector.encryption_key_version = self.encryption_key_version
        connector.last_error_code = None
        connector.last_error_at = None
        connector.updated_at = now_fn()

        succeeded_event_type = (
            "evt.connector.google.reconnect.succeeded"
            if flow == "reconnect"
            else "evt.connector.google.connect.succeeded"
        )
        _append_connector_event(
            db=db,
            connector_id=connector.id,
            event_type=succeeded_event_type,
            payload_data={
                **_connector_event_payload(connector, scopes=granted_scopes),
                "requested_scopes": _normalize_scope_list(state_record.requested_scopes),
                "granted_scopes": granted_scopes,
            },
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        return _connector_payload(connector)

    def disconnect(
        self,
        *,
        db: Session,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> dict[str, Any]:
        connector = self._ensure_connector(db=db, now_fn=now_fn)
        tokens_to_revoke: list[str] = []
        for encrypted in (connector.refresh_token_enc, connector.access_token_enc):
            if encrypted is None:
                continue
            try:
                tokens_to_revoke.append(
                    _decrypt_secret(
                        ciphertext=encrypted,
                        secret=self.encryption_secret,
                        expected_key_version=connector.encryption_key_version,
                        encryption_keys=self.encryption_keys,
                    )
                )
            except Exception:
                continue
        revoked_remote = False
        for token in tokens_to_revoke:
            try:
                self.oauth_client.revoke_token(token=token)
                revoked_remote = True
            except Exception:
                continue

        connector.status = "not_connected"
        connector.granted_scopes = []
        connector.access_token_enc = None
        connector.refresh_token_enc = None
        connector.access_token_expires_at = None
        connector.token_obtained_at = None
        connector.last_error_code = None
        connector.last_error_at = None
        connector.updated_at = now_fn()

        _append_connector_event(
            db=db,
            connector_id=connector.id,
            event_type="evt.connector.google.disconnected",
            payload_data={
                **_connector_event_payload(connector, scopes=[]),
                "revoked_remote": revoked_remote,
            },
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        return _connector_payload(connector)

    def list_events(
        self,
        *,
        db: Session,
        limit: int,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(200, limit))
        connector = db.scalar(
            select(GoogleConnectorRecord).where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID).limit(1)
        )
        if connector is None:
            return []
        events = db.scalars(
            select(GoogleConnectorEventRecord)
            .where(GoogleConnectorEventRecord.connector_id == connector.id)
            .order_by(
                GoogleConnectorEventRecord.created_at.asc(),
                GoogleConnectorEventRecord.id.asc(),
            )
            .limit(bounded_limit)
        ).all()
        return [
            {
                "id": event.id,
                "event_type": event.event_type,
                "payload": event.payload,
                "created_at": to_rfc3339(event.created_at),
            }
            for event in events
        ]

    def _typed_failure(
        self,
        *,
        failure_class: TypedAuthFailureClass,
    ) -> GoogleCapabilityExecutionResult:
        return GoogleCapabilityExecutionResult(
            status="failed",
            output=None,
            auth_failure=TypedAuthFailure(
                failure_class=failure_class,
                recovery=_AUTH_FAILURE_RECOVERY[failure_class],
            ),
            error=failure_class,
        )

    def _refresh_access_token_if_needed(
        self,
        *,
        db: Session,
        connector: GoogleConnectorRecord,
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> tuple[str | None, GoogleCapabilityExecutionResult | None]:
        if connector.access_token_enc is None:
            _set_connector_error(
                connector=connector,
                error_code="token_missing",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            return None, self._typed_failure(failure_class="token_expired")
        access_token = _decrypt_secret(
            ciphertext=connector.access_token_enc,
            secret=self.encryption_secret,
            expected_key_version=connector.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        now = now_fn()
        if connector.access_token_expires_at is None or connector.access_token_expires_at > now:
            return access_token, None
        if connector.refresh_token_enc is None:
            _set_connector_error(
                connector=connector,
                error_code="refresh_missing",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            _append_connector_event(
                db=db,
                connector_id=connector.id,
                event_type="evt.connector.google.refresh.failed",
                payload_data={
                    **_connector_event_payload(
                        connector,
                        scopes=_normalize_scope_list(connector.granted_scopes),
                    ),
                    "failure_reason": "token_expired",
                },
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            return None, self._typed_failure(failure_class="token_expired")

        refresh_token = _decrypt_secret(
            ciphertext=connector.refresh_token_enc,
            secret=self.encryption_secret,
            expected_key_version=connector.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        try:
            refreshed_payload = self.oauth_client.refresh_access_token(refresh_token=refresh_token)
        except Exception as exc:
            reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}").lower()
            if "invalid_grant" in reason or "revoked" in reason:
                connector.status = "revoked"
                _set_connector_error(
                    connector=connector,
                    error_code="access_revoked",
                    now_fn=now_fn,
                    preserve_existing_blocking=True,
                )
                _append_connector_event(
                    db=db,
                    connector_id=connector.id,
                    event_type="evt.connector.google.refresh.failed",
                    payload_data={
                        **_connector_event_payload(
                            connector,
                            scopes=_normalize_scope_list(connector.granted_scopes),
                        ),
                        "failure_reason": "access_revoked",
                    },
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return None, self._typed_failure(failure_class="access_revoked")
            _set_connector_error(
                connector=connector,
                error_code="token_expired",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            _append_connector_event(
                db=db,
                connector_id=connector.id,
                event_type="evt.connector.google.refresh.failed",
                payload_data={
                    **_connector_event_payload(
                        connector,
                        scopes=_normalize_scope_list(connector.granted_scopes),
                    ),
                    "failure_reason": "token_expired",
                },
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            return None, self._typed_failure(failure_class="token_expired")

        access_token_raw = refreshed_payload.get("access_token")
        if not isinstance(access_token_raw, str) or not access_token_raw.strip():
            _set_connector_error(
                connector=connector,
                error_code="token_expired",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            return None, self._typed_failure(failure_class="token_expired")

        refreshed_refresh_token_raw = refreshed_payload.get("refresh_token")
        refreshed_refresh_token = (
            refreshed_refresh_token_raw.strip()
            if isinstance(refreshed_refresh_token_raw, str) and refreshed_refresh_token_raw.strip()
            else refresh_token
        )
        expires_in_raw = refreshed_payload.get("expires_in_seconds")
        expires_in_seconds = expires_in_raw if isinstance(expires_in_raw, int) else 3600
        if expires_in_seconds <= 0:
            expires_in_seconds = 60
        connector.access_token_enc = _encrypt_secret(
            plaintext=access_token_raw.strip(),
            secret=self.encryption_secret,
            key_version=self.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        connector.refresh_token_enc = _encrypt_secret(
            plaintext=refreshed_refresh_token,
            secret=self.encryption_secret,
            key_version=self.encryption_key_version,
            encryption_keys=self.encryption_keys,
        )
        connector.encryption_key_version = self.encryption_key_version
        now_refreshed = now_fn()
        connector.access_token_expires_at = now_refreshed + timedelta(seconds=expires_in_seconds)
        connector.token_obtained_at = now_refreshed
        _clear_connector_error(
            connector=connector,
            now_fn=now_fn,
            preserve_existing_blocking=True,
        )
        _append_connector_event(
            db=db,
            connector_id=connector.id,
            event_type="evt.connector.google.refresh.succeeded",
            payload_data={
                **_connector_event_payload(
                    connector,
                    scopes=_normalize_scope_list(connector.granted_scopes),
                ),
                "access_token_expires_at": to_rfc3339(connector.access_token_expires_at),
            },
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        return access_token_raw.strip(), None

    def execute_capability(
        self,
        *,
        db: Session,
        capability_id: str,
        normalized_input: dict[str, Any],
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> GoogleCapabilityExecutionResult:
        required_scopes = GOOGLE_CAPABILITY_SCOPES.get(capability_id)
        if required_scopes is None:
            return GoogleCapabilityExecutionResult(
                status="failed",
                output=None,
                auth_failure=None,
                error="unknown_capability",
            )
        connector = self._connector_for_update(db=db)
        if connector is None or connector.status == "not_connected":
            return self._typed_failure(failure_class="not_connected")
        if connector.status == "revoked":
            return self._typed_failure(failure_class="access_revoked")
        granted_scopes = set(_normalize_scope_list(connector.granted_scopes))
        if not required_scopes.issubset(granted_scopes):
            _set_connector_error(
                connector=connector,
                error_code="consent_required",
                now_fn=now_fn,
                preserve_existing_blocking=True,
            )
            return self._typed_failure(failure_class="consent_required")

        try:
            access_token, refresh_failure = self._refresh_access_token_if_needed(
                db=db,
                connector=connector,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        except Exception as exc:
            reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
            return GoogleCapabilityExecutionResult(
                status="failed",
                output=None,
                auth_failure=None,
                error=reason,
            )
        if refresh_failure is not None:
            return refresh_failure
        if access_token is None:
            return self._typed_failure(failure_class="token_expired")

        attendee_intersection_enabled = GOOGLE_CALENDAR_FREEBUSY_SCOPE in granted_scopes
        try:
            if capability_id == "cap.calendar.list":
                output_payload = self.workspace_provider.calendar_list(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.calendar.propose_slots":
                output_payload = self.workspace_provider.calendar_propose_slots(
                    access_token=access_token,
                    normalized_input=normalized_input,
                    attendee_intersection_enabled=attendee_intersection_enabled,
                )
            elif capability_id == "cap.calendar.create_event":
                output_payload = self.workspace_provider.calendar_create_event(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.email.search":
                output_payload = self.workspace_provider.email_search(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.email.read":
                output_payload = self.workspace_provider.email_read(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.email.draft":
                draft_projection = self.workspace_provider.email_create_draft(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
                projection_payload = (
                    draft_projection if isinstance(draft_projection, dict) else {}
                )
                output_payload = _canonical_draft_output(
                    normalized_input=normalized_input,
                    provider_projection=projection_payload,
                )
            elif capability_id == "cap.drive.search":
                output_payload = self.workspace_provider.drive_search(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.drive.read":
                output_payload = self.workspace_provider.drive_read(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            elif capability_id == "cap.drive.share":
                output_payload = self.workspace_provider.drive_share(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
            else:
                output_payload = self.workspace_provider.email_send(
                    access_token=access_token,
                    normalized_input=normalized_input,
                )
        except Exception as exc:
            reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
            lowered = reason.lower()
            if "insufficient" in lowered or "permission" in lowered:
                _set_connector_error(
                    connector=connector,
                    error_code="scope_missing",
                    now_fn=now_fn,
                    preserve_existing_blocking=True,
                )
                return self._typed_failure(failure_class="scope_missing")
            if "invalid_grant" in lowered or "revoked" in lowered:
                connector.status = "revoked"
                _set_connector_error(
                    connector=connector,
                    error_code="access_revoked",
                    now_fn=now_fn,
                    preserve_existing_blocking=True,
                )
                return self._typed_failure(failure_class="access_revoked")
            if "token" in lowered and "expired" in lowered:
                _set_connector_error(
                    connector=connector,
                    error_code="token_expired",
                    now_fn=now_fn,
                    preserve_existing_blocking=True,
                )
                return self._typed_failure(failure_class="token_expired")
            typed_provider_error = _classify_google_provider_failure(reason)
            if typed_provider_error is not None:
                return GoogleCapabilityExecutionResult(
                    status="failed",
                    output=None,
                    auth_failure=None,
                    error=typed_provider_error,
                )
            return GoogleCapabilityExecutionResult(
                status="failed",
                output=None,
                auth_failure=None,
                error=reason,
            )

        if not isinstance(output_payload, dict):
            return GoogleCapabilityExecutionResult(
                status="failed",
                output=None,
                auth_failure=None,
                error="invalid_provider_output",
            )
        redacted_output = redact_json_value(output_payload)
        output_dict = redacted_output if isinstance(redacted_output, dict) else {"value": redacted_output}
        return GoogleCapabilityExecutionResult(
            status="succeeded",
            output=output_dict,
            auth_failure=None,
            error=None,
        )

    def execute_read_capability(
        self,
        *,
        db: Session,
        capability_id: str,
        normalized_input: dict[str, Any],
        now_fn: Callable[[], datetime],
        new_id_fn: Callable[[str], str],
    ) -> GoogleCapabilityExecutionResult:
        return self.execute_capability(
            db=db,
            capability_id=capability_id,
            normalized_input=normalized_input,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
