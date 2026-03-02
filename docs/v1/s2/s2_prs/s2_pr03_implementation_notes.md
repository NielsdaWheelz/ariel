# S2 PR-03 Implementation Notes

## Scope Landed

this implementation closes the PR-03 trust gap in runtime code:

- side-effect taint is derived from runtime provenance, not model-declared taint booleans.
- model-declared taint is advisory only and cannot de-escalate runtime-derived taint.
- malformed taint metadata is treated as provenance-ambiguous and fails closed for side effects.
- action lifecycle events now include taint provenance evidence and model-declared taint status.

## Runtime Behavior

### Runtime Provenance Source

runtime provenance is derived from persisted, in-context prior tool outputs:

- source records: prior `action_attempts` in the current context window (`max_recent_turns`) where:
  - `policy_decision == allow_inline`
  - `status == succeeded`
- each provenance evidence entry includes:
  - `kind=prior_tool_output_in_context`
  - `turn_id`
  - `action_attempt_id`
  - `capability_id`
  - `impact_level`

if one or more such records exist in the current context window, runtime provenance is `tainted`; otherwise it is `clean`.

### Model Taint Is Advisory

proposal metadata `influenced_by_untrusted_content` is normalized to:

- `missing` (field absent)
- `true`
- `false`
- `malformed` (present but not a boolean)

effective proposal provenance status is resolved as:

- runtime `tainted` -> `tainted` (cannot be cleared by model metadata)
- runtime `clean` + model `true` -> `tainted`
- runtime `clean` + model `malformed` -> `ambiguous`
- runtime `clean` + model `false|missing` -> `clean`

### Fail-Closed Side-Effect Authorization

for side-effecting proposals when provenance is `tainted` or `ambiguous`:

- `write_reversible` -> `requires_approval` (`taint_escalated_requires_approval`)
- `write_irreversible` / `external_send` -> `deny` (`taint_denied_untrusted_side_effect`)

non-side-effect `read` behavior remains under existing allowlist/policy controls.

### Event/Audit Payload Enrichment

`evt.action.proposed` and `evt.action.policy_decided` now include:

- `taint.influenced_by_untrusted_content` (effective boolean used for policy)
- `taint.provenance_status` (`clean|tainted|ambiguous`)
- `taint.runtime_provenance` (`status` + `evidence[]`)
- `taint.model_declared_taint.status` (`missing|true|false|malformed`)

this is sufficient to reconstruct why taint escalation or deny was applied.

## Test Coverage

PR-03 acceptance coverage lives in `tests/integration/test_s2_pr03_acceptance.py` and validates:

- side-effecting bypass attempts with omitted/false/malformed taint metadata are blocked by runtime provenance controls.
- tainted `write_reversible` escalates to approval and tainted `external_send` is denied.
- malformed taint metadata triggers deterministic fail-closed behavior for side effects.
- read behavior remains allowlisted and does not clear taint for later side effects.
- lifecycle events include provenance evidence and decision-basis metadata.

## Known Tradeoffs

- provenance is context-window level, not token-level causal influence tracing.
- conservative tainting can increase approval prompts for side effects when prior tool output remains in context.
