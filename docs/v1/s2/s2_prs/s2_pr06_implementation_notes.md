# S2 PR-06 Implementation Notes

## Scope Landed

this implementation locks Slice 2 user-facing response boundaries so surfaced contracts are explicit, allowlisted, and regression-tested:

- `POST /v1/sessions/{session_id}/message` success responses are emitted through a strict surfaced contract.
- `GET /v1/sessions/{session_id}/events` timeline responses are emitted through the same strict surfaced turn contract.
- `POST /v1/approvals` success responses are emitted through a strict surfaced approval contract.
- turn `events[].payload` is now schema-enforced per `event_type` (no open payload dict contract).
- response contract enforcement is centralized in `src/ariel/response_contracts.py`.

## API Contract Changes

### `POST /v1/sessions/{session_id}/message`

success response remains:

```json
{
  "ok": true,
  "session": { "...": "..." },
  "turn": { "...": "..." },
  "assistant": {
    "message": "..."
  }
}
```

legacy surfaced fields removed:

- `assistant.provider`
- `assistant.model`

these fields are intentionally not reintroduced for compatibility. clients that depended on them must migrate to the surfaced contract above.

### `GET /v1/sessions/{session_id}/events`

response remains:

```json
{
  "ok": true,
  "session_id": "ses_xxx",
  "turns": [/* surfaced turn contract */]
}
```

each turn is contract-projected to explicit allowlisted keys:

- turn envelope keys: `id`, `session_id`, `user_message`, `assistant_message`, `status`, `created_at`, `updated_at`, `events`, `surface_action_lifecycle`
- lifecycle item keys: `action_attempt_id`, `proposal_index`, `proposal`, `policy`, `approval`, `execution`

internal lifecycle keys such as `capability_contract_hash`, `payload_hash`, `impact_level`, and `approval_required` remain excluded from surfaced lifecycle payloads.

### `POST /v1/approvals`

response remains surfaced-only:

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
    "message": "..."
  }
}
```

legacy compatibility stance remains explicit:

- request payloads using `approval_id` are invalid (must use `approval_ref`)
- response payloads do not include `action_attempt`

## Boundary Enforcement Strategy

- response shaping now uses explicit, allowlisted projection functions in `response_contracts.py`.
- nested contract models are `extra="forbid"` and validated with pydantic.
- turn/lifecycle/event envelopes and per-event payloads are strict; unknown keys are rejected.
- unknown event types are rejected.
- malformed surfaced payload shape raises a contract violation (`E_RESPONSE_CONTRACT`) rather than leaking internals.
- contract errors return sanitized diagnostics (`loc`, `type`) without echoing offending payload values.

## Test Coverage

`tests/integration/test_s2_pr06_acceptance.py` adds contract-focused regression coverage for:

- inline read surfaced response contracts (message + timeline)
- denied approval flow contracts
- expired approval flow contracts
- approved execution success/failure contracts
- serializer drift/leak regression is fail-closed (`E_RESPONSE_CONTRACT`) when injected internal fields appear

existing PR-05 acceptance coverage remains intact for lifecycle semantics and approval flow continuity.
