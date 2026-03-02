# Slice 2: Safe Action Framework — PR Roadmap

### PR-01: Action Attempt Engine + Approval Surface + Inline Read Execution
- **goal**: deliver the first full safe-action vertical slice: proposal validation, deterministic policy decision, inline allowlisted reads, and approval-gated execution from the approval surface.
- **builds on**: Slice 1 PR-02 merged state (single active session + bounded turn/event loop); current codebase has no capability registry, action-attempt persistence, or approvals endpoint yet.
- **acceptance**:
  - when a model-proposed capability call is schema-valid and policy-authorized as allowlisted low-impact `read`, Ariel executes it inline without approval, returns redacted output in the user-visible flow, and records proposal -> policy decision -> execution -> outcome events.
  - when a model-proposed call is approval-required, Ariel persists a pending approval request and does not execute before explicit user approval.
  - `POST /v1/approvals` supports explicit approve/deny decisions; approvals are single-use, actor-bound, expiry-bound, and execute only the exact frozen proposed payload once.
  - denied or expired approvals never execute and produce clear user-visible and auditable terminal reasons.
  - unknown, schema-invalid, or policy-denied proposals are blocked before execution with explicit rejection reasons and safe assistant fallback behavior.
  - users can inspect the full lifecycle for each action attempt in the surface timeline/action details (proposal, policy outcome, approval outcome, execution outcome, output/error).
  - orchestration enforces Slice 2 MVP bounds: zero or more allowlisted reads may run inline per turn, while at most one approval-gated action may remain pending from that turn.
- **non-goals**: no agency- or calendar-domain behavior beyond minimal framework-probe capabilities; no batched/delegated approvals; no cross-session memory/session-rotation/provider-portability changes; no roadmap-level lock-in to concrete probe capability IDs (those are defined in PR-01 brief/tests).

### PR-02: Taint-Aware Authorization + Execution Integrity + Egress Controls
- **goal**: harden Slice 2 boundaries so untrusted content cannot escalate side effects and runtime execution stays integrity-checked and least-privilege.
- **builds on**: PR-01.
- **acceptance**:
  - policy is taint-aware: side-effecting proposals materially influenced by untrusted external/tool content cannot auto-execute and are escalated to approval or denied.
  - execution fails closed if capability identity/version/contract metadata differs from proposal-time capture.
  - capability outbound destinations are explicitly allowlisted per capability; non-allowlisted egress is blocked with auditable deny reasons.
  - side-effecting actions execute serially with deterministic ordering and idempotent execution boundaries.
  - layered pre-execution and post-execution guardrails block unsafe inputs/outputs before side effects or unsafe user surfacing.
  - event/audit streams remain reconstructable for allow, deny, approval, expiry, execution success/failure, and guardrail-blocked outcomes.
- **non-goals**: no dynamic plugin loading, no generic shell/ssh capability exposure, no autonomous background side-effect loops, no multi-user tenancy/public hosting changes.
- **status**: landed in current implementation branch; see `s2_prs/s2_pr02_implementation_notes.md`.

### PR-03: Runtime Taint Provenance + Fail-Closed Side-Effect Authorization
- **goal**: close the trust gap by deriving taint from runtime provenance instead of model-declared flags, so untrusted content cannot silently authorize side effects.
- **builds on**: PR-02.
- **acceptance**:
  - when side-effecting proposals are materially influenced by tool/external content, policy escalates or denies even if the model omits taint hints.
  - if side-effect taint provenance is missing/ambiguous, execution fails closed (explicit approval escalation or deny) with auditable reasons.
  - action events include provenance evidence sufficient to reconstruct why taint controls applied.
  - regression tests prove side-effecting auto-execution cannot bypass taint controls via missing/malformed taint metadata.
- **non-goals**: no probabilistic trust scoring engine; no cross-session memory trust-policy redesign.
- **status**: landed in current implementation branch; see `s2_prs/s2_pr03_implementation_notes.md`.

### PR-04: Surface Lifecycle Inspectability (planned after PR-03 merges)
- **goal**: ensure the phone-first surface exposes the full action lifecycle required by Slice 2.
- **builds on**: PR-03.
- **acceptance**:
  - users can inspect proposal payload summary, policy outcome, approval outcome (including denied/expired reasons), execution outcome, and output/error for each action attempt.
  - approval and execution outcomes are visible in timeline/action details without relying on internal logs.
  - surfaced data remains redacted and excludes internal-only execution metadata.
- **non-goals**: no visual redesign, no bulk/delegated approvals UX, no multi-user tenancy changes.
