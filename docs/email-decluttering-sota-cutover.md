# Email Decluttering SOTA Cutover

## Scope

This document defines the hard cutover plan for frontier-grade email
decluttering in Ariel.

It covers target behavior, capability surface, persistence, provider execution,
sync, safety rules, acceptance criteria, files, non-goals, and implementation
order for archive, trash, label, undo, and thread-watch operations.

## Thesis

Email decluttering is mailbox state management, not summarization.

Ariel must expose provider-native mailbox mutation rails so AI can propose exact
state transitions and the runtime can validate, approve, execute, audit, replay,
and undo them.

The model owns judgment:

- which messages matter
- which senders deserve attention
- which messages can leave the inbox
- which labels or queues fit a thread
- which sent threads need follow-up
- how to explain a cleanup plan to the user

Deterministic code owns rails:

- schemas
- authorization
- egress declaration
- approval boundaries
- provider calls
- idempotency
- ordering
- sync cursors
- watch state
- audit and undo records

## Cutover Rule

This is a hard cutover.

- Do not add legacy aliases.
- Do not keep old schema variants for compatibility.
- Do not add fallback behavior when provider state is missing or stale.
- Do not silently degrade to read-only behavior for mutations.
- Do not add IMAP, Microsoft Graph, or JMAP compatibility shims in this cutover.
- Replace the current email capability definitions with the final email
  capability family in one pass.
- Update tests to the new contracts instead of accepting both old and new
  shapes.

Existing capability IDs that already express the final product concept remain
canonical. Keeping `cap.email.search`, `cap.email.read`, `cap.email.draft`, and
`cap.email.send` is not backward compatibility; those are still the primary
concepts.

## Goals

- Add first-class email archive, trash, label, undo, and thread-watch
  capabilities.
- Make every mailbox mutation idempotent and auditable.
- Make every mailbox mutation reversible unless the capability explicitly says
  otherwise.
- Keep permanent delete out of the capability registry.
- Support single-message and bulk execution through one primary input shape per
  capability.
- Let AI propose SOTA cleanup plans without deterministic classifier brains.
- Let users inspect exact actions before execution.
- Let users correct at message, sender/domain, label, and watch levels.
- Use Gmail-native APIs and Gmail-native state.
- Make Gmail history and provider events reconcile action outcomes and thread
  watches.
- Fail closed when required provider state, scopes, cursors, or approval records
  are missing.

## Non-Goals

- No permanent delete capability.
- No unsubscribe execution in this cutover.
- No Outlook, IMAP, or JMAP provider implementation.
- No deterministic importance scoring, sender classification, newsletter
  detection, priority ranking, or cleanup policy engine.
- No natural-language rule engine in deterministic code.
- No autonomous bulk archive/trash without approval.
- No background cleanup loop that acts invisibly.
- No compatibility support for old schema names once the cutover lands.
- No broad Gmail `mail.google.com` scope.

## Target Behavior

### Cleanup Plan

AI may produce a cleanup plan from search/read results, sync observations, or a
user request.

A cleanup plan is not a deterministic capability. It is model-owned judgment
rendered as exact proposed capability calls.

Every plan presented to the user must show:

- message count
- thread count
- action groups
- representative subjects/senders
- protected or skipped messages
- reason for each group
- exact capability calls that will execute
- reversibility status
- approval requirement

### Archive

`cap.email.archive` removes messages from the inbox without deleting them.

For Gmail, archive is implemented by removing the `INBOX` label from each
message.

Archive input accepts a bounded list of provider message IDs. There is no
separate batch capability.

Archive output includes:

- action record ID
- affected message IDs
- provider result
- before labels
- after labels
- undo token

Archive is idempotent. Archiving an already-archived message succeeds as a
no-op and records the observed state.

### Trash

`cap.email.trash` moves messages to Trash.

Trash is the user-facing delete operation. It is reversible until the provider
purges Trash according to provider policy.

