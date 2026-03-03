# Slice 4: Google Workspace Core (Calendar + Email) — PR Roadmap

### PR-01: Google OAuth Foundation + Read Flows
- **goal**: deliver full Google OAuth connector foundations plus read-only calendar/email journeys (schedule context, slot proposals, inbox search/read) with typed consent/scope failures and explicit recovery messaging.
- **builds on**: Slice 3 PR-03 merged state (safe action runtime, surfaced lifecycle contracts, and grounded-response path). Current codebase has no Google connector/auth state or Google capabilities yet.
- **acceptance**:
  - Ariel exposes a complete Google OAuth authorization-code connector flow (connect/start, callback completion, connected-status visibility, reconnect for additional consent, and disconnect/revoke) with PKCE, CSRF-safe state handling, and durable connector state.
  - connector state persists least-privilege granted scopes and refreshable token material securely, never surfaces secrets in user-visible responses/logs, and supports deterministic typed runtime state checks from capability execution.
  - connector lifecycle behavior is surfaced through explicit connector endpoints and auditable connector events so capability failures can reference concrete reconnect/remediation paths.
  - given connected calendar access, Ariel executes `cap.calendar.list` as allowlisted `read` behavior (no approval), returns schedule context in-chat, and preserves auditable action lifecycle entries.
  - given slot-planning constraints, Ariel executes `cap.calendar.propose_slots` as `read` and returns concrete slot options with user-visible timing context.
  - when attendee free/busy access is available, slot proposals reflect attendee intersection; when it is unavailable, Ariel explicitly discloses the limitation, falls back to user-calendar-only planning, and provides a concrete recovery step.
  - given Gmail read access, Ariel executes `cap.email.search` and `cap.email.read` as allowlisted `read` actions without approval, returns relevant in-chat results, and keeps lifecycle output redacted/auditable.
  - for calendar/email read requests with auth or consent issues, Ariel surfaces typed failures (`not_connected`, `consent_required`, `scope_missing`, `token_expired`, `access_revoked`) with clear recovery paths and durable lifecycle reasons.
- **non-goals**: no side-effecting Google actions (`cap.calendar.create_event`, `cap.email.draft`, `cap.email.send`); no Drive/Maps workflows; no proactive Google-triggered notifications.

### PR-02: Approval-Safe Writes (Calendar Create + Email Draft/Send) (planned after PR-01 merges)
- **goal**: complete Slice 4 write boundaries so calendar creation and email send stay approval-gated while drafting remains low-friction and non-sending by construction.
- **builds on**: PR-01.
- **acceptance**:
  - write-path required scopes are handled through incremental OAuth consent upgrade (no over-broad up-front scope grants), with deterministic `consent_required`/`scope_missing` outcomes and clear reconnect guidance when missing.
  - Ariel proposes `cap.calendar.create_event` as approval-required `write_reversible`; execution never occurs before approval, approved execution runs only once against the frozen payload hash, and user-facing result is explicitly created/failed.
  - Ariel executes `cap.email.draft` as allowlisted `write_reversible` without approval, persists draft intent/content only, and does not produce external delivery side effects.
  - Ariel proposes `cap.email.send` as `external_send`; execution always requires explicit approval and runs only once for the exact approved payload.
  - draft and send remain separate action attempts with independently inspectable lifecycle/approval history so users can distinguish what was drafted from what was sent.
  - typed connector/consent failures remain deterministic and recoverable across write paths too, with no silent downgrade to unsafe fallback behavior.
- **non-goals**: no auto-send/autonomous delivery behavior; no advanced calendar management (series-wide edits, RSVP orchestration, resource booking/conflict optimization); no advanced Gmail automation beyond core search/read/draft/send.
