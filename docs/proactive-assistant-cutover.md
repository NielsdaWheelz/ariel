# Proactive Assistant Hard Cutover

## Scope

This document defines Ariel's hard cutover from subscription polling and
synthetic watch payloads into a production proactive assistant architecture with
provider-backed deltas, normalized workspace state, deterministic attention
signals, auditable judgment, and operator-visible lifecycle controls.

The cutover covers provider event ingress, subscription renewal, cursor-backed
sync, normalized calendar/email/drive state, internal system signals, attention
signal derivation, ranking, notification, user controls, action proposal, and
recovery.

It does not cover model-provider hosted memory, generic document RAG, browser
automation, voice, mobile push, public Ariel API exposure, or autonomous
write-side effects.

## Cutover Policy

- This is a hard cutover.
- Remove synthetic `calendar_watch`, `email_watch`, and `drive_watch`
  `check_payload` behavior.
- Remove source-specific proactive polling branches from the user-facing
  attention path.
- Do not keep legacy proactive subscription code.
- Do not dual-write old and new proactive stores.
- Do not add compatibility views for old proactive tables, endpoints, response
  shapes, event names, source types, tasks, or tests.
- Do not preserve current `/v1/proactive/subscriptions` behavior unless every
  field is native to the new contract.
- Do not route raw provider notifications, email bodies, calendar descriptions,
  Drive content, or webhook payloads directly into a model prompt.
- Do not let transport callbacks perform judgment, notification, or side
  effects.
- Do not ship a polling-only provider mode as a compatibility path.
- Required resync after missed provider notifications, cursor invalidation, or
  subscription repair is part of the primary sync protocol, not a legacy path.
- If provider state cannot be synced to a trustworthy cursor, fail closed with a
  typed error and an auditable event.
- No intermediate state is production-shippable while old and new proactive
  systems are both reachable.

## Goals

- Ariel notices important changes from real calendar, email, Drive, Agency,
  approval, memory, and capture state.
- Every proactive item has durable evidence, source provenance, and a replayable
  path from ingress to notification.
- Provider notifications trigger sync work; provider deltas and local state are
  the source of truth.
- Model judgment is bounded, auditable, and unable to mutate external systems
  directly.
- Users can understand, correct, snooze, resolve, and audit proactive behavior.
- Operators can inspect subscription health, cursor health, sync runs, signal
  derivation, notification delivery, and dead letters.
- The system remains correct across duplicate notifications, missed
  notifications, retries, process restarts, provider outages, and concurrent
  workers.

## Target Behavior

### User Experience

- Ariel surfaces timely attention items from real workspace changes.
- Ariel explains why each item exists, what changed, which evidence supports it,
  and what action is available.
- Ariel supports acknowledge, snooze, resolve, cancel, refresh, and feedback on
  every attention item.
- Ariel can answer:
  - "why did you notify me?"
  - "what changed?"
  - "what evidence did you use?"
  - "where did this come from?"
  - "stop notifying me about this"
  - "show related signals"
  - "mark this pattern as important"
  - "mark this pattern as noise"
- Ariel groups related changes when separate notifications would create noise.
- Ariel does not pretend that a provider notification contains complete facts.
- Ariel does not send email, create calendar events, share Drive files, or
  mutate external systems without the normal action policy and approval path.

### Provider Behavior

- Google Calendar sync uses stored calendar sync tokens.
- Gmail sync uses stored mailbox history state and Pub/Sub-triggered work.
- Google Drive sync uses stored start page tokens and changes pages.
- Microsoft Graph sync, when added, uses stored delta links and lifecycle
  notifications.
- Provider subscriptions are renewed before expiration by durable worker tasks.
- Subscription removal, reauthorization, missed notification, cursor
  invalidation, and permission loss are explicit lifecycle states.
- Provider callbacks authenticate the producer, store an event envelope, enqueue
  sync work, and return quickly.
- Sync workers retrieve provider deltas, normalize state, derive signals, and
  advance cursors only after all committed local writes for that sync page
  succeed.

### Judgment Behavior

- Deterministic rules create initial `attention_signals`.
- LLM ranking or summarization reads only a bounded evidence bundle.
- LLM output can create or update attention items only through a typed,
  policy-checked worker step.
