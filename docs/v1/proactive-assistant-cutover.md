# Proactive Assistant Cutover

## Scope

This document defines the hard cutover from bounded worker, Agency event, notification,
and scheduled-ish infrastructure to Ariel as a proactive assistant.

The cutover is direct. There is no legacy mode, compatibility layer, fallback runtime,
dual-write period, or alternate proactive path.

## Goals

- Ariel continuously notices owner-relevant work and life signals from configured sources.
- Ariel prioritizes those signals into a durable attention queue.
- Ariel checks in through Discord only when a signal crosses a configured attention threshold.
- Ariel follows up on unresolved items until they are acknowledged, snoozed, resolved, or expired.
- Ariel uses existing capability validation, policy, egress, approval, memory, and audit rules.
- Ariel stores all proactive state in Postgres and survives process restarts without losing work.
- Ariel emits clear events for every proactive detection, decision, delivery, acknowledgement,
  snooze, resolution, and expiry.

## Non-Goals

- No unbounded autonomous background agent.
- No hidden side effects.
- No proactive writes without explicit approval.
- No web, PWA, mobile, or alternate primary surface.
- No generic shell, ssh, browser, or arbitrary connector execution path.
- No compatibility with removed phone-web or legacy model-provider flows.
- No fallback to chat-only behavior when proactive configuration is missing.
- No duplicate API surface for the same proactive capability.
- No model-owned scheduler state.

## Target Behavior

### Continuous Noticing

Ariel maintains durable subscriptions for owner-approved domains:

- Open jobs and Agency work.
- Pending approvals.
- Explicit commitments in memory.
- Calendar windows and scheduling conflicts.
- Email or Drive queries saved as watches.
- Quick-capture items that require later review.
- Connector health and consent state.

Each subscription has a next run timestamp, source, owner-visible label, check policy, dedupe
policy, and notification policy. The worker claims due subscriptions through Postgres and
records every check attempt.

### Prioritization

Ariel converts raw observations into attention candidates. Each candidate has:

- `source_type` and `source_id`
- `priority`
- `urgency`
- `confidence`
- `reason`
- `evidence`
- `dedupe_key`
- `expires_at`
- `status`

Priority is deterministic and persisted. It does not live only in model output. Model calls may
summarize or classify bounded evidence, but the stored priority record is the system of record.

### Check-Ins

Ariel sends a Discord notification when a candidate is actionable and not suppressed by dedupe,
snooze, quiet-hours, or acknowledgement state. The notification must explain:

- What changed.
- Why Ariel is surfacing it now.
- What the owner can do next.
- Whether any action requires approval.

Every check-in has buttons for the valid next actions. At minimum:

- Acknowledge.
- Snooze.
- Resolve when the item supports resolution.
- Refresh when the item is linked to a job or external read.

### Follow-Up

Unresolved proactive items remain durable. Follow-up is scheduled by item state, not by an
ephemeral timer.

Ariel follows up when:

- A due time arrives.
- A previously blocked connector becomes healthy.
- A job changes state.
- A pending approval is near expiry or has expired.
- A commitment has not been resolved by its review window.

Follow-up stops when the item is acknowledged, resolved, expired, cancelled, or superseded.

### Side Effects

Proactive checks may execute read capabilities inline. Proactive checks may not execute writes or
external sends directly.

When Ariel proposes a write from proactive context, it creates the existing approval lifecycle.
The owner must approve the exact payload before execution.

## Final State

The worker is the only background execution path.

The proactive assistant has these durable concepts:

- `proactive_subscriptions`: configured watches and periodic checks.
- `proactive_check_runs`: each claimed check attempt and result.
- `attention_items`: deduped, prioritized items that may need owner attention.
- `attention_item_events`: append-only lifecycle events for attention items.
- `notifications`: user-visible delivery records for attention items and jobs.
- `notification_deliveries`: delivery attempts and provider responses.
- `background_tasks`: durable worker queue for due checks, delivery, reconciliation, and cleanup.

