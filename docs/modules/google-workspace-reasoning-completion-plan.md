# Google Workspace Reasoning Completion Plan

## Role

This document is the implementation contract for finishing the Google Workspace
reasoning hard cutover after the audit. It narrows
[google-workspace-reasoning-cutover.md](google-workspace-reasoning-cutover.md)
into concrete remediation work.

The current branch has a strong vertical slice, but it is not the final
production design. The remaining work is correctness, privacy, security,
schema, lifecycle, and legacy-removal work. It is not polish.

## Authority

This plan owns the completion work. The broader cutover spec owns product
direction. If this plan is more specific about a remediation item, follow this
plan until the broader spec is updated.

Repo-wide rules still apply:

- [../ai-first.md](../ai-first.md)
- [../boundaries.md](../boundaries.md)
- [../correctness.md](../correctness.md)
- [../database.md](../database.md)
- [../operation-types.md](../operation-types.md)
- [../simplicity.md](../simplicity.md)

## Current Verdict

Do not ship the current implementation as the Google Workspace cutover.

Implemented foundations:

- typed Gmail search and read responses
- bounded Gmail body blocks
- Calendar event normalization
- provider objects, evidence, evidence blocks, commitments, loops, and receipts
- evidence-backed commitment extraction
- review-required commitment creation
- follow-up loop evaluation and notification creation
- calendar write receipts for the basic create path

Blocking gaps:

- Gmail sync does not hydrate all relevant history changes.
- Non-OK Gmail body reads are not treated as first-class typed sync outcomes.
- HTML body security handling does not detect hidden text or link mismatches.
- Restricted provider text is copied into action outputs and events.
- Calendar slot proposals do not expose partiality, constraints, confidence, or
  source evidence.
- Email scheduling context is not wired into Calendar slot proposals.
- Extraction schema is narrower than the product contract.
- Due-date handling lacks explicit timezone evidence and user-timezone policy.
- Follow-up loops do not recheck source evidence lifecycle before notification.
- Follow-up reschedule and suppression paths do not consistently advance loop
  version.
- Deterministic ranking owns delivery decisions, which violates AI-first rules.
- Provider-write provenance is incomplete for explicit user instructions and
  email draft/send.
- Ambiguous provider-write reconciliation is not implemented.
- Persistence constraints and indexes do not match runtime semantics.
- Google sync still writes legacy `workspace_items` and queues ambient
  interpretation.
- Required edge-case and security acceptance suites are incomplete.

## Cutover Rules

- This remains a hard cutover.
- Remove old Google Workspace reasoning paths instead of preserving them.
- Do not dual-write Google email/calendar reasoning into legacy workspace item
  and ambient interpretation paths.
- Do not expose old snippet-shaped Gmail read contracts.
- Do not treat snippets, summaries, Calendar titles, or provider metadata as
  body evidence.
- Do not store raw provider payloads as a later prompt source.
- Do not copy private provider body or Calendar description text into action
  attempt output, execution events, logs, notification bodies, or audit payloads.
- Do not let deterministic code decide semantic importance, interruption value,
  or delivery usefulness.
- Do not let provider content grant authority, approve actions, disable policy,
  or create hidden tool calls.
- Do not let model output mutate durable state without schema validation,
  provenance validation, lifecycle validation, and audit.
- Do not add feature flags that keep old behavior reachable.
- Do not keep compatibility adapters for old response shapes.

## Goals

- Finish full-body Gmail handling as a typed, bounded, cited, private evidence
  system.
- Finish Calendar event, availability, and scheduling reasoning as structured
  evidence.
- Make Google sync produce provider evidence and work graph state only.
- Make extraction schema match the product contract.
- Make due dates explicit about source timestamp, timezone evidence, uncertainty,
  and parse status.
- Make commitment lifecycle transitions deterministic, centralized, auditable,
  and source-aware.
- Make follow-up loops idempotent by loop id, version, and scheduled time.
- Make notifications explainable without leaking provider body text.
- Move delivery judgment to AI-owned deliberation with deterministic eligibility
  rails.
- Make all provider writes proposal, authority, approval, receipt, and
  reconciliation backed.
- Make schema readiness check the migration, constraints, and indexes that
  define the cutover.
