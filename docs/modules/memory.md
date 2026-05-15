# Memory North Star Hard Cutover

## Scope

This document defines Ariel's hard cutover to a gold-standard long-memory
architecture for personal assistance, coding continuity, proactive behavior, and
operator-visible audit.

The design combines the useful parts of current frontier systems:

- Claude Code-style compact hot index plus lazy topic memory.
- Letta/MemGPT-style core, recall, and archival memory separation.
- LangMem-style semantic, episodic, and procedural memory taxonomy.
- Zep/Graphiti-style temporal graph, provenance, and validity windows.
- Cursor/Windsurf/Devin-style memory inbox, rules, and knowledge promotion.
- Cline-style project memory bank discipline.
- Aider/Sourcegraph-style code map and symbol-aware retrieval.
- ChatGPT/Gemini/Claude-style user controls for inspect, edit, delete, export,
  and temporary/no-memory modes.

PostgreSQL remains Ariel's canonical memory state. Markdown files, compact
indexes, embeddings, graph caches, and summaries are projections.

This document covers memory storage, extraction, review, consolidation, recall,
context assembly, privacy, evaluation, APIs, AI-mediated operations, worker jobs,
proactive integration, and hard-cutover cleanup.

Memory follows [../ai-first.md](../ai-first.md): AI owns extraction judgment,
relevance judgment, consolidation judgment, omission judgment, relationship
interpretation, and continuity interpretation. Deterministic code owns
canonical state, schemas, lifecycle invariants, policy, access, budgets,
provenance, audit, and fail-closed behavior.

## Cutover Policy

- This is a hard cutover.
- Do not keep legacy memory code.
- Do not dual-write old and new memory stores.
- Do not keep compatibility shims for old memory classes, helpers, events,
  endpoints, response fields, database tables, or tests.
- Do not preserve the old `/v1/memory` response shape unless every field is
  native to the new contract.
- Do not fall back to command parsing, lexical keyword recall, vector-only
  recall, transcript replay, provider-hosted memory, markdown-only memory,
  rolling-summary-only memory, or long-context full-history replay.
- Do not let any path directly create active trusted memory without evidence,
  lifecycle policy, review policy, projection invalidation, and audit.
- Do not let proactive decisions bypass the same memory lifecycle used by user
  turns.
- Do not let untrusted tool, web, file, or assistant content become active
  trusted memory without explicit review policy.
- Do not silently omit required memory context. Fail closed with a typed error
  and an auditable event when policy requires memory and memory cannot be
  assembled.
- Deterministic code may omit candidates only because access, lifecycle, scope,
  trust, or budget rails exclude them. AI curation may omit for relevance only
  with an auditable reason.
- Any preservation of production memories must be a one-time import into the new
  canonical schema before the cutover. Runtime code must not contain import
  adapters.
- No intermediate state is production-shippable while old and new memory systems
  are both reachable.

## Thesis

Memory is not personalization magic. Memory is an evidence-backed operating
system for continuity.

Every durable memory moves through this lifecycle:

1. Evidence is recorded.
2. AI proposes typed memory candidates.
3. Deterministic rails validate shape, scope, trust, sensitivity, and authority.
4. Review policy promotes, rejects, routes, conflicts, or queues candidates.
5. Canonical state changes produce versions, events, and projection jobs.
6. Retrieval produces bounded candidates with provenance and diagnostics.
7. AI curation selects the context that matters now and explains omissions.
8. Consolidation periodically merges, supersedes, prunes, and rebuilds hot
   context projections without erasing audit history.

## Goals

- Preserve cross-session continuity without replaying transcripts.
- Preserve project and coding continuity across chats, sessions, branches, and
  proactive work.
- Make memory inspectable, editable, correctable, deletable, exportable, and
  auditable.
- Keep memory scoped by user, project, repo, thread, proactive case, source, and
  temporary/no-memory mode.
- Represent uncertainty, contradiction, staleness, and temporal validity
  explicitly.
- Preserve source provenance for every active memory and every derived
  projection.
- Separate canonical memory from retrieval projections.
- Keep the turn hot path fast by enqueueing extraction, projection, and
  consolidation work.
- Make memory recall bounded, explainable, and observable.
- Preserve negative knowledge: failed approaches, rejected hypotheses, checked
  files, insufficient tests, and user corrections.
- Turn repeated successful behavior into reviewed procedural memory.
- Turn durable team or repo knowledge into versionable rule projections when
  appropriate.
