# Slice 2: Safe Action Framework — Spec

## Goal

Enable tool-enabled behavior with deterministic policy and approval controls.

## Acceptance Criteria

### read actions execute without approval and are visible to the user
- **given**: an active session and a model-proposed capability call that is schema-valid and policy-authorized as `read`
- **when**: Ariel processes the turn
- **then**: Ariel executes the action without approval, returns the result in the user-visible response flow with standard redaction applied, and records an auditable proposal->authorization->execution chain

### approval-required actions are proposed but not executed before approval
- **given**: a model-proposed capability call that is schema-valid and policy-classified as approval-required (`write_reversible` unless allowlisted, or always for `write_irreversible`/`external_send`)
- **when**: Ariel processes the turn
- **then**: Ariel records a pending approval request and does not execute the action before explicit user approval

### approved actions execute only for the exact approved payload
- **given**: a pending, unexpired approval request for a proposed action
- **when**: the user explicitly approves via the approval surface
- **then**: Ariel executes exactly the approved action payload once, records approval and execution events linked to the original proposal, and makes the returned result visible to the user

### denied or expired approvals prevent execution with clear reason
- **given**: a pending approval-required action
- **when**: the user denies it, or approval expires before approval is granted
- **then**: Ariel does not execute the action, records terminal denied/expired approval outcome, and shows a clear user-visible reason

### invalid or unauthorized tool calls are blocked before execution
- **given**: a model-proposed action that is unknown, schema-invalid, or policy-denied
- **when**: Ariel evaluates the proposal
- **then**: Ariel rejects execution, records the rejection reason in the audit chain, and returns a safe failure/next-step response instead of performing side effects

### untrusted content cannot silently escalate action authority
- **given**: a turn includes untrusted external/tool-sourced content and a proposed action with side effects
- **when**: policy evaluates the proposal
- **then**: Ariel applies taint-aware policy controls (approval escalation or deny), and does not auto-execute side effects based only on untrusted content

### capability and destination constraints are enforced at execution time
- **given**: an authorized action reaches execution
- **when**: executor validates runtime constraints
- **then**: execution proceeds only if the capability definition matches the proposed contract and outbound access remains within policy-allowed destinations; otherwise execution is blocked with an auditable reason

### user can inspect the full action lifecycle
- **given**: an action proposal has been created for a turn
- **when**: the user inspects Ariel’s timeline/action details
- **then**: the user can see what was proposed, what was approved or denied (including expiry outcome), what executed, and what output/error was returned

## Key Decisions

**Action attempt is the engine-level unit of work**: Each model-proposed action is persisted as an immutable proposal plus lifecycle state (`proposed -> awaiting_approval -> approved|denied|expired -> executing -> succeeded|failed`). This decouples action safety semantics from turn status and supports post-turn approval handling.

**Policy and schema checks are strict preconditions for execution**: Model output is untrusted. Capability existence/version, input schema validity, and policy authorization are all evaluated before any capability runs.

**Approval is bound to exact payload identity**: Approval tokens are single-use and include actor identity, expiry, and a hash of the canonical action payload. Any mismatch, replay, or expiry blocks execution.

**Approval executes the frozen proposed action, not a re-planned action**: `POST /v1/approvals` authorizes execution of the already-proposed payload; users are not required to resend the original request. This avoids model drift between proposal and execution.

**Bounded tool orchestration for MVP safety**: A turn may execute zero or more policy-allowed `read` actions inline, but only one approval-gated action can be pending from a turn at a time. Side-effecting action execution remains serialized for determinism and audit clarity.

**Capability contracts enforce runtime guardrails**: Every capability definition includes explicit input/output schemas, impact level, timeout, output-size limits, and idempotency requirements, and execution fails closed when these constraints are violated.

**Read-without-approval is constrained, not open-ended**: Slice 2 keeps roadmap-aligned no-approval reads, but only for policy-allowlisted low-impact read capabilities, with redaction and full auditability. Read access is not treated as implicitly safe for arbitrary data surfaces.

**Policy is taint-aware for untrusted content**: External/tool-returned content is treated as untrusted input and cannot independently authorize side effects. Policy can escalate such proposals to approval or deny them outright.

**Capability identity is execution-bound**: Execution is bound to stable capability identity/contract metadata captured at proposal time; mismatches at execution time are treated as integrity failures and blocked.

**Capability egress is least-privilege by default**: Each capability runs with explicit outbound destination constraints, so successful model prompting alone cannot grant arbitrary network reach.

**Layered guardrails wrap execution boundaries**: In addition to schema and policy gates, Ariel applies pre-execution and post-execution safety checks so unsafe inputs/outputs are caught before side effects or user surfacing.

**Action lifecycle observability is first-class**: Event chains must make proposal, policy decision, approval request/decision, execution start/end, and returned outcome reconstructable by the user and by audit tooling.

## Out of Scope

- Agency-specific workflows (`cap.agency.run/status/artifacts/request_pr`) and long-running coding-job UX (-> Slice 3)
- Calendar domain behaviors (`cap.calendar.list/propose_slots/create_event`) beyond reusing the Slice 2 safety framework (-> Slice 4)
- Cross-session memory writes/retrieval policy and session rotation behavior (-> Slice 5)
- Provider portability/failover hardening beyond preserving Slice 1 behavior (-> Slice 6)
- Multi-user tenancy, public internet exposure, and autonomous background action loops
- Bulk/batched approvals, delegated approvals, and adaptive auto-approval heuristics
- Automatic trust of tool output as policy authority