- LLM output cannot execute write-side effects.
- Untrusted provider content is tainted and remains tainted through signal,
  attention, summary, and action proposal records.
- Tainted or ambiguous content cannot authorize an external send,
  irreversible write, or Drive share.

## Architecture

The primary pipeline is:

1. Provider subscription manager creates, renews, verifies, and retires provider
   subscriptions.
2. Provider ingress validates callback authenticity and stores append-only event
   envelopes.
3. Durable sync tasks use provider cursors to retrieve authoritative deltas.
4. Normalizers update canonical workspace item state and append item events.
5. Signal producers derive deterministic attention signals from workspace and
   internal state.
6. Attention workers rank, group, summarize, and upsert attention items.
7. Notification workers deliver user-visible messages and lifecycle controls.
8. Action workers turn accepted proposals into normal policy-governed action
   attempts.

Provider push shortens latency. Cursor-backed sync provides correctness.

## Structure

### Durable Tables

- `connector_subscriptions`: provider watch or subscription records, target
  resource, channel identifiers, expiration, status, renewal schedule, auth
  posture, and last error.
- `sync_cursors`: per connector/resource cursor state, cursor version, sync
  scope, high-water mark, invalidation state, and last successful sync.
- `provider_events`: append-only authenticated callback envelopes with provider,
  resource, external event id, headers, body digest, received timestamp, status,
  and dedupe key.
- `sync_runs`: durable sync attempts with cursor before/after, trigger event,
  status, page counts, item counts, signal counts, error, started time, and
  completed time.
- `workspace_items`: canonical current state for calendar events, email
  messages/threads, Drive files/comments/shares, and provider-owned metadata.
- `workspace_item_events`: append-only normalized item-level changes with
  source event references and before/after summaries.
- `attention_signals`: deterministic candidates for user attention with source
  item references, evidence, reason, urgency, priority, confidence, taint, and
  lifecycle state.
- `attention_items`: user-facing attention lifecycle state.
- `attention_item_events`: append-only user and system lifecycle events.
- `notification_records`: outbound notification lifecycle state.
- `proactive_feedback`: user feedback on signals, items, rules, and topics.
- `action_proposals`: proposed actions derived from attention items with exact
  payload hash, policy state, approval state, and evidence references.

### Workers

- `provider_subscription_renewal_due`: renews or repairs provider
  subscriptions.
- `provider_event_received`: validates stored provider events that require
  asynchronous checks and schedules sync.
- `provider_sync_due`: runs cursor-backed provider sync.
- `workspace_signal_derivation_due`: derives attention signals from normalized
  changes.
- `attention_review_due`: ranks, groups, summarizes, and upserts attention
  items.
- `attention_item_follow_up_due`: keeps existing follow-up lifecycle semantics.
- `deliver_discord_notification`: keeps Discord as the delivery surface.
- `action_proposal_review_due`: routes proposed actions through existing policy
  and approval mechanics.

### APIs

- `POST /v1/providers/{provider}/events`: narrow provider callback ingress.
- `GET /v1/connectors/{provider}/subscriptions`: inspect subscription state.
- `POST /v1/connectors/{provider}/subscriptions/{id}/renew`: force renewal.
- `GET /v1/connectors/{provider}/sync-cursors`: inspect cursor state.
- `POST /v1/connectors/{provider}/sync`: force a sync for an owned scope.
- `GET /v1/provider-events`: inspect callback envelopes.
- `GET /v1/sync-runs`: inspect sync attempts.
- `GET /v1/workspace-items`: inspect normalized provider state.
- `GET /v1/attention-signals`: inspect deterministic candidates.
- `GET /v1/attention-items`: inspect user-facing attention items.
- `POST /v1/attention-items/{id}/ack|snooze|resolve|cancel|refresh`: preserve
  attention item controls in the new contract.
- `POST /v1/attention-items/{id}/feedback`: record signal quality feedback.
- `GET /v1/action-proposals`: inspect proposed actions.

### Modules

