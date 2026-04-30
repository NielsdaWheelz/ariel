# Memory Hard Cutover

## Scope

This document defines the hard cutover from narrow keyword memory to a semantic,
auditable, production memory system.

The cutover covers durable user memory, project state, memory review, conflict
resolution, salience, retrieval, and context assembly.

It does not cover model provider state, prompt caching, transcript storage except as
memory evidence, or general document search.

## Cutover Policy

- This is a hard cutover.
- Do not keep legacy memory code.
- Do not dual-write old and new memory stores.
- Do not add compatibility shims for old memory classes, keys, endpoints, events, or
  tests.
- Do not fall back to lexical keyword recall when semantic retrieval or graph retrieval
  fails.
- If memory context required by policy cannot be assembled, fail closed with a typed
  error.
- A memory subsystem failure must not degrade into a legacy path, lexical path, or
  transcript replay path.
- Context sections can be omitted only because of deterministic scope and budget rules,
  not because a subsystem failed.
- Existing narrow memory rows are not a compatibility contract.
- Any preservation of existing production memories must be a one-time import into the new
  canonical schema before cutover. The runtime must not contain migration adapters.

## Target Behavior

### User Experience

- Ariel remembers durable preferences, profile facts, commitments, decisions, project
  state, and recurring procedural instructions across sessions.
- Ariel distinguishes facts about the user, facts about a project, agent commitments,
  task history, and procedural operating preferences.
- Ariel can answer "what do you remember?", "why do you think that?", "forget this",
  "correct this", "prioritize this", and "show history" from canonical memory state.
- Ariel exposes reviewable memory candidates before they become trusted memory unless a
  policy explicitly permits auto-promotion.
- Ariel surfaces unresolved contradictions as uncertainty instead of silently choosing one
  fact.
- Ariel can maintain long-range project state without replaying full transcripts or relying
  on one rolling summary.
- Ariel remembers only useful high-level details. It does not treat memory as a store for
  large verbatim blocks, secrets, templates, or arbitrary transcript chunks.

### Write Behavior

- Every memory write starts from evidence.
- Evidence records identify the source turn, session, actor, content class, timestamp, and
  trust boundary.
- Model extraction creates candidate assertions. It does not directly mutate active memory.
- Review policy promotes, rejects, routes, or conflicts candidate assertions.
- Corrections supersede prior assertions. They do not overwrite history.
- Forget/delete retracts active memory and removes derived retrieval projections.
- Tool outputs, web content, files, and quoted user-provided text cannot become trusted
  memory without an explicit user or system-owned review decision.

### Recall Behavior

- The context builder assembles memory in deterministic sections:
  - pinned core memory
  - current project state
  - active commitments and unresolved decisions
  - relevant semantic assertions
  - relevant episodic evidence snippets
  - unresolved conflicts that affect the turn
- Retrieval uses structured filters, semantic similarity, keyword match, graph distance,
  temporal validity, salience, confidence, verification age, source quality, and task
  relevance.
- Every recalled item includes a memory id, lifecycle state, provenance id, rank reason,
  and validity interval when applicable.
- Token budget enforcement is deterministic.
- Recall never includes superseded, rejected, retracted, or deleted assertions unless the
  user is inspecting history.

## Goals

- Build a robust semantic personal and project model.
- Make memory reviewable, correctable, deletable, explainable, and auditable.
- Preserve provenance from derived memory back to source evidence.
- Represent changing facts with temporal validity windows.
- Detect and represent conflicts explicitly.
- Make salience a stored, inspectable signal, not a hidden scoring side effect.
- Keep PostgreSQL as canonical state.
- Treat embeddings, vector indexes, BM25 indexes, graph indexes, and summaries as
  rebuildable projections.
- Keep memory context bounded without losing long-range continuity.
- Make memory safe under concurrency, retries, process restarts, and background worker
  failure.
- Add acceptance tests that exercise long-term memory behavior, not just row persistence.

## Non-Goals

- No provider-hosted memory as canonical memory.
- No vector-only memory system.
- No full-transcript replay strategy.
- No old `profile`, `preference`, `project`, `commitment`, `episodic_summary` class model
  as the new abstraction.
