# Memory SOTA Hard Cutover

## Scope

This document defines Ariel's hard cutover from command-pattern and keyword-ish
memory into a production memory architecture with typed canonical state, temporal
graph retrieval, evidence-backed recall, deterministic context assembly, and
operator-visible lifecycle controls.

The cutover covers durable user memory, project state, episodic evidence, action
and reasoning traces, procedural memory, candidate extraction, review, conflict
resolution, temporal validity, salience, retrieval projections, and context
assembly.

It does not cover model provider hosted memory, prompt caching, generic document
RAG, model fine-tuning, or transcript storage except when transcript content is
preserved as explicit memory evidence.

## Cutover Policy

- This is a hard cutover.
- Do not keep legacy memory code.
- Do not dual-write old and new memory stores.
- Do not add compatibility shims for old memory classes, keys, endpoints, events,
  database rows, or tests.
- Do not preserve the current `/v1/memory` response shape unless every field is
  native to the new contract.
- Do not fall back to command parsing, lexical keyword recall, transcript replay,
  provider-hosted memory, or long-context full-history replay when memory
  retrieval fails.
- Keyword/BM25 match may be one signal inside the new hybrid retrieval planner.
  It must not be a standalone fallback path.
- If memory context required by policy cannot be assembled, fail closed with a
  typed error and an auditable event.
- Context sections can be omitted only because deterministic scope, lifecycle, or
  budget rules exclude them.
- Existing narrow memory rows are not a compatibility contract.
- Any preservation of production memories must be a one-time import into the new
  canonical schema before cutover. Runtime code must not contain import adapters.
- No intermediate state is production-shippable while old and new memory systems
  are both reachable.

## Target Behavior

### User Experience

- Ariel remembers durable preferences, profile facts, commitments, decisions,
  project state, and recurring procedural instructions across sessions.
- Ariel distinguishes working memory, semantic memory, episodic memory, and
  procedural memory.
- Ariel can answer:
  - "what do you remember?"
  - "why do you think that?"
  - "where did that come from?"
  - "what changed?"
  - "forget this"
  - "correct this"
  - "prioritize this"
  - "show history"
- Ariel surfaces unresolved contradictions as uncertainty instead of silently
  choosing one fact.
- Ariel can maintain long-range project state without replaying full transcripts
  or relying on one rolling summary.
- Ariel remembers only useful high-level details. It does not treat memory as a
  store for secrets, arbitrary transcript chunks, large templates, or verbatim
  blocks.
- Ariel exposes memory controls that are understandable to an operator:
  inspect, approve, reject, correct, retract, delete, prioritize, deprioritize,
  resolve conflicts, export, and audit history.

### Write Behavior

- Every memory write starts from evidence.
- Evidence records identify source turn, session, actor, content class, trust
  boundary, timestamp, source URI or artifact when applicable, and redaction
  posture.
- Model extraction creates candidate assertions, episodes, relationship facts,
  and procedure proposals. It does not directly mutate active trusted memory.
- Review policy promotes, rejects, routes, conflicts, or queues candidates.
- Corrections supersede prior assertions. They do not overwrite history.
- Delete and privacy deletion remove or invalidate all derived projections.
- Tool outputs, web content, files, quoted user-provided text, and assistant
  guesses cannot become trusted memory without explicit user or system-owned
  review.
- Explicit user memory commands are one ingress path, not the canonical
  extraction engine.
- Background extraction is the default. The primary turn path records the turn
  and enqueues memory work; it does not run full consolidation.

### Recall Behavior

- The context builder assembles memory in deterministic sections:
  - pinned core memory
  - current project state
  - active commitments and unresolved decisions
  - relevant semantic assertions
  - relevant episodic evidence snippets
  - relevant action and reasoning traces
  - reviewed procedural memory
  - unresolved conflicts that affect the turn
- Retrieval uses structured filters, semantic similarity, BM25/full-text match,
  entity match, graph distance, temporal validity, salience, confidence,
  verification age, source quality, task relevance, and reranking.
- Every recalled item includes id, lifecycle state, source evidence ids,
  evidence snippet or evidence reference, rank reason, rank score, validity
  interval, source trust, and projection version.