- `persistence.py` owns all ORM records.
- `app.py` owns request handlers and response construction.
- `worker.py` owns task claiming and dispatch.
- `provider_events.py` owns callback envelope parsing and dedupe helpers.
- `connector_subscriptions.py` owns provider subscription lifecycle.
- `sync_runtime.py` owns cursor-backed sync orchestration.
- `workspace_items.py` owns provider delta normalization.
- `attention_signals.py` owns deterministic signal derivation.
- `proactivity.py` owns attention item creation, grouping, review, and lifecycle.
- `google_connector.py` owns Google OAuth, provider calls, watch setup, and delta
  reads.
- `policy_engine.py` and `action_runtime.py` remain the only side-effect policy
  and execution gates.
- `response_contracts.py` owns typed API response contracts.

## Rules

### Ingress

- Every provider callback is untrusted input.
- Every provider callback must be authenticated before it can enqueue sync work.
- Ingress stores the event envelope before processing provider state.
- Ingress dedupes by provider-owned delivery id or by a stable provider,
  resource, message number, and channel key.
- Ingress returns success after durable acceptance, not after sync completion.
- Payload conflicts for an already accepted external event id are hard errors.

### Sync

- Sync advances a cursor only after normalized item writes and signal derivation
  scheduling are durable.
- Sync must be idempotent for the same cursor, page token, and provider event.
- Sync stores deleted provider resources as tombstones, not silent omissions.
- Sync stores enough metadata to explain provider state without storing
  unnecessary full content.
- Sync treats expired credentials, missing scopes, revoked access, provider
  rate limits, and invalid cursors as typed states.
- Sync never calls a model while holding a database transaction.

### Signals

- All attention items start from one or more attention signals.
- Internal DB states such as jobs, approvals, memory commitments, and captures
  produce attention signals through the same pipeline as provider deltas.
- Source-specific polling branches do not create user-facing attention items
  directly.
- Every signal has source references, evidence, reason, priority, urgency,
  confidence, taint, and dedupe key.
- Signal rules are precise and inspectable.
- User feedback changes future signal derivation through durable rules or
  preferences, not hidden prompt state.

### Model Use

- The model receives bounded evidence bundles, not raw provider callbacks.
- The model cannot see hidden email HTML, invisible text, tracking pixels, or
  provider metadata that is irrelevant to the task.
- Provider content in model context is marked as untrusted quoted content.
- Model-generated summaries must cite evidence ids.
- Model ranking cannot override hard policy, permission, taint, or user
  preference rules.
- Model failures produce typed worker errors and do not create silent attention
  items.

### Notifications

- Notification dedupe is schema-backed.
- Notifications include enough context for the user to act without opening
  raw provider content unless required.
- Notifications expose controls for acknowledge, snooze, resolve, refresh, and
  feedback.
- Notification delivery is independent from attention item correctness.
- Failed notification delivery does not roll back attention item state; it
  creates delivery state for retry and operator inspection.

### Actions

- Proactivity proposes actions; it does not execute actions.
- Every proposed action has exact payload, evidence ids, taint state, policy
  decision, and approval state.
- External sends, calendar writes, Drive shares, and irreversible writes use the
  existing approval and exact-payload-hash execution path.
- A stale attention item cannot execute a stale action proposal.
- User approval applies only to the exact proposal shown to the user.

### Operations

- Every durable task has idempotency and retry semantics.
- Every external side effect has a discoverable local record before or after the
  side effect according to mutation-ordering rules.
- Every provider subscription has renewal monitoring.
- Every sync cursor has freshness monitoring.
- Every dead letter has enough context for replay or manual remediation.
- Metrics cover callback volume, sync latency, cursor age, subscription age,
  signal volume, notification latency, approval outcomes, and dead letters.

## Final State

- `calendar_watch`, `email_watch`, and `drive_watch` are gone.
- `check_payload` no longer carries synthetic proactive signals.
- Proactive attention is driven by `attention_signals`.
- Provider callbacks are narrow ingress only.
- Provider deltas are stored in normalized workspace state.
- Internal states and provider states feed the same attention signal pipeline.
- Attention items are explainable, deduped, auditable, and user-controllable.
- LLM judgment is optional, bounded, and policy-contained.
- All write-side effects remain behind the existing action policy and approval
  gates.
- Operators can inspect every stage from provider event to delivered
  notification.

## Acceptance Criteria