- Keep memory safe under retries, concurrency, process restarts, background job
  failure, redaction, and privacy deletion.
- Evaluate memory quality with product-specific long-memory tests, not only row
  persistence tests.

## Non-Goals

- No provider-hosted memory as canonical memory.
- No model fine-tuning.
- No prompt cache as memory.
- No generic enterprise document RAG in this cutover.
- No hidden personalization store.
- No secret vault replacement.
- No transcript archive marketed as memory.
- No vector database as canonical memory.
- No graph database as canonical memory.
- No markdown directory as canonical memory.
- No deterministic product brain that decides memory relevance.
- No backward compatibility for old memory APIs or response shapes.
- No external dependency on Zep, Mem0, Letta, LangGraph, Neo4j, or any managed
  memory product for correctness.
- No multi-agent theater. Add specialized model calls only when the input,
  authority, and output contract are bounded.

## Target Behavior

### User Experience

- Ariel remembers durable user preferences, profile facts, commitments,
  decisions, project state, recurring workflows, and important corrections.
- Ariel distinguishes working memory, hot core memory, semantic memory,
  episodic memory, procedural memory, project memory, reasoning/action memory,
  and archival evidence.
- Ariel can answer:
  - "what do you remember?"
  - "what did you recall for this turn?"
  - "why do you think that?"
  - "where did that come from?"
  - "what changed?"
  - "what are you unsure about?"
  - "what did you ignore?"
  - "forget this"
  - "correct this"
  - "never remember this type of thing"
  - "prioritize this"
  - "show history"
  - "export memory"
- Ariel surfaces unresolved contradictions as uncertainty instead of silently
  choosing one claim.
- Ariel admits when relevant memory is unavailable, stale, deleted, redacted, or
  outside the current scope.
- Ariel exposes a memory inbox with approve, edit, reject, merge, mark stale,
  and never-remember controls.
- Ariel exposes recall diagnostics: selected memories, omitted memories,
  candidate count, scope filters, projection health, source evidence, curation
  rationale, and conflict warnings.
- Ariel supports temporary/no-memory sessions that still preserve operational
  audit but do not extract or recall user memory.
- Ariel never stores secrets as ordinary memory. Secret-like content is redacted,
  blocked, or routed to a dedicated future secret-handling flow.

### Coding And Project Continuity

- Ariel remembers repo conventions, architecture decisions, active risks,
  accepted patterns, known bad paths, command recipes, test gates, review
  feedback, CI quirks, deployment constraints, and file ownership boundaries.
- Ariel remembers execution-critical state, not just summaries:
  - files already inspected
  - commands already run
  - tests that passed or failed
  - hypotheses rejected
  - assumptions corrected by the user
  - blockers and next actions
  - branch, PR, issue, and incident context
- Ariel promotes repeated coding lessons into reviewed procedural memory or
  versionable project-rule projections.
- Ariel can export a repo memory pack for other tools as a projection, not as
  canonical state.
- Ariel can ingest project rule files as evidence with provenance and trust
  labels, not as unreviewed active memory.

### Proactive Behavior

- Proactive observations can propose memory candidates.
- Proactive decisions cannot directly create active memory.
- Proactive "remember" actions go through evidence recording, candidate
  extraction or structured proposal, review policy, conflict detection,
  projection invalidation, and audit.
- Proactive corrections feed memory as user-correction evidence and can
  supersede prior memories or procedures.
- Proactive recurring patterns can become procedural memory only after review.

### Failure Behavior

- If memory extraction fails, the turn succeeds only if memory extraction is
  non-required for that path, and the failure is auditable.
- If memory curation is required and fails, the request fails closed with a
  typed AI judgment error.
- If projections are stale or failed, retrieval exposes projection health and
  uses only valid canonical state plus healthy projections. It does not use
  legacy fallback recall.
- If deletion or redaction affects recalled memory, future recall excludes the
  invalidated memory and all invalidated projections.

## Memory Structure

### Memory Types

- Working memory: active thread state, recent turns, open tool results,
  in-flight jobs, action state, pending approvals, and current scratch context.
- Hot core memory: a compact, always-considered context block containing
  currently important user, project, scope, conflict, and procedure pointers.
- Semantic memory: typed durable facts about users, projects, repos, artifacts,
  preferences, commitments, decisions, risks, people, organizations, and domain
  concepts.
- Episodic memory: timestamped evidence episodes, source snippets, task events,
  action outcomes, decision history, provider events, and proactive observations.