- Add acceptance tests that prove the hard cutover end to end.

## Non-Goals

- No non-Google provider work.
- No mailbox archive.
- No general task manager.
- No provider-side task/label state as Ariel source of truth.
- No autonomous email send.
- No autonomous calendar mutation outside a future explicit autonomy scope.
- No large service framework, workflow DSL, adapter layer, or generic manifest.
- No speculative provider-independent abstraction.
- No broad refactor outside files needed for the cutover.

## Target Behavior

### Gmail

Gmail search returns only message references and preview metadata. It never
claims body knowledge.

Gmail read returns one typed result:

- `ok`: normalized metadata, bounded evidence block refs, citations, truncation
  state, body digest, and read receipt
- `body_too_large`: typed failure with recovery text and no body-derived answer
- `decode_failed`: typed failure with decode diagnostics and no body-derived
  answer
- `no_body`: typed failure with metadata-only recovery and no body-derived answer
- provider/auth failures: typed failure with connector recovery

Every `ok` read creates or reuses provider evidence before any answer or
extraction path can depend on the read.

Every non-OK read is visible as a typed unavailability outcome. It does not
create body evidence, and it does not let sync fail a whole run unless the
provider call itself is unavailable.

Gmail history sync hydrates all changed messages enough to update provider
objects and decide whether body evidence is needed:

- `messagesAdded`: full read and evidence when body is available
- `labelsAdded`: metadata update plus full read when the change can affect thread
  state, ownership, sent/received direction, or commitment status
- `labelsRemoved`: metadata update plus full read when the change can affect the
  same state
- `messagesDeleted`: provider object deletion and evidence lifecycle update

No Gmail sync path writes Google reasoning into legacy `workspace_items` or
ambient interpretation tasks.

### Gmail HTML Security

HTML body handling preserves security-relevant facts without making raw HTML a
prompt source.

Normalized body evidence records:

- visible text
- omitted hidden text diagnostics
- hidden text count and digest
- link text and destination pairs when the destination is present
- link text versus destination mismatch markers
- script/style/head omission counts
- HTML conversion notes

Hidden text is never silently merged into visible body evidence. Link
destinations are evidence, not instructions.

### Calendar

Calendar list returns typed event evidence. The validator rejects event outputs
missing required event identity and reasoning fields:

- calendar id
- event id
- iCal UID when provider supplies it
- recurring event id when provider supplies it
- status
- start/end value, timezone, and all-day marker
- organizer
- attendees and response status
- recurrence metadata
- location and conference metadata
- updated time
- etag when provider supplies it
- raw payload digest

Calendar sync creates provider objects and evidence for changed events, including
all-day, cancelled, recurring, attendee-status, timezone, and description-block
cases. It does not write the legacy workspace item path.

Slot proposals return typed availability with:

- `availability_scope`: `all_attendees` or `primary_calendar_only`
- `partial`: true when attendee intersection was requested but incomplete
- `partial_reason`: typed reason when partial
- timezone
- confidence
- source evidence refs
- constraints used
- attendees considered
- freebusy error diagnostics without provider body text

A partial slot result cannot be presented as confirmed all-attendee
availability.

Email scheduling requests pass source evidence, quoted-content caveat,
participants, proposed windows, timezone evidence, and constraints into slot
proposal input. Calendar availability is combined with that context, not called
as an isolated time-window calculator.

### Evidence

Provider evidence is the only canonical promptable source for Google body and
Calendar description content.

Evidence records include:

- provider identity
- provider account id
- provider object id
- source kind
- external ids
- source timestamp
- observed timestamp
- content digest
- taint
- sensitivity
- retention policy
- extraction status
- lifecycle state
- provenance metadata

Evidence blocks include bounded text and offsets. Raw provider JSON, raw MIME,
raw HTML, OAuth tokens, and unbounded body text are not evidence blocks.

Action attempts and action events may store evidence refs, provider object ids,
block ids, digests, counts, and typed statuses. They must not store Gmail body
text or Calendar description text.

### Extraction

Extraction uses strict JSON schema. The schema includes:

- candidate kind
- action text
- action category
- owner
- requester
- counterparty
- due expression text
- normalized due window proposal
- timezone evidence
- confidence
- due confidence
- evidence block ids
- quoted-content caveat
- ambiguity reason
- suggested lifecycle transition
- suggested follow-up policy
- safety notes
- review-required flag
- rationale
- uncertainty