- Token budget enforcement is deterministic and emits omitted-item diagnostics.
- Recall never includes superseded, rejected, retracted, or deleted assertions in
  normal context.
- Recall never includes conflicted facts as if they were active facts. It returns
  conflict warnings and candidate evidence for uncertainty handling.
- The model sees a structured memory bundle. It does not see raw database rows.

## Structure

### Memory Types

- Working memory: active thread state, recent turns, scratch state, open tool
  results, in-flight jobs, and action state. This is lossless and scoped.
- Semantic memory: durable typed facts about users, projects, repos, preferences,
  commitments, decisions, procedures, and domain concepts.
- Episodic memory: timestamped evidence episodes, source snippets, task events,
  action outcomes, and decision history.
- Procedural memory: reviewed operating rules that can influence Ariel's future
  behavior.
- Project memory: compact project state, active risks, milestones, decisions,
  open questions, and repo-specific conventions.
- Reasoning memory: selected past task traces that explain how Ariel solved or
  failed to solve a task, used as examples and diagnostics.

### Canonical Records

Canonical state must be normalized enough to inspect and audit:

- evidence records
- entity records
- relationship records
- semantic assertion records
- episodic episode records
- action/reasoning trace records
- procedural rule records
- project state snapshots
- assertion-evidence links
- review decisions
- conflict sets and members
- salience records
- projection jobs
- memory versions and deletion/redaction records

### Projections

The following are rebuildable projections, not canonical memory:

- vector embeddings
- BM25 or PostgreSQL full-text indexes
- entity mention indexes
- entity-linking caches
- graph traversal caches
- temporal search indexes
- reranker feature rows
- compact pinned context blocks
- project state context blocks
- procedure context blocks
- evaluation fixtures derived from canonical evidence

## Architecture

### Canonical Layers

1. Evidence log

   Append-only source evidence for memory claims. Evidence can be redacted or
   privacy-deleted through explicit workflows, but normal correction creates new
   evidence and supersession history.

2. Semantic assertions

   Typed claims derived from evidence:

   - subject
   - predicate
   - object value
   - assertion type
   - scope
   - confidence
   - temporal validity
   - lifecycle
   - source evidence links
   - extraction model and prompt version

3. Temporal memory graph

   Entities and relationships for users, projects, repos, artifacts, files,
   tasks, commitments, decisions, risks, preferences, procedures, people, and
   organizations. Relationships have provenance and validity windows.

4. Episodic and reasoning layer

   Episodes capture what happened, when, where it came from, and what outcome it
   produced. Reasoning traces capture selected tool paths, failures, user
   corrections, and successful patterns.

5. Review and conflict control plane

   Candidate review state, promotion decisions, rejection reasons, conflict
   sets, resolution winners, review actor ids, and audit history.

6. Salience model

   Stored, inspectable rank inputs:

   - user priority
   - project relevance
   - open commitment linkage
   - graph centrality
   - recency
   - frequency
   - confidence
   - verification age
   - source trust
   - retrieval feedback
   - user correction history

7. Retrieval projection layer

   Rebuildable indexes and summaries. Projection failure must be observable and
   must not silently corrupt active memory.

8. Retrieval planner

   The only runtime owner of memory recall decisions. It combines scope filters,
   semantic search, BM25, graph traversal, temporal filtering, salience, and
   reranking into one deterministic recall bundle.

9. Context compiler

   Converts planner output into bounded context sections with ids, snippets,
   provenance, uncertainty markers, and omission diagnostics.

### Request Flow

1. The request is admitted and the current session/thread state is loaded.
2. The retrieval planner builds memory context from canonical memory and healthy
   projections.
3. The context compiler emits a bounded, structured context bundle.
4. The model receives only the compiled context bundle and current user message.
5. The assistant response, tool calls, action outcomes, and surfaced sources are
   persisted.
6. Background memory tasks are enqueued for extraction, consolidation, graph
   update, salience recomputation, and projection rebuild.
7. Review policy decides whether candidates become active, require review,
   conflict, or are rejected.
8. The next turn reads only canonical memory plus current projection versions.

### Background Work

