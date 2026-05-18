# Memory Cutover

## Scope

This document owns the hard cutover of Ariel's memory subsystem: from a
31-table, ~9,400-line "SOTA" memory engine to a flat fact store and two
AI-maintained documents, owned end to end by two AI subagents.

It supersedes and deletes `docs/modules/memory-completion-cutover.md` and
`docs/modules/memory-consolidation-cutover.md`. It replaces the design in
`docs/modules/memory.md`, which is rewritten to the short standing module doc
for the new design. It cancels Phase 2 of `docs/schema-consolidation-cutover.md`:
the planned 31→~25 `memory_*` table consolidation is moot — this cutover takes
`memory_*` to two tables directly.

The cutover is incompatible with the assertion / conflict-set / projection data
model, with the deterministic retrieval engine, and with the
summarization/compaction machinery. There is no compatibility layer, no legacy
mode, no fallback path, no dual-write, and no feature flag. Work is sequenced
across commits; the merged final state contains only the new surface.

## Thesis

Memory is a store of facts the AI fully owns, plus two living documents the AI
keeps current. Code exists only to enable and facilitate AI judgment; it never
performs product judgment on its own.

Ariel's memory has three parts, all authored by AI:

- the **fact store** — durable, plain-language facts, in one flat table with no
  categories;
- the **profile** — one always-loaded document: who the user is, how they work,
  and the standing context, including privacy guardrails;
- the **session digest** — one per-session document: the working state of the
  current conversation.

Every memory judgment is an AI judgment, made by one of two subagents:

- the **retriever** decides, at the start of every deliberative wake, which
  facts are relevant now, and surfaces them to the main Jarvis thread;
- the **rememberer** decides what to write to the fact store, and keeps the
  profile and the session digest current.

The main Jarvis agent reaches memory only by delegating to these two: the
`memory.recall` syscall runs the retriever, the `memory.remember` syscall runs
the rememberer. The main agent never touches the store directly.

Deterministic code does exactly five things, all of them rails: it stores
facts and the two documents, it gathers candidate facts for the retriever
(vector + keyword + recent), it injects the profile and digest into context, it
runs the subagents, and it audits every subagent call. It performs no relevance
judgment, no "worth remembering" judgment, no categorization, no conflict
resolution, no ranking, and no summarization.

This is the architecture ChatGPT and Anthropic's memory tool ship — a small
store and an always-on document the model maintains, driven through a tiny tool
surface — with the one addition Ariel needs that a chatbot does not: because
Ariel wakes itself on ambient events, retrieval and remembering are subagents
rather than inline tools, so the main thread stays uncluttered and memory is
surfaced even when no human prompts for it.

## Goals

- Replace the 31 `memory_*` tables with two: a flat `memory_facts` table and a
  singleton `memory_profile` document, plus a per-session `digest` column on
  `sessions`.
- Hold no kind, type, or category field on a fact. Facts are flat, rich,
  plain-language statements; the rememberer and retriever judge them by reading
  content.
- Replace `memory.py` (~9,400 lines) with a fact-store module of roughly
  700–1,000 lines.
- Replace the 29 `cap.memory.*` capabilities with exactly two syscalls:
  `memory.recall` and `memory.remember`.
- Make every memory judgment a bounded AI subagent call: a retriever and a
  rememberer, each shaped like the existing `_*_with_model` bounded calls.
- Run the retriever automatically at the start of every deliberative wake, and
  expose it on demand as `memory.recall`.
- Make the rememberer maintain three things from one call — the fact store, the
  always-loaded profile, and the per-session conversation digest — and run it
  after every turn, on a periodic sweep, and on demand as `memory.remember`.
- Inject the profile and the session digest into every turn with no retrieval
  call and no token-budget machinery.
- Delete all summarization and compaction machinery: the context-compaction
  adapter and the rotation context block. The session digest is the running
  continuity.
- Make memory writes land active immediately. AI extraction is the judgment;
  there is no candidate state and no review step.
- Delete the predicate registry, the conflict-set lifecycle, RRF retrieval
  fusion, the seven retrieval signals and their projection tables, bi-temporal
  validity, the entity/relationship graph, the candidate/review control plane,
  topics, the sensitivity/retention/scope-binding machinery, the memory event
  log and version history, the memory eval suite, import/export, and the
  `AGENTS.md` projection.
- Keep every rail `ai-first.md` requires: durable storage, bounded candidate
  retrieval with provenance, and audit records.

## Non-Goals

- Do not keep the assertion / conflict-set / projection data model, or any of
  the 31 current tables.
- Do not give a fact a kind, type, category, or tag field. Classification is
  the subagents reading content, never a schema column.
- Do not keep RRF, multi-signal fusion, a similarity threshold, or any
  deterministic relevance ranking.
- Do not keep a candidate or review lifecycle. An extracted fact is an active
  fact.
- Do not keep bi-temporal validity, `as_of` recall, the entity/relationship
  graph, memory topics, or per-row version history.
- Do not keep any summarization or compaction code. The session digest is the
  only running-context mechanism.