The model extracts and explains. Deterministic code validates.

Extraction cannot activate commitments, resolve commitments, create notifications,
or create provider writes directly. It creates reviewable candidates or proposes
validated lifecycle transitions.

### Due Dates

Due dates are intervals, not strings.

The final due representation records:

- source text
- parsed start
- parsed end
- timezone
- timezone source: provider, user profile, evidence text, or explicit user input
- parse status
- confidence
- ambiguity reason

Relative dates resolve against source timestamp. If source timestamp lacks a
timezone and the expression needs one, use the user's configured timezone. If no
timezone can be justified, keep the candidate review-only and do not schedule a
due-date loop.

### Commitments

Commitments are operational work graph state. They are not plain memory text.

Every commitment has:

- owner
- requester when known
- counterparty when known
- action text
- action category
- source evidence and block ids
- source thread or calendar scope
- due window
- timezone evidence
- priority
- confidence
- lifecycle state
- review state
- lifecycle history
- resolution evidence or user action source when resolved

Lifecycle mutation is owned by one code path. Routes, workers, extraction, and
provider-write paths call that code path instead of each mutating the same fields
directly.

### Follow-Up Loops

Follow-up loops are durable workflows.

Every loop state change that changes delivery eligibility, next check time,
notification time, stale time, snooze, suppression, or resolution increments
`version`.

Every queued follow-up task is idempotent by:

- loop id
- loop version
- scheduled time

Every evaluation rechecks:

- loop version
- scheduled time
- commitment lifecycle
- source evidence lifecycle
- source evidence freshness
- connector status
- snooze
- dismissal
- pending or delivered notifications
- policy version

If evidence is deleted, redacted, superseded, stale, or unavailable, the loop
records a no-op or repair state and does not notify.

Dismissal suppresses delivery only. It does not mutate commitment truth.

### Attention And Proactivity

Deterministic code builds an eligibility and feature packet. It does not choose
semantic importance or delivery usefulness.

The feature packet may include:

- lifecycle state
- owner
- due window
- overdue duration
- time until due
- waiting direction
- thread recency
- source evidence state
- confidence
- snooze and dismissal state
- connector health
- sensitivity
- calendar conflict state
- prior notification history

Hard deterministic rails may suppress only for safety or correctness:

- deleted, rejected, resolved, superseded, expired, or stale commitment
- missing or invalid source evidence
- redacted or deleted evidence
- connector unavailable where provider rehydration is required
- snoozed until a future time
- active pending notification for the same loop/version

AI deliberation decides whether to notify, wait, ask for clarification, propose a
draft, propose a calendar event, or do nothing inside those rails. The result is
recorded as an AI judgment with feature refs, rationale, uncertainty, and policy
version.

### Provider Writes

Every provider write has a typed proposal, authority source, policy result,
approval when required, external execution, receipt, and audit event.

Authority source is exactly one of:

- live source evidence
- live commitment
- explicit user instruction from the current or referenced turn

`user_instruction_ref` is not a free string. It resolves to a durable turn,
message, action proposal, or approval source that belongs to the same provider
account and session scope.

The rule covers:

- email draft
- email send
- email archive
- email label mutation
- email trash
- calendar create
- calendar update
- calendar RSVP

Every receipt records:

- proposal/action attempt id
- capability id
- provider account id
- idempotency key
- request digest
- provider ids
- etag or history id when returned
- provider timestamp when returned
- status: succeeded, failed, ambiguous
- response digest
- redacted response payload

Ambiguous writes create a reconciliation task. Retrying the same logical write is
blocked until reconciliation proves whether the provider side effect happened.

### Schema And Readiness

Schema readiness checks:

- Alembic head
- required tables
- required columns
- required constraints
- required indexes
- required check constraint vocabularies

A database with only table names present is not schema-ready.

Migrations are reversible. Downgrade removes or transforms rows that violate the
prior constraints before restoring old checks.

## Final Runtime Structure

Keep the implementation direct. Add or keep an extracted function only when it
removes real duplication, centralizes a lifecycle invariant, or prevents audit
drift.

