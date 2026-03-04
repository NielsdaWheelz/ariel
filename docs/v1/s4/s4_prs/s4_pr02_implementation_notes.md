# s4 pr-02 implementation notes

## scope delivered

- added google write capabilities and contracts:
  - `cap.calendar.create_event` (`write_reversible`, approval-gated)
  - `cap.email.draft` (`write_reversible`, allowlisted inline)
  - `cap.email.send` (`external_send`, approval-gated)
- implemented strict input validation for calendar create and email draft/send payloads.
- extended google connector runtime to execute write capabilities with the same deterministic typed
  auth failure model used by read capabilities (`not_connected`, `consent_required`,
  `scope_missing`, `token_expired`, `access_revoked`).
- added capability-intent reconnect support:
  - `POST /v1/connectors/google/reconnect?capability_intent=<capability_id>`
  - reconnect preserves previously granted scopes and adds only intent-required scopes.
- wired approval-time google execution through connector runtime, preserving exact-once semantics
  (single-use approvals + transactional locking).

## key hardening decisions

### canonical draft boundary

- draft execution returns canonical local draft state:
  - `status: drafted_not_sent`
  - `delivery_state: draft_only`
  - `sent: false`
  - canonical `draft` payload (`to`, `cc`, `bcc`, `subject`, `body`)
- provider draft state is treated as optional projection (`provider_draft_ref`) and never used as
  implicit send authorization.

### reconnect least-privilege contract

- reconnect computes requested scopes from:
  1) already granted connector scopes
  2) optional capability intent requirements
- unsupported capability intents fail closed with `E_CONNECTOR_RECONNECT_INVALID_INTENT` (400).

### provenance refinement for practical send workflows

- runtime taint provenance now marks prior successful inline `read` outputs as taint evidence.
- successful inline write-reversible drafts are not treated as runtime taint evidence by default.
- this preserves s2 taint protections for untrusted read data while allowing explicit approval-gated
  send flows after local drafting across turns.

## files changed

- `src/ariel/capability_registry.py`
- `src/ariel/google_connector.py`
- `src/ariel/action_runtime.py`
- `src/ariel/app.py`
- `tests/integration/test_s4_pr02_acceptance.py`
- `README.md`

## verification

- targeted acceptance + taint regressions:
  - `.venv/bin/python -m pytest tests/integration/test_s4_pr02_acceptance.py tests/integration/test_s2_pr03_acceptance.py -q`
- full repo gates:
  - `make verify`
- e2e smoke:
  - `make e2e`