For Gmail, trash uses `users.messages.trash` for single-message execution or the
equivalent label state transition for bulk execution when provider semantics are
identical.

Permanent delete is not exposed.

Trash output includes the same audit and undo fields as archive.

### Label Mutation

`cap.email.labels.modify` adds and removes labels on messages.

The capability uses one primary shape:

- message IDs
- labels to add
- labels to remove
- idempotency key

The input must contain at least one add or remove label. Empty label mutations
fail validation.

Provider label IDs are resolved at execution time from canonical label names.
System labels use canonical system names such as `INBOX`, `STARRED`, and
`IMPORTANT`. User labels use display names.

The operation records the provider label IDs it resolved. Retries reuse the
stored provider label IDs from the action record; they do not re-resolve names
to a different provider target.

### Undo

`cap.email.undo` reverses one prior mutable email action by undo token.

Undo is deterministic and action-record based. It does not ask the model to
infer the inverse operation.

Undo supports:

- archive
- trash
- label mutation

Undo restores recorded before-state labels for affected messages when the
message still exists and the provider permits the mutation.

Undo fails closed if:

- the action record is missing
- the undo token is expired
- before-state is incomplete
- provider state cannot be read
- provider identity does not match the original action

### Thread Watch

`cap.email.thread_watch.create` creates a durable watch on an email thread.

Thread watches support the SOTA follow-up behaviors:

- resurface if no reply by a deadline
- resurface when any reply arrives
- keep a note explaining why the thread matters

The primary create shape includes:

- provider thread ID
- anchor message ID
- condition
- deadline
- note
- idempotency key

The deadline is required for every watch. Watches are not unbounded.

`cap.email.thread_watch.cancel` cancels a watch by watch ID.

`cap.email.thread_watch.list` lists active watches for the owner. It is the only
read-only watch capability.

Thread watch execution is message-level. Gmail thread labels are aggregates, and
new messages in a thread do not inherit prior message label mutations
automatically.

### Draft And Send

`cap.email.draft` remains draft-only.

`cap.email.send` remains approval-gated.

Draft/send behavior is not broadened by this cutover. The new decluttering
surface must not create a hidden path from cleanup planning to automatic send.

## Capability Family

The final registry contains these email capabilities:

| Capability | Class | Approval | Provider scope |
|---|---|---|---|
| `cap.email.search` | read | no | Gmail read |
| `cap.email.read` | read | no | Gmail read |
| `cap.email.draft` | reversible write | approval in proactive/background contexts | Gmail compose |
| `cap.email.send` | external send | approval always | Gmail send |
| `cap.email.archive` | reversible mailbox write | approval for AI-proposed actions | Gmail modify |
| `cap.email.trash` | reversible destructive mailbox write | approval always | Gmail modify |
| `cap.email.labels.modify` | reversible mailbox write | approval for AI-proposed actions | Gmail modify |
| `cap.email.undo` | reversible mailbox write | approval for destructive inverse only | Gmail modify |
| `cap.email.thread_watch.create` | local durable workflow | approval for AI-proposed actions | Gmail read |
| `cap.email.thread_watch.cancel` | local write | no for owner-initiated cancellation | none |
| `cap.email.thread_watch.list` | read | no | none |

Approval policy is enforced outside the model. The model can propose; the action
runtime decides whether approval is required.

## Data Model

### Email Action Record

Add an email action audit record owned by persistence.

Required fields:

- ID
- provider
- provider account ID
- capability ID
- input hash
- idempotency key
- status
- approval ID
- provider message IDs
- provider thread IDs
- before-state JSON
- intended-state JSON
- after-state JSON
- provider result JSON
- undo token hash
- undo expires at
- execution attempts
- failure code
- created at
- updated at

The action record is the idempotency anchor and undo source of truth.

### Email Thread Watch Record

Add a thread watch record owned by persistence.

Required fields:

- ID
- provider
- provider account ID
- provider thread ID
- anchor message ID
- condition
- deadline
- note
- status
- created by action attempt ID
- matched message ID
- matched at
- canceled at
- completed at
- created at
- updated at