### `src/ariel/google_workspace_normalization.py`

Owns provider-boundary normalization:

- Gmail MIME decoding
- Gmail HTML-to-text security diagnostics
- Gmail body block creation
- Gmail address/header normalization
- Calendar event normalization
- Calendar description block creation
- provider payload digests

Rules:

- Missing required provider ids are typed boundary failures, not empty strings.
- Provider payload shape is validated once here.
- Raw provider payloads do not escape this layer.

### `src/ariel/google_connector.py`

Owns Google API calls and typed provider outputs:

- no DB access
- no lifecycle logic
- no old `results` shapes
- no unknown Google capability output accepted as valid
- all reads and writes return strict typed outputs or typed failures
- all external calls bounded by timeout, page size, and byte size

### `src/ariel/sync_runtime.py`

Owns provider sync orchestration:

- refresh/access token outside persistence writes
- page through provider deltas
- hydrate changed objects as required
- persist provider objects and evidence
- mark evidence lifecycle on deletion/redaction/supersession
- enqueue extraction tasks from evidence

Rules:

- no Google `workspace_items`
- no Google ambient interpretation task creation
- no provider calls inside DB transactions
- sync non-OK read outcomes are typed sync observations, not silent success

### `src/ariel/action_runtime.py`

Owns capability execution orchestration:

- execute reads outside DB transactions
- execute writes as durable multi-step side effects
- persist evidence refs for reads
- redact provider body text from action output and events
- validate write provenance before external calls
- persist write receipts
- enqueue write reconciliation tasks for ambiguous writes

Evidence persistence may be extracted only if the function is concrete and
object-kind specific. A single direct writer per source kind is acceptable:

- persist Gmail message evidence
- persist Gmail thread evidence
- persist Calendar event evidence
- persist Calendar availability evidence

Do not introduce a generic evidence builder, manifest, adapter, or DSL.

### `src/ariel/workspace_reasoning.py`

Owns deterministic work-graph rules:

- commitment candidate validation
- due window normalization
- lifecycle transition validation
- follow-up eligibility validation
- source evidence freshness checks
- work graph mutation functions used by routes and workers

This file may contain direct functions such as:

- approve commitment
- edit commitment
- reject commitment
- resolve commitment
- dismiss commitment
- delete commitment
- snooze commitment
- evaluate follow-up loop

Each function must have an obvious lifecycle payoff. Do not add a service class.

### `src/ariel/proactivity.py`

Owns AI-owned proactive deliberation and task dispatch:

- calls extraction model
- records AI judgments
- builds bounded feature packets
- calls proactive deliberation for notification decisions
- creates notifications from AI decisions that pass deterministic rails

Rules:

- no raw provider body text in ranking or deliberation prompts
- no deterministic delivery scoring
- no lifecycle mutation outside `workspace_reasoning.py` functions

### `src/ariel/attention_ranking.py`

Either remove this file or reduce it to feature-packet construction.

It must not return:

- semantic rank score
- priority judgment
- urgency judgment
- delivery decision
- interruption decision

If kept, it returns deterministic facts only.

### `src/ariel/app.py`

Owns HTTP route wiring:

- parse request
- load scoped rows
- call workspace mutation functions
- return response contracts

Rules:

- no provider parsing
- no duplicated lifecycle rules
- no raw provider body text in responses unless the endpoint is explicitly an
  evidence inspection endpoint with redaction policy

### `src/ariel/persistence.py`

Owns ORM models and serializers:

- schema constraints
- indexes
- serializers that redact restricted fields
- no lifecycle product logic

Required schema changes:

- evidence retention policy column
- evidence extraction status column
- dedicated commitment source scope fields needed for uniqueness
- active commitment unique index aligned with runtime dedupe scope
- follow-up loop owner columns for all supported owner types or remove unsupported
  loop kinds
- task idempotency key or equivalent unique key for loop/version/scheduled time
- provider write receipt constraints aligned with all write capabilities
- lifecycle consistency constraints where the DB can enforce them

### `src/ariel/response_contracts.py`

Owns public response validation:

- Gmail read/search typed response contracts
- Calendar read/slot typed response contracts
- commitment review/list/detail contracts
- follow-up explanation contracts
- provider write receipt contracts

