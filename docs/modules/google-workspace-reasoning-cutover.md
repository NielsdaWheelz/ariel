# Google Workspace Reasoning Hard Cutover

## Role

This document is the hard-cutover implementation spec for Ariel's Gmail,
Calendar, commitment, due-date, and follow-up reasoning system.

It turns Google Workspace content into a typed, evidence-backed work graph. The
assistant reasons over email and calendar content, but durable state,
follow-up timing, write authority, provenance, privacy, and lifecycle transitions
belong to Ariel.

This document is a product and architecture contract. It is not a prompt-tuning
plan.

## Scope

In scope:

- Gmail message, thread, header, body, label, and history handling.
- Calendar event, attendee, availability, deadline, and scheduling handling.
- Email and calendar evidence normalization.
- Commitment and action-item extraction from provider evidence.
- Due-date parsing, validation, timezone handling, and uncertainty.
- Waiting-on-me, waiting-on-them, due-soon, overdue, and stale follow-up loops.
- Draft, send, calendar-create, and calendar-update proposal behavior.
- Proactive attention ranking integration.
- Memory integration for durable commitments and decisions.
- Test, audit, observability, privacy, and safety contracts.

Out of scope:

- Non-Google provider support.
- A generic enterprise mail archive.
- A general-purpose task manager replacement.
- A full CRM.
- A provider-independent workflow engine outside Ariel's current persistence and
  background-task architecture.

## Cutover Policy

- This is a hard cutover.
- Do not preserve snippet-only Gmail read behavior for any path that claims to
  read email content.
- Do not keep metadata-only Gmail reasoning as a fallback.
- Do not keep old response fields for compatibility if they do not match the
  new typed contracts.
- Do not dual-write old and new commitment stores.
- Do not keep legacy ranking behavior for commitments once due-date-aware
  ranking lands.
- Do not let model text mutate commitment, reminder, memory, or calendar state
  without a typed capability result and deterministic lifecycle validation.
- Do not let raw provider payloads become model instructions.
- Do not let raw email bodies, raw calendar payloads, or raw attachment payloads
  enter proactive ranking prompts.
- Do not use model memory as the source of truth for commitments, due dates,
  follow-up timing, thread state, or calendar state.
- Do not silently fall back to Gmail snippets, Calendar summaries, keyword
  matching, local timezone guesses, or stale sync cursors.
- Do not add feature flags whose off path keeps old behavior reachable after the
  cutover.
- Work is sequenced across commits only as branch-local implementation staging.
  The merged final state contains only the new surfaces.

## Thesis

Email and calendar reasoning is not "summarize this inbox." It is a work-state
system.

The canonical product object is a work graph:

```text
Provider Account
  -> Mailbox Thread
  -> Message
  -> Evidence Block
  -> Person
  -> Commitment
  -> Due Window
  -> Follow-Up Loop
  -> Draft, Event, Task, Notification, or No-Op
  -> Audit Event
```

The model extracts, interprets, summarizes, and drafts. Deterministic code owns
identity, source evidence, state transitions, permissions, scheduling,
deduplication, follow-up timing, approvals, and audit.

## Reference Model

Mature email and calendar assistants converge on these product patterns:

- Email-thread summaries cite or link back to the source messages they use.
- Action items are represented separately from prose summaries.
- Scheduling suggestions combine email context with calendar availability.
- Follow-up reminders are stateful loops tied to sent mail, replies, due dates,
  and user snoozes.
- Task/calendar systems represent work with priority, duration, start windows,
  due windows, status, and rescheduling policy.
- Production agents keep send, delete, archive, invite, and reschedule actions
  behind explicit authority and audit.
- Security guidance treats email and calendar bodies as untrusted content that
  can contain prompt injection, data-exfiltration instructions, false authority,
  and malicious links.

Ariel's final state follows those patterns, but keeps PostgreSQL and Ariel's
existing policy/action/memory systems as the source of truth.

## Goals

- Make Gmail full-body handling reliable, bounded, cited, and safe.
- Make Calendar events and availability first-class structured evidence.
- Turn email/calendar evidence into typed commitments and follow-up loops.
- Preserve provenance from provider object to evidence block to memory candidate
  to attention item to final user-facing answer.
- Make every proactive nudge explain why it exists and why now.
- Keep all provider writes behind proposal, policy, approval, execution receipt,
  and audit.
- Support due dates, vague due windows, relative dates, user timezone, provider
  timezone, and uncertainty explicitly.
- Make stale, superseded, resolved, snoozed, and invalid follow-ups no-op safely.
- Avoid storing or prompting with raw provider corpus when bounded evidence is
  enough.
- Preserve enough provider identity to resync, dedupe, resolve, cite, and audit.
- Make failures typed and visible instead of letting the assistant answer from
  incomplete context.