Existing turns, action attempts, approvals, jobs, Agency events, captures, artifacts, and memory
remain canonical in their existing tables. Proactive state links to them by id.

Discord is the primary surface. FastAPI remains the internal typed core. Postgres remains the
canonical state store. The Responses API remains the only model path.

## Architecture

### Data Flow

1. A source creates or updates a proactive subscription.
2. The scheduler enqueues a due background task.
3. The worker claims the task with `SELECT ... FOR UPDATE SKIP LOCKED`.
4. The worker executes a bounded read check.
5. The worker records a check run.
6. The worker upserts one or more attention items by dedupe key.
7. The worker records attention item events.
8. The worker decides whether a Discord notification is due.
9. The worker creates a notification and delivery task.
10. Discord buttons update attention item and notification state.

### Runtime Modules

- `worker.py` owns task claiming and dispatch.
- A new proactive runtime module owns subscription checks, attention item creation, and follow-up
  scheduling.
- `app.py` owns typed API endpoints and Discord-facing state transitions.
- `persistence.py` owns all ORM records.
- `response_contracts.py` owns surfaced proactive response contracts.
- `discord_bot.py` owns buttons and formatting for proactive items.
- `capability_registry.py` remains the only tool/capability registry.

### Worker Tasks

Replace the bounded worker task set with a complete proactive set:

- `proactive_check_due`
- `attention_item_follow_up_due`
- `deliver_discord_notification`
- `expire_approvals`
- `reap_stale_tasks`
- `agency_event_received`

Task types are closed and schema-constrained. Adding a task requires updating the ORM constraint,
migration, dispatcher, tests, and docs in one change.

### Scheduler

The scheduler is database-backed. It does not use process-local timers for durable work.

Scheduling happens by setting `run_after` on `background_tasks` and `next_run_after` on
subscriptions or attention items. Polling is the fallback mechanism. LISTEN / NOTIFY may be added
only as an optimization and must not carry data.

### Attention Model

Attention item statuses:

- `open`
- `notified`
- `acknowledged`
- `snoozed`
- `resolved`
- `expired`
- `cancelled`
- `superseded`

Only `open`, `notified`, and `snoozed` items are eligible for follow-up.

Priority values:

- `critical`
- `high`
- `normal`
- `low`

Priority must be explainable from persisted evidence. Do not infer priority from notification
text.

### Notification Model

Notifications support multiple source types:

- `agency_event`
- `attention_item`
- `approval`
- `connector_event`

Discord remains the only delivery channel in this cutover.

Notifications are deduped by stable keys. Delivery attempts are append-only. Acknowledgement is a
state transition, not deletion.

## Rules

- All proactive ingress is explicit, typed, and owner-scoped.
- All proactive checks are durable workflows.
- All external reads go through capability contracts.
- All side effects go through existing policy and approval.
- All attention decisions are persisted before delivery.
- All check runs are idempotent by subscription id and scheduled window.
- All notifications are idempotent by source and attention item revision.
- All button interactions are single-use state transitions with locking.
- All proactive state is inspectable through typed API responses.
- All event payloads are bounded and redacted.
- No background work holds a database transaction across external calls.
- No read handler performs writes except explicit reconciliation endpoints already designated as
  mutation surfaces.
- No model output directly mutates subscription, attention, notification, or approval state
  without validation.

## Key Decisions

### Bounded Proactivity

Proactivity is subscription and job bounded. Ariel can continuously check configured watches, but
it cannot invent arbitrary goals or browse unboundedly.

### Attention Before Notification

Every proactive notification is backed by an attention item. Notifications are delivery records,
not the source of truth.

### Read-Only Checks