- No regex command parser as canonical extraction.
- No compatibility with current `/v1/memory` response shape.
- No compatibility with current memory event names.
- No model weight updates or fine-tuning.
- No external managed memory dependency as a requirement for correctness.
- No generic enterprise document RAG in this cutover.
- No partial shipping state where both old and new memory systems are reachable.

## Architecture

### Canonical Layers

1. Evidence log

   Stores raw or normalized source evidence. Evidence is append-only except for explicit
   privacy deletion workflows.

2. Semantic assertions

   Stores typed claims derived from evidence:

   - subject
   - predicate
   - object value
   - assertion type
   - scope
   - confidence
   - temporal validity
   - lifecycle
   - source evidence links

3. Memory graph

   Stores entities and relationships for users, projects, repos, artifacts, decisions,
   tasks, commitments, risks, preferences, and recurring procedures.

4. Review and conflict control plane

   Stores candidate review state, promotion decisions, rejection reasons, conflict sets,
   selected winners, and resolution history.

5. Salience model

   Stores rank inputs and computed salience:

   - user pin/deprioritize state
   - project relevance
   - open commitment linkage
   - recency
   - frequency
   - confidence
   - verification age
   - source quality
   - retrieval performance feedback

6. Retrieval projections

   Stores rebuildable indexes and generated summaries:

   - embeddings
   - keyword index rows
   - graph traversal caches
   - compact core memory blocks
   - project state blocks

### Request Flow

1. A turn is persisted.
2. The context builder retrieves the current thread state and canonical memory context.
3. The model receives only the bounded context bundle.
4. The assistant response is persisted.
5. A background task records extraction evidence and proposes memory candidates.
6. Review policy promotes, rejects, conflicts, or queues candidates.
7. Projection tasks rebuild affected embeddings, graph cache rows, compact blocks, and
   salience rows.
8. The next turn reads only the new canonical memory and projections.

### Background Work

- Extraction, reflection, consolidation, salience recomputation, conflict detection, and
  projection rebuilds run outside the primary response path.
- Background tasks are durable PostgreSQL tasks.
- Task claiming uses `FOR UPDATE SKIP LOCKED`.
- Mutations use serializable transactions.
- Logical resources use advisory locks when concurrent updates can target the same memory
  subject or conflict set.
- Background task failure never invokes legacy recall or legacy extraction.

## Data Model

Exact names may change during implementation, but the final schema must express these
concepts directly.

### Required Tables

- `memory_evidence`
- `memory_entities`
- `memory_assertions`
- `memory_assertion_evidence`
- `memory_reviews`
- `memory_conflict_sets`
- `memory_conflict_members`
- `memory_salience`
- `memory_projection_jobs`
- `memory_embedding_projections`
- `memory_context_blocks`
- `project_state_snapshots`

### Required Lifecycles

Assertions:

- `candidate`
- `active`
- `conflicted`
- `superseded`
- `retracted`
- `rejected`
- `deleted`

Reviews:

- `pending`
- `approved`
- `rejected`
- `auto_approved`
- `needs_user_review`
- `needs_operator_review`

Projection jobs:

- `pending`
- `running`
- `completed`
- `failed`
- `dead_letter`

### Required Invariants

- Exactly one active assertion can own a single-valued predicate for a subject and scope.
- Multi-valued predicates must declare that they are multi-valued in the schema or type
  registry.
- An active assertion must have at least one evidence link.
- A conflicted assertion must belong to a conflict set.
- A superseded assertion must identify its superseding assertion.
- A retracted or deleted assertion cannot be recalled in normal context.
- A projection row must identify the canonical row and projection version that produced it.
- A context block must be rebuildable from canonical assertions and project state.
- Deleting derived memory deletes or invalidates all projection rows for that memory.

## Types

Use Pydantic models for semantic values and JSONB payloads.

Required typed concepts:

- `MemorySubject`
- `MemoryPredicate`
- `MemoryObject`
- `MemoryAssertionValue`
- `TemporalScope`
- `EvidenceRef`
- `ReviewDecision`
- `ConflictResolution`
- `SalienceSignals`
- `RecallReason`
- `ProjectState`
- `ContextBlock`

