# Slice 5: Durable Memory + Session Rotation — PR Roadmap

### PR-01: Canonical Memory Core + Session Rotation + Lifecycle Semantics
- **goal**: deliver the shipped continuity vertical slice with canonical durable memory, read-only projection, explicit + threshold-driven rotation, lifecycle-safe memory mutation, replay-safe ingress, and deterministic cursored timeline reads.
- **builds on**: Slice 4 PR-03 merged state (bounded turn orchestration, surfaced response contracts, and policy/audit runtime already in place).
- **acceptance**:
  - canonical memory persists in Postgres with typed classes and revision metadata; projection is derived/read-only via `GET /v1/memory`.
  - memory mutation remains conversation-mediated (no generic write endpoint) and supports `remember`, `correct`, `forget`, and inferred candidate capture.
  - candidate memory is excluded from cross-session recall until explicitly promoted.
  - memory lifecycle is append-only and inspectable (`candidate`, `validated`, `superseded`, `retracted`) with active-revision supersession semantics.
  - explicit rotation is supported via `POST /v1/sessions/rotate`, with inspectable history via `GET /v1/sessions/rotations`.
  - deterministic auto-rotation triggers run at turn boundaries with typed reasons (`threshold_turn_count`, `threshold_age`, `threshold_context_pressure`) while preserving one-active-session safety.
  - rotate and message ingress are idempotency-safe, including conflict detection (`E_IDEMPOTENCY_KEY_REUSED`) and typed key validation (`E_IDEMPOTENCY_KEY_INVALID`).
  - cursored timeline reads (`GET /v1/sessions/{session_id}/events?after=...`) are deterministic, session-scoped, and omit turns without post-cursor events.
  - context assembly remains deterministic and bounded after memory integration (fixed order + bounded windows/top-k).
  - regression coverage protects lifecycle, rotation, idempotency, context-order, and timeline-cursor contracts.
- **non-goals**: no generic memory CRUD write API, no autonomous background memory rewriting/consolidation loops, no Nexus sync-authority changes, and no proactive memory-driven notification policy.
