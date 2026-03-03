# S2 PR-02 Implementation Notes

## Scope Landed

this implementation hardens slice-2 execution boundaries in running code:

- taint-aware authorization for side-effecting proposals.
- proposal-time execution identity capture and execution-time integrity checks.
- deny-by-default egress controls with per-capability destination allowlists.
- pre-execution input and post-execution output guardrails.
- serialized side-effecting execution in approval-triggered flows.

## Runtime Behavior

### Taint-Aware Policy

proposal payloads can include `influenced_by_untrusted_content: true`.

- side-effecting `write_reversible` proposals are escalated to approval (`taint_escalated_requires_approval`).
- side-effecting `external_send` / `write_irreversible` proposals are denied (`taint_denied_untrusted_side_effect`).
- decisions are persisted on `action_attempts` and emitted in `evt.action.policy_decided`.

### Execution Integrity

`action_attempts` now persist:

- `capability_version`
- `capability_contract_hash`

before invocation, execution verifies runtime capability metadata still matches proposal-time capture:

- `capability_id`
- `capability_version`
- `capability_contract_hash`

integrity mismatches fail closed before invocation with auditable `integrity_mismatch:*` errors.

### Egress Controls

capabilities declare allowlisted outbound destinations in the registry.

pr-02 originally used internal `__egress__` output metadata for outbound intents. as of pr-08, egress moved to explicit preflight intent declaration (`declare_egress_intent`) validated before centralized dispatch; legacy `__egress__` output metadata is now treated as contract-invalid/undeclared intent and blocked fail-closed.

### Guardrails

- pre-execution guardrail blocks unsafe side-effecting inputs (`guardrail_pre_input_blocked:*`).
- post-execution guardrail blocks unsafe outputs before user surfacing (`guardrail_post_output_blocked:*`).

### Deterministic Side-Effect Serialization

approval-triggered side-effecting actions acquire a postgres advisory lock before invocation, ensuring deterministic serialized execution boundaries across concurrent approval runs.

## Data Model / Migration

added migration `20260302_0003` to create `action_attempts.capability_contract_hash`.

for pre-existing rows, migration backfills from `payload_hash` and then enforces non-nullability. this intentionally fails closed for legacy pending approvals that do not have a matching capability contract snapshot.

## Probe Capability Set (Post PR-02)

- `cap.framework.read_echo` (`read`, allow inline)
- `cap.framework.read_private` (`read`, policy deny)
- `cap.framework.write_note` (`write_reversible`, approval required)
- `cap.framework.write_draft` (`write_reversible`, allow inline; used to exercise taint escalation)
- `cap.framework.external_notify` (`external_send`, approval required, egress allowlist enforced)

## Test Coverage

acceptance coverage lives in `tests/integration/test_s2_pr02_acceptance.py` and validates:

- taint escalation and taint deny outcomes.
- execution integrity mismatch blocking.
- egress allow/deny outcomes.
- guardrail input/output blocked outcomes.
- replay-safe approval execution boundaries.

regression coverage continues to run through `make verify` and `make e2e`.

## Known Tradeoffs

- taint propagation is explicit proposal metadata for mvp, not full provenance inference.
- egress controls are capability-contract enforcement, not OS-level network sandboxing.
