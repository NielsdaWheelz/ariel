# Slice 5: Durable Memory + Session Rotation — PR Roadmap

### PR-01: Canonical Memory Core + Explicit Session Rotation + Cross-Session Recall
- **goal**: deliver the first full continuity vertical slice with canonical durable memory, explicit new-conversation rotation, and validated cross-session recall.
- **builds on**: Slice 4 PR-03 merged state (bounded turn orchestration, surfaced response contracts, and policy/audit runtime already in place).
- **acceptance**:
  - Ariel persists durable memory in Postgres as canonical state with typed memory classes and required provenance/verification metadata, and exposes a dedicated read-only memory projection endpoint derived from canonical state.
  - memory projection responses are contract-enforced and redaction-safe, with no projection-only authority over canonical behavior.
  - explicit user-provided facts/preferences/commitments can be captured as validated durable memory through normal turn flow with auditable outcomes.
  - memory mutation remains conversation-mediated in MVP (no generic memory CRUD write endpoint in this PR).
  - user can explicitly start a new conversation through a dedicated rotation-intent surface; Ariel rotates sessions safely (exactly one active session), marks the prior session inactive, and records `user_initiated` rotation reason for auditability.
  - existing session bootstrap/status surfaces remain backward-compatible and non-rotating by default unless explicit rotation intent is provided.
  - rotation intent handling is idempotent and race-safe; duplicate rotate submissions do not create multiple active sessions or duplicate user-visible rotation outcomes.
  - session rotation writes bounded continuity artifacts (including episodic summary and open commitments context) used for future continuity without full transcript replay.
  - when the next active session starts, Ariel recalls relevant validated memory (including open commitments) across sessions while excluding candidate-only memory from recall.
  - context assembly remains deterministic and bounded after memory integration; memory retrieval uses explicit top-k bounded selection with stable ordering and does not bypass existing turn-budget failure semantics.
- **non-goals**: no automatic threshold-triggered rotation, no inferred-candidate promotion workflow, and no correction/removal lifecycle beyond baseline validated capture.

### PR-02: Candidate Promotion + Correction/Removal + Threshold Rotation Hardening (planned after PR-01 merges)
- **goal**: complete Slice 5 trust and lifecycle semantics by adding candidate validation flows, immediate correction/removal behavior, and deterministic automatic rotation triggers.
- **builds on**: PR-01.
- **acceptance**:
  - Ariel stores inferred memory as `candidate` and never uses candidate-only memory for cross-session recall.
  - promotion from `candidate` to `validated` requires explicit user confirmation in MVP, with auditable transition outcomes.
  - user can correct or remove remembered facts/preferences/projects/commitments through normal interaction; changes apply immediately without approval and future behavior reflects only active corrected memory.
  - memory lifecycle transitions remain append-only and idempotent (`candidate`, `validated`, `superseded`, `retracted`) with user-visible inspectable history.
  - deterministic automatic rotation triggers (age/turn-count/context-pressure) execute only at turn boundaries, preserve one-active-session invariant, and emit typed threshold-based rotation reasons.
  - memory recall/mutation/skip behavior remains user-inspectable and projection-consistent with canonical state across create/promote/correct/remove/rotate paths.
- **non-goals**: no autonomous background memory rewriting/consolidation loops, no Nexus sync-authority changes, no proactive memory-driven notification policy, and no generic memory CRUD write API.