- Do not keep the memory eval suite or the `make verify` memory regression gate.
- Do not keep memory import/export or the `AGENTS.md` rules projection.
- Do not add approval gates to memory operations.
- Do not add a persistent agent process or a conversational thread for the
  subagents. Each subagent call is stateless and bounded, matching every other
  AI call in the codebase.
- Do not add a separate memory event log. The `ai_judgments` table audits every
  subagent call.
- Do not add a second model tier speculatively. The subagents use
  `settings.model_name` until measured cost requires otherwise.
- Do not give ambient interpretation a retriever. It is high-volume triage;
  proactive deliberation downstream is where memory-aware judgment belongs.
- Do not split `memory.py` into sub-modules. `codebase.md` keeps modules flat
  until a concrete complexity problem forces a split.

## Current State To Replace

These are replacement targets, not compatibility promises.

- `src/ariel/memory.py` — 9,362 lines, 73 top-level symbols. Three functions
  exceed 900 lines each (`build_memory_context`, `consolidate_memory`,
  `process_memory_extract_turn`).
- 31 `memory_*` tables in `src/ariel/persistence.py` (~1,400 lines of model
  definitions).
- 29 `cap.memory.*` capabilities (`MEMORY_CAPABILITY_IDS`,
  `capability_registry.py:52-82`; definitions `4069-4507`; run-callable aliases
  `4612-4639`), all dispatched through `_execute_memory_capability`
  (`action_runtime.py:807-1276`).
- 7,359 lines of memory tests across four files, including a model-graded eval
  regression gate wired into `make verify`.
- 16 `memory_*` settings in `config.py` (lines 40-55).
- The deterministic judgment machinery: a 50-entry predicate registry, the
  conflict-set open/settle/close lifecycle, RRF fusion over seven retrieval
  signals, hot-index token budgets, the selective-forgetting value floor, and
  the reflective-consolidation phase.
- The summarization machinery: the `OpenAIContextCompactionAdapter`,
  `validate_continuity_compaction_payload`, and `record_rotation_context_block`.
- Dormant scaffolding never exercised by an automatic flow: the entity/graph
  subsystem (`create_relationship` has one call site, an admin HTTP endpoint),
  bi-temporal `as_of` recall (never passed by a production caller), and the
  candidate→review control plane (no automatic flow approves a candidate).

## Target Behavior

### The Fact Store

One table, `memory_facts`. A fact is a rich, plain-language statement — as long
and detailed as it needs to be. There is no kind, type, category, or tag field:
the rememberer decides what a fact says and the retriever decides when it
matters, both by reading content.

| Column | Type | Notes |
|---|---|---|
| `id` | String(32) PK | id-prefix `mfa` |
| `content` | Text, not null | the fact, in plain language |
| `status` | String(16), not null | `active` \| `forgotten` |
| `source_turn_id` | String(32), null, FK→`turns` | provenance: the turn the fact came from |
| `source_excerpt` | Text, null | a short snippet of the originating evidence |
| `embedding` | Vector(1536), null | pgvector; populated by the rememberer on write; null = pending |
| `search_vector` | TSVECTOR | generated/`Computed` from `content`; GIN-indexed |
| `created_at` | DateTime(tz), not null | |
| `updated_at` | DateTime(tz), not null | |
| `last_recalled_at` | DateTime(tz), null | set when the retriever surfaces the fact |

`status` is the lifecycle: `active` or `forgotten`. `forgotten` is a reversible
soft-delete; the periodic sweep hard-deletes rows left `forgotten` long enough.
There is no `candidate`, no `conflicted`, no `superseded`, and no version
history — when a fact changes the rememberer edits it in place; when a fact is
wrong or stale the rememberer forgets it.

No projection tables. The hot index, topic blocks, embeddings table, keyword
table, entity/temporal/symbol/graph projection tables, and context-block table
are all deleted. `embedding` and `search_vector` live on the row.

### The Profile

The profile is one document — `memory_profile`, a single row — that the
rememberer keeps current: who the user is, how they want work done, durable
preferences, key relationships, the shape of their ongoing work, and standing
guardrails including privacy instructions ("never remember X"). It is
synthesized prose authored by the rememberer, not a list of rows, with whatever
internal structure the rememberer chooses.

The profile is injected into every turn and every proactive deliberation as a
`system` message, with no retrieval call, no scoring, and no token-budget
machinery. It stays small because the rememberer keeps it small — it is
prompted to synthesize, merge, and drop. This is the Claude Code `MEMORY.md` /
ChatGPT user-knowledge pattern: a small always-on document the model maintains.

The profile is also given to the rememberer on every run, so privacy guardrails
in it are always honored: "never remember X" is a standing line in the profile,
not a policy table or a memory mode.

### The Session Digest

Each session carries a digest — a `digest` column on the session record — that
the rememberer keeps current: the working state of the current conversation.
It holds what a long conversation needs to stay coherent but that is not a
durable fact — the thread of the discussion, what has been tried, open
questions, where things stand. The post-turn rememberer updates it; it is
injected for that session's turns alongside the verbatim recent turns.