No compatibility shapes.

### `src/ariel/worker.py`

Owns task dispatch only:

- dispatch extraction tasks
- dispatch follow-up evaluation tasks
- dispatch write reconciliation tasks
- enqueue due idempotent tasks

Rules:

- no lifecycle logic
- no provider parsing
- no duplicate task creation for same loop/version/scheduled time

## Key Decisions

- Provider evidence is the canonical bridge from Google to reasoning.
- Google sync produces evidence and work graph state, not workspace items.
- Private provider text stays in evidence blocks and controlled inspection
  endpoints only.
- AI owns interruption and usefulness decisions.
- Deterministic code owns validity, eligibility, safety suppression, idempotency,
  and audit.
- Follow-up loop version changes are the concurrency boundary.
- Provider write reconciliation is required for ambiguous writes.
- Slot proposals with missing attendee authority are partial, not confirmed.
- User instruction provenance must resolve to durable state.
- Schema readiness is migration readiness, not table-name readiness.

## Implementation Sequence

The implementation can be staged on the branch. The final merged state has no
legacy behavior.

1. Fix schema foundations.
   - Add missing evidence columns and indexes.
   - Align active commitment unique index with runtime dedupe scope.
   - Add follow-up task idempotency.
   - Add schema readiness checks for head, constraints, and indexes.
   - Add reversible migration tests.

2. Remove legacy Google sync surfaces.
   - Delete Google sync writes to `workspace_items`.
   - Delete Google sync ambient interpretation enqueueing.
   - Update tests to assert the legacy path is unreachable.

3. Finish provider normalization.
   - Reject missing required provider ids at boundary.
   - Add HTML hidden text and link mismatch diagnostics.
   - Add all-day, cancelled, timezone, recurring, and attendee edge cases.

4. Finish Gmail sync.
   - Hydrate relevant `labelsAdded`, `labelsRemoved`, and delete transitions.
   - Persist typed non-OK read outcomes without body evidence.
   - Keep sync running when individual body reads return typed unavailability.

5. Finish Calendar and slot contracts.
   - Strengthen Calendar output validation.
   - Add partial availability fields.
   - Add constraints, confidence, and source evidence refs.
   - Wire email scheduling evidence into slot proposal input.

6. Centralize work graph mutation.
   - Move route and worker lifecycle mutation into direct functions in
     `workspace_reasoning.py`.
   - Make every loop timing/suppression/resolution mutation increment version.
   - Recheck evidence lifecycle before notification.

7. Replace deterministic ranking.
   - Remove delivery decisions from `attention_ranking.py`.
   - Build deterministic feature packets only.
   - Add AI proactive deliberation for notify/wait/ask/propose/no-op.
   - Preserve hard deterministic suppression for safety and correctness.

8. Finish extraction contract.
   - Add requester, counterparty, due proposal, timezone evidence, caveats,
     ambiguity, suggested policy, and safety notes.
   - Persist the new fields in commitments or metadata where queryability is not
     required.
   - Add tests for ambiguous ownership, not-actionable content, quoted content,
     forwarded content, and Calendar description injection.

9. Finish provider writes.
   - Add authority fields to email draft/send and other write contracts.
   - Resolve `user_instruction_ref` to durable state.
   - Validate evidence/commitment/user instruction scope before every write.
   - Record receipts for draft/send/update/respond.
   - Implement ambiguous write reconciliation.

10. Remove private text propagation.
    - Replace action outputs/events containing evidence text with refs and
      digests.
    - Keep provider text only in evidence blocks and explicit inspection
      endpoints.
    - Add tests that action events do not contain Gmail body or Calendar
      description text.

11. Complete acceptance tests.
    - Split omnibus tests only where file size prevents skimming.
    - Add missing named acceptance suites or equivalent focused files.
    - Add security, provider-write, Calendar sync, lifecycle, and migration
      coverage.

12. Run final verification.
    - `make verify`
    - full unit suite
    - full integration suite
    - Alembic upgrade/downgrade/upgrade
    - targeted privacy text-leak scan
    - targeted legacy path scan

## Acceptance Criteria

The cutover is complete only when all criteria pass:

- Gmail search returns structured refs only.
- Gmail read returns typed full-body evidence or typed unavailability.
- No snippet-only Gmail read response shape remains.
- Gmail sync hydrates relevant history changes and never reasons from metadata
  body substitutes.
- Calendar read returns typed event evidence with required event fields.
- Calendar sync persists provider evidence for all required event edge cases.
- Slot proposals expose partiality, constraints, confidence, timezone, and source
  evidence.
- Email scheduling evidence can feed Calendar slot proposals.
- Provider evidence has retention policy, extraction status, lifecycle state, and
  bounded blocks.
- Provider body text does not appear in action attempt output, action events,
  logs, notification body text, or ranking/deliberation prompts.
- Extraction schema matches this plan.
- Ambiguous and unparseable due dates remain review-only.
- Relative due dates resolve against source timestamp or justified user timezone.
- Commitments preserve owner, requester/counterparty when known, due window,
  source evidence, confidence, state, and lifecycle history.
- Commitment review APIs work through one lifecycle mutation path.
- Follow-up loops are idempotent by loop id, version, and scheduled time.
- Every follow-up evaluation rechecks source evidence lifecycle before notifying.
- Dismissal and snooze suppress delivery without changing commitment truth.
- AI deliberation owns notification usefulness and interruption decisions.
- Deterministic code only hard-suppresses for safety and correctness.
- All provider writes require durable authority source.
- Email draft/send and Calendar create/update/respond produce write receipts.
- Ambiguous writes cannot be retried until reconciliation completes.
- Schema readiness validates Alembic head, tables, columns, constraints, and
  indexes.
- Google sync no longer writes `workspace_items` or queues ambient interpretation.
- Prompt-injection tests cover body text, quoted text, forwarded text, Calendar
  descriptions, hidden HTML text, and link text versus URL conflicts.
- `rg` scans for legacy Google Workspace product paths are clean or covered by a
  narrow documented test-only allowlist.
- `make verify` passes.

## Required Acceptance Tests

Unit:

- Gmail MIME normalization
- Gmail HTML hidden text diagnostics
- Gmail link mismatch diagnostics
- Calendar event normalization for timed, all-day, cancelled, recurring,
  timezone, attendee-status, and conference/location cases
- Gmail/Calendar typed output validators
- due window normalization with source timezone and user timezone
- commitment candidate validator
- lifecycle transition validator
- follow-up loop version/idempotency rules
- provider write authority validator
- provider write receipt idempotency and ambiguity classification

Integration:

- Gmail search -> read -> evidence -> extraction -> review -> follow-up
- Gmail history sync for messages added, labels added, labels removed, and
  deleted
- Gmail typed non-OK body read in sync
- Calendar sync -> evidence -> extraction
- Calendar read -> evidence -> extraction
- Email scheduling evidence -> slot proposal
- Slot partial availability with missing attendee authority
- Commitment approve/edit/reject/resolve/dismiss/delete/snooze
- Supersede and stale lifecycle behavior
- Active commitment -> AI deliberation -> notification
- Snooze and dismiss -> future follow-up without truth mutation
- Resolved before queued follow-up -> no notification
- Connector revoked -> no notification and repair action
- Email draft/send approval -> receipt
- Calendar create/update/respond approval -> receipt
- Provider failure -> failed receipt
- Ambiguous provider response -> reconciliation task -> retry gate
- Duplicate provider write retry -> replay without second side effect
- Schema readiness rejects drifted schema

Security:

- Email body attempts to approve or send mail
- Quoted email assigns fake authority
- Forwarded email contains malicious instructions
- Calendar description contains prompt injection
- Hidden HTML text conflicts with visible text
- Link text conflicts with URL
- Low-scope connector attempts high-scope write
- Provider content attempts to disable audit or memory mode
- Action/event/log payloads do not leak provider body text

## Completion Gate

The work is not done when the tests first pass. It is done when:

- all acceptance criteria pass
- the legacy Google workspace item and ambient paths are deleted
- the implementation follows the file ownership rules above
- every remaining abstraction has an obvious current payoff
- maintainers can trace a Google fact from provider object to evidence block to
  commitment to follow-up to notification or no-op without reading raw provider
  payloads
- a fresh database can upgrade, downgrade, and upgrade again
- a drifted database fails schema readiness
- final verification output is recorded in the PR or change summary
