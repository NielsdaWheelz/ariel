# Memory SOTA Completion Plan

## Role

This document is the hard-cutover implementation plan for closing Ariel's memory
system from the current vertical slice to the intended gold-standard design.

The plan supersedes any memory UX that depends on Discord slash commands. Discord
is only a transport for user messages and normal assistant responses. Memory is
handled by Ariel through AI-mediated conversation and model-authorized memory
capabilities, with deterministic policy rails and audit underneath.

## Current State

The current implementation has a useful foundation:

- evidence-backed semantic candidates
- review-required candidate flow
- approval, activation, supersession, deletion, and projection rows
- AI-curated recall for turn context
- session-level `normal`, `temporary`, and `no_memory` modes for the chat turn
  path
- proactive `remember` routed to candidate creation instead of active writes
- project-state hot/topic blocks
- partial action trace and scope binding tables

It is not yet the full spec. The missing work is not polish; it includes
correctness, privacy, scope, lifecycle, and public surface gaps.

## Cutover Rules

- No legacy memory code remains.
- No compatibility API shape remains.
- No Discord memory commands remain.
- No fallback memory paths remain.
- No direct active-memory writes bypass lifecycle.
- No dual-write path exists.
- No model-provider-hosted memory is used as durable state.
- Markdown, export files, hot indexes, and topic blocks are projections only.
- Runtime code has one canonical lifecycle for user, proactive, action, and
  correction memory.
- If a feature cannot meet lifecycle, audit, and recall rules, it is not shipped
  as a hidden fallback.

## Target Behavior

### User Experience

Users talk to Ariel normally:

- "remember this"
- "what do you remember about this?"
- "forget that"
- "show pending memory"
- "why did you remember that?"
- "do not remember this thread"
- "export my memory"
- "delete this permanently"

Ariel handles those requests through memory capabilities and deterministic
policy. The user does not need slash commands or separate memory UI commands.

Memory responses must expose:

- what was found
- why it was relevant
- source evidence
- candidate or active lifecycle state
- confidence and uncertainty
- conflicts
- projection health when relevant
- what was omitted when recall diagnostics are requested

### Model Behavior

The model can request memory operations only through explicit capabilities:

- inspect memory
- inspect recall diagnostics
- propose memory
- approve allowed low-risk memory when policy permits
- request user review for pending memory
- correct memory
- retract memory
- delete memory
- privacy-delete memory
- redact evidence
- set never-remember rule
- resolve conflict using an assertion that belongs to the conflict set
- run consolidation
- export memory

Every capability validates authority, scope, lifecycle state, payload shape, and
policy before mutating state.

### Memory Modes

`normal`:

- recall enabled
- extraction enabled
- proactive memory proposals enabled
- action traces enabled
- consolidation enabled

`temporary`:

- no user memory recall
- no user memory extraction
- no proactive memory writes
- no action trace memory writes
- no consolidation using the session
- operational audit still records turns, events, jobs, approvals, and failures

`no_memory`:

- same as `temporary`
- stronger user intent: future features must treat it as a hard block unless the
  user explicitly changes mode

Mode enforcement applies to:

- chat turns
- proactive deliberation
- background extraction tasks
- explicit memory search
- memory recall diagnostics
- action trace memory writes
- consolidation
- import/export scoped to the session

### Privacy

Ariel distinguishes:

- retract: hide from normal recall, retain audit
- delete: hide from normal recall and invalidate projections, retain audit
- privacy-delete: destroy or irreversibly redact user memory content and block
  future projection rebuilds from the deleted source
- redact evidence: remove sensitive source text while preserving safe metadata
- never-remember: prevent future extraction for a configured pattern, source, or
  scope

Secrets are never ordinary memory. Secret-like content is redacted, blocked, or
routed to user review.

## Final Architecture

### Canonical Tables

Final persistence must include:

- `memory_evidence`
- `memory_entities`
- `memory_relationships`
- `memory_assertions`
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
- `memory_deletions`
- `memory_versions`
- `memory_topics`
- `memory_topic_members`
- `project_state_snapshots`