- Extraction, reflection, consolidation, salience recomputation, conflict
  detection, entity linking, graph edge maintenance, context block generation,
  and projection rebuilds run outside the primary response path.
- Background tasks are durable PostgreSQL tasks.
- Task claiming uses `FOR UPDATE SKIP LOCKED`.
- Mutations use transaction boundaries that preserve memory invariants.
- Logical resources use advisory locks when concurrent updates can target the
  same memory subject, entity, project, or conflict set.
- Background task failure never invokes legacy recall or legacy extraction.
- Dead-lettered tasks are inspectable and retryable.

### Retrieval Planner

The planner receives:

- user id
- organization or workspace id when present
- session id
- current project id when known
- current turn text
- tool/action context
- open jobs and commitments
- allowed memory scopes
- maximum context budget
- current time

The planner returns:

- pinned context blocks
- project state blocks
- ranked semantic assertions
- ranked episodic evidence snippets
- ranked reasoning traces
- reviewed procedural rules
- conflict warnings
- omitted-item diagnostics
- projection health diagnostics

### Ranking

Ranking is deterministic for identical inputs and database state.

Ranking inputs:

- structured scope filters
- semantic vector similarity
- BM25/full-text score
- entity match
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
- reranker score

Ties are resolved by stable ids after semantic score, salience, priority, and
timestamp.

## Final State

- `src/ariel/app.py` contains route wiring and request handling only.
- `src/ariel/memory.py` contains domain lifecycle operations, not extraction,
  projection, or retrieval planner logic.
- `src/ariel/memory_extraction.py` owns evidence normalization and candidate
  extraction.
- `src/ariel/memory_projection.py` owns projection job execution and rebuilds.
- `src/ariel/memory_retrieval.py` owns retrieval planning, ranking, and context
  assembly.
- `src/ariel/persistence.py` owns ORM models for canonical memory and
  projections.
- Memory APIs expose new contracts only.
- Memory events expose new event concepts only.
- Old command-pattern extraction is removed from production recall/write paths.
- Old memory response shapes, constants, table names, helper names, tests, and
  compatibility assumptions are gone.
- The system has local long-memory evals that run against PostgreSQL and fail
  when recall regresses.

## Rules

- PostgreSQL is canonical memory state.
- Provider-hosted memory is never canonical.
- Embeddings are projections, not memory.
- Graph state is first-class, not inferred at prompt time.
- Temporal validity is first-class.
- Evidence snippets are first-class recall items.
- Review is first-class.
- Conflict is first-class.
- Deletion and redaction are first-class.
- Salience is first-class and inspectable.
- Project state is separate from generic memory assertions but links to them.
- Procedural memory can affect assistant behavior only when active and reviewed.
- Untrusted content cannot create active trusted memory without review.
- Hidden instructions from retrieved memory must not bypass policy.
- Memory context must be bounded and deterministic.
- Memory failures must be typed, surfaced, and auditable.
- Managed products such as Zep, Mem0, Letta, LangGraph, or Neo4j Agent Memory are
  reference models, not required runtime dependencies.

## Goals

- Build a robust semantic personal and project model.
- Support multi-session project continuity.
- Preserve provenance from derived memory back to source evidence.
- Represent changing facts with temporal validity windows.
- Support temporal and relationship-aware recall.
- Detect and represent conflicts explicitly.
- Make memory reviewable, correctable, deletable, explainable, exportable, and
  auditable.
- Keep context bounded without losing long-range continuity.
- Keep memory safe under concurrency, retries, process restarts, and background
  worker failure.
- Use hybrid retrieval instead of vector-only or keyword-only recall.
- Evaluate memory behavior, not just row persistence.

## Non-Goals

- No provider-hosted memory as canonical memory.
- No vector-only memory system.
- No keyword-only memory system.
- No full-transcript replay strategy.
- No rolling-summary-only strategy.
- No regex or command parser as canonical extraction.
- No compatibility with the current `/v1/memory` response shape.
- No compatibility with current memory event names.
- No external managed memory dependency as a requirement for correctness.
- No model weight updates or fine-tuning.
- No generic enterprise document RAG in this cutover.
- No partial shipping state where both old and new memory systems are reachable.
- No memory of secrets unless explicitly approved by policy and protected by a
  dedicated secret-handling flow.