- Keep implementation aligned with:
  - [../ai-first.md](../ai-first.md)
  - [../boundaries.md](../boundaries.md)
  - [../operation-types.md](../operation-types.md)
  - [../correctness.md](../correctness.md)
  - [../concurrency.md](../concurrency.md)
  - [memory.md](memory.md)
  - [attachments.md](attachments.md)

## Non-Goals

- No full mailbox mirror as the product architecture.
- No raw email body prompt stuffing.
- No Calendar history warehouse.
- No autonomous email send.
- No autonomous calendar invite creation unless a later autonomy policy
  explicitly grants that authority for a narrow scope.
- No hidden task list that users cannot inspect, correct, snooze, or delete.
- No provider-side labels or tasks as Ariel's canonical state.
- No deterministic keyword-only task extraction.
- No recurring reminder engine detached from evidence.
- No "AI decided this is handled" lifecycle transition without evidence or user
  action.
- No compatibility API for old snippet-only `email_read` responses.
- No external dependency on a managed agent, memory, vector, or task product for
  correctness.

## Target Behavior

### User-Facing Behavior

When the user asks about email:

- Ariel can read full Gmail bodies when authorized.
- Ariel can summarize a message or thread with citations to messages and
  evidence blocks.
- Ariel distinguishes sender claims, quoted text, signatures, forwarded content,
  and Ariel's own interpretation.
- Ariel can identify requested actions, promises, deadlines, meeting proposals,
  waiting-on state, and unanswered questions.
- Ariel exposes uncertainty when ownership, deadline, or requested action is
  ambiguous.
- Ariel can answer "why are you reminding me?" with source message, due window,
  current state, and follow-up rule.
- Ariel can answer "what commitments do I have?" from canonical commitments, not
  by rescanning arbitrary email.
- Ariel can draft replies and scheduling emails, but sending remains an explicit
  side effect with policy and approval.

When the user asks about calendar:

- Ariel can read upcoming events, event details, attendees, location, conference
  links, status, reminders, and event source metadata.
- Ariel can propose meeting times from availability, attendee constraints, and
  email context.
- Ariel can detect calendar conflicts with commitments and proposed due windows.
- Ariel can create or update events only through approval-gated write
  capabilities.
- Ariel can explain when missing scopes prevent attendee availability or event
  details.

When Ariel is proactive:

- Ariel nudges about email/calendar commitments only from canonical state.
- Ariel includes a concise reason, source, due window, and available action.
- Ariel respects snooze, dismissal, resolution, stale evidence, connector state,
  user memory mode, and autonomy policy.
- Ariel does not notify from raw provider payloads or ad hoc model output.

### System Behavior

- Gmail search returns structured message references, not only snippets.
- Gmail read returns typed message and body evidence, not only Gmail snippets.
- Calendar reads return typed event evidence.
- Provider sync creates normalized provider objects and evidence records.
- Commitment extraction runs from normalized evidence through AI judgment with a
  strict schema.
- Deterministic validators normalize and verify due dates, owners, source
  anchors, confidence, and lifecycle eligibility.
- Commitment candidates enter the memory/review lifecycle.
- Active commitments produce proactive signals with due-date-aware rank inputs.
- Follow-up loops are persisted state machines with durable scheduled tasks.
- Model-generated drafts cite the commitments or evidence they respond to.
- Every provider write produces an execution receipt tied to the proposal and
  approval that authorized it.

## Current State To Replace

The current implementation has useful foundations but must not remain as the
final behavior:

- `src/ariel/google_connector.py`
  - `email_search` fetches Gmail metadata and returns title/source/snippet/date.
  - `email_read` requests Gmail `format=full` but only surfaces subject, URL,
    snippet, and date.
  - Calendar list and slot proposal paths are bounded but not connected to
    commitment state.
- `src/ariel/action_runtime.py`
  - Retrieval artifacts are snippet-oriented.
  - Google read answer synthesis is generic and source-candidate based.
- `src/ariel/sync_runtime.py`
  - Gmail history sync stores generic message signals without subject, sender,
    thread, body evidence, or commitment extraction.
  - Calendar sync creates title-like workspace items and signals.
- `src/ariel/memory.py`
  - Commitments exist as memory assertions, but current extraction is turn-text
    oriented and does not capture owner, counterparty, due windows, or provider
    evidence anchors as required typed fields.
- `src/ariel/proactivity.py`
  - Active commitments produce broad signals without due-date semantics.
- `src/ariel/attention_ranking.py`
  - Commitment ranking is static and confidence-oriented. It does not compute
    due-soon, overdue, waiting-on, stale, or snooze-aware urgency.

These are replacement targets, not compatibility promises.

## Final Architecture

### Layer 1: Provider Acquisition

Google acquisition owns OAuth-scoped external calls.

Required behavior:

- Gmail message search returns `message_id`, `thread_id`, result ordering,
  history id when available, labels, subject, sender, recipients when available,
  internal date, snippet, and a provider URL.