- Procedural memory: reviewed instructions about how Ariel should work in a
  scope.
- Project memory: compact state for active projects, repos, branches, PRs,
  issues, milestones, open questions, and risks.
- Reasoning/action memory: selected traces of successful paths, failed paths,
  diagnostics, user corrections, and action outcomes.
- Negative memory: durable "do not repeat" knowledge such as rejected
  approaches, invalid assumptions, unsafe operations, already-checked areas, and
  insufficient evidence.
- Archival evidence: source records and artifacts that are not injected by
  default but remain inspectable and retrievable by explicit tools.

### Hot Index

The hot index is Ariel's version of Claude Code's `MEMORY.md`, but it is a
rebuildable projection from canonical state.

The hot index:

- is compact enough to consider on every turn
- is scoped to the active user, project, repo, thread, and task where possible
- contains stable pointers, not large details
- names topic blocks that may be relevant
- includes active conflict warnings
- includes active commitments and deadlines
- includes pinned preferences and high-priority procedures
- includes "do not repeat" items that affect current work
- includes projection version and rebuild timestamp
- never contains secrets or large verbatim content
- is rebuilt by consolidation and explicit projection jobs

Budget target:

- default hot index budget: 1,500 tokens
- hard maximum hot index budget: 2,500 tokens
- every item must carry ids or topic pointers so details can be fetched on
  demand

### Topic Blocks

Topic blocks are Ariel's lazy memory files, but canonical state remains in
PostgreSQL.

Required topic block families:

- `user-profile`
- `user-preferences`
- `active-projects`
- `repo-conventions`
- `architecture-decisions`
- `commitments`
- `procedures`
- `negative-knowledge`
- `recent-failures`
- `proactive-patterns`
- `external-connectors`
- `open-risks`
- `resolved-conflicts`

Topic blocks:

- are rebuilt projections
- contain concise summaries plus canonical ids
- are selected by AI curation or explicit inspection
- can be exported to markdown for human review or cross-tool use
- cannot be edited as canonical state unless edits flow back through memory
  mutation APIs

### Canonical Records

Canonical state must be normalized enough to inspect and audit:

- evidence records
- source artifact records
- entity records
- relationship records
- semantic assertion records
- assertion-evidence links
- episodic episode records
- reasoning/action trace records
- procedural rule records
- negative memory records or typed assertions
- project state snapshots
- topic definitions
- topic membership records
- review decisions
- conflict sets and members
- salience and feedback records
- retention, sensitivity, and scope records
- memory versions
- deletion and redaction records
- projection jobs
- AI judgment records

### Projections

The following are rebuildable projections:

- vector embeddings
- PostgreSQL full-text or BM25 indexes
- entity mention indexes
- graph traversal caches
- temporal search indexes
- symbol and repo map projections
- reranker feature rows
- hot index blocks
- topic blocks
- project state blocks
- procedure blocks
- markdown export files
- cross-agent memory packs
- evaluation fixtures derived from canonical evidence

Projection failure must be observable and must not silently corrupt active
memory.

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
   - sensitivity
   - temporal validity
   - lifecycle state
   - source evidence links
   - extraction model
   - extraction prompt version

3. Temporal memory graph

   Entities and relationships for users, projects, repos, artifacts, files,
   tasks, commitments, decisions, risks, preferences, procedures, people, and
   organizations. Relationships have evidence, confidence, validity windows, and
   lifecycle state.

4. Episodic and reasoning layer

   Episodes capture what happened, when, where it came from, and what outcome it
   produced. Reasoning/action traces capture run callable paths, failures, user
   corrections, successful patterns, action receipts, and diagnostic decisions.

5. Review and conflict control plane

   Candidate review state, promotion decisions, rejection reasons, merge
   decisions, conflict sets, resolution winners, review actor ids, and audit
   history.

6. Salience and feedback layer

   Stored, inspectable signals:

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
   - negative feedback
   - deletion and never-remember preferences

7. Retrieval projection layer

   Rebuildable indexes and summaries. Projection rows identify the canonical
   source row, projection kind, projection version, source versions, and build
   timestamp.

8. Candidate retrieval service

   Gathers eligible candidates and provenance. It combines scope filters,
   lifecycle filters, semantic search, full-text search, entity match, graph
   traversal, temporal filtering, salience, source trust, project linkage, and
   budget ordering into one bounded candidate pool. It does not decide final
   relevance.

