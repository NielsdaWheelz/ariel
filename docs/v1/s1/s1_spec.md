# Slice 1: Core Conversation Loop — Spec

## Goal

Deliver natural multi-turn conversation with bounded assistant decision-making.

## Acceptance Criteria

### user gets coherent follow-up across related turns
- **given**: an active session with recent related turns
- **when**: the user sends a follow-up that references prior turn context
- **then**: Ariel responds coherently using that short-term context and remains consistent with prior in-session facts

### assistant requests missing details when intent is ambiguous
- **given**: a user message with missing details or conflicting constraints
- **when**: Ariel processes the turn
- **then**: Ariel asks for missing details instead of guessing hidden assumptions

### assistant answers directly when intent is clear
- **given**: a user message that is specific and answerable from available context
- **when**: Ariel processes the turn
- **then**: Ariel returns a direct response in that turn without unnecessary detours

### bounded limits fail clearly and audibly
- **given**: turn processing would exceed configured limits (context budget, response budget, or model-attempt budget)
- **when**: a limit is reached
- **then**: the turn terminates with a clear bounded-failure message to the user, a terminal failed state, and a standard error code (`E_TURN_LIMIT_REACHED`) with auditable limit details

### out-of-window context does not silently degrade
- **given**: a session long enough that older turns fall outside configured context bounds
- **when**: the user asks about detail outside that bound
- **then**: Ariel does not fabricate continuity; it states uncertainty or asks a recovery clarification

### unresolved turn loops fail explicitly
- **given**: required details are still missing after repeated unresolved interaction
- **when**: configured turn limits are reached
- **then**: Ariel ends the turn with a bounded failure (`E_TURN_LIMIT_REACHED`) that states what input is still needed to proceed

## Key Decisions

**Deterministic bounded context bundle for Slice 1**: Each turn context is assembled in fixed order from policy/system instructions plus a bounded recent-turn window from the active session. Unbounded full-session replay is explicitly disallowed.

**Model-led messaging without response-type state machine**: For Slice 1, the model decides from prompt+context what assistant message to produce (direct answer, request for missing details, or uncertainty). Ariel does not require a brittle answer-vs-clarification classifier.

**Explicit turn-budget enforcement**: Turn execution is bounded by configured limits for model attempts, context budget, response budget, and wall time. Budget exhaustion is a first-class, user-visible failure condition, not silent degradation.

**Runtime contracts over prompt rigidity**: Determinism is enforced in context assembly order, budget checks, terminal turn states, and auditable event semantics. Prompt wording can evolve without changing these contracts.

**Dedicated bounded-failure error semantics**: Any turn that exhausts a configured limit returns `E_TURN_LIMIT_REACHED` with structured limit metadata and ends in terminal failed status. Limit failures are not merged into generic provider/network failures.

**Loop guardrails are global, not clarification-specific**: Ariel does not use a dedicated clarification counter. Repeated unresolved interaction is bounded by the same global turn limits and fails clearly when limits are exhausted.

**Configurable budgets with operational defaults**: Limit thresholds are configuration-driven so they can be tuned without API contract changes. Initial defaults are: `max_recent_turns=12`, `max_context_tokens=6000`, `max_response_tokens=700`, `max_model_attempts=2`, `max_turn_wall_time_ms=20000`.

**Auditable decision and limit semantics**: The append-only turn event chain must make it observable whether a turn produced an assistant message or a bounded-failure terminal outcome, and still end in one terminal turn status.

**Single active session remains continuity boundary**: Slice 1 continues the one-active-session model for short-term continuity. Session rotation and cross-session recall stay deferred.

## Out of Scope

- Capability/tool planning and execution, policy authorization, and approval workflows (-> Slice 2)
- Agency task runs, status orchestration, and artifact retrieval UX (-> Slice 3)
- Calendar-specific assistant behavior (-> Slice 4)
- Durable cross-session memory retrieval, memory correction flows, and session rotation policy (-> Slice 5)
- Provider portability/failover hardening (-> Slice 6)
- Public ingress, multi-user tenancy, or autonomous background action loops
