# S2 PR-04 Implementation Notes

## Scope Landed

this implementation delivers PR-04 lifecycle inspectability in running code:

- turn/timeline APIs now include a dedicated surfaced lifecycle projection (`surface_action_lifecycle`) per turn.
- the projection is explicitly allowlisted and redacted server-side.
- the phone-first surface now renders lifecycle details from the surfaced projection directly.
- acceptance coverage now asserts inspectability for required PR-04 paths.

## API Surface Changes

### Turn payloads (`POST /v1/sessions/{session_id}/message` and `GET /v1/sessions/{session_id}/events`)

each serialized turn now includes:

```json
"surface_action_lifecycle": [
  {
    "action_attempt_id": "aat_xxx",
    "proposal_index": 1,
    "proposal": {
      "capability_id": "cap.framework.read_echo",
      "input_summary": {"text": "[REDACTED]"}
    },
    "policy": {
      "decision": "allow_inline",
      "reason": "allowlisted_read"
    },
    "approval": {
      "status": "not_requested | pending | approved | denied | expired",
      "reason": null,
      "expires_at": null,
      "decided_at": null
    },
    "execution": {
      "status": "succeeded | failed | not_executed | in_progress",
      "output": {"text": "[REDACTED]"},
      "error": null
    }
  }
]
```

### Allowlist boundary

the surfaced projection intentionally excludes internal-only execution metadata such as:

- capability contract hashes
- payload hashes
- impact-level internals
- internal egress sentinel metadata

historical note: PR-04 shipped with raw `action_attempts` still present for compatibility in that iteration.
PR-05 removes raw turn `action_attempts` from user-facing turn/timeline payloads and enforces surfaced-only approval flow handles.

## Implementation Details

### Server-side projection

- `src/ariel/persistence.py`
  - added `_serialize_surface_action_lifecycle(...)`
  - added `_policy_reasons_by_action_attempt(...)` so surfaced policy reasons reflect proposal-time policy decisions from events, not later approval transition statuses
  - added `_redacted_optional_text(...)` to enforce redaction on surfaced reason/error text

### Redaction consolidation

- added `src/ariel/redaction.py` for shared secret-like redaction primitives:
  - `safe_failure_reason(...)`
  - `redact_text(...)`
  - `redact_json_value(...)`
- `src/ariel/executor.py` and `src/ariel/app.py` now import these shared functions, removing duplicate pattern logic.

### Phone surface rendering

- `src/ariel/phone_surface.py`
  - replaced action detail rendering source from `turn.action_attempts` to `turn.surface_action_lifecycle`
  - renders proposal/policy/approval/execution details directly from surfaced API contract

## Test Coverage

`tests/integration/test_s2_pr04_acceptance.py` covers:

- inline read success (inspectable + redacted)
- approval denied (terminal non-executed + reason visible/redacted)
- approval expired (terminal non-executed + reason visible)
- approval-approved execution success
- approval-approved execution failure

additional hardening assertions validate:

- strict allowlisted field shape for surfaced lifecycle items
- no secret-like leakage in surfaced proposal/output/reason/error fields
- policy reasons remain coherent with policy decision events

## Tradeoffs / Follow-ups

- compatibility tradeoff: raw lifecycle internals are still present in responses. this keeps existing clients stable but means strict API minimization is not yet complete.
- follow-up option: add a dedicated timeline endpoint that returns surfaced-only lifecycle data, migrate clients, and deprecate raw lifecycle internals from user-facing responses.