9. AI memory curator

   Decides which candidates matter for the current turn, which details to
   include, how conflicts affect uncertainty, and which items to omit. It
   returns bounded context sections with ids, snippets, provenance, uncertainty
   markers, and omission diagnostics.

10. Consolidation service

    Periodically merges duplicates, proposes supersessions, updates topic
    blocks, rebuilds the hot index, detects stale claims, opens conflicts, and
    proposes durable procedures. It does not bypass lifecycle or review policy.

### Write Flow

1. Ingress records source evidence from user turns, captures, tool outputs,
   attachments, workspace events, proactive observations, action outcomes, and
   corrections.
2. The hot path enqueues memory extraction or structured memory proposal tasks
   unless the session is no-memory.
3. AI extraction receives bounded evidence and returns typed candidate payloads.
4. Deterministic validation rejects malformed candidates and labels scope,
   sensitivity, trust, source, and policy posture.
5. Review policy chooses:
   - auto-approve
   - needs user review
   - needs operator review
   - reject
   - route to conflict
   - route to consolidation
6. Candidate activation records versions, evidence links, salience rows, events,
   and projection jobs.
7. Conflicting single-valued claims open a conflict set instead of silently
   overwriting.
8. Corrections create new evidence and supersede old assertions.
9. Retraction removes a memory from normal recall but keeps audit.
10. Privacy deletion invalidates canonical content and derived projections.

### Recall Flow

1. The request is admitted and scope is resolved.
2. Access, no-memory, retention, sensitivity, and lifecycle rails determine
   eligible memory scopes.
3. The hot index for the scope is loaded.
4. Candidate retrieval gathers bounded candidates from canonical memory and
   healthy projections.
5. AI curation selects relevant memory, conflict warnings, topic blocks, and
   omitted-item diagnostics.
6. Deterministic budget enforcement validates the final bundle shape and token
   limit.
7. The master model receives only the curated memory bundle, current working
   context, and user request.
8. The recall decision is recorded as an AI judgment with selected ids, omitted
   ids, rationale, uncertainty, prompt version, provider response id, and
   projection health.
9. The assistant response and action outcomes become new evidence for future
   memory work when policy allows.

### Consolidation Flow

Consolidation is Ariel's audited version of Claude Code AutoDream.

Triggers:

- session rotation
- memory candidate backlog
- projection drift
- conflict backlog
- user request
- scheduled worker cadence
- repeated user corrections
- repeated successful procedures
- project milestone or PR completion

Phases:

1. Orient

   Load hot index, topic blocks, recent memory versions, projection health,
   conflict backlog, and candidate backlog for the scope.

2. Gather signal

   Inspect recent evidence, selected episodes, corrections, failed actions,
   successful action paths, stale topic blocks, and project state changes.

3. Propose changes

   Return typed proposals for merges, supersessions, stale markers, new topic
   memberships, hot index changes, project-state updates, procedural memories,
   negative memories, and conflict openings.

4. Validate and route

   Deterministic rails validate payloads and route them through the normal memory
   lifecycle. Consolidation does not directly mutate active trusted memory
   except for rebuilding projections from already-active canonical state.

5. Rebuild projections

   Rebuild hot index, topic blocks, embeddings, full-text rows, entity
   projections, graph caches, and project-state blocks as needed.

6. Audit

   Record AI judgment, selected sources, omitted sources, changes proposed,
   changes applied, rejected changes, projection versions, and latency.

## Data Model

Existing table names can be retained when they already match the new contract.
The final schema must directly express these concepts.

### Required Canonical Tables

- `memory_evidence`
- `memory_entities`
- `memory_relationships`
- `memory_assertions`
- `memory_assertion_evidence`
- `memory_episodes`
- `memory_reasoning_traces`
- `memory_action_traces`
- `memory_procedures`
- `memory_reviews`
- `memory_conflict_sets`
- `memory_conflict_members`
- `memory_salience`
- `memory_scope_bindings`
- `memory_retention_policies`
- `memory_sensitivity_labels`
- `memory_topics`
- `memory_topic_members`
- `memory_versions`
- `memory_deletions`
- `project_state_snapshots`

### Required Projection Tables

- `memory_projection_jobs`
- `memory_embedding_projections`
- `memory_keyword_projections`
- `memory_entity_projections`
- `memory_graph_projections`
- `memory_temporal_projections`
- `memory_symbol_projections`
- `memory_context_blocks`
- `memory_export_artifacts`