## Data Model

Exact names can change during implementation, but the final schema must express
these concepts directly.

### Required Tables

- `memory_evidence`
- `memory_entities`
- `memory_relationships`
- `memory_assertions`
- `memory_assertion_evidence`
- `memory_episodes`
- `memory_reasoning_traces`
- `memory_procedures`
- `memory_reviews`
- `memory_conflict_sets`
- `memory_conflict_members`
- `memory_salience`
- `memory_versions`
- `memory_projection_jobs`
- `memory_embedding_projections`
- `memory_keyword_projections`
- `memory_entity_projections`
- `memory_graph_projections`
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

Evidence:

- `available`
- `redacted`
- `privacy_deleted`

### Required Invariants

- Every active assertion links to at least one evidence record.
- Exactly one active assertion can own a single-valued predicate for a subject and
  scope.
- Multi-valued predicates must declare that they are multi-valued in the schema
  or type registry.
- A conflicted assertion must belong to an open conflict set.
- A superseded assertion must identify its superseding assertion.
- A retracted, rejected, deleted, or privacy-deleted assertion cannot be recalled
  in normal context.
- Relationship edges must link to evidence and validity intervals.
- A projection row must identify the canonical row and projection version that
  produced it.
- A context block must be rebuildable from canonical assertions, episodes, graph
  records, and project state.
- Deleting or redacting canonical memory invalidates all derived projections.
- No raw anonymous dictionaries cross module boundaries for typed memory
  concepts.

## Types

Use Pydantic models for semantic values and JSONB payloads.

Required typed concepts:

- `MemorySubject`
- `MemoryPredicate`
- `MemoryObject`
- `MemoryAssertionValue`
- `MemoryRelationship`
- `MemoryEpisode`
- `MemoryReasoningTrace`
- `TemporalScope`
- `ValidityInterval`
- `EvidenceRef`
- `EvidenceSnippet`
- `ReviewDecision`
- `ConflictResolution`
- `SalienceSignals`
- `RecallReason`
- `ProjectionVersion`
- `ProjectState`
- `ContextBlock`
- `MemoryContextBundle`

## API and Events

### API

Rewrite memory APIs around the new concepts.

Required endpoints or equivalent slash commands:

- list active memory
- search memory
- inspect memory with provenance
- inspect evidence
- inspect memory history
- list candidates needing review
- approve candidate
- reject candidate
- correct assertion
- retract assertion
- delete assertion
- redact evidence
- export memory
- import one-time cutover memory
- prioritize assertion
- deprioritize assertion
- inspect conflict set
- resolve conflict set
- inspect project state
- inspect projection health
- retry projection job

No endpoint preserves current response shape for compatibility.

### Events

Required event concepts:

- memory evidence recorded
- memory evidence redacted
- memory candidate proposed
- memory review required
- memory candidate approved
- memory candidate rejected
- memory assertion activated
- memory assertion superseded
- memory assertion retracted
- memory assertion deleted
- memory relationship linked
- memory conflict opened
- memory conflict resolved
- memory projection queued
- memory projection rebuilt
- memory projection failed
- memory recalled
- memory recall omitted item
- memory import completed
- memory export completed

Events must include ids, lifecycle state, projection version when relevant, and
enough metadata to reconstruct behavior.

## Files

### Source Files

- `src/ariel/app.py`
  - FastAPI route wiring and request handling only.
  - Remove old extraction, mutation, and recall helpers.
- `src/ariel/persistence.py`
  - ORM models for canonical memory, review, conflict, salience, versions, and
    projections.
  - Remove old memory item/revision models.
- `src/ariel/memory.py`
  - Domain services for assertion lifecycle, review decisions, conflict
    operations, project state operations, and deletion/redaction workflows.
- `src/ariel/memory_models.py`
  - Pydantic models and owned types for memory values, evidence, recall, and
    projection payloads.
- `src/ariel/memory_extraction.py`
  - Evidence normalization, candidate extraction, entity extraction, relationship
    extraction, and procedure proposal generation.
- `src/ariel/memory_projection.py`
  - Projection rebuild jobs for embeddings, BM25/full-text rows, entity indexes,
    graph caches, compact context blocks, and project state blocks.