- A Google Calendar event create, update, and delete produces normalized
  workspace item events and advances the stored sync cursor exactly once.
- A Gmail message create, update, label change, and delete produces normalized
  workspace item events and advances the stored history cursor exactly once.
- A Drive file create, update, permission change, comment change, and delete
  produces normalized workspace item events and advances the stored changes
  cursor exactly once.
- Duplicate provider notifications do not duplicate provider events, sync runs,
  workspace item events, attention signals, attention items, or notifications.
- A missed notification lifecycle event schedules required resync and records an
  auditable lifecycle event.
- Cursor invalidation records a typed state, performs required resync, and does
  not silently skip changes.
- Provider subscription expiration creates a visible connector-health attention
  signal before the subscription stops delivering.
- Revoked access creates a connector-health attention signal and blocks provider
  sync for that connector until reconnect.
- An untrusted email containing hidden prompt instructions cannot create an
  external send, calendar write, or Drive share.
- Every attention item includes source signal ids, evidence ids, reason,
  priority, urgency, confidence, and taint state.
- The user can acknowledge, snooze, resolve, refresh, and give feedback on an
  attention item.
- Feedback changes future signal behavior through durable inspectable state.
- Existing Agency jobs, pending approvals, memory commitments, and quick captures
  create attention signals through the same pipeline as provider deltas.
- No code path creates an attention item directly from old proactive
  subscription source-specific polling.
- No API response exposes old proactive subscription response shapes.
- No migration creates compatibility views for old proactive tables.
- Focused integration tests cover provider ingress, duplicate delivery, cursor
  advancement, required resync, signal derivation, attention lifecycle, and
  action proposal approval boundaries.
- The production runbook documents subscription repair, cursor repair, dead
  letter replay, provider outage handling, and user data deletion.

## Non-Goals

- Do not build a generic automation builder.
- Do not build free-form stream processing where arbitrary events run arbitrary
  prompts.
- Do not build autonomous email send, calendar write, or Drive share.
- Do not store full mailbox, full Drive corpus, or full calendar history unless a
  specific normalized item requires bounded fields.
- Do not use model memory as the source of truth for proactive state.
- Do not support old proactive source types, old task names, or old response
  shapes.
- Do not expose provider callbacks as public generic ingestion endpoints.
- Do not add a provider-agnostic abstraction before one provider vertical proves
  the internal contract.
- Do not implement mobile push, SSE, WebSocket, or LISTEN/NOTIFY as the
  correctness mechanism.

## Key Decisions

- Provider notifications are hints; cursor-backed deltas are truth.
- The database is the canonical event log and recovery surface.
- Attention signals are the single join point for provider and internal
  proactivity.
- Attention items remain the user-facing lifecycle object.
- Discord remains a delivery surface, not the owner of proactive work.
- LLM review improves ranking and summaries but never owns correctness.
- Taint follows provider content through every derived record.
- Side effects stay in the existing action runtime and policy engine.
- Required provider resync is part of normal operation.
- Hard cutover removes compatibility instead of hiding old behavior behind flags.

## File Plan

- `docs/proactive-assistant-cutover.md`: canonical cutover spec.
- `docs/production-runbook.md`: add proactive provider operations after
  implementation.
- `.env.example`: add only real provider callback and subscription settings.
- `alembic/versions/*_proactive_assistant_cutover.py`: replace old proactive
  schema with new durable tables.
- `src/ariel/persistence.py`: add new ORM records and remove old proactive
  records.
- `src/ariel/app.py`: replace old proactive endpoints and add provider event,
  sync, signal, and inspection endpoints.
- `src/ariel/worker.py`: replace old proactive task dispatch with new task
  types.
- `src/ariel/sync_runtime.py`: own provider event task handling, cursor-backed
  sync orchestration, sync cursor advancement, normalized item projections, and
  provider-derived signals.
- `src/ariel/proactivity.py`: rewrite attention review and lifecycle on top of
  deterministic internal and provider signals.
- `src/ariel/google_connector.py`: add Google background access and delta read
  APIs.
- `src/ariel/response_contracts.py`: replace proactive response contracts.
- `tests/integration/`: add provider-ingress, sync, lifecycle, attention, and
  action-boundary acceptance tests.