### Required Evaluation Tables Or Fixtures

- `memory_eval_cases`
- `memory_eval_runs`
- `memory_eval_results`

These can be local fixtures instead of production tables if production does not
need persistent eval history.

### Required Lifecycles

Assertions:

- `candidate`
- `active`
- `conflicted`
- `superseded`
- `stale`
- `retracted`
- `rejected`
- `deleted`
- `privacy_deleted`

Evidence:

- `available`
- `redacted`
- `privacy_deleted`

Reviews:

- `pending`
- `approved`
- `rejected`
- `auto_approved`
- `needs_user_review`
- `needs_operator_review`
- `merged`
- `superseded`

Conflicts:

- `open`
- `resolved`
- `ignored`

Projection jobs:

- `pending`
- `running`
- `completed`
- `failed`
- `dead_letter`

Topic blocks and context blocks:

- `active`
- `stale`
- `superseded`
- `deleted`

### Required Invariants

- Every active assertion links to at least one evidence record.
- Every active relationship links to at least one evidence record.
- Every active procedure links to evidence and review state.
- Exactly one active assertion can own a single-valued predicate for a subject
  and scope.
- Multi-valued predicates must declare that they are multi-valued in the schema
  or type registry.
- A conflicted assertion must belong to an open conflict set.
- A superseded assertion must identify its superseding assertion.
- A stale assertion must identify staleness evidence or consolidation rationale.
- A retracted, rejected, deleted, or privacy-deleted assertion cannot be recalled
  in normal context.
- Privacy-deleted evidence cannot be used to rebuild projections.
- Relationship edges must link to evidence and validity intervals.
- Projection rows must identify canonical source ids, projection version, and
  source memory version.
- Hot index and topic blocks must be rebuildable from canonical state.
- Deleting or redacting canonical memory invalidates all derived projections.
- No raw anonymous dictionaries cross module boundaries for typed memory
  concepts.

## Retrieval

### Candidate Retrieval Inputs

- actor id
- user id
- organization or workspace id when present
- session id
- Discord channel/thread context when present
- current project id
- current repo id and branch when present
- current turn text
- tool/action context
- open jobs, cases, commitments, approvals, and incidents
- allowed memory scopes
- no-memory mode
- maximum candidate budget
- maximum context budget
- current time

### Candidate Retrieval Signals

- scope filters
- lifecycle state
- trust boundary
- sensitivity label
- retention policy
- semantic vector similarity
- full-text or BM25 match
- entity match
- graph distance
- temporal validity
- assertion confidence
- salience score
- user priority
- project/task linkage
- open commitment linkage
- source quality
- verification age
- recency
- frequency
- correction history
- conflict state
- procedure applicability
- topic membership
- symbol and repo map match

### Candidate Retrieval Outputs

- hot index block
- candidate topic blocks
- ordered semantic assertion candidates
- ordered episodic candidates
- ordered reasoning/action trace candidates
- reviewed procedural rules
- project state candidates
- negative memory candidates
- conflict warnings
- projection health diagnostics
- deterministic rail omissions
- candidate-order features

### AI Curation Outputs

- selected memory sections
- selected topic blocks
- selected candidate ids
- selection reasons
- omitted relevant candidate ids
- omission reasons
- uncertainty notes
- conflict handling notes
- source evidence refs
- source projection versions
- confidence
- model
- prompt version
- provider response id

AI curation must account for every candidate in the bounded candidate set as
selected or omitted.

## APIs And Commands

### HTTP APIs

The memory API is rewritten around the new concepts.

Required endpoints or equivalent routes:

- list active memory
- list hot index for a scope
- list topic blocks for a scope
- inspect topic block
- search memory
- inspect memory assertion
- inspect evidence
- inspect memory history
- inspect recall for a turn
- list candidates needing review
- approve candidate
- reject candidate
- edit candidate
- merge candidates
- correct assertion
- retract assertion
- delete assertion
- privacy-delete assertion
- redact evidence
- prioritize assertion
- deprioritize assertion
- set never-remember rule
- inspect conflict set
- resolve conflict set
- inspect project state
- inspect projection health
- retry projection job
- run consolidation for a scope
- inspect consolidation result
- export memory
- import one-time cutover memory
- run memory eval
- inspect memory eval result

No endpoint preserves old response fields for compatibility.

### AI-Mediated Operations

Discord memory commands are not part of the final product. Users ask Ariel about
memory in normal language, and Ariel handles the request through model memory
capabilities guarded by deterministic policy.

