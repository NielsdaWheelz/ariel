# S2 PR-07 Implementation Notes

## Scope Landed

this implementation closes the remaining Slice 2 approval-expiry lifecycle gap by reconciling expired pending approvals during timeline reads:

- adds shared terminal expiry transition logic in `src/ariel/action_runtime.py` via `_mark_approval_expired(...)`.
- adds session-scoped reconciliation entrypoint `reconcile_expired_approvals_for_session(...)`.
- updates `GET /v1/sessions/{session_id}/events` to run reconciliation before timeline serialization.
- keeps approval decision behavior stable for already-resolved approvals (`E_APPROVAL_NOT_PENDING`).

## Runtime Behavior

### Reconciliation Trigger

`GET /v1/sessions/{session_id}/events` now runs in a transaction and invokes:

`reconcile_expired_approvals_for_session(db, session_id, now_fn, new_id_fn)`

before collecting turns/events/action lifecycle payloads.

### Reconciliation Criteria

an approval is reconciled when all are true:

- `approval.status == "pending"`
- `approval.expires_at < now`
- approval belongs to the requested session

for each reconciled approval, runtime updates:

- `approval_requests.status` -> `expired`
- `approval_requests.decision_reason` -> `approval_expired`
- `approval_requests.decided_at`/`updated_at` -> reconciliation time
- `action_attempts.status` -> `expired`
- `action_attempts.policy_reason` -> `approval_expired`

and appends exactly one:

- `evt.action.approval.expired` with payload `{action_attempt_id, approval_ref, reason}`

### Invariant Hardening

expiry transition logic now fails fast on inconsistent state:

- non-pending approval passed to expiry transition
- approval/action-attempt id mismatch
- approval/action-attempt session or turn mismatch

this guards against accidental duplicate expiry outcomes or cross-entity mutation bugs.

## Exactly-Once and Idempotency Semantics

- reconciliation path acquires row-level locks on pending expired approvals (`FOR UPDATE`) and linked action attempts before mutation.
- decision path (`POST /v1/approvals`) also uses row-level locks; concurrent decision/read races converge on one terminal expiry outcome.
- once reconciliation sets status `expired`, subsequent approval decisions remain non-executing/idempotent through existing resolved-approval conflict semantics (`E_APPROVAL_NOT_PENDING`).
- repeated timeline reads do not emit duplicate `evt.action.approval.expired` events.

## User-Facing Surface Outcome

after reconciliation, timeline/action lifecycle views surface terminal expiry directly:

- `approval.status == "expired"`
- `approval.reason == "approval_expired"`
- `execution.status == "not_executed"`

this no longer requires a failed `POST /v1/approvals` call to materialize expiry in user-visible lifecycle views.

## Tradeoff / Non-Goal Confirmation

reconciliation is intentionally **read-triggered** (timeline endpoint) rather than scheduler-driven. this matches PR-07 non-goals:

- no scheduler architecture redesign
- no autonomous background side-effect loops

operationally, that means stale pending rows converge on next relevant read/decision, not by independent wall-clock background processing.

## Test Coverage Added

`tests/integration/test_s2_pr07_acceptance.py` adds acceptance coverage for:

- reconciliation to terminal `expired` on timeline read without approval decision call.
- exactly-once `evt.action.approval.expired` emission with linked action-attempt and approval reference.
- idempotent/non-executing behavior for repeated timeline reads and post-reconcile approval decision attempts.
- surfaced lifecycle redaction/contract safety for reconciled terminal state.