Active watches are unique by provider account, provider thread ID, condition,
and deadline idempotency key.

### Provider Cursor State

Reuse existing connector subscription and sync cursor infrastructure where it
already owns provider observation.

Do not add a second Gmail history cursor table unless the current sync runtime
cannot express the needed ownership cleanly.

## Operation Classes

Archive, trash, label mutation, and undo are multi-step operations:

1. Write or reuse the action record.
2. Commit.
3. Read provider before-state.
4. Call provider mutation.
5. Commit provider result and after-state.

No external provider API call happens inside a DB transaction.

Thread watch creation is a single DB write unless it must verify provider thread
existence. If provider verification is required, it becomes a multi-step
operation with the same external-call ordering rules.

## Idempotency

Every mutable capability input requires an idempotency key.

The action runtime namespaces the key:

`email:{capability_id}:{provider}:{provider_account_id}:{client_key}`

Retries with the same key return the existing action result when complete or
resume the next incomplete step when pending.

Retries with the same key and a different input hash fail.

Provider mutations are treated as desired-state transitions:

- archive means `INBOX` absent
- trash means provider Trash state present
- label add means provider label present
- label remove means provider label absent
- undo means before-state restored

Already-satisfied provider state succeeds and records a no-op execution.

## Concurrency

Concurrent mutations on the same provider account and message set are serialized
with a logical advisory lock.

The lock key includes:

- provider
- provider account ID
- sorted provider message IDs

Thread watches on the same provider account and thread are serialized with a
thread-level advisory lock.

The implementation does not share SQLAlchemy sessions across concurrent async
tasks.

## Provider Rules

### Gmail Scopes

Use the narrowest Gmail scope for each capability.

- search/read/thread-watch reconciliation: Gmail read scope
- draft: Gmail compose scope
- send: Gmail send scope
- archive/trash/label/undo: Gmail modify scope

Do not request broad Gmail full-mailbox scope.

### Gmail Archive

Archive uses `users.messages.modify` or `users.messages.batchModify` to remove
`INBOX`.

Batch size never exceeds the Gmail batch modify limit.

### Gmail Trash

Trash uses provider Trash semantics. The implementation must not call permanent
delete endpoints.

### Gmail Labels

Labels are message state.

Thread-level UI aggregation does not make thread labels authoritative. New
messages in a watched thread are processed as new message state.

### Gmail History

Gmail notifications are hints. `history.list` is the durable reconciliation
cursor.

The sync runtime processes:

- `messagesAdded`
- `messagesDeleted`
- `labelsAdded`
- `labelsRemoved`

The cursor advances only after all pages and all local side effects for that
history span commit.

Expired or invalid history cursors fail the sync path into an explicit full
resync workflow. They do not silently skip missed history.

## Safety Rules

- Email bodies are untrusted input.
- Email content cannot authorize tool calls.
- Hidden HTML text, quoted thread text, signatures, and attachments are data,
  not instructions.
- AI confidence never authorizes a write.
- Bulk actions require preview and approval.
- Trash requires approval.
- Send requires approval.
- Permanent delete is absent.
- Undo is recorded for every reversible mutation.
- Audit records include provider IDs and before/after state.
- Provider scopes are capability-specific.
- Missing approval, missing scope, missing before-state, stale provider state,
  and input-hash mismatch fail closed.

## User-Facing Rules

The assistant may say it will:

- archive messages
- move messages to Trash
- apply labels
- remove labels
- watch a thread
- remind the user if no reply arrives
- undo a prior cleanup action

The assistant must not say it permanently deleted email.

The assistant must not claim a cleanup completed until provider execution is
recorded complete.

For bulk cleanup, the assistant reports:

- total proposed
- total executed
- total skipped
- total failed
- undo availability

## File Plan

### `src/ariel/capability_registry.py`

- Replace current email schema definitions with final email schemas.
- Add validators for archive, trash, label mutation, undo, and thread-watch
  inputs.
