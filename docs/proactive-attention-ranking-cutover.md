# Proactive Attention Ranking Cutover

## Scope

This document defines Ariel's hard cutover from heuristic proactive attention into a
production attention-ranking and follow-up system.

The cutover covers signal feature extraction, deterministic ranking, grouping,
delivery decisions, follow-up policy, feedback learning, action proposal
boundaries, inspection APIs, and operator recovery.

It does not cover new provider integrations, mobile push, public API exposure,
voice, autonomous external writes, generic automation builders, or model-provider
hosted memory.

## Cutover Policy

- This is a hard cutover.
- Remove one-signal-one-item attention review.
- Remove source-local priority heuristics as the user-facing ranking mechanism.
- Remove notification delivery decisions based only on categorical priority.
- Remove refresh behavior that ignores the requested attention item.
- Remove feedback that is merely recorded and not used by later ranking.
- Do not keep compatibility code for old attention review, old ranking semantics,
  old response shapes, old task payloads, old notification thresholds, or old tests.
- Do not add fallback paths that bypass ranking, grouping, delivery policy, or
  feedback rules.
- Do not dual-write old and new attention item shapes.
- Do not route raw provider content, raw email bodies, raw attachment content, or
  raw webhook payloads into ranking prompts.
- Do not let model output notify, suppress, schedule, rank, or propose actions in
  the proactive pipeline.
- No intermediate state is production-shippable while old and new attention review
  are both reachable.

## Goals

- Ariel surfaces the few attention items that matter, at the right time, with the
  right delivery channel.
- Every attention item is explainable: why this, why now, what changed, what
  evidence, what confidence, and what the user can do.
- Ranking is deterministic, inspectable, replayable, and testable.
- Grouping reduces notification noise by merging related signals into one
  actionable situation.
- Follow-up is lifecycle-aware, not only user-snooze-driven.
- Feedback changes future behavior through durable rules and preferences.
- Taint and provenance survive through signal, rank, group, notification,
  feedback, and action proposal records.
- Operators can inspect every stage from raw signal to ranked item to notification
  and follow-up.
- The system remains correct across retries, duplicate signals, process restarts,
  concurrent workers, provider outages, and stale connectors.

## Target Behaviour

### User Experience

- Ariel produces a small ranked attention queue, not an unfiltered event stream.
- Ariel can answer:
  - "why did you notify me?"
  - "why now?"
  - "what changed?"
  - "what evidence did you use?"
  - "what else did you suppress?"
  - "what is waiting on me?"
  - "what is stale?"
  - "what can I safely do next?"
  - "stop notifying me about this pattern"
  - "make this pattern more important"
- Attention items show source signal ids, evidence ids, rank reason, rank score,
  delivery decision, freshness, confidence, taint, and available controls.
- Related signals produce one grouped item when separate notifications would create
  noise.
- Critical approvals and near-expiry decisions can interrupt.
- Low-urgency captures and workspace changes appear in digests or queues unless
  ranking policy finds a concrete reason to interrupt.
- Follow-ups happen when the underlying situation is still open, stale, due soon,
  waiting on the user, or awaiting external completion.
- User feedback changes future ranking and delivery through visible durable state.
- Ariel never implies an external action was taken unless the existing action
  runtime recorded that exact side effect.

### Ranking Behaviour

- Every new signal is converted into normalized rank features before review.
- Ranking uses deterministic features first:
  - source type
  - lifecycle state
  - explicit due time or expiry
  - user wait state
  - external blocker state
  - recency
  - staleness
  - recurrence
  - source trust
  - taint
  - confidence
  - user feedback
  - pinned or muted rules
  - project, repo, person, thread, and commitment linkage
  - prior acknowledgements, snoozes, resolves, and corrections
- Ranking produces:
  - numeric `rank_score`
  - bounded categorical priority
  - bounded categorical urgency
  - `rank_reason`
  - structured `rank_inputs`
  - `delivery_decision`
  - `delivery_reason`
  - optional `suppression_reason`
  - optional `next_follow_up_after`
- Delivery decisions are separate from rank:
  - `interrupt_now`
  - `queue`
  - `digest`
  - `suppress`
- Ties are resolved by score, urgency, source-specific deadline, updated time, and
  stable id.

### Follow-Up Behaviour

- Follow-up policy is derived from item state and evidence, not only from manual
  snooze.
- Pending approvals follow up before expiry and expire when the approval expires.
- Waiting jobs follow up when stale or blocked.
- Connector-health items follow up when still disconnected, revoked, expired, or
  erroring after the repair window.
- Commitments follow up when due, stale, or explicitly user-pinned.
- Captures follow up only when they contain task-like intent, explicit reminder
  language, or user feedback marks similar captures as useful.