- Gmail message read fetches the full message with `format=full`.
- Gmail thread read fetches every message needed for a thread-level answer or
  follow-up decision, bounded by count and bytes.
- Gmail history sync hydrates changed messages enough to identify thread,
  headers, labels, sent/received direction, and whether a full read job is
  needed.
- Calendar sync and reads retain event id, recurring event id, iCal UID, status,
  start/end, timezone, attendees, organizer, conference/location, updated time,
  and etag.
- Provider calls never occur inside database transactions.
- External calls use bounded page sizes, byte limits, timeout limits, retry
  policy, and provider error classification.

Target files:

- `src/ariel/google_connector.py`
- `src/ariel/google_workspace_normalization.py`
- `src/ariel/sync_runtime.py`
- `src/ariel/worker.py`

### Layer 2: Provider Normalization

Provider normalization turns untrusted provider payloads into narrow internal
objects.

Required Gmail normalized objects:

- `NormalizedGmailMessage`
  - provider account id
  - Gmail message id
  - Gmail thread id
  - history id
  - RFC message id
  - in-reply-to and references
  - subject
  - normalized subject key
  - sender
  - recipients
  - cc
  - bcc when visible
  - reply-to
  - internal date
  - header date
  - sent/received/draft direction
  - labels
  - attachments metadata
  - body representation
  - provider URL
  - raw payload digest

- `NormalizedGmailBody`
  - preferred text body
  - sanitized HTML-derived text body when text/plain is absent or inferior
  - bounded blocks with stable block ids
  - quoted-section markers when detectable
  - signature markers when detectable
  - forwarded-message markers when detectable
  - truncation flags
  - charset and transfer decoding notes
  - body digest
  - attachment references

Required Calendar normalized objects:

- `NormalizedCalendarEvent`
  - provider account id
  - calendar id
  - event id
  - iCal UID
  - recurring event id
  - status
  - summary
  - description blocks
  - organizer
  - creator
  - attendees and response statuses
  - start/end with timezone
  - all-day flag
  - recurrence metadata
  - location and conference data
  - reminders
  - updated time
  - etag
  - provider URL
  - raw payload digest

Rules:

- Normalize once at provider boundary.
- Preserve provider identifiers in dedicated typed fields.
- Do not force provider objects into lossy `title/source/snippet` shapes.
- Treat provider body and description content as tainted evidence.
- Keep raw payloads out of prompts and logs.
- Use content hashes for dedupe and audit.

Target file:

- `src/ariel/google_workspace_normalization.py`

### Layer 3: Evidence Persistence

Provider evidence is Ariel's durable bridge between external content and AI
reasoning.

Required evidence records:

- provider object identity
- source kind: `gmail_message`, `gmail_thread`, `calendar_event`,
  `calendar_availability`
- source provider account id
- external ids
- source timestamp
- observed timestamp
- content digest
- bounded evidence blocks
- block ids and offsets when available
- taint label
- sensitivity label
- retention policy
- extraction status
- provenance chain

Rules:

- Store bounded body blocks, not prompt-ready raw corpus.
- Store enough provider identity to rehydrate, cite, dedupe, and invalidate.
- If full raw payload storage is introduced, it must be encrypted, access-gated,
  retention-bound, and never used as a prompt fallback.
- Evidence creation is idempotent by provider object id plus content digest.
- Provider changes produce new evidence versions. They do not mutate old
  evidence into a misleading state.

Target files:

- `src/ariel/persistence.py`
- `src/ariel/memory.py`
- `src/ariel/sync_runtime.py`
- `src/ariel/action_runtime.py`

### Layer 4: Work Graph

The work graph is the canonical product state for commitments and follow-up.

Required entities:

- `WorkPerson`
  - provider account scoped identity
  - email addresses
  - display names
  - relation to user when known

- `WorkThread`
  - provider thread id
  - normalized subject key
  - participants
  - last inbound message
  - last outbound message
  - last evidence id
  - current thread state

- `WorkCommitment`
  - commitment id
  - owner: user, counterparty, shared, unknown
  - requester
  - counterparty
  - action text
  - structured action category
  - source evidence ids and block ids
  - source thread/event ids
  - due window
  - earliest start when known
  - expected duration when known
  - priority
  - confidence
  - lifecycle state
  - resolution evidence id
  - supersession link
  - user review state
  - created/updated timestamps

- `WorkFollowUpLoop`
  - commitment id or thread id
  - loop kind
  - state
  - next check time
  - next notification time
  - stale-after time
  - last evaluated evidence id
  - last user feedback
  - snooze state
  - policy version

The implementation stores these as dedicated work-graph tables. Commitments and
follow-up loops are operational state, not only memory, and must remain typed,
queryable, indexed, and lifecycle-owned without JSON-stringly call sites.

### Layer 5: AI Extraction

AI extraction converts evidence into structured candidates. It does not activate
or resolve commitments directly.

