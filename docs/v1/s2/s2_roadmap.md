# Slice 2: Safe Action Framework — PR Roadmap

### PR-01: Capability Proposal + Deterministic Policy + Approval Lifecycle
- **goal**: introduce the Slice 2 action engine so Ariel can safely propose actions, authorize via deterministic policy, and enforce approval-gated execution.
- **builds on**: Slice 1 PR-02 merged state (bounded turn loop + durable event chain).
- **acceptance**:
  - model-proposed capability calls are treated as untrusted and validated against registered capability contracts before execution.
  - allowlisted low-impact `read` actions can execute inline without approval and produce visible, redacted results.
  - approval-required actions create durable pending approval records and do not execute pre-approval.
  - approval decisions are bound to exact payload hash + actor + expiry, and approved actions execute only the frozen proposed payload.
  - denied or expired approvals never execute and produce clear user-visible + auditable reasons.
  - unknown/schema-invalid/policy-denied proposals are blocked with auditable rejection reasons.
  - users can inspect proposal, approval decision, execution outcome, and returned output/error in the timeline/surface.
- **non-goals**: no agency-specific or calendar-specific domain workflows, no cross-session memory/session-rotation changes, no provider portability changes, no batched/delegated approvals.

### PR-02: Taint-Aware Policy + Capability Integrity + Egress Guardrails (planned after PR-01 merges)
- **goal**: harden Slice 2 safety boundaries so untrusted content cannot silently escalate authority and execution remains least-privilege.
- **builds on**: PR-01.
- **acceptance**:
  - policy is taint-aware: proposals influenced by untrusted external/tool content cannot auto-authorize side effects and must be escalated or denied.
  - execution verifies capability identity/version/contract metadata against proposal-time capture; mismatch blocks execution.
  - capability outbound access is constrained to explicit policy-allowed destinations, with deny-by-default behavior for arbitrary egress.
  - side-effecting action execution is serialized for deterministic behavior and clearer audit reconstruction.
  - pre-execution and post-execution guardrails are applied so unsafe inputs/outputs are blocked before side effects or user surfacing.
- **non-goals**: no dynamic plugin loading, no generic shell/ssh capability exposure, no autonomous background action loops, no multi-user tenancy/public hosting changes.
