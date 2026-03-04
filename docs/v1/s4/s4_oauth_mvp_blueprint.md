# Slice 4: Google OAuth + Workspace Core — Production MVP Blueprint

## Purpose

Define the long-term-safe, production-ready MVP baseline for Google connector auth, capability scope gating, and operational safety in Slice 4.

## OAuth Connector Contract

### Flow

1. User starts connect/reconnect from Ariel surface.
2. Ariel creates a one-time OAuth state record (short TTL), PKCE verifier/challenge pair, and requested-scope intent.
3. Ariel redirects user to Google OAuth consent screen.
4. Google redirects to Ariel callback with authorization code + state.
5. Ariel validates state, exchanges code for tokens, persists connector state, and marks connector `connected`.
6. Capability runtime uses connector state for token/scopes and refreshes tokens when needed.
7. User disconnect revokes provider token (best effort) and tombstones local connector token material.

### Required Connector Endpoints

- `POST /v1/connectors/google/start`
- `GET /v1/connectors/google/callback`
- `GET /v1/connectors/google`
- `POST /v1/connectors/google/reconnect`
- `DELETE /v1/connectors/google`

These endpoints are surface-oriented connector control APIs and are separate from capability execution APIs.

## Canonical Connector State (Postgres)

Minimum durable fields:

- `id` (`con_google`)
- `provider` (`google`)
- `status` (`not_connected|connected|error|revoked`)
- `account_subject` (Google user subject id)
- `account_email` (display/recovery aid)
- `granted_scopes[]`
- `access_token_enc` (encrypted)
- `refresh_token_enc` (encrypted)
- `access_token_expires_at`
- `token_obtained_at`
- `encryption_key_version`
- `last_error_code` / `last_error_at`
- `created_at` / `updated_at`

Derived readiness (response surface):

- expose raw connector `status` plus derived readiness `connected|not_connected|reconnect_required`.
- `reconnect_required` means user action is required before safe capability execution (missing consent/scope, revoked access, or non-recoverable token state).
- transient provider/network failures do not by themselves remap a connected connector to `reconnect_required`.
- readiness remains `reconnect_required` until successful reconnect (or explicit disconnect).

Readiness classification contract:

- blocking failures (`consent_required`, `scope_missing`, `access_revoked`) remap readiness to `reconnect_required`.
- transient retryable failures (for example upstream timeout/network instability/retryable provider errors) do not remap readiness by themselves.
- classification must be consistent across read and write capabilities.

Security baseline:

- Token fields are encrypted at rest.
- No token plaintext appears in logs, events, or user-facing responses.
- Disconnect removes active token usability immediately in local state.

## Capability-to-Scope Matrix

| Capability | OAuth Classification | Minimum Google Scopes |
|---|---|---|
| `cap.calendar.list` | read | `https://www.googleapis.com/auth/calendar.readonly` |
| `cap.calendar.propose_slots` | read | `https://www.googleapis.com/auth/calendar.readonly` (+ attendee availability check requires permitted free/busy access) |
| `cap.calendar.create_event` | write_reversible (approval required) | `https://www.googleapis.com/auth/calendar.events` |
| `cap.email.search` | read | `https://www.googleapis.com/auth/gmail.readonly` |
| `cap.email.read` | read | `https://www.googleapis.com/auth/gmail.readonly` |
| `cap.email.draft` | write_reversible (allowlisted) | `https://www.googleapis.com/auth/gmail.compose` |
| `cap.email.send` | external_send (approval required) | `https://www.googleapis.com/auth/gmail.send` |

Scope policy:

- Start with read scopes in PR-01.
- Request broader scopes only when a user-triggered capability requires them.
- Never request all Gmail/Calendar scopes up front.

## Typed Failure Mapping (Deterministic)

| Typed Failure | Runtime Meaning | User Recovery |
|---|---|---|
| `not_connected` | No valid Google connector exists | Connect Google account |
| `consent_required` | Connector exists but required scope not yet granted | Reconnect and grant requested scope |
| `scope_missing` | Provider rejects call for insufficient permissions despite expected scope intent | Reconnect; if persistent, re-consent full required scope set |
| `token_expired` | Access token is expired and refresh cannot complete in current request window | Retry once; if still failing, reconnect |
| `access_revoked` | Refresh token invalid/revoked or app access removed | Reconnect from scratch |

Rules:

- Capability runtime must emit one typed class only.
- No silent fallback to broader or unsafe behavior.
- Typed reason must be persisted in lifecycle/audit events.
- Typed failures are surfaced as structured machine-readable outcomes (typed class + recovery guidance), not free-text parsing contracts.

Recommended runtime payload shape:

```json
{
  "ok": false,
  "auth_failure": {
    "class": "consent_required",
    "recovery": "Reconnect Google and grant requested scope"
  }
}
```

## Token Refresh and Concurrency

- Refresh is centralized in a connector runtime boundary.
- Refresh path acquires row-level lock on connector row.
- Only one refresh attempt is allowed concurrently per connector.
- On refresh success, persist new expiry/token metadata atomically.
- On refresh invalid-grant style failures, fail closed with `access_revoked`.

## Security Controls (Minimum Bar)

- OAuth authorization code + PKCE only.
- One-time `state` handles with TTL and replay protection.
- Strict callback redirect allowlist and exact redirect-uri matching.
- Secret redaction for token-like keys across logs/events/responses.
- Explicit egress allowlist for:
  - `accounts.google.com`
  - `oauth2.googleapis.com`
  - `www.googleapis.com`
  - `gmail.googleapis.com`
- Timeout + bounded retry policy for Google HTTP calls.

## Audit and Observability

Required auditable connector events:

- `evt.connector.google.connect.started`
- `evt.connector.google.connect.succeeded`
- `evt.connector.google.connect.failed`
- `evt.connector.google.reconnect.started`
- `evt.connector.google.reconnect.succeeded`
- `evt.connector.google.reconnect.failed`
- `evt.connector.google.refresh.succeeded`
- `evt.connector.google.refresh.failed`
- `evt.connector.google.disconnected`

Each event includes: connector id, account subject/email (redacted where needed), requested/granted scopes (non-secret), and safe failure reason when applicable.

Boundary rule:

- connector lifecycle events are connector-domain audit records and are not forced into turn/action timeline schemas.
- capability execution still records turn/action lifecycle events and may reference connector failure classes/reasons when applicable.

## Non-Goals for Slice 4

- Multi-user tenancy connector model.
- Background auto-reconnect loops.
- Drive/Maps connector permissions.
- Broad mailbox/calendar automation beyond explicit capabilities in `s4_spec.md`.
