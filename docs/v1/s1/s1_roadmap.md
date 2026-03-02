# Slice 1: Core Conversation Loop — PR Roadmap

### PR-01: Deterministic Turn Decision + Bounded Session Context
- **goal**: implement the Slice 1 conversation contract so each turn produces a model-led assistant message from bounded recent in-session context, with deterministic observability of context application and terminal outcome.
- **builds on**: Slice 0 PR-02 merged state (durable single-session chat + auditable turn/event chain).
- **acceptance**:
  - when user intent is clear, Ariel returns a direct answer in the same turn without unnecessary back-and-forth.
  - when user intent is ambiguous or conflicting, Ariel asks for missing details instead of guessing, based on model judgment from prompt+context.
  - each turn uses a deterministic bounded context bundle in fixed order (policy/system instructions first, then bounded recent-session context), and the applied bounds are auditable in turn metadata/events.
  - related follow-up turns remain coherent within the configured recent-turn window for the active session.
  - when a user asks about details outside the configured context window, Ariel does not fabricate continuity and instead states uncertainty or asks for recovery details.
  - turn events make completed assistant emission and terminal turn outcome observable, without requiring a hard-coded answer-vs-clarification classifier.
- **non-goals**: no tool/capability execution, approvals, cross-session memory retrieval, budget-exhaustion failure semantics, brittle rule-tree intent classification, or clarification-count constraints.

### PR-02: Turn Budget Guardrails + Explicit Bounded-Failure Semantics (planned after PR-01 merges)
- **goal**: enforce configuration-driven turn limits and fail explicitly with `E_TURN_LIMIT_REACHED` when any configured budget is exhausted.
- **builds on**: PR-01.
- **acceptance**:
  - limit thresholds are configuration-driven with Slice 1 defaults (`max_recent_turns=12`, `max_context_tokens=6000`, `max_response_tokens=700`, `max_model_attempts=2`, `max_turn_wall_time_ms=20000`).
  - when context, response, model-attempt, or wall-time limits are exceeded, the turn ends in terminal failed status and user receives a clear bounded-failure message with error code `E_TURN_LIMIT_REACHED`.
  - bounded-failure responses include auditable structured limit details describing which limit was hit.
  - behavioral and machine-readable contracts are normative (decision outcome, terminal status, error code, structured details); exact user-facing copy is non-normative and may evolve without contract changes.
  - unresolved interaction loops are explicitly bounded by global turn limits; no dedicated clarification counter is required.
  - event chains preserve observability of decision outcome and terminal turn result for both successful and bounded-failure turns.
- **non-goals**: no provider failover strategy changes, no new capability domains, and no session-rotation/cross-session memory behavior.