- `src/ariel/memory_retrieval.py`
  - Retrieval planner, ranking, hybrid search, temporal filtering, graph
    traversal, reranking, and context bundle construction.
- `src/ariel/memory_eval.py`
  - Local long-memory evaluation runner and fixtures.
- `src/ariel/response_contracts.py`
  - New memory API and event contracts.
- `src/ariel/worker.py`
  - Executes durable memory projection and extraction tasks.
- `src/ariel/action_runtime.py`
  - Emits action/reasoning evidence and preserves fail-closed conflict posture.
- `src/ariel/config.py`
  - New `ARIEL_` settings only when a real implementation needs them.

### Migration Files

- Add one Alembic migration for the hard cutover.
- The migration removes old memory tables unless the same migration transforms
  them into the new canonical tables.
- Runtime code must not reference old memory table names after the migration.
- The migration creates new memory tables and projection tables.
- The migration must not create compatibility views for old memory APIs.

### Test Files

- `tests/unit/test_memory_models.py`
- `tests/unit/test_memory_lifecycle.py`
- `tests/unit/test_memory_extraction.py`
- `tests/unit/test_memory_projection.py`
- `tests/unit/test_memory_retrieval.py`
- `tests/unit/test_memory_conflicts.py`
- `tests/unit/test_memory_context_bundle.py`
- `tests/integration/test_memory_cutover_acceptance.py`
- `tests/integration/test_memory_project_state.py`
- `tests/integration/test_memory_review_api.py`
- `tests/integration/test_memory_projection_jobs.py`
- `tests/integration/test_memory_temporal_graph.py`
- `tests/integration/test_memory_privacy_deletion.py`
- `tests/integration/test_memory_eval_acceptance.py`

## Acceptance Criteria

### Code Removal

- No production references to the old regex memory parser remain.
- No production references to old memory classes remain.
- No production references to old memory recall helper names remain.
- No old memory API compatibility layer remains.
- No lexical fallback path exists.
- No transcript replay fallback exists.
- No provider-hosted memory fallback exists.
- No dual-write path exists.

### Persistence

- New schema exists in Alembic and SQLAlchemy.
- All lifecycle columns have named check constraints.
- All semantic JSONB fields have typed Pydantic ingress and egress models.
- Every active assertion links to evidence.
- Every active relationship links to evidence.
- Supersession, conflict membership, redaction, projection invalidation, and
  deletion invariants are enforced by application code and tested.

### Extraction

- Explicit memory commands create reviewed or auto-approved assertions according
  to policy.
- Non-command user statements can create candidates through model extraction.
- Untrusted tool, web, file, quoted, and assistant content cannot create active
  memory without review.
- Extraction records model name, prompt version, confidence, source evidence, and
  parse diagnostics.
- Extraction can propose entities, relationships, semantic assertions, episodes,
  project state updates, and procedure changes.

### Retrieval

- Recall is deterministic for identical inputs and database state.
- Recall uses hybrid retrieval with structured filters, vector similarity,
  BM25/full-text, entity matching, graph traversal, temporal validity, salience,
  and reranking.
- Recall returns provenance, evidence snippets, rank reasons, rank scores,
  validity windows, and projection versions.
- Recall emits deterministic omitted-item diagnostics under budget pressure.
- Unresolved conflicts appear as uncertainty in context when relevant.
- Temporal questions use validity intervals and event times rather than latest row
  order alone.
- Graph questions can use relationship distance and entity neighborhoods.
- Keyword-only and vector-only retrieval tests fail the acceptance suite.

### Behavior

- Correction supersedes prior assertions and activates the corrected assertion.
- Forget/retract removes assertion from normal recall and invalidates projections.
- Delete/privacy deletion invalidates projections and prevents future recall.
- Conflicting candidates open a conflict set.
- Project state persists across session rotation without relying on a generic
  rolling summary.
- Procedural memory influences behavior only when active and reviewed.
- Evidence snippets explain why Ariel recalled a memory.

### API

- Users can list, inspect, search, correct, delete, redact, export, prioritize,
  and deprioritize memories.