Do not pass anonymous dictionaries across module boundaries for these concepts.

## Retrieval and Context Assembly

### Retrieval Planner

The retrieval planner owns all memory recall decisions. It receives:

- user id
- session id
- current project id when known
- current turn text
- tool/action context
- token budget
- allowed memory scopes

It returns:

- pinned context blocks
- project state block
- ranked assertions
- ranked evidence snippets
- conflict warnings
- omitted-item diagnostics

### Ranking

Ranking is deterministic for identical inputs and database state.

Ranking inputs:

- semantic similarity
- keyword match
- graph distance to current entities
- temporal validity
- assertion lifecycle
- confidence
- salience
- recency
- verification age
- source trust
- user priority
- project/task linkage
- open commitment linkage

Ties are resolved by stable ids after semantic score, salience, priority, and timestamp.

### Context Contract

The model sees a structured memory bundle, not raw database rows.

The bundle must separate:

- facts
- preferences
- project state
- commitments
- decisions
- procedures
- conflicts
- provenance markers

The bundle must not include hidden instructions from retrieved memory. Procedural memory can
influence assistant behavior only if it is an active, reviewed procedural assertion.

## API and Events

### API

Rewrite memory APIs around the new concepts.

Required endpoints or equivalent slash commands:

- list active memory
- search memory
- inspect memory with provenance
- list candidates needing review
- approve candidate
- reject candidate
- correct assertion
- retract assertion
- delete assertion
- prioritize assertion
- deprioritize assertion
- inspect conflict set
- resolve conflict set
- inspect project state

No endpoint is required to preserve current response shape.

### Events

Required event concepts:

- memory evidence recorded
- memory candidate proposed
- memory review required
- memory candidate approved
- memory candidate rejected
- memory assertion activated
- memory assertion superseded
- memory assertion retracted
- memory assertion deleted
- memory conflict opened
- memory conflict resolved
- memory projection rebuilt
- memory recalled
- memory recall omitted item

Events must include ids, lifecycle state, and enough metadata to trace behavior.

## Files

### Source Files

- `src/ariel/app.py`
  - FastAPI route wiring and request handling only.
  - Remove old extraction, mutation, and recall helpers.
- `src/ariel/persistence.py`
  - New ORM models for canonical memory, review, conflict, salience, and projections.
  - Remove old memory item/revision models.
- `src/ariel/memory.py`
  - Domain services for assertion lifecycle, review decisions, conflict operations, and
    project state updates.
- `src/ariel/memory_extraction.py`
  - Candidate extraction and evidence normalization.
- `src/ariel/memory_retrieval.py`
  - Retrieval planner, ranking, and context bundle construction.
- `src/ariel/memory_projection.py`
  - Projection rebuild jobs for embeddings, compact context blocks, and graph caches.
- `src/ariel/response_contracts.py`
  - New memory API and event contracts.
- `src/ariel/action_runtime.py`
  - Keep existing fail-closed conflict posture aligned with memory conflicts.
- `src/ariel/config.py`
  - New `ARIEL_` settings only when a real implementation needs them.

### Migration Files

- Add one Alembic migration for the hard cutover.
- The migration removes old memory tables unless the same migration transforms them into
  the new canonical tables.
- Runtime code must not reference old memory table names after the migration.
- The migration creates new memory tables.
- The migration must not create compatibility views for old memory APIs.

### Test Files

- `tests/unit/test_memory_lifecycle.py`
- `tests/unit/test_memory_retrieval.py`
- `tests/unit/test_memory_conflicts.py`
- `tests/unit/test_memory_context_bundle.py`
- `tests/integration/test_memory_cutover_acceptance.py`
- `tests/integration/test_memory_project_state.py`
- `tests/integration/test_memory_review_api.py`
- `tests/integration/test_memory_projection_jobs.py`

## Acceptance Criteria

### Code Removal