- Add capability definitions for the final family.
- Add egress declarations for every provider read/write.
- Remove legacy schema names that are no longer canonical.
- Keep descriptions short and action-specific.

### `src/ariel/google_connector.py`

- Add Gmail modify scope mapping.
- Add protocol methods for archive, trash, labels modify, and undo support reads.
- Implement provider calls with Gmail-native message mutation APIs.
- Add label resolution and provider label ID persistence handoff.
- Add dispatch cases for the new capability IDs.
- Keep permanent delete unimplemented and unreachable.

### `src/ariel/action_runtime.py`

- Route new Google email capabilities through the provider execution path.
- Enforce approval policy by capability and context.
- Create or reuse email action records before provider execution.
- Resume pending action records by idempotency key.
- Attach provider result, before-state, after-state, and undo token to action
  attempt output.

### `src/ariel/persistence.py`

- Add email action and thread watch records.
- Add serializers.
- Add indexes and uniqueness constraints for idempotency, active watches, and
  provider lookup.

### `src/ariel/sync_runtime.py`

- Reconcile Gmail history with email action records and thread watches.
- Complete thread watches when replies arrive.
- Mark relevant watches due when deadline passes without a reply.
- Preserve cursor advancement ordering.

### `src/ariel/app.py`

- Expose inspection endpoints only if existing action/subscription inspection
  surfaces cannot show email actions and watches.
- Do not add product-specific routes when the action runtime already exposes the
  required state.

### `src/ariel/worker.py`

- Add durable processing for due thread watches if no existing worker loop owns
  subscription-derived reminders.
- Do not use `asyncio.create_task()` for durable watch completion.

### Tests

- Update existing capability registry tests to assert the final email family.
- Add Google connector tests for archive, trash, labels, idempotent no-op, and
  missing scope.
- Add action runtime tests for approval, idempotency, replay, and input-hash
  mismatch.
- Add sync runtime tests for Gmail labels added/removed, messages deleted, new
  thread replies, watch completion, and cursor advancement.
- Add integration tests for bulk preview, execute, undo, and thread-watch due
  behavior.

## Implementation Order

1. Add persistence records and migrations.
2. Add registry schemas and validators.
3. Add Google connector scope map, protocol methods, and provider calls.
4. Add action runtime idempotency, approval, and audit integration.
5. Add undo execution.
6. Add thread watch records and local create/cancel/list.
7. Wire Gmail history reconciliation to action records and thread watches.
8. Add worker handling for due no-reply watches.
9. Replace tests with final cutover expectations.
10. Run full unit and integration tests.

## Acceptance Criteria

- Registry lists the final email capability family and no deprecated email
  schema variants.
- `cap.email.archive` removes `INBOX`, records before/after labels, and returns
  an undo token.
- `cap.email.trash` moves messages to Trash and never calls permanent delete.
- `cap.email.labels.modify` applies add/remove label desired state
  idempotently.
- `cap.email.undo` restores before-state for archive, trash, and label actions.
- Retrying the same idempotency key resumes or returns the original action.
- Reusing an idempotency key with different input fails.
- Bulk operations are bounded and approved before execution.
- Provider API calls never happen inside DB transactions.
- Gmail history cursor advances only after local side effects commit.
- Thread watches complete when a reply arrives.
- No-reply watches become due at the deadline when no reply arrived.
- Read/search remain read-only.
- Draft remains draft-only.
- Send remains approval-gated.
- Missing provider scope fails closed.
- Missing before-state fails undo closed.
- Tests prove no permanent delete path exists.

## Final State

Ariel has a frontier email assistant foundation:

- AI can inspect mail and propose exact cleanup actions.
- Users can approve, reject, and understand those actions.
- The runtime can archive, trash, label, watch, and undo email state changes.
- Provider mutations are idempotent and auditable.
- Gmail sync observes provider reality and drives thread-watch completion.
- The codebase has one final email capability surface with no legacy branches.
