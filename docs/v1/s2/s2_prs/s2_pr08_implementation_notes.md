# S2 PR-08 Implementation Notes

## Scope Landed

this implementation closes the egress pre-dispatch gap by moving external-send execution to a strict two-phase runtime boundary:

- capabilities must declare outbound intent through `declare_egress_intent(...)`.
- runtime preflights declared destinations against per-capability allowlists before any outbound dispatch.
- outbound dispatch is routed through a centralized runtime choke point (`_dispatch_egress_request`).
- deny/malformed/missing/undeclared intent fails closed with deterministic execution errors.

## Runtime Behavior

### Two-Phase Egress Boundary

for `impact_level == "external_send"`, execution now follows this order:

1. pre-execution guardrails on input.
2. preflight egress intent declaration and policy validation.
3. capability execution (business output generation, no dispatch metadata contract).
4. post-execution output guardrails.
5. centralized outbound dispatch.
6. redacted surfaced output projection.

this ordering ensures:

- denied/malformed intent never dispatches.
- execution failures after preflight do not dispatch.
- dispatch stays centralized instead of being capability-implementation dependent.

### Fail-Closed Error Contracts

new deterministic preflight errors surfaced through existing execution failure paths:

- `egress_preflight_missing_intent`
- `egress_preflight_contract_invalid`
- `egress_preflight_undeclared_intent`

existing deny semantics are preserved for policy failures:

- `egress_destination_invalid`
- `egress_destination_denied:<normalized_host>`

dispatch boundary failures remain fail-closed under:

- `egress_dispatch_contract_invalid`
- `egress_dispatch_failed:<reason>`

### Capability Contract Update

`CapabilityDefinition` now includes:

- `declare_egress_intent: Callable[[dict[str, Any]], list[dict[str, Any]] | None] | None`

`cap.framework.external_notify` was updated to:

- declare egress intent via `declare_egress_intent(...)`
- return surfaced output payload only (`status`, `destination`, `message`)
- no internal `__egress__` output sentinel

legacy `__egress__` output metadata is now treated as contract violation (`egress_preflight_undeclared_intent`) to prevent bypass paths.

## Tests Added

`tests/integration/test_s2_pr08_acceptance.py` adds acceptance coverage for:

- allowlisted path executes and dispatches exactly once with clean surfaced lifecycle output.
- non-allowlisted destination is denied before capability execution and before dispatch.
- missing/malformed/undeclared egress intent fails closed before capability execution and dispatch.
- allowlisted preflight plus downstream execution failure still performs zero dispatch attempts.

## Non-Goals Confirmed

- no OS-level sandbox/firewall guarantees.
- no approval token semantics changes.
- no new capability domains.