- Resolved, cancelled, superseded, expired, and suppressed items do not notify.
- Snooze always overrides automatic follow-up until the snooze time is reached.

### Ranking Behaviour

- Deterministic ranking runs without a model.
- Ranking decisions are stored as rank snapshots before user-facing item review.
- Ranking tasks require the exact group being ranked.
- Attention review tasks require the exact rank snapshots being reviewed.
- Invalid task payloads fail the task instead of falling back to broad queue
  scans.

## Architecture

The primary pipeline is:

1. Signal producers write durable `attention_signals`.
2. Feature extraction normalizes each signal into rank features.
3. Grouping assigns related signals to an attention group.
4. Deterministic ranking computes score, reason, and delivery decision.
5. Attention review upserts user-facing attention items from ranked groups.
6. Delivery policy creates notification records for interruptible items.
7. Follow-up policy schedules durable follow-up tasks.
8. Feedback records update durable ranking rules or preferences.
9. Action proposal review routes proposed actions through existing policy and
    approval mechanics.

Signal production remains the source of truth. Ranking and summaries are derived
projections that can be rebuilt.

## Structure

### Durable Tables

- `attention_signals`: deterministic candidates with source references,
  evidence, reason, priority, urgency, confidence, taint, and lifecycle state.
- `attention_rank_features`: one normalized feature row per active signal and
  ranking version.
- `attention_groups`: stable grouping records for one actionable situation across
  one or more signals.
- `attention_group_members`: signal-to-group membership with grouping reason and
  ranking version.
- `attention_rank_snapshots`: append-only ranking outputs for each group review:
  score, rank inputs, rank reason, delivery decision, suppression reason, and
  follow-up recommendation.
- `attention_items`: user-facing lifecycle object created from the latest active
  rank snapshot.
- `attention_item_events`: append-only lifecycle, rank, delivery, feedback, and
  follow-up events.
- `notification_records`: outbound delivery lifecycle state.
- `proactive_feedback`: raw user feedback events.
- `proactive_feedback_rules`: durable inspectable rules derived from explicit user
  feedback.
- `action_proposals`: proposed actions derived from attention items, routed through
  existing policy and approval boundaries.

### Workers

- `workspace_signal_derivation_due`: derives internal-state signals.
- `provider_sync_due`: derives provider-backed workspace signals.
- `attention_feature_extraction_due`: normalizes new or changed signals into rank
  features.
- `attention_grouping_due`: assigns active signals to stable groups.
- `attention_ranking_due`: computes deterministic rank snapshots.
- `attention_review_due`: upserts attention items from accepted rank snapshots.
- `attention_delivery_due`: creates notification records from delivery decisions.
- `attention_item_follow_up_due`: rechecks follow-up conditions before delivery.
- `proactive_feedback_review_due`: converts explicit feedback into durable rules.
- `action_proposal_review_due`: preserves existing action proposal policy flow.
- `deliver_discord_notification`: delivers notifications and lifecycle controls.

### APIs

- `GET /v1/attention-signals`: inspect source candidates.
- `GET /v1/attention-rank-features`: inspect normalized ranking inputs.
- `GET /v1/attention-groups`: inspect grouped situations.
- `GET /v1/attention-rank-snapshots`: inspect ranking outputs.
- `GET /v1/attention-items`: inspect user-facing items ordered by rank and state.
- `GET /v1/attention-items/{id}`: inspect one item.
- `GET /v1/attention-items/{id}/events`: inspect item lifecycle.
- `POST /v1/attention-items/{id}/ack`: acknowledge current item.
- `POST /v1/attention-items/{id}/snooze`: defer current item.
- `POST /v1/attention-items/{id}/resolve`: mark situation complete.
- `POST /v1/attention-items/{id}/cancel`: cancel the item and acknowledge pending
  notifications.
- `POST /v1/attention-items/{id}/refresh`: recompute the targeted item group.
- `POST /v1/attention-items/{id}/feedback`: record explicit feedback.
- `POST /v1/attention-signals/derive`: enqueue derivation through the new pipeline.

### Modules

- `persistence.py` owns all ORM records.
- `app.py` owns request handlers and response construction.
- `worker.py` owns task claiming and dispatch.
- `sync_runtime.py` owns provider sync and provider-derived signals.
- `proactivity.py` owns internal signal derivation and attention lifecycle
  orchestration.
- `attention_ranking.py` owns feature extraction, grouping, deterministic scoring,
  delivery decisions, and follow-up recommendations.
- `memory.py` remains the source for commitments, decisions, salience, and memory
  feedback signals.
- `policy_engine.py` and `action_runtime.py` remain the only side-effect policy and
  execution gates.
- `response_contracts.py` owns typed API response contracts.

## Rules

### Signals

