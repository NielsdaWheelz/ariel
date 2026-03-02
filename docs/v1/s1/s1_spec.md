# Slice 1: Core Conversation Loop — Spec

## Goal

Deliver natural multi-turn conversation with bounded assistant decision-making.

## Acceptance Criteria

### user gets coherent follow-up across related turns
- **given**: an active session with recent related turns
- **when**: the user sends a follow-up that references prior turn context
- **then**: Ariel responds coherently using that short-term context and remains consistent with prior in-session facts

### assistant asks clarification when intent is ambiguous
- **given**: a user message with missing details or conflicting constraints
- **when**: Ariel processes the turn
- **then**: Ariel asks one focused clarifying question instead of guessing hidden assumptions

### assistant answers directly when intent is clear
- **given**: a user message that is specific and answerable from available context
- **when**: Ariel processes the turn
- **then**: Ariel returns a direct response in that turn without unnecessary clarification

### bounded limits fail clearly and audibly
- **given**: turn processing would exceed configured limits (context budget, response budget, or model-attempt budget)
- **when**: a limit is reached
- **then**: the turn terminates with a clear bounded-failure message to the user, a terminal failed state, and a standard error code (`E_TURN_LIMIT_REACHED`) with auditable limit details

### out-of-window context does not silently degrade
- **given**: a session long enough that older turns fall outside configured context bounds
- **when**: the user asks about detail outside that bound
- **then**: Ariel does not fabricate continuity; it states uncertainty or asks a recovery clarification

### clarification budget exhaustion is explicit
- **given**: Ariel has already asked the configured maximum consecutive clarifying questions for unresolved intent
- **when**: required disambiguation is still missing
- **then**: Ariel ends the turn with a bounded failure (`E_TURN_LIMIT_REACHED`) that states what input is missing for the user to proceed

## Key Decisions

**Deterministic bounded context bundle for Slice 1**: Each turn context is assembled in fixed order from policy/system instructions plus a bounded recent-turn window from the active session. Unbounded full-session replay is explicitly disallowed.

**Two-path decision contract (answer vs clarify)**: The engine produces exactly one conversational decision per turn: direct answer or clarifying question. Ambiguous intent routes to clarification by default.

**Explicit turn-budget enforcement**: Turn execution is bounded by configured limits for model attempts, context budget, response budget, and clarification budget. Budget exhaustion is a first-class, user-visible failure condition, not silent degradation.

**Dedicated bounded-failure error semantics**: Any turn that exhausts a configured limit returns `E_TURN_LIMIT_REACHED` with structured limit metadata and ends in terminal failed status. Limit failures are not merged into generic provider/network failures.

**Clarification loop guardrail**: Ariel may ask at most one clarifying question per assistant turn and at most two consecutive clarification turns for the same unresolved intent. After that, it must fail clearly and request the missing specifics from the user.

**Configurable budgets with operational defaults**: Limit thresholds are configuration-driven so they can be tuned without API contract changes. Initial defaults are: `max_recent_turns=12`, `max_context_tokens=6000`, `max_response_tokens=700`, `max_model_attempts=2`, `max_consecutive_clarifications=2`, `max_turn_wall_time_ms=20000`.

**Auditable decision and limit semantics**: The append-only turn event chain must make it observable whether the turn took direct-answer, clarifying-question, or bounded-failure path, and still ends in one terminal turn status.

**Single active session remains continuity boundary**: Slice 1 continues the one-active-session model for short-term continuity. Session rotation and cross-session recall stay deferred.

## Out of Scope

- Capability/tool planning and execution, policy authorization, and approval workflows (-> Slice 2)
- Agency task runs, status orchestration, and artifact retrieval UX (-> Slice 3)
- Calendar-specific assistant behavior (-> Slice 4)
- Durable cross-session memory retrieval, memory correction flows, and session rotation policy (-> Slice 5)
- Provider portability/failover hardening (-> Slice 6)
- Public ingress, multi-user tenancy, or autonomous background action loops