Checks in this cutover read existing persisted Ariel state and explicit typed watch payloads.
Calendar, email, and Drive watch subscriptions exist as durable source types, but they do not
perform provider network reads in the worker. Future provider-backed proactive reads must go
through existing capability validation and egress policy, and must not hold a database transaction
across external calls.

### Discord-Only Surface

All proactive check-ins happen in Discord. API endpoints exist for typed state inspection and
button backing only.

### No Legacy Runtime

The cutover removes any one-off scheduled-ish code that does not use subscriptions, attention
items, and durable worker tasks.

## File Plan

### Migrations

- Add proactive subscription, check run, attention item, and attention item event tables.
- Expand notification source type constraints.
- Expand background task type constraints.
- Add indexes for due subscription checks, due follow-ups, open attention items, dedupe keys, and
  notification lookup.

### Source

- `src/ariel/persistence.py`
  - Add proactive ORM records and serializers.
  - Update notification and task constraints.
- `src/ariel/worker.py`
  - Add proactive task dispatch.
  - Keep durable claiming, retry, heartbeat, and reaper behavior.
- `src/ariel/proactivity.py`
  - Add subscription check execution.
  - Add attention item upsert and prioritization.
  - Add follow-up scheduling.
- `src/ariel/app.py`
  - Add typed endpoints for subscriptions and attention items.
  - Add state transitions for acknowledge, snooze, resolve, cancel, and refresh.
  - Keep existing turn orchestration path as the only conversational path.
- `src/ariel/discord_bot.py`
  - Add proactive notification buttons and formatting.
  - Reuse job refresh and notification acknowledgement patterns.
- `src/ariel/response_contracts.py`
  - Add surfaced subscription, attention item, and proactive notification contracts.
- `src/ariel/config.py`
  - No new settings are required in this cutover.
  - Notification policy is durable subscription data, not process configuration.
- `src/ariel/capability_registry.py`
  - No registry changes are required in this cutover.
  - Do not add side-effecting proactive capabilities.

### Tests

- Unit tests for prioritization, dedupe, state transitions, and response contracts.
- Integration tests for due check claiming, check run persistence, attention item creation,
  notification delivery, Discord button transitions, restart recovery, and approval gating.
- Regression tests that prove proactive checks cannot execute writes or external sends inline.

### Docs

- Update production runbook with proactive service settings and health checks.
- Update README with the proactive assistant contract.
- Keep this document as the source of truth for cutover behavior.

## Acceptance Criteria

- A due subscription is claimed once by one worker even with multiple workers running.
- A successful check writes a check run and either creates, updates, or intentionally suppresses
  an attention item.
- Duplicate checks for the same subscription window do not duplicate attention items or
  notifications.
- A high-priority attention item creates a Discord notification with valid buttons.
- Acknowledging a notification updates both notification and attention item state when linked.
- Snoozing an attention item schedules a future follow-up through durable state.
- Resolving an attention item prevents future follow-up.
- Expired attention items are not notified or followed up.
- Worker restart does not lose due checks, running checks, notifications, or follow-ups.
- Dead-lettered proactive tasks remain inspectable and do not block unrelated tasks.
- Pending approval expiry still reconciles through the existing approval lifecycle.
- Provider-backed proactive reads are outside this cutover; when added, they use capability
  validation and egress policy.
- Proactive writes and external sends remain outside this cutover and require approval and exact
  payload execution when added.
- Discord is the only proactive delivery channel.
- API responses expose redacted surfaced state only.
- `make verify` passes.
- Integration coverage proves there is no legacy scheduled-ish path outside the durable worker.

## Cutover Steps

1. Add schema and contracts.
2. Add proactive runtime and worker tasks.
3. Add typed API state transitions.
4. Add Discord buttons and rendering.
5. Add tests for durable checks, dedupe, follow-up, and approval gating.
6. Remove one-off scheduled-ish paths that do not use attention items.
7. Update README, runbook, and environment examples.
8. Run full verification.

The final merged state must contain one proactive architecture only.
