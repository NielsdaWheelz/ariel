# S2 PR-05 Implementation Notes

## Scope Landed

this implementation closes the Slice 2 lifecycle/approval surface boundary gap:

- user-facing turn/timeline payloads expose surfaced lifecycle data only (`surface_action_lifecycle`).
- raw turn `action_attempts` are removed from serialized user-facing turn payloads.
- surfaced lifecycle approval data now includes a stable handle: `approval.reference`.
- approval submissions are keyed by surfaced handle only (`approval_ref`).
- approval endpoint responses are surfaced-only and do not include internal `action_attempt` payloads.

## API Contract Changes

### Turn/timeline payloads

user-facing turn payloads include:

- `surface_action_lifecycle[]` (allowlisted + redacted)
  - `proposal`: `capability_id`, `input_summary`
  - `policy`: `decision`, `reason`
  - `approval`: `status`, `reference`, `reason`, `expires_at`, `decided_at`
  - `execution`: `status`, `output`, `error`

and no longer include `turn.action_attempts`.

### `POST /v1/approvals`

request body:

```json
{
  "approval_ref": "apr_xxx",
  "decision": "approve | deny",
  "actor_id": "optional; defaults to configured actor",
  "reason": "optional"
}
```

response body:

```json
{
  "ok": true,
  "approval": {
    "reference": "apr_xxx",
    "status": "approved | denied | expired",
    "reason": "redacted-or-null",
    "expires_at": "RFC3339",
    "decided_at": "RFC3339-or-null"
  },
  "assistant": {
    "message": "user-facing outcome"
  }
}
```

strict boundary behavior:

- legacy `approval_id` request payloads are rejected with validation errors.
- approval responses do not expose raw `action_attempt`.

## Implementation Notes

- `src/ariel/persistence.py`
  - `serialize_turn(...)` now emits surfaced lifecycle only for action details.
  - surfaced lifecycle `approval` block now includes `reference`.
- `src/ariel/app.py`
  - `ApprovalDecisionRequest` requires `approval_ref`.
  - approval response now returns surfaced approval contract only.
  - approval reason text in response is redacted.
- `src/ariel/action_runtime.py`
  - approval runtime/event naming normalized to `approval_ref`.
  - reduced error-detail exposure of internal action-attempt ids in approval error paths.
- `src/ariel/phone_surface.py`
  - timeline approval controls submit approve/deny via surfaced `approval_ref`.
  - escaping hardened for attribute contexts.

## Test Coverage

`tests/integration/test_s2_pr05_acceptance.py` verifies:

- surfaced-only turn/timeline lifecycle contract (no raw `action_attempts`).
- stable surfaced approval reference for approval-required actions.
- deny/expire/approve(success)/approve(failure) lifecycle outcomes via surfaced-only flows.
- approval response shape is surfaced-only.
- legacy `approval_id` request rejection.

PR-01 through PR-04 acceptance suites were updated to consume surfaced lifecycle contracts only.