The digest replaces compaction. Today the `OpenAIContextCompactionAdapter`
summarizes overflowing history and `record_rotation_context_block` summarizes a
closing session. Both are deleted. The running context is inherently bounded —
the verbatim recent turns plus the digest — so there is nothing to compact. On
session rotation the rememberer writes a carry-forward digest that seeds the
new session, so conversation continuity survives a rotation. The digest is
allowed to be as long and detailed as the conversation needs; it is working
memory and earns its context budget.

### The Retriever

A bounded AI subagent that decides which facts are relevant to the current
wake.

- Input: the wake context (the user message, or the proactive case summary)
  plus a deterministically gathered candidate set of facts.
- Candidate gather (a rail, not a ranking): a generous, unranked union of
  vector-similarity matches over `embedding`, keyword matches over
  `search_vector`, and recent context — with no similarity threshold and a
  generous limit. The retriever is meant to see many facts, including
  low-similarity ones, and decide for itself; a distance cutoff would be code
  making the relevance call. At the prototype's scale the gather is effectively
  most of the store; vector and keyword keep it bounded only as the store
  grows. There is no RRF, no fusion, and no per-candidate feature vector.
- Output: the subset of facts that matter now, validated by a pure
  `_validated_*` function that fails closed.
- The selected facts are rendered into a `recalled memory` `system` message,
  separate from the profile and digest; their `last_recalled_at` is updated.
- Triggers: automatically as a pre-turn step on every deliberative wake, and on
  demand via the `memory.recall` syscall.
- Failure is non-fatal. If the retriever's model call fails, the turn proceeds
  with the profile and digest alone and the failure is recorded as an
  `AIJudgmentRecord`. This is an improvement over today, where a curation
  failure fails the whole turn.

### The Rememberer

A bounded AI subagent that maintains all three forms of memory.

- Input: a conversation (a completed turn, or a closing session) or the fact
  store (the sweep), plus the current profile, the current session digest, and
  a gathered candidate set of existing facts so it can edit instead of
  duplicate.
- Output, validated by a pure `_validated_*` function that fails closed:
  - fact operations — `write { content }`, `edit { fact_id, content }`,
    `forget { fact_id }`;
  - an optional rewritten `profile` document;
  - an optional rewritten session `digest`.
- One subagent call, reviewing one conversation, updates the fact store, the
  profile, and the digest as needed. The handler applies fact operations
  deterministically and computes the embedding for each written or edited fact.
  Applying operations is a rail; deciding them is the subagent's judgment.
- Facts land `active` immediately. There is no candidate state and no approval.
- The rememberer honors the profile's privacy guardrails: it is given the
  profile on every run and instructed not to store anything they forbid.
- Triggers: a `memory_remember` background task enqueued after every turn
  (maintains facts and the digest, and the profile when something durable
  changed); the same task on session rotation (writes the carry-forward
  digest); a periodic `memory_sweep` background task (prune stale facts, merge
  duplicates, re-tighten the profile, hard-delete long-`forgotten` rows); and
  on demand via the `memory.remember` syscall.
- Failure is non-fatal and recorded as an `AIJudgmentRecord`; the task retries
  under the normal background-task retry policy.

### The Two Syscalls

The main Jarvis agent's entire memory surface is two `allow_inline` syscalls,
exposed to the `run` program:

- `memory.recall(query)` — runs the retriever; returns the facts it judged
  relevant.
- `memory.remember(note)` — runs the rememberer over `note`; returns the
  operations it applied.

Both are `allow_inline` (no approval gate — see Key Decisions) and both run
their subagent model call host-side, outside the sandbox. The main agent never
receives a lower-level memory operation; it cannot write, edit, or forget a
fact, or touch the profile or digest, directly — only delegate. The automatic
pre-turn retriever is not a syscall; it is a step in the turn engine. The
syscall is the on-demand path.

### Wakes

The retriever, the profile, and (for turns) the digest are wired into the two
deliberative wakes:

- Human / API / capture turns — all funnel into `_execute_turn_for_session`
  (`app.py:5445`). The retriever pre-step, profile, and digest replace the
  `build_memory_context` call at `app.py:5746`.
- Proactive deliberation — `process_proactive_deliberation_due`
  (`proactivity.py:987`). The retriever and profile replace the
  `build_memory_context` call at `proactivity.py:1024`. Proactive deliberation
  is case-scoped, not session-scoped, so it gets no digest.

Ambient interpretation (`process_ambient_interpretation_due`) gets no retriever.
It is high-volume triage that decides whether an observation deserves a case;
if it promotes one, proactive deliberation — which does recall — performs the
memory-aware judgment.

## Architecture And Final State

### Data model

Two tables and one column:

- `memory_facts` — the flat fact store, as specified above. `embedding` is a
  `pgvector` column with an HNSW index; `search_vector` is a generated
  `TSVECTOR` with a GIN index. Foreign keys follow `database.md`
  (`ondelete=RESTRICT`).
