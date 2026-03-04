# Slice 4: Google Workspace Core (Calendar + Email) — Spec

## Goal

Deliver the highest-value Google productivity flows with correct safety boundaries.

## Acceptance Criteria

### google oauth connector lifecycle is complete and secure by default
- **given**: no active Google connector, or a connector that needs additional scopes
- **when**: the user initiates connect, reconnect, or disconnect for Google Workspace access
- **then**: Ariel completes OAuth authorization-code flow with PKCE and CSRF-safe state, persists connector status/scopes/tokens securely in canonical state, and returns clear connected/not-connected/reconnect-required status without exposing secrets

### schedule retrieval and slot proposals work as read flows
- **given**: an active session and a connected Google account with calendar access
- **when**: the user asks to view schedule context or asks for available meeting times under stated constraints (date range, duration, or participants/time windows)
- **then**: Ariel returns calendar availability information and concrete slot options as `read` behavior without approval, with user-visible timing context and auditable action lifecycle entries

### slot proposals are attendee-aware when possible and explicit when constrained
- **given**: a slot-planning request includes attendees
- **when**: Ariel evaluates free/busy data for those attendees
- **then**: Ariel proposes slots based on attendee intersection when consent/scope allows; if attendee free/busy is unavailable, Ariel explicitly discloses the limitation, falls back to user-calendar planning only, and provides a clear recovery step

### calendar event creation is approval-gated and result-confirmed
- **given**: Ariel has enough event details to create a calendar event
- **when**: Ariel proposes `cap.calendar.create_event`
- **then**: execution does not occur before approval, approval executes only the frozen proposed payload, and Ariel returns a clear created/failed result status after approval resolution

### email search and read run without approval
- **given**: an active session and a connected Google account with Gmail read scopes
- **when**: the user asks to find or open email content
- **then**: Ariel executes email search/read as `read` actions without approval, returns relevant results in-chat, and preserves auditable lifecycle records with standard redaction

### drafting is low-friction and non-sending by construction
- **given**: the user asks Ariel to compose email content
- **when**: Ariel proposes `cap.email.draft`
- **then**: drafting executes as allowlisted `write_reversible` without approval, persists as a draft artifact/intent only, and cannot produce external delivery side effects

### sending remains approval-gated external delivery
- **given**: a draft or sendable email payload exists
- **when**: Ariel proposes `cap.email.send`
- **then**: send execution remains `external_send`, requires explicit approval, and executes only the exact approved payload once

### permission and consent failures are typed and recoverable
- **given**: connector auth is missing, expired, under-scoped, or denied for a requested calendar/email action
- **when**: Ariel attempts the capability call
- **then**: Ariel surfaces a typed failure class (`not_connected`, `consent_required`, `scope_missing`, `token_expired`, or `access_revoked`) with a clear user-visible recovery path, records the reason in lifecycle events, and does not silently downgrade to unsafe fallback behavior

## Key Decisions

**Capability surface is narrow and explicit for MVP**: Slice 4 introduces only `cap.calendar.list`, `cap.calendar.propose_slots`, `cap.calendar.create_event`, `cap.email.search`, `cap.email.read`, `cap.email.draft`, and `cap.email.send`. This keeps safety boundaries tight while covering the highest-value workflows.

**Policy classes are product semantics, not implementation accidents**: Calendar/email reads execute as allowlisted `read`; calendar creation is `write_reversible` with approval; email draft is explicitly allowlisted `write_reversible`; email send is `external_send` and always approval-gated.

**Scheduling engine is attendee-aware with deterministic fallback**: Slot proposal remains a read-only planning operation. When attendee free/busy access is available, slots are computed from attendee intersection; when unavailable, Ariel falls back to user-calendar-only planning and must disclose that constraint.

**Draft/send separation is a hard boundary**: Drafting produces editable message intent/content but has no external side effect. Sending is a distinct action attempt with its own approval token, payload hash, and execution lifecycle.

**Consent and scope state are first-class typed runtime outcomes**: Google auth failures are modeled as explicit typed capability failures (`not_connected`, `consent_required`, `scope_missing`, `token_expired`, `access_revoked`) with deterministic user recovery guidance. Ariel does not mask these as generic model/tool errors.

**Least-privilege OAuth is incremental per capability**: Calendar and Gmail capabilities request only minimal required scopes and expand only when a newly requested operation requires broader scope. Scope usage remains auditable per capability.

**OAuth flow is explicit and web-security hardened**: Google connector authorization uses OAuth authorization-code with PKCE, state-bound CSRF protection, short-lived one-time state handles, and strict callback redirect validation.

**Connector state is canonical and secret-safe**: Connector status, granted scopes, and token metadata are durable Postgres state; token material is encrypted at rest, never surfaced in user APIs, and never logged in plaintext.

**Token refresh behavior is centralized and race-safe**: Access-token refresh is handled in one connector runtime boundary with deterministic locking semantics so concurrent capability calls cannot race into inconsistent token state.

**Capability-scope mapping is deterministic and testable**: Each Google capability declares required scopes; runtime checks connector scope coverage before dispatch, returning typed recoverable failures instead of unsafe or silent fallback behavior.

**Connector lifecycle is auditable end-to-end**: Connect/reconnect/disconnect/refresh outcomes are captured as durable events so users can inspect what authorization state changed, when it changed, and why capability execution was allowed or blocked.

**Connector audit events stay in the connector domain**: Connector lifecycle audit records are first-class and durable, but are kept separate from turn/action timeline contracts to preserve clear event-domain boundaries.

**Typed auth/scope failures are structured contracts**: Runtime surfaces auth/scope failures as machine-readable typed outcomes with explicit recovery guidance, not free-text parsing conventions.

**`reconnect_required` is a derived readiness state**: User-visible connector readiness maps to `connected|not_connected|reconnect_required`; `reconnect_required` includes missing consent/scope, revoked access, and non-recoverable token state while excluding transient provider/network failures.

**Readiness classification is explicit and deterministic**: Runtime uses a documented blocking-vs-transient classifier for readiness remapping. Blocking typed auth/scope failures drive `reconnect_required`; transient retryable upstream/network failures do not.

**User-visible status must map to durable action lifecycle**: Every Google action (read, draft, create, send) remains reconstructable through proposal/policy/approval/execution events, so users can inspect what was requested, what was authorized, and what actually happened.

Reference implementation baseline: `docs/v1/s4/s4_oauth_mvp_blueprint.md`.

## Out of Scope

- Drive and Maps workflows (-> Slice 8)
- Proactive reminders/notification subscriptions over Google signals (-> Slice 12)
- Autonomous or auto-approved external sending behavior
- Advanced calendar management (series-wide edits, RSVP orchestration, resource booking optimization, cross-calendar conflict auto-resolution)
- Rich Gmail workflows beyond core search/read/draft/send (thread triage automation, attachment authoring pipelines, mailbox rule management)
