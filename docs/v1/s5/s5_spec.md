# Slice 5: Durable Memory + Session Rotation — Spec

## Goal

Preserve continuity across sessions with durable canonical memory and user-visible projection.

## Acceptance Criteria

### new conversation recall uses validated durable memory
- **given**: prior sessions contain both candidate and validated durable memories for user preferences and open commitments
- **when**: the user starts a new conversation after session rotation and asks a related follow-up
- **then**: Ariel recalls only relevant validated memory in the new active session, never recalls candidate-only memory across sessions, preserves continuity without replaying full prior-history turns, and keeps recall behavior auditable

### session rotation is explicit-first with deterministic automatic fallback
- **given**: an active session exists
- **when**: the user explicitly starts a new conversation through a dedicated rotation-intent surface, or deterministic rotation thresholds are reached at a turn boundary
- **then**: Ariel closes the prior session, opens exactly one new active session, records the rotation reason (`user_initiated` or threshold-based), links continuity artifacts for auditability, and keeps default session-bootstrap behavior non-rotating unless rotation intent is explicit

### user corrections and removals deterministically change future behavior
- **given**: a remembered fact, preference, or commitment is incorrect, stale, or no longer wanted
- **when**: the user corrects or removes it
- **then**: the change applies immediately without an approval flow, future turns stop using the superseded/removed memory, and Ariel uses only the corrected active memory state

### memory context remains bounded and does not become full-history replay
- **given**: long-running usage across many sessions
- **when**: Ariel builds context for a turn
- **then**: context includes only bounded, deterministic memory sections (for example recent session context, summaries, and top relevant durable memory) rather than unbounded full transcript replay

### memory behavior remains user-inspectable and auditable
- **given**: memory reads/writes and session rotations occur over time
- **when**: the user inspects Ariel’s surfaced history/state
- **then**: the user can inspect what memory was recalled, created, corrected, removed, or skipped, with provenance and verification metadata

### user-visible memory projection stays consistent with canonical memory
- **given**: canonical memory state in Postgres changes (create/update/correct/remove)
- **when**: the user views memory through surface projection(s)
- **then**: projected memory state reflects canonical behavior without divergence or hidden projection-only overrides

### dedicated memory projection is available without enabling generic write APIs
- **given**: canonical memory records exist
- **when**: the user requests a memory view from Ariel’s surface API
- **then**: Ariel returns a strict read-only memory projection contract sourced from canonical state, while memory creation/correction/removal continues to flow through normal conversation turns rather than generic memory CRUD write endpoints

## Key Decisions

**Canonical memory model is fixed and typed**: Durable memory uses explicit memory classes (`profile`, `preference`, `project`, `commitment`, `episodic_summary`) with required provenance (`source_turn_id`), confidence, and `last_verified_at`. Postgres remains canonical memory source of truth.

**Memory lifecycle is append-safe, not destructive**: Memory records use durable lifecycle states (`candidate`, `validated`, `superseded`, `retracted`) instead of silent hard-delete mutation. Cross-session retrieval uses only active validated memory state.

**Validation is required before cross-session recall**: Ariel stores inferred facts as `candidate` memory but does not use them for cross-session recall. Promotion to `validated` requires explicit user confirmation in MVP.

**Session rotation is hybrid and deterministic**: Ariel keeps exactly one active session at a time. Rotation happens on explicit user intent first (through a dedicated rotate-intent surface), and also on deterministic threshold triggers (age/turn-count/context-pressure) at turn boundaries, with typed auditable reason codes.

**Rotation intent is explicit and backward-compatible**: Existing session bootstrap/status flows remain non-rotating by default; rotation requires explicit user intent (or threshold policy), avoiding ambiguous contract behavior for existing clients.

**Rotation transitions are idempotent and race-safe**: Rotation execution is transaction-safe and duplicate rotate requests cannot produce multiple active sessions or duplicate user-visible rotation outcomes.

**Rotation closes sessions with durable continuity artifacts**: Session rotation writes a bounded episodic continuity artifact so future sessions can recover key context without replaying full historical turns.

**Context builder is extended but remains deterministic and bounded**: Turn context keeps fixed-order assembly and explicit limits while adding durable-memory sections. Added memory recall cannot bypass existing turn-budget guardrails.

**Corrections/removals are immediate but integrity-controlled**: Memory correction/removal does not use approval workflows in MVP, but still requires authenticated user intent, idempotency-safe mutation semantics, append-only audit records, and optional bounded undo support.

**Projection is derived from canonical state, never authoritative**: User-facing memory projection is a derived representation of canonical memory behavior. Any external integration/projection metadata cannot overwrite canonical memory semantics.

**Memory surface follows read-only projection plus conversation-mediated mutation**: MVP exposes dedicated read-only memory projection for inspectability, while memory writes/promotions/corrections/removals are performed through authenticated conversation flow and auditable runtime decisions, not generic write endpoints.

**Recall selection is deterministic and top-k bounded**: Durable-memory retrieval for turn context uses deterministic ranking/selection (relevance + recency + confidence) with explicit top-k bounds and stable ordering so memory recall remains reproducible and budget-safe.

**Open commitments are first-class continuity objects**: Commitments remain distinctly modeled so Ariel can carry forward pending obligations across session rotation and stop surfacing them once completed/cancelled/removed.

## Out of Scope

- Advanced automatic long-horizon memory optimization (embedding-only ranking, autonomous consolidation, or background self-edit loops)
- Multi-user memory partitioning/tenancy behavior
- Nexus notes integration and external memory sync authority changes (still deferred)
- Proactive notification logic driven by memory subscriptions (-> Slice 11B)
- Quick-capture channel expansion and mobile ingestion UX (-> Slice 8)
- Generic memory CRUD write APIs for direct out-of-band mutation