- `memory_profile` — a single-row table holding the profile document:
  `id` (PK), `content` (Text), `updated_at`. Seeded with one empty row by the
  migration.
- `sessions.digest` — a new nullable `Text` column on the session record,
  holding the per-session conversation digest.

Schema delta: drop 31 `memory_*` tables, create `memory_facts` and
`memory_profile`, add `sessions.digest`. Net schema change: 86 → 57 application
tables — which also exceeds the `schema-consolidation-cutover.md` end-state
estimate of ~72 and absorbs its Phase 2.

### Module structure

`src/ariel/memory.py` stays one flat module, rewritten to roughly 700–1,000
lines, containing:

- the reads and writes against `memory_facts` and `memory_profile`;
- `gather_candidates(...)` — the vector + keyword + recent union;
- `run_retriever(...)` and `_validated_retrieval(...)` — the retriever subagent;
- `run_rememberer(...)`, `_validated_rememberer_output(...)`, and
  `apply_rememberer_output(...)` — the rememberer subagent, including the
  profile and digest updates;
- `render_profile(...)` and `render_recalled_facts(...)` — context rendering;
- `embed_text(...)` — the embedding call;
- `enqueue_memory_remember(...)` and the `memory_sweep` enqueuer;
- prompt strings and prompt-version constants.

`memory.py` keeps importing only `config`, `persistence`, and `redaction`. No
import cycle is introduced. The session `digest` is plain text returned by the
rememberer; the caller (the turn engine / worker) writes it onto the session,
so `memory.py` does not depend on the session model.

### The bounded-AI-call shape

Both subagents are stateless bounded calls, identical in shape to the existing
`_curate_memory_context_with_model` / `_reflect_on_scope_with_model` pattern:

- a raw `httpx.post` to `https://api.openai.com/v1/responses`, `store: False`,
  `model = settings.model_name`, `timeout = settings.model_timeout_seconds`;
- a module-level system prompt string and a module-level prompt-version
  constant embedded in the user-message JSON;
- a separate pure `_validated_*` function that fails closed — every malformed
  field raises `AIJudgmentFailure`; no partial parse;
- the caller writes an `AIJudgmentRecord` on both the success and failure
  paths.

There is no persistent agent or thread; "subagent" here means a bounded,
audited, single model call, consistent with every other AI call in the
codebase. Both subagents run host-side and use the direct `httpx` path, so they
work identically in the API process and the worker process (the worker has no
`ModelAdapter`).

### Audit

Every retriever call is an `ai_judgments` row with `judgment_type =
memory_recall`; every rememberer call, `judgment_type = memory_remember`. These
two values replace `memory_curation`, `memory_extraction`,
`reflective_consolidation`, and `continuity_compaction` in the
`ck_ai_judgment_type` CHECK constraint and in
`SurfaceEventAIJudgmentPayloadContract`. The `ai_judgments` table is the
complete memory audit trail; no separate memory event log exists.

## Key Decisions

### A flat fact store, no categories

A fact is a plain-language sentence. It has no kind, type, category, or tag
column. A category field is the code deciding what sort of thing a fact is;
that is judgment, and the rememberer (when it writes) and the retriever (when it
recalls) make it by reading content. Removing the category also removes the
last reason for a closed vocabulary anywhere in memory. Facts are written rich
and detailed; the retriever sees many and picks. This satisfies `simplicity.md`
("no speculative API surface") and `ai-first.md` (code owns no classification).

### Profile and digest are documents, not fact-categories

The two things that must always be in context — durable user knowledge and the
current conversation's working state — are AI-maintained documents, not facts
flagged with a category. A synthesized profile document reads better as
always-on context than a list of atomic rows, keeps `memory_facts` perfectly
flat, and matches what Claude Code and ChatGPT actually ship. The profile
restating some facts in synthesized form is intentional, not duplication to
eliminate: the profile is the always-on synthesized view, the fact store is the
granular searchable record, and one subagent owns both so they stay coherent.

### One table, two tables, two subagents — the one deviation from the minimalist baseline

ChatGPT and Anthropic's memory tool collapse memory into the main agent plus
tools, with no separate memory agent. Ariel keeps two memory subagents. The
justification is specific and bounded: Ariel is an autonomous operator that
wakes on ambient events with no human in the loop, and its main thread runs
operator work whose context must stay clean. `ai-first.md` already prescribes
subagents for exactly this — "memory relevance and recall curation" and
"memory extraction" are listed subagent uses, so the master "does not need
every intermediate token." The minimalist lesson Ariel does take is the
decisive one: no machinery. The subagents replace the engine; they are not
stacked on top of it.

### The main agent delegates only

The main agent's memory surface is `memory.recall` and `memory.remember` and
nothing else. The low-level store operations belong to the subagents. One owner
for store access; the main thread never sees raw candidate dumps; the model
always gets a curated digest. This is `cleanliness.md`'s "one concern, one
owner."

### Continuity is the session digest; compaction machinery is deleted