Input:

- normalized evidence blocks
- source metadata
- current time and timezone
- known user identity
- known participants
- existing candidate or active commitments for the same thread/event
- calendar context when needed for scheduling interpretation

Output schema:

- candidate kind:
  - `commitment`
  - `decision`
  - `waiting_on_user`
  - `waiting_on_counterparty`
  - `meeting_request`
  - `schedule_proposal`
  - `deadline`
  - `resolved_commitment`
  - `not_actionable`
- action text
- owner
- requester
- counterparty
- due expression text
- normalized due window proposal
- timezone evidence
- confidence
- evidence block ids
- quoted-content caveat
- ambiguity reason
- suggested lifecycle transition
- suggested follow-up policy
- safety notes

Rules:

- Extraction uses strict JSON schema.
- Extraction must cite evidence block ids for every candidate.
- Candidates without evidence anchors are rejected.
- Candidates from quoted or forwarded text require explicit caveat and lower
  confidence unless current sender reaffirms them.
- Relative dates are resolved against the message/event timestamp, not the
  worker runtime timestamp, unless the evidence explicitly says otherwise.
- The user's configured timezone is the default only when source timezone is
  absent and the expression needs a timezone.
- Low-confidence or ambiguous candidates go to review.
- High-confidence candidates still require review when they affect
  notifications, memory, calendar, or provider writes under policy.

Target files:

- `src/ariel/workspace_reasoning.py`
- `src/ariel/memory.py`
- `src/ariel/worker.py`
- `src/ariel/response_contracts.py`

### Layer 6: Deterministic Validation

Deterministic validation owns correctness and lifecycle invariants.

Validators must check:

- evidence ids exist and belong to the same user/provider scope
- provider account matches active authority
- due window is parseable, timezone-aware when needed, and not fabricated
- owner is one of the allowed owner enum values
- confidence is within contract range
- lifecycle transition is allowed from current state
- duplicate candidate does not already exist for same evidence/action/due tuple
- resolved or superseded transitions cite later evidence
- follow-up times are within policy bounds
- notification eligibility respects user mode, snooze, dismissal, and feedback
- provider write proposals cite a live commitment or evidence source

Trusted database state that fails validation is a defect. Do not silently
renormalize it deeper in the stack.

### Layer 7: Commitment Lifecycle

Commitment states:

- `candidate`
- `needs_review`
- `active`
- `waiting_on_user`
- `waiting_on_counterparty`
- `scheduled`
- `snoozed`
- `resolved`
- `superseded`
- `dismissed`
- `rejected`
- `stale`
- `expired`
- `deleted`

Allowed transitions:

- Evidence extraction creates `candidate`.
- Policy promotes `candidate` to `active` only when confidence, source trust,
  sensitivity, and review policy allow.
- User review can promote, edit, reject, dismiss, or delete.
- Later evidence can suggest waiting-on or resolved transitions.
- Deterministic validation applies the transition only when source evidence is
  newer or explicitly authoritative.
- Snooze changes attention/follow-up timing, not source truth.
- Resolution requires user action, provider evidence, or explicit user feedback.
- Deletion invalidates recall, proactive signals, attention items, projections,
  and follow-up tasks.

Rules:

- Do not infer resolution from silence alone.
- Do not infer user commitment from a third party assigning work unless the
  source and confidence policy allow it.
- Do not create user-facing reminders for commitments still in `candidate`
  unless the reminder is explicitly a review prompt.
- Do not let follow-up state change the underlying commitment action text or due
  window.

### Layer 8: Follow-Up Loops

Follow-up loops are durable workflows, not prompt artifacts.

Loop kinds:

- `due_date`
- `waiting_for_reply`
- `needs_user_reply`
- `meeting_scheduling`
- `event_prep`
- `event_follow_up`
- `stale_commitment_review`
- `connector_repair`

Evaluation inputs:

- active commitment state
- due window
- current time
- user timezone
- source evidence age
- thread last inbound/outbound message
- calendar event status
- attendee response status
- snooze state
- dismissal state
- feedback state
- notification history
- connector health
- policy version

Outputs:

- no-op with reason
- update loop state
- enqueue future evaluation
- create or update attention signal
- create or update attention item
- propose draft
- propose calendar event
- ask user for clarification
- mark stale

Rules:

- Every scheduled follow-up rechecks current state before acting.
- Stale follow-up tasks exit without notification.
- Snooze overrides next notification time but not due truth.
- Dismissal suppresses the specific attention item but not the commitment unless
  the user explicitly resolves or deletes it.
- Follow-up loops must be idempotent by loop id, state version, and scheduled
  time.
- Notifications include the reason, source, due window, and primary action.
- Repeated notifications back off according to policy and user feedback.
- Loop policy is versioned so old queued jobs can be evaluated safely after a
  policy change.

Target files:

- `src/ariel/proactivity.py`
- `src/ariel/attention_ranking.py`
- `src/ariel/worker.py`
- `src/ariel/persistence.py`

### Layer 9: Attention Ranking

Commitment ranking must use structured features, not raw text.

Required features:

- lifecycle state
- owner
- due window start/end
- overdue duration
- time until due
- waiting direction
- last inbound age
- last outbound age
- last user action age
- source trust
- confidence
- user feedback
- snooze state
- dismissal state
- calendar conflict state
- connector health
- sensitivity label
- autonomy scope

Required outputs:

- rank score
- priority
- urgency
- delivery decision
- rank reason
- next follow-up time
- expiry time when applicable
- suppression reason when not delivered

Rules:

- Raw provider payloads are not ranking inputs.
- AI-generated prose is not a ranking input unless it has been stored as a
  reviewed memory or audited evidence summary.
- Due-soon and overdue commitments outrank generic active commitments.
- Waiting-on-user and unresolved meeting scheduling commitments can interrupt
  only when due, stale, or explicitly high priority.
- Waiting-on-counterparty loops default to lower urgency and longer backoff.
- Calendar conflicts with user-owned commitments raise urgency.
- Snooze, dismissal, and negative feedback reduce delivery, not source truth.

### Layer 10: Provider Writes

Provider writes are proposal-driven side effects.

Write categories:

- create draft
- send email
- archive email
- label email
- trash email
- create calendar event
- update calendar event
- RSVP to calendar event

Rules:

- Reads can be inline when policy allows.
- Writes require proposal, policy evaluation, approval when required, execution,
  receipt, and audit.
- Send and calendar mutation default to approval-required.
- Draft creation has lower impact than send, but still needs a typed proposal
  and receipt.
- A write proposal must cite source evidence, active commitment, or explicit
  user instruction.
- Provider response ids, etags, message ids, event ids, and timestamps are
  recorded.
- External writes are never performed inside DB transactions.
- Retried writes use provider idempotency where available and Ariel side-effect
  receipts everywhere.
- The model never receives a broad "send anything" tool because it read an email
  containing instructions.

## Capability Contracts

### Gmail Read

Capability id:

- `cap.email.read`

Input:

- `message_id` or `thread_id`
- read mode: `message`, `thread`, or `thread_context`
- optional reason

Output:

- typed status
- provider message/thread ids
- normalized metadata
- bounded body blocks
- attachments metadata
- evidence ids
- citations
- truncation flags
- taint and sensitivity labels
- provider URL
- read receipt id

Hard failures:

- missing scope
- connector unavailable
- not found
- provider permission denied
- decode failed
- body too large
- unsupported payload
- transient provider error

### Gmail Search

Capability id:

- `cap.email.search`

Input:

- Gmail query
- bounded result count
- optional recency/window

Output:

- structured message refs
- message ids
- thread ids
- subject
- sender
- recipients when available
- date
- labels
- snippet for preview only
- provider URL
- evidence status: `metadata_only`, `body_available`, or `needs_read`

Search never pretends snippets are full content.

### Calendar Read

Capability id:

- `cap.calendar.list`

Output includes typed events, not only snippets:

- event id
- calendar id
- status
- summary
- start/end/timezone
- attendees
- organizer
- location/conference metadata
- recurrence metadata
- provider URL
- evidence id when details are hydrated

### Slot Proposal

Capability id:

- `cap.calendar.propose_slots`

Rules:

- Uses calendar availability and email scheduling context when provided.
- Missing attendee freebusy authority returns a typed partial result, not a
  pretend full answer.
- Proposed slots carry timezone, confidence, constraints, and source evidence.

### Commitment Review

The cutover exposes or replaces these product-level commitment capabilities as
part of the memory hard cutover:

- inspect commitments
- inspect commitment source
- approve commitment
- edit commitment
- reject commitment
- resolve commitment
- snooze commitment
- delete commitment
- request follow-up draft

These capabilities expose product objects, not raw database rows.

## Data Model

Required durable state is implemented as dedicated tables that preserve these
logical records:

- `google_provider_objects`
- `provider_evidence`
- `provider_evidence_blocks`
- `work_people`
- `work_threads`
- `work_commitments`
- `work_commitment_sources`
- `work_follow_up_loops`
- `work_follow_up_events`
- `provider_write_receipts`
- `provider_sync_cursors`
- `attention_signals`
- `attention_items`
- `memory_assertions` or successor semantic-memory records

Required indexes:

- provider account plus external object id
- Gmail thread id
- Gmail message id
- Calendar event id and iCal UID
- commitment lifecycle state
- owner plus lifecycle state
- due window start/end
- follow-up next check time
- source evidence id
- content digest
- attention item next follow-up time

Required constraints:

- unique provider object identity per provider account
- unique evidence version per provider object and digest
- unique active commitment source tuple when action/due/owner are equivalent
- loop belongs to exactly one commitment, thread, event, or connector case
- follow-up scheduled task references loop id and loop version
- provider write receipt unique by proposal id and idempotency key