- No production references to the old regex memory parser remain.
- No production references to old memory classes remain.
- No production references to old memory recall helper names remain.
- No old memory API compatibility layer remains.
- No lexical fallback path exists.
- No dual-write path exists.

### Persistence

- New schema exists in Alembic and SQLAlchemy.
- All lifecycle columns have named check constraints.
- All semantic JSONB fields have typed Pydantic ingress and egress models.
- Every active assertion links to evidence.
- Supersession, conflict membership, and deletion invariants are enforced by application
  code and tested.

### Behavior

- Explicit remember creates a reviewed or auto-approved assertion according to policy.
- Correction supersedes the prior assertion and activates the corrected assertion.
- Forget/delete removes assertion from normal recall and invalidates projections.
- Conflicting candidates open a conflict set.
- Unresolved conflicts appear as uncertainty in recall when relevant.
- Trusted user statements can create candidates.
- Untrusted tool, web, or quoted content cannot create active memory without review.
- Project state persists across session rotation without relying on a generic rolling
  summary.
- Recall is deterministic for identical inputs and database state.
- Recall returns provenance and rank reasons.
- Token budget overflow produces deterministic omissions and diagnostic events.

### API

- Users can list, inspect, search, correct, delete, prioritize, and deprioritize memories.
- Users can inspect candidate memories before approval when review is required.
- Users can inspect conflicts and resolution history.
- `/v1/memory` or its replacement returns the new projection only.
- Old response fields are not preserved unless they are also native fields in the new
  contract.

### Evaluation

- Add a local long-memory evaluation fixture with:
  - information extraction
  - multi-session reasoning
  - temporal reasoning
  - knowledge updates
  - abstention
  - conflict handling
  - project continuity
- The eval must include at least one case where vector similarity alone chooses the wrong
  memory and graph or temporal filtering fixes it.
- The eval must include at least one case where the correct answer is to abstain.
- The eval must include at least one deletion/correction case.

### Operations

- Projection jobs are observable.
- Dead-lettered projection jobs are inspectable and retryable.
- Memory recall emits diagnostics for omitted candidates and projection failures.
- No failed background job can silently corrupt active memory.

### Verification

- `make verify` passes.
- Integration tests pass against PostgreSQL.
- A grep check confirms removed legacy helper names and old memory class constants are not
  present in production code.

## Key Decisions

- PostgreSQL is canonical memory state.
- Embeddings are retrieval projections, not memory.
- Graph state is first-class, not inferred at prompt time.
- Temporal validity is first-class.
- Review is first-class.
- Conflict is first-class.
- Salience is first-class.
- Project state is separate from generic memory assertions but links to them.
- Background consolidation is required for production quality.
- The hot path reads memory and enqueues memory work; it does not run full consolidation.
- Provider-side state is not canonical.
- The cutover does not preserve old runtime APIs or schema.
- The implementation starts with Ariel-owned infrastructure, not a required dependency on
  Zep, Mem0, Letta, LangGraph, or any managed memory product.

## Reference Models

- LangGraph: short-term thread memory plus long-term semantic, episodic, and procedural
  memory namespaces.
- Letta/MemGPT: small in-context core memory plus large archival memory.
- Zep/Graphiti: temporal context graph with entities, relationships, validity windows,
  provenance episodes, and hybrid retrieval.
- Mem0: explicit memory lifecycle operations and production-oriented search/update/delete
  semantics.
- LongMemEval and LoCoMo: evaluation should cover cross-session, temporal, update,
  abstention, and long-range reasoning behavior.

## Implementation Plan

Implementation can be split into reviewable commits, but no intermediate state is
production-shippable until the old system is removed.

1. Add new schema, Pydantic models, and lifecycle services.
2. Add evidence recording and candidate extraction behind the new domain service.
3. Add review, correction, delete, conflict, and salience operations.
4. Add retrieval planner and context bundle contract.
5. Add projection jobs and deterministic rebuild behavior.
6. Rewrite memory APIs and events.
7. Replace model-loop memory recall with the new context bundle.
8. Remove old memory helpers, classes, tests, and schema.
9. Add acceptance, integration, and long-memory eval coverage.
10. Run `make verify` and legacy grep checks.