Required model-accessible operations:

- inspect active memory
- inspect pending candidates
- inspect recall diagnostics
- inspect unresolved conflicts
- inspect projection health
- propose memory
- approve, reject, edit, merge, correct, retract, delete, privacy-delete, and
  redact memory where policy allows
- set never-remember rules
- run consolidation for the active scope
- create export artifacts
- toggle memory mode for the current session or scope

Every operation is bounded, typed, policy-checked, and auditable. The model does
not receive raw database access.

### Events

Required event concepts:

- memory evidence recorded
- memory evidence redacted
- memory evidence privacy-deleted
- memory candidate proposed
- memory review required
- memory candidate approved
- memory candidate rejected
- memory candidate merged
- memory assertion activated
- memory assertion superseded
- memory assertion marked stale
- memory assertion retracted
- memory assertion deleted
- memory assertion privacy-deleted
- memory relationship linked
- memory conflict opened
- memory conflict resolved
- memory salience changed
- memory topic rebuilt
- memory hot index rebuilt
- memory projection queued
- memory projection rebuilt
- memory projection failed
- memory recalled
- memory recall omitted item
- memory consolidation queued
- memory consolidation completed
- memory import completed
- memory export completed
- memory eval completed

Events must include ids, lifecycle state, actor id, projection version when
relevant, source evidence ids when relevant, and enough metadata to reconstruct
behavior.

## Files

### Source Files

- `src/ariel/app.py`
  - Route wiring and request handling only.
  - Removes old memory response assumptions.
  - Calls memory services through typed APIs.
- `src/ariel/persistence.py`
  - ORM models for canonical memory, review, conflict, salience, versions,
    scopes, retention, topics, projections, and eval history when persisted.
- `src/ariel/memory.py`
  - Canonical memory lifecycle, policy rails, extraction contracts, retrieval,
    projection rebuilds, consolidation, import/export, evals, and serialization.
  - Keeps control flow local and explicit until a split clearly reduces concrete
    complexity.
- `src/ariel/response_contracts.py`
  - New memory API, recall diagnostics, AI operation, and event contracts.
- `src/ariel/worker.py`
  - Executes extraction, projection, consolidation, export, import, and eval
    jobs.
- `src/ariel/proactivity.py`
  - Removes direct active memory writes.
  - Emits evidence and structured memory proposals through the standard memory
    lifecycle.
- `src/ariel/action_runtime.py`
  - Emits action trace evidence and action outcome memory candidates.
- `src/ariel/capability_registry.py`
  - Exposes memory inspection, recall diagnostics, and mutation capabilities to
    the model only where authority and policy allow.
- `src/ariel/config.py`
  - Adds settings only for real implementation needs, such as budgets,
    consolidation cadence, eval toggles, and export paths.

Do not add memory submodules, facade layers, adapters, or registries unless the
existing flat modules have a concrete complexity problem that the split reduces.

### Migration Files

- Add one Alembic migration for the hard cutover.
- The migration removes old memory tables unless the same migration transforms
  them into the new canonical tables.
- Runtime code must not reference old memory table names after the migration.
- The migration creates required canonical and projection tables.
- The migration must not create compatibility views for old APIs.

### Test Files

- `tests/integration/test_north_star_memory_pass.py`
- `tests/integration/test_worker_memory_jobs.py`
- `tests/unit/test_responses_tool_contract.py`
- `tests/unit/test_discord_bot.py`
- Existing slice acceptance tests continue covering session, capture,
  proactive, auth, and transport regressions that memory depends on.

## Acceptance Criteria

### Code Removal

- No production references to old memory parser behavior remain.
- No production references to old memory helper names remain.
- No production references to old memory response shape assumptions remain.
- No old memory API compatibility layer remains.
- No lexical fallback path exists.
- No vector-only fallback path exists.
- No transcript replay fallback exists.
- No provider-hosted memory fallback exists.
- No markdown-canonical memory path exists.
- No direct active-memory write path bypasses lifecycle.
- No dual-write path exists.

### Persistence

- New schema exists in Alembic and SQLAlchemy.
- All lifecycle columns have named check constraints.
- All semantic JSONB fields have typed Pydantic ingress and egress models.
- Every active assertion links to evidence.
- Every active relationship links to evidence.
- Every active procedure links to evidence and review state.
- Supersession, conflict membership, staleness, redaction, projection
  invalidation, and deletion invariants are enforced and tested.