### Projection Tables

Final projections must include:

- `memory_embedding_projections`
- `memory_keyword_projections`
- `memory_entity_projections`
- `memory_graph_projections`
- `memory_temporal_projections`
- `memory_symbol_projections`
- `memory_context_blocks`
- `memory_projection_jobs`
- `memory_export_artifacts`

Projection rows must always include canonical source ids and projection version.
Deleting or redacting canonical memory invalidates all derived projections.

### Runtime Ownership

Keep the implementation as small as possible. Split files only where ownership is
clear and the extracted module earns its cost.

Required final files:

- `src/ariel/app.py`
  - HTTP route wiring only.
  - No lifecycle logic.
  - No memory compatibility responses.
- `src/ariel/persistence.py`
  - SQLAlchemy models and serializers.
  - No business lifecycle decisions.
- `src/ariel/response_contracts.py`
  - Public response validation.
  - Memory contracts include all public memory surfaces.
- `src/ariel/memory.py`
  - Canonical memory lifecycle, retrieval, curation, projection, policy, and
    helper functions until the file is too large to skim.
  - If split, use direct names: `memory_lifecycle.py`, `memory_retrieval.py`,
    `memory_projection.py`, `memory_consolidation.py`, `memory_eval.py`.
- `src/ariel/proactivity.py`
  - Proactive code may emit evidence and structured proposals only.
  - No proactive active-memory writes.
- `src/ariel/action_runtime.py`
  - Emits action outcome evidence or trace updates through memory APIs.
- `src/ariel/capability_registry.py`
  - Exposes model memory capabilities.
  - Does not expose raw database operations.
- `src/ariel/worker.py`
  - Executes extraction, projection, consolidation, export, import, eval, and
    stale-job repair.
- `src/ariel/discord_bot.py`
  - No memory slash commands.
  - No memory-specific deterministic command handlers.

## Work Plan

### 1. Remove Discord Memory Commands

Delete memory-specific Discord slash commands and command handlers.

Required final state:

- No `/memory` command.
- No `/memory-inbox` command.
- No `/memory-recall` command.
- No `/memory-conflicts` command.
- No `/memory-consolidate` command.
- No `/memory-export` command.
- No `/memory-no` command.
- Discord messages asking about memory go through the normal assistant path.
- AI uses memory capabilities to satisfy the request.

Acceptance:

- `rg "/memory|memory-inbox|memory-recall|memory-conflicts|memory-consolidate|memory-export|memory-no" src tests docs/modules/memory.md` finds no Discord command references.
- Existing Discord transport behavior still works.

### 2. Enforce Memory Modes Everywhere

Fix all bypass paths.

Required changes:

- `build_memory_context` always receives effective session or scope.
- Proactive deliberation passes session/scope into recall.
- Proactive `remember` checks active mode before proposing candidates.
- `/v1/memory/search` resolves active session mode and returns no user memory in
  `temporary` or `no_memory`.
- Background extraction no-ops for non-normal sessions.
- Action trace writes no-op for non-normal sessions.
- Consolidation no-ops for non-normal sessions.

Acceptance:

- One test proves chat recall is empty in `temporary` and `no_memory`.
- One test proves proactive remember cannot create candidates in those modes.
- One test proves explicit memory search is empty in those modes.
- One test proves stale queued extraction cannot create candidates after mode
  changes.
- One test proves rotation preserves memory mode.

### 3. Make Scope Binding Real

`memory_scope_bindings` must become policy input, not an audit side table.

Required behavior:

- Scope binding resolution checks session, thread, project, repo, proactive case,
  and user scopes.
- Most specific binding wins.
- `no_memory` beats `temporary`, and `temporary` beats `normal`.
- Expired bindings do not apply.
- Every binding change is auditable.

Acceptance:

- Cross-scope tests prove session/thread/project bindings affect extraction and
  recall.