- All attention items start from one or more attention signals.
- Every signal has source references, evidence, reason, priority, urgency,
  confidence, taint, and dedupe key.
- Signal producers do not create user-facing attention items directly.
- Source-specific rules produce signals, not notifications.
- Signal rules are precise, inspectable, and deterministic.
- Signal dismissal and supersession are durable lifecycle transitions.

### Ranking

- Ranking is deterministic for a given ranking version and database snapshot.
- Ranking output is stored before item mutation or notification creation.
- Ranking stores enough inputs to explain and replay the decision.
- Ranking never reads raw provider payloads.
- Ranking never mutates external systems.
- Ranking cannot remove taint.
- User feedback affects ranking only through durable rules or preferences.
- Suppression is explicit and inspectable.
- A suppressed item records why it was suppressed.
- A muted rule cannot suppress critical approval expiry, credential revocation, or
  operator-health items.

### Grouping

- Grouping is based on stable keys:
  - approval id
  - job id
  - connector id
  - memory assertion id
  - capture id
  - workspace item id
  - provider thread id
  - calendar event id
  - Drive file id
  - project or repo id
- Group membership is durable and replayable.
- A group can contain multiple signals only when one user-facing item is clearer
  than separate items.
- Merging preserves all source signal ids and evidence ids.
- Splitting or superseding a group records an event.

### Delivery

- Notification creation is based on `delivery_decision`, not raw priority.
- Delivery respects quiet hours, notification budget, snooze, muted rules, item
  state, and channel configuration.
- Critical approval and credential-loss items can bypass digest mode.
- Notification dedupe is schema-backed.
- Failed notification delivery does not roll back item state.
- Delivered Discord messages include acknowledge, snooze, resolve, refresh, and
  feedback controls when supported.
- Delivery state is not the owner of attention correctness.

### Follow-Up

- Follow-up tasks recheck current item, signal, rank, and evidence state before
  notifying.
- Follow-up tasks are idempotent.
- Follow-up delivery creates a new notification only when the item is still
  actionable.
- Stale follow-up tasks exit without mutation.
- Resolved, cancelled, expired, superseded, and suppressed items do not follow up.
- Expiry is explicit for time-bound items.

### Feedback

- Feedback events are append-only.
- Feedback rules are derived through an auditable worker step.
- Feedback rules are inspectable and reversible.
- Explicit feedback beats inferred behavior.
- Inferred behavior cannot create broad suppressions without explicit feedback.
- Feedback never overrides hard policy, taint, permission, approval, or operator
  health rules.

### Prompt Isolation

- Proactive ranking, grouping, delivery, suppression, and follow-up are not prompt
  state.
- Provider payloads, emails, attachment content, and webhook bodies are normalized
  into durable evidence before ranking.
- Free-form model output cannot mutate proactive rank state.
- Proposed actions remain behind deterministic policy and exact-payload approval
  gates.

### Actions

- Proactivity proposes actions; it does not execute actions.
- Every action proposal references the attention item, rank snapshot, evidence ids,
  taint state, exact payload, payload hash, and policy state.
- External sends, calendar writes, Drive shares, and irreversible writes use the
  existing exact-payload approval path.
- A stale attention item cannot execute a stale action proposal.
- User approval applies only to the exact proposal shown to the user.

### Operations

- Every durable task has idempotency and retry semantics.
- Every mutation has a clear transaction boundary.
- Every concurrent mutation has a linearization strategy.
- Every derived projection can be rebuilt from canonical state.
- Every dead letter includes enough context for replay or remediation.
- Metrics cover signal volume, group volume, ranking latency, delivery decisions,
  notification latency, follow-up outcomes, feedback outcomes, suppression volume,
  false-positive feedback, missed-critical correction, and dead letters.

## Final State

- Attention review ranks, groups, and follows up through one pipeline.
- User-facing attention items are created only from ranked groups.
- List APIs return rank-aware ordering.
- Notifications are created only from delivery decisions.
- Feedback changes future behavior through durable rules.
- Refresh recomputes the targeted group and item.
- Follow-up is automatic when evidence says the situation is still actionable.
- Low-value signals can be queued, digested, or suppressed with an inspectable
  reason.
- Existing side-effect policy and approval gates remain the only execution path.
- No old heuristic attention review, old notification threshold, old response
  contract, old task payload, or legacy fallback remains reachable.

## Acceptance Criteria

- A pending approval near expiry creates a ranked item with high urgency,
  `interrupt_now`, evidence ids, rank inputs, and a scheduled pre-expiry follow-up.
- An expired approval marks its item expired and prevents further notification.
- A waiting job creates one grouped item and follows up only when stale, blocked,
  or awaiting user action.
- A running job that is still fresh is queued or digested, not immediately
  notified.