- Privacy deletion prevents future projection rebuilds from deleted evidence.

### Extraction

- User turns, captures, proactive observations, action outcomes, and corrections
  can create evidence.
- AI extraction can propose assertions, episodes, relationships, project-state
  updates, procedures, action traces, and negative memories.
- Extraction records model name, prompt version, confidence, source evidence,
  parse status, validation status, and provider response id.
- Explicit user memory requests create candidates or reviewed memories according
  to policy.
- Tool, web, file, assistant, and quoted content cannot create active trusted
  memory without review policy.
- Extraction respects no-memory mode and never-remember rules.

### Review And Lifecycle

- Candidate review supports approve, edit, reject, merge, mark stale, and route
  to conflict.
- Correction supersedes prior assertions and activates the corrected assertion
  according to policy.
- Retraction removes assertions from normal recall and invalidates projections.
- Delete and privacy deletion invalidate derived projections.
- Conflicting single-valued candidates open conflict sets.
- Resolved conflicts preserve history and evidence.
- Salience and priority changes are auditable.

### Retrieval And Curation

- Candidate retrieval is deterministic for identical inputs and database state.
- Candidate retrieval uses hybrid retrieval with structured filters, vector
  similarity, full-text or BM25, entity match, graph traversal, temporal
  validity, salience, source trust, topic membership, and project linkage.
- AI memory curation decides which candidates matter for the current turn.
- AI memory curation accounts for every candidate as selected or omitted.
- Recall returns provenance, evidence snippets, candidate-order features,
  selection reasons, omission reasons, validity windows, source trust, conflict
  status, projection versions, and projection health.
- Recall never includes superseded, rejected, retracted, deleted, or
  privacy-deleted assertions in normal context.
- Recall never includes conflicted facts as settled facts.
- Temporal questions use validity intervals and event times.
- Graph questions can use relationship distance and entity neighborhoods.
- Keyword-only and vector-only retrieval tests fail the acceptance suite.

### Hot Index And Topics

- Hot index is rebuilt from canonical memory.
- Hot index includes ids or topic pointers for all details.
- Hot index stays within configured token budgets.
- Topic blocks are rebuilt projections with source ids and projection versions.
- Topic blocks can be lazily selected by curation.
- Markdown exports are projections only and cannot become canonical by editing
  files directly.

### Consolidation

- Consolidation can be scheduled, manually queued, and triggered by session
  rotation or memory churn.
- Consolidation records input sources, selected sources, omitted sources,
  proposed changes, applied changes, rejected changes, and projection versions.
- Consolidation can propose merges, supersessions, staleness markers,
  procedures, negative memories, topic changes, and hot index changes.
- Consolidation does not bypass review policy for canonical changes.
- Consolidation rebuilds projections from active canonical state only.

### Proactive Integration

- Proactive remember decisions do not directly create active assertions.
- Proactive cases emit evidence and memory candidates through the standard
  lifecycle.
- Proactive corrections create correction evidence.
- Proactive feedback can change salience, procedures, or never-remember rules
  only through audited memory operations.

### API And AI Operations

- Users can list, search, inspect, correct, delete, privacy-delete, redact,
  export, prioritize, deprioritize, consolidate memory, and set scoped memory
  mode.
- Users can inspect pending candidates before approval when review is required.
- Users can inspect conflicts and resolution history.
- Users can inspect evidence, memory versions, projection health, and recall
  diagnostics.
- No Discord memory commands remain.
- AI memory operations are bounded, deterministic, policy-checked, and auditable.
- Old response fields are not preserved unless they are native fields in the new
  contract.

### Privacy And Consent

- No-memory mode prevents user memory extraction and recall for the selected
  scope while preserving operational audit.
- Never-remember rules prevent future extraction of configured content classes.
- Sensitive content is labeled, redacted, blocked, or routed to review.
- Deletion semantics are explicit: retract, delete, privacy-delete, and redact
  have different effects and are visible to the user.
- Export includes source ids, projection versions, and redaction posture.

### Evaluation

Add a local long-memory evaluation suite covering:

- durable preference recall
- project continuity
- multi-session reasoning
- temporal reasoning
- knowledge updates
- stale fact replacement
- contradiction handling
- deletion and redaction compliance
- abstention
- graph relationship reasoning
- procedural memory adherence
- negative memory adherence
- proactive correction learning
- hot index budget pressure
- topic lazy loading
- recall diagnostics

The eval includes at least:

- one case where vector similarity alone chooses the wrong memory
- one case where keyword matching alone chooses the wrong memory
- one case where temporal validity changes the answer
- one case where conflict uncertainty must be surfaced
- one case where the correct answer is to abstain
- one correction/supersession case
- one deletion/privacy deletion case
- one no-memory mode case
- one proactive feedback case

The eval records:

- answer accuracy
- candidate recall
- curation precision
- selected relevant memory count
- omitted relevant memory count
- conflict handling accuracy
- context tokens
- extraction latency
- retrieval latency
- curation latency
- projection latency
- consolidation latency

### Operations

- Projection jobs are observable.
- Dead-lettered jobs are inspectable and retryable.
- Memory recall emits diagnostics for omitted candidates and projection
  failures.
- Memory extraction emits parse and validation diagnostics.
- Memory curation emits selection and omission reasons.
- Consolidation emits proposed and applied changes.
- Import/export is observable.
- Redaction and deletion produce auditable records.
- Background job failure cannot silently corrupt active memory.

### Verification

- `make verify` passes.
- PostgreSQL integration tests pass.
- Memory eval acceptance tests pass.
- A grep check confirms removed legacy helper names are not present in
  production code.
- A grep check confirms no runtime references to old memory table names remain
  unless those tables are retained as native new-schema tables.
- A grep check confirms no direct active-memory writes bypass lifecycle.
- A grep check confirms no provider-hosted memory or transcript replay fallback
  exists.

## Key Decisions

- PostgreSQL is canonical memory state.
- Claude-style hot index and topic files are implemented as projections, not
  canonical storage.
- Markdown export is a projection and cross-tool affordance, not the source of
  truth.
- Provider-hosted memory is not canonical and is not a fallback.
- External managed memory products are reference models, not required runtime
  dependencies.
- Embeddings, full-text rows, graph caches, temporal indexes, hot index blocks,
  topic blocks, and repo maps are projections.
- Hybrid candidate retrieval is required.
- Temporal validity is required.
- Evidence snippets are required.
- Review is required.
- Conflict is required.
- Salience is required.
- Deletion and redaction are required.
- Consolidation is required.
- The hot path records evidence and enqueues memory work. It does not run full
  extraction, projection, or consolidation unless policy explicitly requires a
  blocking memory judgment.
- AI curation owns relevance. Deterministic ordering only bounds transport and
  budget.
- Proactive memory uses the same lifecycle as user-turn memory.
- Old runtime APIs and schema are not preserved.

## Implementation Plan

Implementation can be split into reviewable commits, but no intermediate state
is production-shippable until the old system is removed.

1. Define typed memory models and response contracts.
2. Add the hard-cutover Alembic migration.
3. Move memory lifecycle operations behind typed services.
4. Remove legacy memory helpers, response assumptions, and fallback paths.
5. Add evidence recording for all ingress paths, including proactive and action
   outcomes.
6. Add AI extraction contracts for assertions, episodes, relationships,
   procedures, project state, action traces, and negative memory.
7. Add review, correction, merge, conflict, staleness, deletion, redaction,
   priority, and never-remember operations.
8. Add projection rebuilders for embeddings, full-text, entities, graph,
   temporal indexes, symbol maps, hot index, and topic blocks.
9. Add hybrid candidate retrieval and AI curation contracts.
10. Replace turn and proactive memory recall with the new context bundle.
11. Add consolidation jobs and projection rebuild triggers.
12. Rewrite memory APIs and AI-mediated operations around new contracts.
13. Add import/export and cross-agent memory pack projections.
14. Add long-memory eval fixtures and acceptance tests.
15. Run `make verify`, PostgreSQL integration tests, memory evals, and legacy
    grep checks.

## Final State

- Ariel has one memory system.
- Memory is evidence-backed, typed, scoped, temporal, auditable, and
  inspectable.
- Every memory write starts from evidence.
- Every active memory has provenance.
- Every memory mutation has version history.
- Every projection is rebuildable.
- Every recall is explainable.
- Every omission is attributable to a rail or AI curation reason.
- Every conflict is explicit.
- Every deletion and redaction invalidates derived context.
- Every proactive memory follows the same lifecycle as turn memory.
- Hot index and topic blocks provide Claude-style low-cost continuity without
  turning markdown into canonical state.
- Long-memory evals protect against regression.
- No legacy memory code, compatibility route, fallback recall path, or direct
  active-write bypass remains.