- Expiration tests prove old bindings stop applying.
- Recall diagnostics report the binding that controlled memory mode.

### 4. Fix Conflict Lifecycle

Conflicts must be first-class, not side effects.

Required behavior:

- Single-valued active assertion plus conflicting candidate opens a conflict set.
- A conflicted candidate cannot be approved through the normal approval endpoint.
- Conflict resolution only accepts assertion ids that belong to the conflict set.
- Resolution activates the winner, marks losing candidates/assertions according
  to policy, records review/version history, and closes the conflict.
- Recall never presents conflicted facts as settled facts.

Acceptance:

- Tests cover conflict open, invalid resolution id, valid resolution,
  historical preservation, and recall uncertainty.

### 5. Complete Deletion And Redaction

Deletion semantics must be explicit and projection-safe.

Required behavior:

- Retract, delete, privacy-delete, and redact are separate operations.
- Deleting any assertion invalidates:
  - embeddings
  - keyword projections
  - entity projections
  - graph projections
  - temporal projections
  - symbol projections
  - salience
  - procedures created from the assertion
  - project snapshots
  - hot index blocks
  - topic blocks
  - topic memberships
  - export artifacts
- Privacy-delete also redacts or removes source evidence content.
- Projection rebuild jobs skip privacy-deleted sources.

Acceptance:

- Tests prove deleted procedure assertions remove procedural recall.
- Tests prove privacy-deleted evidence cannot rebuild projections.
- Tests prove redacted evidence keeps safe metadata but removes source text.
- Tests prove normal recall excludes retracted, deleted, privacy-deleted,
  rejected, superseded, and conflicted-as-settled memory.

### 6. Complete Action Trace Memory

Action traces must reflect final action outcomes.

Required behavior:

- Action proposal, policy decision, approval decision, execution, outcome, and
  undo can create or update action traces.
- Worker execution updates trace outcome after actual execution.
- Proactive action execution emits traces.
- Trace recall excludes the current session unless explicitly requested.
- Action traces can generate reviewed procedural memory only through
  consolidation and review policy.

Acceptance:

- Tests cover successful, failed, denied, and undone action traces.
- Tests prove async worker outcomes update existing trace rows.
- Tests prove proactive action execution creates trace evidence.

### 7. Complete Retrieval

Candidate retrieval must be deterministic and hybrid.

Required signals:

- lifecycle state
- memory mode and scope binding
- semantic vector distance
- full-text or BM25 match
- entity match
- graph neighborhood
- temporal validity
- salience score
- user priority
- source trust
- sensitivity policy
- topic membership
- project linkage
- symbol/repo map match

Required output:

- hot index candidates
- topic block candidates
- semantic assertions
- episodes
- reasoning/action traces
- procedures
- project state
- negative memory
- conflicts
- projection health
- candidate-order features

Acceptance:

- AI curation accounts for every bounded candidate as selected or omitted.
- Search and recall can return every selected memory kind, not only semantic
  assertions.
- Tests fail if vector-only or keyword-only retrieval is enough to pass.

### 8. Complete Hot Index And Topics

Hot index and topic blocks must be rebuildable projections.

Required behavior:

- Hot index is rebuilt from canonical state by projection/consolidation jobs.
- Hot index has token budget enforcement.
- Hot index entries include source ids or topic pointers.
- Topic blocks support all required topic families.
- Topic blocks include source ids, projection version, lifecycle state, and
  redaction posture.
- Topic context blocks require `topic_id` when `block_type = 'topic'`.

Acceptance:

- Tests cover rebuild from canonical state, source ids, projection version,
  deletion invalidation, token budgets, and curation selection.

### 9. Complete Public HTTP API

Keep HTTP routes direct and boring. No compatibility layer.

Required routes:

- list active memory
- search memory
- inspect assertion
- inspect evidence
- inspect memory versions
- inspect recall diagnostics for a turn
- list pending candidates
- approve candidate
- reject candidate
- edit candidate
- merge candidates
- correct assertion
- mark stale
- retract assertion
- delete assertion
- privacy-delete assertion
- redact evidence
- prioritize/deprioritize assertion
- set never-remember rule
- inspect conflict set
- resolve conflict set
- inspect hot index
- inspect topic block
- inspect action traces
- inspect projection health
- retry projection job
- run consolidation
- inspect consolidation result
- export memory
- import one-time cutover memory
- run eval
- inspect eval result

Acceptance:

- Missing ids return typed 404s.
- Wrong lifecycle transitions return typed 409s.
- Every mutation emits events and version/deletion/review audit as appropriate.
- Public contracts expose every public memory surface.

### 10. Add Model Memory Capabilities

The AI handles memory requests through capabilities, not Discord commands.

Required capabilities:

- memory.inspect
- memory.search
- memory.recall_diagnostics
- memory.propose
- memory.review
- memory.correct
- memory.retract
- memory.delete
- memory.privacy_delete
- memory.redact_evidence
- memory.set_never_remember
- memory.resolve_conflict
- memory.consolidate
- memory.export

Acceptance:

- The model cannot mutate memory except through these capabilities.
- Capability calls enforce policy and produce audit.
- User-facing memory requests are satisfied through these capabilities.

### 11. Add Consolidation

Consolidation is the maintenance engine.

Required behavior:

- Loads candidate backlog, versions, conflicts, salience, hot index, topic
  blocks, and projection health for a scope.
- Proposes merges, supersessions, stale markers, procedure candidates, negative
  memory, topic changes, and hot index changes.
- Applies projection-only changes directly.
- Routes canonical changes through review policy.
- Records input sources, selected sources, omitted sources, proposed changes,
  applied changes, rejected changes, model, prompt version, and provider
  response id.

Acceptance:

- Tests cover merge proposal, stale marker, topic rebuild, hot index rebuild,
  procedure proposal, and review-gated canonical change.

### 12. Add Import, Export, And Eval

Required behavior:

- Export produces redacted projection artifacts with source ids, projection
  versions, lifecycle state, and redaction posture.
- Import is one-time cutover only and writes canonical evidence/candidates, not
  active memory without review policy.
- Evals run locally and write durable result records.

Acceptance:

- Export cannot become canonical by editing a file.
- Import has no runtime compatibility mode.
- Eval suite covers preference recall, project continuity, multi-session
  reasoning, temporal reasoning, contradiction, deletion/redaction, abstention,
  graph reasoning, procedural memory, negative memory, proactive feedback, hot
  index pressure, topic lazy loading, and recall diagnostics.

## Acceptance Checklist

The cutover is complete only when all items below are true:

- No Discord memory commands exist.
- No direct active-memory write bypass exists.
- Memory modes are enforced in chat, proactive, search, workers, action traces,
  and consolidation.
- Conflict lifecycle is closed and tested.
- Procedure, hot, topic, project, action, graph, temporal, and symbol projections
  invalidate on deletion/redaction.
- Public APIs expose all memory surfaces.
- Model capabilities handle memory UX.
- Privacy-delete, redact, and never-remember are implemented and tested.
- Consolidation is implemented and review-gated.
- Import/export are projections or cutover-only flows.
- Eval suite exists and must pass.
- `ruff`, `mypy`, migrations, and integration tests pass.

## Non-Goals

- No Discord memory command UX.
- No provider-hosted durable memory.
- No markdown file as canonical memory.
- No legacy API compatibility.
- No "best effort" fallback recall.
- No hidden auto-approval of proactive memory.
- No broad generic memory framework abstraction before local code proves the
  need.

## Key Decisions

- AI-mediated memory UX replaces Discord command UX.
- Memory mode is policy, not a UI preference.
- All memory mutation goes through one lifecycle.
- Hot index and topics are projections, not source of truth.
- Consolidation can mutate projections directly but canonical changes go through
  review policy.
- Deletion and privacy deletion must invalidate every derived projection.
- Public routes exist for auditability, but normal user interaction happens
  through the assistant.