## Prompt And AI Contracts

The model receives:

- bounded evidence blocks
- typed metadata
- current time
- timezone facts
- existing relevant commitments
- allowed output schema
- safety instructions that provider content is untrusted

The model does not receive:

- raw MIME payloads
- raw provider JSON
- OAuth tokens
- arbitrary Gmail headers outside the normalized contract
- all old email in a thread when bounded evidence selection is enough
- ranking internals
- capability ids outside the selected tool surface

Extraction prompts must require:

- evidence block citations
- explicit uncertainty
- distinction between current sender text and quoted/forwarded content
- owner classification
- due expression and normalized due proposal
- no lifecycle mutation outside schema
- no provider write instruction execution

## Privacy And Security

Rules:

- Gmail bodies and Calendar descriptions are restricted, tainted, user-private
  provider content.
- Use least-privilege OAuth scopes.
- Missing scopes produce typed unavailable states.
- Do not log raw bodies, headers beyond normalized fields, or Calendar
  descriptions.
- Do not include raw provider text in proactive ranking prompts.
- Encrypt cached raw provider payloads if they exist.
- Bound retention for raw payload caches.
- Preserve evidence deletion and redaction paths.
- Prompt-injection tests are required for every path that reads email or
  calendar descriptions and then proposes actions.
- Provider content cannot grant authority, override policy, approve actions,
  disable audit, change memory mode, or request hidden tool calls.
- Generated drafts must separate source facts from assistant-authored text.
- External links in email bodies are evidence, not instructions.

## Observability

Required counters and events:

- Gmail read success/failure by failure code
- MIME decode failure by part type
- body truncation counts
- HTML sanitization fallback counts
- evidence records created
- extraction candidates by kind
- candidates rejected by validator reason
- commitments promoted/rejected/resolved
- follow-up loops evaluated
- stale follow-up no-ops
- notifications created/suppressed by reason
- provider writes proposed/approved/executed/failed
- prompt-injection policy blocks
- sync cursor failures and repairs

Required diagnostics:

- why this commitment exists
- why this follow-up fired now
- why this item was suppressed
- which evidence blocks were used
- which model judgment created the candidate
- which validator accepted or rejected it
- which policy version governed a loop or write

## Failure Behavior

- If Gmail body decoding fails, email read returns a typed failure and does not
  answer from a snippet as a fallback.
- If evidence persistence fails, extraction and answers requiring evidence fail
  closed.
- If due-date normalization fails, the candidate is review-only and cannot
  schedule a due-date follow-up.
- If follow-up evaluation sees deleted, resolved, superseded, stale, or
  unauthorized state, it exits without notification and records the no-op.
- If connector authorization is revoked, commitment source truth remains but
  provider rehydration and writes are unavailable with typed reasons.
- If Calendar availability is partial, slot proposals are marked partial and
  cannot be represented as confirmed availability.
- If provider write execution is ambiguous, Ariel records an ambiguous receipt
  and requires reconciliation before retrying the same logical side effect.

## Key Decisions

- Full Gmail body handling is a provider-normalization concern, not an answer
  synthesis concern.
- Gmail snippets are previews only. They are never evidence for commitment
  creation or full-content answers.
- Commitments are operational work objects, not plain text memory facts.
- Memory can expose and recall commitments, but follow-up loops belong to
  operational state.
- Due dates are intervals with timezone and confidence, not strings.
- Silence does not resolve commitments.
- Snooze is an attention decision, not a truth mutation.
- Raw provider payloads are not ranking inputs.
- Provider writes always create receipts.
- The final cutover removes old snippet-only contracts instead of supporting
  compatibility shapes.

## Implementation Plan

### 1. Define Contracts And Persistence

Required work:

- Add normalized Gmail and Calendar domain types.
- Add provider evidence and evidence block persistence.
- Add work graph persistence or typed memory extension for commitments and
  follow-up loops.
- Add provider write receipt persistence.
- Add response contracts for Gmail read/search, Calendar read, evidence blocks,
  commitments, and follow-up loops.

Acceptance:

- Schemas are typed and documented.
- Migrations include reversible upgrade and downgrade.
- Constraints enforce provider identity and loop idempotency.
- No public response contract still claims snippets are full reads.

### 2. Replace Gmail Read And Search

Required work:

- Replace Gmail metadata-only search results with structured message refs.
- Replace Gmail read with full MIME normalization.
- Decode nested MIME parts.
- Decode base64url part bodies.
- Prefer `text/plain`; use sanitized HTML-derived text when needed.
- Detect and mark quoted, forwarded, and signature sections where practical.
- Return bounded body blocks and evidence ids.
- Add body truncation and decode diagnostics.

Acceptance:

- Unit tests cover multipart alternative, nested multipart, HTML-only,
  attachments, missing body data, non-UTF charsets, malformed base64, long body
  truncation, quoted text, and forwarded text.
- Integration tests prove `cap.email.read` returns evidence-backed body blocks.
- Existing snippet-only assertions are removed or replaced.

### 3. Replace Calendar Reads And Scheduling Context

Required work:

- Return typed event objects from Calendar list/detail paths.
- Persist event evidence and description blocks.
- Preserve attendee, organizer, recurrence, timezone, and status metadata.
- Connect email scheduling requests to Calendar slot proposal input.
- Mark partial availability explicitly.

Acceptance:

- Tests cover all-day events, timed events, timezone conversion, recurring event
  identity, cancelled events, attendee statuses, missing freebusy scope, and
  partial availability.

### 4. Add Evidence-Backed Extraction

Required work:

- Add AI extraction job for Gmail and Calendar evidence.
- Add strict extraction schema.
- Add deterministic validator.
- Add dedupe against existing candidates and active commitments.
- Route candidates through memory/review policy.

Acceptance:

- Tests cover user-owned tasks, counterparty-owned tasks, ambiguous ownership,
  explicit due dates, relative due dates, vague due windows, quoted tasks,
  forwarded tasks, resolved tasks, and not-actionable messages.
- Candidates without evidence anchors are rejected.
- Relative dates resolve against source timestamp.

### 5. Add Commitment Lifecycle And Review

Required work:

- Implement lifecycle states and allowed transitions.
- Expose inspect/review/edit/reject/resolve/snooze/delete flows.
- Integrate active commitments into memory recall.
- Preserve source evidence and lifecycle history.

Acceptance:

- Tests cover activation, review-required routing, edit, reject, resolve,
  supersede, stale, delete, and recall behavior.
- User-facing commitment views show action, owner, due window, state, source,
  and confidence.

### 6. Add Follow-Up Loops

Required work:

- Add loop records and durable evaluation jobs.
- Compute next check and next notification from commitment state.
- Integrate with attention signals and attention items.
- Implement stale no-op recheck.
- Respect snooze, dismissal, connector state, and feedback.

Acceptance:

- Tests cover due-soon, overdue, waiting-on-user, waiting-on-counterparty,
  meeting scheduling, snooze override, dismissal, stale queued task, resolved
  before follow-up, connector revoked, and repeated notification backoff.

### 7. Replace Commitment Ranking

Required work:

- Replace static commitment scoring with structured feature extraction.
- Add due-window, waiting-direction, calendar-conflict, and feedback features.
- Persist rank reasons and next follow-up times.
- Remove old generic active-commitment ranking behavior.

Acceptance:

- Ranking tests prove due and overdue commitments outrank generic active
  commitments.
- Snoozed and dismissed commitments suppress delivery without deleting source
  truth.
- Raw provider text is absent from ranking inputs.

### 8. Gate Provider Writes

Required work:

- Tie drafts, sends, and calendar mutations to source evidence or explicit user
  instruction.
- Persist proposal, approval, execution, and receipt records.
- Add ambiguous-write reconciliation.
- Ensure retries are idempotent.

Acceptance:

- Tests cover draft creation, send approval, calendar create approval, provider
  failure, ambiguous response, retry, duplicate prevention, and audit receipt.

### 9. Remove Legacy Paths

Required work:

- Remove snippet-only read response shapes.
- Remove deterministic answer synthesis that pretends snippets are full email
  content.
- Remove old commitment ranking.
- Remove tests that encode legacy behavior.
- Update docs and acceptance suites.

Acceptance:

- `rg "snippet-only|metadata-only|legacy|fallback" src tests` has no surviving
  product path references for Gmail read, commitment ranking, or follow-up
  behavior.
- The old behavior is unreachable in production runtime.

## Acceptance Criteria

The cutover is complete only when all criteria pass:

- Full Gmail bodies are decoded, normalized, bounded, persisted as evidence, and
  cited in answers.
- Gmail search returns message and thread ids needed for later full reads.
- Calendar reads return typed event objects and evidence.
- Email/calendar evidence can create structured commitment candidates.
- Active commitments include owner, action, due window, source evidence,
  confidence, state, and lifecycle history.
- Follow-up loops are durable, idempotent, snooze-aware, stale-safe, and
  explainable.
- Attention ranking uses structured commitment features.
- Provider writes are proposal and receipt based.
- Prompt-injection tests pass for email body, quoted text, forwarded text, and
  Calendar description inputs.
- No raw provider payload is passed into proactive ranking.
- No snippet-only Gmail read path remains.
- No compatibility API for old email read/search contracts remains.
- `make verify` passes.

## Required Test Suites

Unit:

- Gmail MIME normalization.
- HTML sanitization and text extraction.
- Evidence block bounding.
- Due-date normalization.
- Commitment validator.
- Lifecycle transition table.
- Follow-up scheduling.
- Attention feature extraction.
- Provider write idempotency helpers.