- Users can inspect candidate memories before approval when review is required.
- Users can inspect conflicts and resolution history.
- Users can inspect evidence and memory versions.
- `/v1/memory` or its replacement returns the new projection only.
- Old response fields are not preserved unless they are native fields in the new
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
  - relationship reasoning
  - deletion and redaction
  - procedural memory adherence
- The eval includes at least one case where vector similarity alone chooses the
  wrong memory and graph or temporal filtering fixes it.
- The eval includes at least one case where keyword matching alone chooses the
  wrong memory and semantic/entity retrieval fixes it.
- The eval includes at least one case where the correct answer is to abstain.
- The eval includes at least one deletion/correction case.
- The eval records accuracy, retrieval precision, omitted relevant memories,
  context tokens, projection latency, and extraction latency.

### Operations

- Projection jobs are observable.
- Dead-lettered projection jobs are inspectable and retryable.
- Memory recall emits diagnostics for omitted candidates and projection failures.
- Memory import/export is observable.
- Redaction and deletion produce auditable records.
- No failed background job can silently corrupt active memory.

### Verification

- `make verify` passes.
- Integration tests pass against PostgreSQL.
- A grep check confirms removed legacy helper names and old memory class
  constants are not present in production code.
- A grep check confirms no runtime references to old memory table names remain.
- Memory eval acceptance tests pass before the cutover branch is considered
  ready.

## Key Decisions

- PostgreSQL remains canonical memory state.
- Graph memory is first-class in Ariel's schema. A future external graph database
  can be a projection backend only if Postgres remains canonical.
- Embeddings, BM25 rows, graph caches, and summaries are projections.
- Hybrid retrieval is required.
- Temporal validity is required.
- Evidence snippets are required.
- Review, conflict, salience, deletion, and redaction are required.
- Background consolidation is required for production quality.
- The hot path reads memory and enqueues memory work; it does not run full
  consolidation.
- Explicit user commands remain useful UX, but model extraction and review own
  canonical memory creation.
- Provider-side memory is not canonical.
- The cutover does not preserve old runtime APIs or schema.
- The implementation starts with Ariel-owned infrastructure, not a required
  dependency on Zep, Mem0, Letta, LangGraph, Neo4j Agent Memory, or any managed
  memory product.

## Reference Models

- LangGraph: short-term thread memory plus long-term semantic, episodic, and
  procedural namespaces.
- Letta/MemGPT: core in-context memory, recall memory, archival memory, and
  self-editing memory tools.
- Zep/Graphiti: temporal context graph with entities, relationships, validity
  windows, provenance episodes, and hybrid retrieval.
- Mem0: explicit memory lifecycle operations, entity linking, hybrid retrieval,
  graph memory, reranking, and production API surfaces.
- Neo4j Agent Memory: graph-native short-term, long-term, and reasoning memory.
- GraphRAG: local, global, and DRIFT-style graph retrieval for static document
  corpora; useful as a retrieval reference, not as Ariel's canonical memory.
- LongMemEval and LoCoMo: evaluation coverage for cross-session, temporal,
  update, abstention, multi-hop, and long-range reasoning behavior.

## Implementation Plan

Implementation can be split into reviewable commits, but no intermediate state is
production-shippable until the old system is removed.

1. Add typed memory models and the new canonical schema.
2. Add one-time import tooling for any production memories worth preserving.
3. Remove old memory parser, helpers, schema references, events, and API
   compatibility assumptions from production code.
4. Add evidence recording and candidate extraction behind the new domain service.
5. Add entity and relationship extraction with temporal validity.
6. Add review, correction, delete, redaction, conflict, and salience operations.
7. Add durable background tasks for extraction, projection, consolidation, and
   retry/dead-letter behavior.
8. Add projection rebuilders for embeddings, BM25/full-text, entity indexes,
   graph caches, context blocks, and project state blocks.
9. Add retrieval planner and context bundle contract.
10. Rewrite memory APIs and events around the new contracts.
11. Replace model-loop memory recall with the new context bundle.
12. Add acceptance, integration, and long-memory eval coverage.
13. Run `make verify`, PostgreSQL integration tests, eval acceptance, and legacy
    grep checks.