- A connector in `revoked` or `error` state creates an interruptible health item.
- A connector in non-critical repair state creates a queued or digest item unless
  stale beyond policy.
- Repeated quick captures do not notify unless ranked as task-like, due, pinned, or
  explicitly useful by feedback.
- Related workspace signals for the same calendar event, email thread, or Drive
  file produce one grouped attention item.
- Duplicate signals do not duplicate groups, rank snapshots, attention items,
  notifications, or follow-up tasks.
- A manually snoozed item does not notify before the snooze time.
- A stale follow-up task exits without notification when the item was resolved.
- `refresh` recomputes only the targeted group and records rank events.
- `feedback=noise` can create an inspectable durable rule that suppresses future
  matching low-risk items.
- `feedback=important` can raise future matching items into queue or interrupt
  policy when not blocked by hard policy.
- Muted feedback cannot suppress critical approval expiry, revoked credentials, or
  operator-health failures.
- Every attention item includes source signal ids, evidence ids, rank score, rank
  reason, rank inputs, delivery decision, confidence, priority, urgency, and taint.
- Attention item APIs return rank-aware ordering.
- Discord notifications include lifecycle controls and acknowledge linked
  notification records on ack, snooze, resolve, or cancel.
- Tainted provider content remains tainted through group, notification, and
  action proposal records.
- Proposed actions remain behind existing policy and exact-payload approval gates.
- Integration tests cover ranking, grouping, delivery, follow-up, feedback,
  refresh, expiry, suppression, and duplicate handling.
- No test, endpoint, worker, or module path depends on old heuristic review or old
  notification-threshold behavior.

## Non-Goals

- Do not build autonomous side-effect execution.
- Do not build a generic automation builder.
- Do not build a provider-agnostic stream-processing prompt runner.
- Do not store full mailbox, full Drive corpus, or full calendar history for
  ranking.
- Do not use model memory as the source of truth for proactive state.
- Do not add mobile push, SSE, WebSocket, or LISTEN/NOTIFY as the correctness
  mechanism.
- Do not create compatibility endpoints, compatibility migrations, compatibility
  response contracts, or feature flags for old attention behavior.
- Do not support old proactive source types, old task names, old response shapes,
  or old notification semantics.

## Key Decisions

- Attention signals remain the single join point for provider and internal
  proactivity.
- Ranking is a durable derived projection, not hidden prompt state.
- Deterministic ranking owns correctness.
- Delivery decisions are separate from priority.
- Grouping is required before user-facing item creation.
- Follow-up is state-driven and must recheck current evidence.
- Feedback must become durable inspectable rules or preferences.
- Suppression is a first-class auditable outcome.
- Taint and provenance cannot be removed by ranking or grouping.
- Side effects remain in the existing action runtime and policy engine.
- Discord remains a delivery surface, not the owner of proactive work.
- Hard cutover removes compatibility instead of hiding old behavior behind flags.

## File Plan

- `docs/proactive-attention-ranking-cutover.md`: canonical cutover spec for
  production attention ranking and follow-up.
- `docs/index.md`: link this cutover spec and remove stale references to deleted
  proactive docs.
- `alembic/versions/*_proactive_attention_ranking_cutover.py`: replace old
  attention review schema with rank features, groups, rank snapshots, feedback
  rules, and required item fields.
- `src/ariel/persistence.py`: add ORM records and fields for rank features,
  groups, group members, rank snapshots, feedback rules, and item rank state.
- `src/ariel/response_contracts.py`: replace attention response contracts with
  rank-aware contracts.
- `src/ariel/proactivity.py`: remove one-signal-one-item review and own lifecycle
  orchestration over ranked groups.
- `src/ariel/attention_ranking.py`: implement feature extraction, grouping,
  deterministic scoring, delivery decisions, and follow-up recommendations.
- `src/ariel/app.py`: replace attention endpoints with rank-aware inspection and
  lifecycle behavior.
- `src/ariel/worker.py`: replace old attention task dispatch with the new ranking,
  grouping, delivery, follow-up, and feedback review tasks.
- `src/ariel/sync_runtime.py`: keep provider-derived signal creation and schedule
  the new feature extraction path.
- `src/ariel/discord_bot.py`: keep attention controls and add feedback controls
  when supported by the API.
- `tests/integration/`: replace old proactive attention tests with ranking,
  grouping, delivery, follow-up, feedback, refresh, and failure-mode acceptance
  tests.
- `tests/unit/`: add deterministic ranking, grouping, delivery decision, and
  feedback-rule tests.
- `README.md`: update proactive attention summary after implementation.
- `docs/production-runbook.md`: add health checks and recovery playbooks for rank
  features, groups, snapshots, feedback rules, suppression, and dead letters.
