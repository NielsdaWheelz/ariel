# S2 PR-01 Implementation Notes

## Scope Landed

This implementation delivers the PR-01 vertical slice in running code:

- inline allowlisted read execution with redacted user-visible output
- approval-gated action persistence and execution via `POST /v1/approvals`
- strict proposal pre-execution gating (unknown capability, schema invalid, policy deny)
- auditable action lifecycle events wired into turn timelines
- deterministic bound: any number of inline reads, max one pending approval per turn

## Probe Capability Set (PR-01 MVP)

- `cap.framework.read_echo` (`read`, inline allow)
- `cap.framework.read_private` (`read`, policy deny)
- `cap.framework.write_note` (`write_reversible`, requires approval)

These are framework-probe capabilities only; domain capabilities are intentionally deferred.

## API Contract Notes

### `POST /v1/sessions/{session_id}/message`

- still returns the existing turn payload
- now includes `turn.action_attempts[]` with:
  - proposal identity (`capability_id`, frozen `proposal_input`, `proposal_index`)
  - policy outcome (`policy_decision`, `policy_reason`)
  - approval snapshot (if approval-gated)
  - execution snapshot (`status`, `output`, `error`)

### `POST /v1/approvals`

Request body:

```json
{
  "approval_id": "apr_xxx",
  "decision": "approve | deny",
  "actor_id": "user.local",
  "reason": "optional"
}
```

Enforced invariants:

- pending-only, single-use approval records
- actor binding (`actor_id` must match stored approval actor)
- expiry binding (`expires_at`)
- payload identity binding (hash over canonical frozen payload)

## Event Lifecycle

Action events emitted into turn timelines:

- `evt.action.proposed`
- `evt.action.policy_decided`
- `evt.action.approval.requested`
- `evt.action.approval.approved`
- `evt.action.approval.denied`
- `evt.action.approval.expired`
- `evt.action.execution.started`
- `evt.action.execution.succeeded`
- `evt.action.execution.failed`

## Module Layout (Post-Refactor)

- `src/ariel/app.py` — HTTP routes + orchestration shell
- `src/ariel/persistence.py` — ORM models + serialization
- `src/ariel/action_runtime.py` — proposal/approval workflow coordinator
- `src/ariel/capability_registry.py` — capability contracts + payload hashing
- `src/ariel/policy_engine.py` — deterministic policy classification
- `src/ariel/executor.py` — execution + redaction + event append helpers
- `src/ariel/phone_surface.py` — phone surface HTML/JS

This split keeps policy/execution logic out of route handlers and makes PR-02 hardening safer.