A long conversation holds detail that is relevant but not a durable fact. That
need is met by the per-session digest the rememberer maintains — not by a
summarization adapter and not by overloading the durable fact store. Because the
running context is the verbatim recent turns plus the digest, it is inherently
bounded, so the `OpenAIContextCompactionAdapter`,
`validate_continuity_compaction_payload`, and `record_rotation_context_block`
are all deleted. One mechanism — the rememberer — covers durable facts, the
profile, and conversation continuity.

### Facts land active; no candidate or review step

The user chose this, and `ai-first.md` backs it: AI extraction is itself the
judgment. The current candidate→review→approve control plane has no automatic
actor — nothing promotes candidates — so today's extracted facts sit inert. The
rememberer writes `active` facts directly. Errors are cheap: the user says
"that's wrong," the rememberer forgets or edits.

### No approval gates on memory

Memory is the user's own data on the user's own host. An approval gate on
"remember a preference" or "forget a stale fact" adds friction with no security
benefit, and every memory write is reversible by the rememberer. Both syscalls
are `allow_inline`. (`impact_level` is `write_reversible`; the contract is
audited like any capability.)

### Retriever failure is non-fatal

The always-loaded profile and digest mean the assistant is never blind. If the
retriever call fails, the turn proceeds on the profile and digest alone and
logs the failure. Recall is no longer on the turn's critical-failure path — a
strict improvement over the current behavior, where a curation failure fails
the turn.

### No budget machinery on the profile or the digest

The hot-index token budget and lowest-salience eviction are deleted. The
profile and digest are kept the right size by the rememberer's judgment, not by
a deterministic cap. Re-introducing a budget rail would contradict this
cutover's goals. Their growth is a rememberer-prompt concern; see Key Risks.

### Candidate gather is a generous union, not a ranking

Vector and keyword retrieval are kept only as candidate-gathering rails — the
union of two `LIMIT`ed queries plus recent context, with no similarity
threshold. The retriever is meant to see many facts, including low-similarity
ones, and own relevance. `ai-first.md` and `north-star-cutover.md` are explicit
that memory ordering is transport, not a relevance score; an unranked,
unfiltered union honors that literally.

### Privacy is the profile, not policy machinery

`memory_sensitivity_labels`, `memory_retention_policies`,
`memory_scope_bindings`, never-remember rules, and `resolve_memory_policy`'s
severity tables are deleted. The genuine single-user need — "don't remember
this" — is a standing line in the profile, which the rememberer is given on
every run and instructed to obey. "Forget that" is the rememberer forgetting.
No modes, no scope chains, no policy tables.

### The eval suite is deleted