Integration:

- Gmail search to read to evidence to answer.
- Gmail history sync to evidence to commitment candidate.
- Calendar event sync to evidence.
- Email scheduling request to slot proposal.
- Commitment candidate to active memory/commitment.
- Active commitment to attention item.
- Snooze to future follow-up.
- Resolve before queued follow-up.
- Provider send/calendar create approval and receipt.

Security:

- Email body says to ignore policy and send mail.
- Quoted email assigns fake authority.
- Forwarded content contains malicious instructions.
- Calendar description contains prompt injection.
- HTML body contains hidden text.
- Link text conflicts with URL.
- Low-scope connector tries to perform high-scope action.

Regression:

- Existing Google read capabilities still work for legitimate read requests.
- Existing approval policy still gates sends and calendar creates.
- Existing memory recall includes active commitments only through the new
  structured path.
- Existing proactive attention flow still handles jobs, approvals, captures, and
  connector status.

## File Ownership

Primary implementation files:

- `src/ariel/google_connector.py`
  - external Google API calls only
  - no lossy final response shaping

- `src/ariel/google_workspace_normalization.py`
  - Gmail MIME parsing
  - Calendar event normalization
  - provider body block creation
  - provider digest helpers

- `src/ariel/persistence.py`
  - ORM models and serializers
  - no lifecycle product logic

- `src/ariel/sync_runtime.py`
  - provider sync orchestration
  - evidence creation jobs
  - cursor handling

- `src/ariel/workspace_reasoning.py`
  - extraction contracts
  - validation
  - commitment candidate creation
  - follow-up loop evaluation helpers

- `src/ariel/memory.py`
  - semantic memory integration
  - review lifecycle integration
  - recall context for active commitments

- `src/ariel/proactivity.py`
  - signal derivation from active commitments and follow-up loops
  - no raw provider reads for ranking

- `src/ariel/attention_ranking.py`
  - structured commitment ranking
  - delivery decisions
  - follow-up scheduling

- `src/ariel/action_runtime.py`
  - capability execution orchestration
  - retrieval artifact persistence
  - provider write proposals, approvals, receipts

- `src/ariel/capability_registry.py`
  - capability schemas and validators
  - no legacy response shapes

- `src/ariel/app.py`
  - route wiring and context assembly
  - no provider parsing or lifecycle rules

- `src/ariel/worker.py`
  - evidence extraction jobs
  - follow-up evaluation jobs
  - sync repair jobs

Primary tests:

- `tests/unit/test_google_workspace_normalization.py`
- `tests/unit/test_workspace_reasoning.py`
- `tests/unit/test_commitment_lifecycle.py`
- `tests/unit/test_follow_up_loops.py`
- `tests/unit/test_attention_ranking_commitments.py`
- `tests/integration/test_google_workspace_evidence_acceptance.py`
- `tests/integration/test_email_commitment_follow_up_acceptance.py`
- `tests/integration/test_calendar_scheduling_commitment_acceptance.py`
- `tests/integration/test_provider_write_receipts_acceptance.py`
- `tests/integration/test_workspace_prompt_injection_acceptance.py`

## Rollout Sequence

This is a hard cutover, but implementation can be staged on the branch:

1. Add contracts, types, persistence, and tests that fail against current code.
2. Replace Gmail normalization and read/search outputs.
3. Replace Calendar normalization outputs.
4. Add evidence persistence and extraction jobs.
5. Add commitment lifecycle and review.
6. Add follow-up loops and attention integration.
7. Replace provider write receipts and approval coupling.
8. Remove legacy response shapes and tests.
9. Run full verification and inspect audit/log output.

No stage is considered production-shippable until the final legacy removal step
is complete.

## Product Completion Checklist

- User can ask Ariel to read an email and get a full-body cited answer.
- User can ask what they owe and see source-backed commitments.
- User can ask why Ariel nudged them and get source, due, and loop reason.
- User can snooze, dismiss, resolve, edit, and delete a commitment.
- Ariel can propose a reply or calendar event from a commitment.
- Ariel cannot send or create events without policy and approval.
- Ariel ignores provider-content instructions that attempt to override policy.
- Ariel records every provider write receipt.
- Ariel can recover from worker crash without duplicate reminders or writes.
- Ariel can explain missing scope, missing body, decode failure, partial
  availability, and ambiguous due date.

## Documentation Completion Checklist

- Update [memory.md](memory.md) with the structured commitment interface once
  implemented.
- Update [attachments.md](attachments.md) only if provider attachments share
  extraction/storage code.
- Update [../production-runbook.md](../production-runbook.md) with operational
  counters, repair workflows, and privacy deletion checks.
- Update [../database.md](../database.md) with new table families and indexes.
- Update [../ai-first-sota-gap-cutover.md](../ai-first-sota-gap-cutover.md) if
  any intentional gap remains after implementation.