The 1,133-line model-graded eval suite and its `make verify` gate are removed.
A self-built memory benchmark is sophistication, not a correctness rail; the
field's public memory benchmarks are themselves unreliable. Correctness is
covered by deterministic behavior tests (a fact written is recalled; the
profile and digest render; the sweep prunes; a retriever failure is non-fatal),
per `ai-first.md` ("tests for judgment use model fixtures ... they must not
reintroduce deterministic judgment as a test oracle").

### Subagents use the primary model

There is no existing cheaper reasoning-model tier. Per `simplicity.md` ("do not
add optional parameters until a real call site needs them"), the retriever and
rememberer use `settings.model_name`. A dedicated cheaper-model setting is a
later change justified by measured cost, following the `attachment_openai_model`
precedent — not added speculatively here.

## Rules

These are the standing rules for the memory module after the cutover. They
belong in the rewritten `docs/modules/memory.md`.

- Memory code stores facts and the two documents, gathers candidates, injects
  the profile and digest, runs the subagents, and writes audit records. It
  makes no relevance, importance, categorization, conflict, ranking, or
  "worth remembering" decision, and it summarizes nothing.
- A fact is a plain-language statement. There is no kind, type, category, tag,
  predicate vocabulary, or fact schema beyond the `memory_facts` columns.
- The profile and the session digest are AI-authored documents. No code
  composes, edits, or summarizes them.
- The main agent's only memory surface is `memory.recall` and `memory.remember`.
  No other code path mutates `memory_facts`, `memory_profile`, or `sessions.digest`
  except the rememberer's applied output.
- Every retriever and rememberer call writes one `ai_judgments` row, on both
  success and failure.
- The retriever and rememberer are stateless bounded model calls. No persistent
  memory agent or thread is introduced.
- Recall failure is non-fatal. Memory writes are never approval-gated.
- Vector and keyword search gather candidates only, with no threshold. No code
  ranks facts by relevance.
- New memory machinery — registries, scorers, projection tables, lifecycle
  states, category fields, summarizers — is forbidden. A real product need is
  rewritten as a subagent prompt change, not as code.

## Files

### Rewritten

| File | Change |
|---|---|
| `src/ariel/memory.py` | 9,362 → ~700–1,000 lines: the fact store and profile reads/writes, `gather_candidates`, `run_retriever`/`_validated_retrieval`, `run_rememberer`/`_validated_rememberer_output`/`apply_rememberer_output`, `render_profile`/`render_recalled_facts`, `embed_text`, enqueue/sweep helpers, prompts. |
| `docs/modules/memory.md` | Rewritten to the short standing module doc for the new design (see Documentation). |
| `tests/integration/test_memory.py` | New file replacing `test_north_star_memory_pass.py` and `test_worker_memory_jobs.py`: behavior tests for the crystallized subsystem. |

### Created

| File | Change |
|---|---|
| `alembic/versions/20260518_0036_memory_crystallization.py` | Drop 31 `memory_*` tables; create `memory_facts` and `memory_profile` (seed one empty profile row) with their indexes; add `sessions.digest`; amend `ck_ai_judgment_type` and `ck_background_task_type`. Working `downgrade()`. |

### Edited

| File | Change |
|---|---|
| `src/ariel/persistence.py` | Delete 31 `Memory*Record` models (~1,400 lines); add `MemoryFactRecord` and `MemoryProfileRecord`; add a `digest` column to the session model. Amend `ck_ai_judgment_type` (drop `memory_curation`/`memory_extraction`/`reflective_consolidation`/`continuity_compaction`; add `memory_recall`/`memory_remember`) and `ck_background_task_type` (drop `memory_extract_turn`; add `memory_remember`/`memory_sweep`). |
| `src/ariel/capability_registry.py` | `MEMORY_CAPABILITY_IDS` → `{cap.memory.recall, cap.memory.remember}`; delete 29 `CapabilityDefinition` entries, add 2; `_RUN_CALLABLE_ALIASES` → 2 entries; remove memory `_ACTION_LABELS_BY_CAPABILITY_ID` entries; delete the dead `_execute_memory_runtime` stub. |
| `src/ariel/action_runtime.py` | Delete ~22 `_validate_memory_*` validators, add `_validate_memory_recall_input` and `_validate_memory_remember_input`; rewrite `_execute_memory_capability` (807-1276) to two handlers calling `memory.run_retriever` / `memory.run_rememberer`. |
| `src/ariel/app.py` | Replace `build_memory_context` (5746) with the retriever pre-step + profile + digest; add `profile`, `session_digest`, and `recalled_memory` to `_build_turn_context_bundle` and `_CONTEXT_SECTION_ORDER`; render them in `_build_responses_input_items` (1357); replace the end-of-turn evidence/trace block (7212-7309) with one `enqueue_background_task("memory_remember", …)`; replace the rotation memory calls (2942-2997) with a `memory_remember` enqueue that writes the carry-forward digest; delete the `OpenAIContextCompactionAdapter` and all `validate_continuity_compaction_payload` use; drop the `cap.memory.eval` eligibility exclusion; delete all `/v1/memory/*` routes except `GET /v1/memory/facts` (operator inspection). |
| `src/ariel/worker.py` | Replace the `memory_extract_turn` dispatch with `memory_remember`; add a `memory_sweep` dispatch and a self-gating `enqueue_due_memory_sweep`; delete `process_memory_projection_job`, `process_memory_graph_projection_job`, `process_memory_maintenance_job`, `enqueue_due_memory_consolidation_jobs`, and `reap_stale_memory_projection_jobs`. |
| `src/ariel/proactivity.py` | Replace `build_memory_context` (1024) with the retriever + profile; replace the `remember`-decision path (`_apply_remember_decision`, `propose_memory_candidate`) with a `memory_remember` enqueue; delete `record_action_trace` / `emit_memory_events` calls. |
| `src/ariel/config.py` | Delete `memory_import_cutover_enabled`, `memory_vector_distance_ceiling`, `memory_rrf_k`, `memory_consolidation_candidate_threshold`, `memory_consolidation_conflict_threshold`, `memory_hot_index_budget_tokens`, `memory_hot_index_hard_max_tokens`, `memory_forgetting_value_floor`, `memory_forgetting_staleness_days`, and their validators. Keep `memory_embedding_provider`/`_model`/`_dimensions`. Rename `memory_consolidation_interval_seconds` → `memory_sweep_interval_seconds`; rename `max_recalled_assertions` → `memory_recall_candidate_limit` (and raise its default — the gather is generous). |
| `src/ariel/db.py` | `REQUIRED_TABLES`: remove the 31 `memory_*` tables, add `memory_facts` and `memory_profile`. |
| `src/ariel/response_contracts.py` | Update `SurfaceEventAIJudgmentPayloadContract.judgment_type`; delete memory-surface contracts for removed `/v1/memory/*` routes, keep the one for `GET /v1/memory/facts`. |
| `.env.example` | Mirror every `config.py` change. |

### Deleted

| File | Reason |
|---|---|
| `tests/integration/test_memory_eval_suite.py` | The eval suite is deleted. |
| `tests/fixtures/memory_eval_cases.py` | Eval fixtures. |
| `tests/integration/test_north_star_memory_pass.py` | Replaced by `test_memory.py`. |
| `tests/integration/test_worker_memory_jobs.py` | Surviving worker tests fold into `test_memory.py`. |
| `docs/modules/memory-completion-cutover.md` | Superseded design. |
| `docs/modules/memory-consolidation-cutover.md` | Cancelled (Phase 2 of schema consolidation). |

Tests in `tests/integration/test_run_program_runtime.py` and the
`test_s*_acceptance.py` suites that reference memory syscalls are updated to the
two-syscall surface.

## Documentation

- `docs/modules/memory.md` — rewritten from the 42 KB North Star doc to a short
  standing module doc: the `memory_facts` model, the profile and digest, the
  two subagents, the two syscalls, the wakes, and the Rules section above.
- `docs/modules/memory-completion-cutover.md` — deleted.
- `docs/modules/memory-consolidation-cutover.md` — deleted.
- `docs/ai-first.md` — the Thesis gains one sentence sharpening the rule:
  "Code exists only to enable and facilitate AI judgment; it never performs
  product judgment on its own." The Memory section is updated to the
  fact-store / profile / retriever / rememberer model.
- `docs/schema-consolidation-cutover.md` — Phase 2 is marked cancelled: this
  cutover takes `memory_*` to two tables, and the schema end-state estimate is
  revised (86 → ~57).
- `docs/database.md` — the memory table family entry becomes `memory_facts` and
  `memory_profile`.
- `docs/modules/index.md` and `docs/index.md` — drop the deleted memory-doc
  links.
- `docs/run-program-cutover.md` — the "Procedural Memory" section is updated:
  procedures are plain facts in `memory_facts`, not a separate memory type.
- `README.md` — update any memory-subsystem description.
- `docs/modules/memory-cutover.md` (this file) — deleted in the final phase
  once `memory.md` is the standing doc; `cleanliness.md` removes finished-era
  cutover docs, and git history preserves the record.

## Implementation Plan

The cutover lands as a sequence of commits. Intermediate phases are not
independently `make verify`-green — a hard schema-plus-code cutover cannot be —
but the merged final state is.

### Phase 1 — Failing contract tests

Add tests that fail against current `main` and define the target: `memory_facts`
and `memory_profile` exist and the 31 tables do not; a fact has no kind/type
column; exactly two memory syscalls exist; a fact the rememberer writes is
immediately `active` and recalled by the retriever; the profile and digest
render into a turn; a retriever failure leaves the turn alive on the profile
and digest; the sweep forgets stale facts.

Acceptance: the tests fail against `main` and define the target.

### Phase 2 — Schema

Write migration `0036`: drop the 31 `memory_*` tables, create `memory_facts`
and `memory_profile` (seed one empty profile row) with their HNSW and GIN
indexes, add `sessions.digest`, amend the two CHECK constraints. Delete the 31
models from `persistence.py`, add `MemoryFactRecord` and `MemoryProfileRecord`,
add the session `digest` column. Update `db.py` `REQUIRED_TABLES`. Ariel holds
no production memory data; the migration drops and recreates freely, with a
working `downgrade()`.

Acceptance: migration runs up and down; `memory_facts` and `memory_profile` are
the only `memory_*` tables.

### Phase 3 — The memory module

Rewrite `memory.py`: the fact-store and profile reads/writes,
`gather_candidates`, `run_retriever` + `_validated_retrieval`, `run_rememberer`
+ `_validated_rememberer_output` + `apply_rememberer_output`, `render_profile`,
`render_recalled_facts`, `embed_text`, the enqueue/sweep helpers, and the
prompts.

Acceptance: the retriever and rememberer run as bounded calls; unit tests cover
the validators and the gather.

### Phase 4 — Syscalls

`capability_registry.py`: collapse to two memory capabilities and aliases.
`action_runtime.py`: two validators, two handlers. `app.py`: the eligibility
branch.

Acceptance: a `run` program can call `memory.recall` and `memory.remember`; no
other `memory.*` syscall exists.

### Phase 5 — Turn and worker integration

`app.py`: the pre-turn retriever, profile and digest injection, context bundle
and section order, end-of-turn `memory_remember` enqueue, rotation
`memory_remember` enqueue with the carry-forward digest. `worker.py`: the
`memory_remember` and `memory_sweep` dispatch and the sweep enqueuer; delete the
projection/consolidation job processing. `proactivity.py`: the retriever and
profile in deliberation, the `memory_remember` enqueue on a `remember` decision.

Acceptance: a turn injects the profile, the digest, and a retriever digest; the
post-turn rememberer updates facts, profile, and digest; the sweep runs on
cadence.

### Phase 6 — Delete the compaction machinery

Delete the `OpenAIContextCompactionAdapter` and `validate_continuity_compaction_payload`.
Confirm the running context is bounded by the recent-turns window plus the
digest, and that conversation continuity survives a rotation via the
carry-forward digest. No summarization code remains.

Acceptance: session rotation and long conversations stay coherent; no
compaction or summarization adapter exists.

### Phase 7 — Delete the old surface

Delete the `/v1/memory/*` routes except `GET /v1/memory/facts`; delete the dead
config settings and mirror `.env.example`; delete the eval suite and fixtures;
replace the two large memory test files with `test_memory.py`; update the
run-program and acceptance suites. Confirm no reference to any deleted symbol or
table remains.

Acceptance: no deleted symbol is referenced; the HTTP memory surface is one
read route.

### Phase 8 — Docs and verification

Rewrite `memory.md`; delete the two superseded memory docs; edit `ai-first.md`,
`schema-consolidation-cutover.md`, `database.md`, `index.md`,
`modules/index.md`, `run-program-cutover.md`, and `README.md`; delete this
cutover doc. Run `make verify` and the acceptance suite.

Acceptance: `make verify` is green; the acceptance suite passes; no stale
memory-doc link remains.

## Acceptance Criteria

The cutover is complete only when all of these are true:

- `memory_facts` and `memory_profile` are the only `memory_*` tables; the other
  31 are gone. `sessions` carries a `digest` column.
- A fact has no kind, type, category, or tag field.
- `memory.py` is one flat module under ~1,000 lines.
- The model's memory surface is exactly two syscalls, `memory.recall` and
  `memory.remember`; no other `memory.*` syscall or `cap.memory.*` capability
  exists.
- Every retriever and rememberer invocation is one bounded model call with one
  `ai_judgments` row.
- Every turn injects the profile and the session digest; every proactive
  deliberation injects the profile; the retriever runs as a pre-step on both.
- The rememberer maintains the fact store, the profile, and the session digest;
  a fact it writes is `active` immediately, with no candidate or review state.
- Memory operations are never approval-gated.
- A retriever model-call failure does not fail the turn.
- No code ranks facts by relevance or applies a similarity threshold; the
  candidate gather is a generous unranked union.
- No summarization or compaction adapter exists; the session digest is the
  running continuity.
- The predicate registry, conflict-set lifecycle, RRF and the seven signals,
  projection tables, bi-temporal validity, the graph subsystem, topics, the
  sensitivity/retention/scope-binding machinery, the memory event log, version
  history, the eval suite, and import/export are all absent.
- The HTTP memory surface is one read route, `GET /v1/memory/facts`.
- `ai-first.md` carries the sharpened thesis sentence; `memory.md` is the short
  standing doc; the two superseded memory docs are deleted; no doc links a
  removed path.
- `make verify` passes.

## Key Risks

- The retriever and rememberer prompts now carry intelligence the deleted
  machinery used to encode. Mitigation: the prompts are first-class design
  artifacts with versioned constants; the `_validated_*` functions fail closed;
  behavior tests cover write-then-recall, editing, forgetting, the profile, the
  digest, and the sweep.
- The profile and digest have no deterministic size cap. If the rememberer lets
  either grow, it bloats the prompt. Mitigation: the rememberer is prompted to
  keep the profile tight and the digest no longer than useful; the sweep
  re-tightens the profile; recurrence is fixed in the prompt, not papered over
  with truncation.
- The post-turn rememberer is a background task, so the digest can be one turn
  stale relative to the live conversation. Mitigation: the verbatim recent-turns
  window covers the gap; the digest only needs to carry context older than that
  window.
- Deleting the compaction adapter assumes the recent-turns window plus the
  digest keep every turn's context bounded. A single pathologically large turn
  could still spike context. Mitigation: Phase 6 verifies the recent-turns
  window and the existing `agent.emit_value` / output guardrails bound this; no
  summarization adapter is reintroduced.
- `memory.recall` and `memory.remember` are the first `allow_inline` syscalls
  whose handlers run a model call. A call inside a running program consumes the
  program's wall-clock and host-call-time budget. Mitigation: most recall is the
  pre-turn step, not a syscall; a single bounded call fits well inside the 30 s
  sandbox backstop; budgets are measured in Phase 5.
- Deleting the candidate/review control plane removes a human checkpoint on
  what gets stored. Mitigation: this is the chosen design — AI extraction is
  the judgment — and every write is reversible by the rememberer or by the user
  saying "forget that."
- Dropping vector RRF and six signals could reduce recall quality on a large
  store. Mitigation: at single-user scale the gather is generous and the
  retriever sees most of the store; vector + keyword keep candidates bounded as
  it grows; recall quality is a retriever-prompt concern, observable in the
  `ai_judgments` log.

## Source Findings

This spec is based on a read-only survey (May 2026) by parallel sub-agents of:

- `src/ariel/memory.py`, `persistence.py`, `app.py`, `worker.py`,
  `proactivity.py`, `action_runtime.py`, `capability_registry.py`,
  `run_runtime.py`, `sandbox_runtime.py`, `config.py`, `db.py`,
  `response_contracts.py`, and the memory test suite;
- `docs/modules/memory.md`, `memory-completion-cutover.md`,
  `memory-consolidation-cutover.md`; `docs/ai-first.md`, `simplicity.md`,
  `cleanliness.md`, `north-star-cutover.md`, `run-program-cutover.md`,
  `schema-consolidation-cutover.md`, `codebase.md`, `database.md`;
- web research on shipped agent-memory architectures (ChatGPT memory,
  Anthropic's memory tool, Letta/MemGPT, Mem0, Zep/Graphiti) and the 2024–2026
  practitioner consensus on minimal agent memory.
