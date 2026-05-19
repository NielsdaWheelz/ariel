# Memory Substrate Cutover

## Scope

This document is the hard-cutover plan for Ariel's memory subsystem. It replaces
the flat fact store, the always-loaded profile, and the per-session digest with
a two-layer **memory substrate** — an append-only **raw log** and an editable
**curated layer** — and replaces deterministic candidate-gathering and
single-call extraction with **agentic recall** and a unified writer.

It owns the memory subsystem end to end. The standing design lands in
`docs/modules/memory.md`, rewritten by the final phase. The plan inherits
[ai-first.md](modules/../ai-first.md), [cleanliness.md](cleanliness.md),
[simplicity.md](simplicity.md), and builds directly on the merged
[agent-loop-cutover.md](agent-loop-cutover.md): the long adaptive loop, the
per-turn `scratch` store, the worker-run async turn, per-program commit, and the
read-only research subagent are prerequisites and are reused, not rebuilt.

It supersedes and deletes `docs/modules/memory-cutover.md` (the crystallization)
and rewrites `docs/modules/memory.md`.

The cutover is hard. There is no compatibility layer, no dual memory path, no
feature flag, no fallback, no legacy mode. Ariel holds no production memory data
worth preserving; the migration drops and recreates the `memory_*` schema
freely. Work is sequenced across phases; the merged final state is what
`make verify` proves green.

## Thesis

Memory is not a system that *contains* a model of cognition. It is a substrate
the agent *operates on*.

There are two layers:

- The **raw log** (`memory_log`) — *chronos*. An append-only, immutable stream
  of everything that happened: every user message, every agent round, every
  tool observation, every assistant message, every proactive trigger, and every
  mutation the rememberer makes to the curated layer. It is never edited and
  never deleted. It is the ground truth.
- The **curated layer** (`memory_notes`) — *kairos*. A flat, freely editable
  set of notes the agent authors: facts, summaries, generalizations,
  connections, consolidations — whatever the agent makes of the raw log. It is
  a re-derivable projection; editing it loses nothing, because the raw log holds
  the history (including the logged history of every note mutation).

Three things act on the substrate, and all three are **one agent loop in
different configurations**:

- The **retriever** reconstructs the working context, agentically, on every
  wake — query, read, follow up, repeat, bounded — over the substrate.
- The **rememberer/dreamer** writes the curated layer: *deliberate encoding*
  when the agent asks, and offline *consolidation* ("dreaming") on a schedule.
- The **researcher** investigates external sources, and now `memories` is one
  of its sources.

Code is the enabler and the floor — durable storage, the search index, the
loop, taint, audit — and nothing else. Code never ranks, scores, classifies,
decays, weights, or shapes what is remembered or recalled. Every memory judgment
is the model's, made by operating on the substrate. The system's quality is a
function of model capability, not of hand-tuned code: this is the bet, and it is
deliberate (Sutton's bitter lesson — general methods that leverage computation
beat systems that encode human knowledge of the domain).

## What This Replaces

The current subsystem (the May 2026 "memory crystallization") and a survey of
the post-agent-loop-cutover code found these faults:

- **Write-time lossy extraction.** The `rememberer` runs automatically after
  every turn and distills the conversation into discrete `memory_facts`. The
  extraction is a bottleneck decided by *today's* model; whatever it drops is
  gone. The raw turn exists in the `turns` table but is never retrievable as
  memory.
- **A human-designed schema.** Memory is carved into `memory_facts` +
  `memory_profile` + `sessions.digest` — three fixed shapes. The carving is a
  guess, and code enforces it.
- **Flat, one-shot retrieval.** `gather_candidates` builds an unranked
  vector+keyword+recency union; `run_retriever` is a single bounded model call
  that picks a subset. There is no follow-up, no multi-hop, no iteration.
- **Always-loaded documents that grow unbounded.** The profile and the digest
  are injected into every turn with no size bound; the survey confirmed neither
  has any cap. The digest is also one rememberer-cycle stale by construction.
- **No within-turn memory.** Post-agent-loop-cutover the loop is long, but its
  only context eviction is an `emit_value` tail-truncate. A long turn's round
  history — every `run` program source, every syscall trace — accumulates in
  context, never evicted, and is never written anywhere retrievable.
- **Dead context machinery.** `max_context_tokens` is surfaced but never
  enforced; `_count_context_tokens` counts whitespace words, not tokens; the
  only live context-pressure mechanism is session rotation on that word count.

These are replacement targets, not compatibility promises.

## Target Architecture

### The raw log — `memory_log`

An append-only, immutable table. One row per event. Rows are never updated and
never deleted (the sole exception: a privileged operator purge for genuine
privacy/legal erasure — see Non-Goals).

The log unifies all three scales the survey identified as the same problem:
within-turn rounds, within-session turns, and cross-session history are all just
events in the one log.

Columns:

| Column | Type | Notes |
|---|---|---|
| `id` | String(32) PK | id-prefix `mev` |
| `created_at` | DateTime(tz), not null | when the event occurred |
| `kind` | String(32), not null | event-type provenance — `user_message`, `agent_round`, `assistant_message`, `tool_observation`, `proactive_trigger`, `note_create`, `note_edit`, `note_delete`, `recall`, `research_finding`. Mechanical, not a cognitive class (see Key Decisions). |
| `content` | Text, not null | the event payload, plain text or serialized |
| `embedding` | Vector, null | pgvector, HNSW-indexed; null = pending |
| `search_vector` | TSVECTOR | generated from `content`, GIN-indexed |
| `session_id` | String(32), null, FK→`sessions` | grouping |
| `turn_id` | String(32), null, FK→`turns` | grouping |
| `taint` | per the taint model | the trust label of the event's content |
| `source_ref` | Text, null | a pointer to the originating record where one exists |

No `updated_at`, no `status`, no version column — append-only is the invariant.

Events are written by **rails**, not by the model. As a turn runs, the turn
engine appends: the user message, each completed agent round (the `run` program
the model wrote plus its syscall results), the assistant message. Proactive
triggers append on wake. The rememberer's note mutations append a
`note_create`/`note_edit`/`note_delete` event each — so the full version history
of the curated layer lives in the log. The model never writes the log directly;
the log records what happened.

### The curated layer — `memory_notes`

A flat, editable table. One row per note. The agent authors and shapes it.

| Column | Type | Notes |
|---|---|---|
| `id` | String(32) PK | id-prefix `mno` |
| `content` | Text, not null | the note, in plain language, whatever shape the agent chooses |
| `embedding` | Vector, null | pgvector, HNSW-indexed; null = pending |
| `search_vector` | TSVECTOR | generated from `content`, GIN-indexed |
| `created_at` | DateTime(tz), not null | |
| `updated_at` | DateTime(tz), not null | |
| `taint` | per the taint model | the trust label of the note's content |

No `kind`, type, category, or tag — the layer is flat; the rememberer decides
what a note says and the retriever decides when it matters, both by reading
content. No `status` — a note is live; editing rewrites it; deleting removes it;
the log preserves the trail. There is no profile and no digest: a note is the
only curated shape, and the agent makes of it what it will (a durable fact, a
user-preference, a topic summary, a generalization, a cross-reference to other
notes or log events by id, a dream-time consolidation).

### One loop, three configurations

The agent-loop cutover left two structurally identical sibling loops — `_wake`
and `run_research`. This cutover extracts the shared loop, `run_agent_loop`, and
the loop runs in three configurations. A **configuration** is a capability
whitelist, a budget, an output mode, and a system prompt.

- **main** — the user-facing agent. Every eligible capability; output mode
  `message`; the conversational prompt. Driven by `_wake`.
- **investigation** — read-only agentic search. Output mode `finding`. Two axes:
  a **domain** (`memories` | `web` | `personal`) and a **preset** (`lite` |
  `heavy`). The *retriever* is `investigation` / `memories` / `lite`. The
  *researcher* is `investigation` / any domain / `heavy`.
- **rememberer** — reads the substrate and writes the curated layer. Output mode
  `operations`. Two triggers: `encode` (small scope, agent-invoked) and `dream`
  (large scope, scheduled).

The loop body is one function. `_wake`, the retriever, the researcher, and the
rememberer are thin drivers around `run_agent_loop` with a configuration. This
is the agent-loop cutover's "one loop, configurations" model, completed.

### The retriever — agentic recall, fired every wake

The retriever runs `run_agent_loop` in `investigation` / `memories` / `lite`. It
is **fired automatically as a pre-turn step on every wake** — user message,
proactive trigger, capture, research completion — with no exception. Its job is
to **reconstruct the working context**: it is seeded with the wake context, it
searches the substrate (`memory.search`), reads what it finds (`memory.read`),
follows up, and repeats until satisfied or its budget is spent. It runs in its
own context (the **context firewall** — its query/read/follow-up rounds never
enter the main agent's context); it returns only a structured `recall_v1`
finding, which the turn engine renders into the main agent's starting context.

This is the single mechanism for continuity. There is no verbatim recent-turns
window, no profile, no digest. "Old stuff from the same conversation" is recalled
exactly like anything else — it is just recent, highly relevant memory. The
retriever covers both relevance-based recall (semantic + keyword) and
recency-based recall (recent events in the session); the system prompt directs
it to reconstruct enough working context to act, including recent continuity.

The retriever is **also a tool**: the main agent calls `memory.recall(query)`
mid-loop when it discovers it needs more. The `lite` preset is bounded small
enough to run inline as a synchronous syscall.

### The researcher — `memories` becomes a third domain

The research subagent (`research_runtime.py`, `research.investigate`) gains a
third domain: `memories`. It is `investigation` / `memories` / `heavy` — the
delegated, context-firewalled, generously-budgeted form of memory search, for
when the agent wants a deep memory dig done off its own context. `web` and
`personal` are unchanged.

The agent-loop cutover's one-domain-per-run rule holds and extends: a run is in
exactly one domain. `memories` must be **mutually exclusive with `web`** for the
same reason `personal` is — memory holds content derived from private sources,
so a single run that reads memory and reaches the open web re-opens the
exfiltration path. This is the security rail (see Key Decisions).

### The rememberer/dreamer — the writer

The rememberer runs `run_agent_loop` in the `rememberer` configuration: it may
read the substrate (`memory.search`, `memory.read`) and write the curated layer
(`memory.note.create`, `memory.note.edit`, `memory.note.delete`). It cannot
reach the web, personal data, or any main-agent capability. It is a loop — for
`encode` it typically terminates in a few rounds; for `dream` it runs long.

- **`encode`** — *deliberate effort to remember*. The main agent calls
  `memory.remember(note)`; a `memory_encode` task is enqueued; the rememberer
  runs in the worker, reads the relevant substrate so it can edit rather than
  duplicate, and writes/edits notes. Fire-and-forget — the main agent does not
  block on it. Raw capture already recorded the turn in the log, so the encode
  is an enrichment, never the only copy.
- **`dream`** — *consolidation*. A scheduled `memory_dream` background task. The
  rememberer reads swaths of the raw log and the curated layer and consolidates:
  it induces higher-order structure — generalizations, summaries, connections,
  schemas — and writes them as notes (*kairos* distilled from *chronos*). It
  may edit or delete superseded notes. Append-only is preserved: every note
  mutation it makes is logged; the raw log is never touched.

Every note mutation — by `encode` or `dream` — is applied by a rail that also
appends the corresponding `note_*` event to `memory_log`. The curated layer is
mutable; its history is immutable, in the log.

### Recall as working-context reconstruction

The brain does not keep a persistent "working note." Every cognitive cycle the
situational model is reconstructed from long-term memory plus current input. So
does Ariel: every wake, the retriever reconstructs the working context. The only
thing always present without recall is the **static system prompt**. There is no
working note, no profile, no digest — by design.

A turn's context at the first model call is exactly: the system prompt, the
retriever's `recall_v1` reconstruction, and the current wake input. From there
the loop runs.

### Within-turn — round eviction to the log

Each completed round appends an `agent_round` event to `memory_log` (a rail, in
the per-program commit). The live `responses_input_items` keeps only the last
`agent_loop_live_rounds` rounds verbatim; older round items — the `function_call`
program source, the `function_call_output` syscall trace, the system nudges —
are evicted from the live context. They are not lost: they are in the log, and
the agent recalls them with `memory.recall` if a later round needs them.

This is the long-turn compaction problem, dissolved: it is the same raw-log +
agentic-recall mechanism as cross-turn memory, at the round scale. The
agent-loop cutover's `emit_value` tail-truncate is replaced by this general
round-history eviction.

## Target Behaviour

### A user turn

1. A user message arrives; the worker takes the `user_message` task and calls
   `_wake`.
2. The turn engine appends a `user_message` event to `memory_log`.
3. The turn engine fires the **retriever** (`investigation`/`memories`/`lite`),
   seeded with the wake context. It runs its bounded agentic search in a
   firewalled context and returns a `recall_v1` reconstruction.
4. `run_agent_loop` runs in `main` config. The starting context is the system
   prompt + the `recall_v1` reconstruction + the user message.
5. Each round: the model writes a `run` program; it executes; the round commits;
   an `agent_round` event is appended to `memory_log`; round history beyond the
   live window is evicted from `responses_input_items`.
6. The loop ends on `agent.emit_message`. An `assistant_message` event is
   appended to `memory_log`. The worker delivers the message.

### A long adaptive turn

The loop runs many rounds. Old rounds evict to the log; if round 50 needs what
round 4 saw, the model calls `memory.recall`. Large intermediate data stays in
the `scratch` store (agent-loop cutover), off-context, as before. The turn's
context stays bounded by the live-rounds window plus the reconstruction.

### Deliberate remembering

Mid-turn or at its end, the agent decides something is worth keeping and calls
`memory.remember(note)`. A `memory_encode` task is enqueued; the agent does not
wait. The worker runs the rememberer in `encode`; it reads the relevant
substrate and writes or edits `memory_notes`; each mutation is logged.

### Dreaming

On the `memory_dream` schedule, the worker runs the rememberer in `dream`. It
reads recent raw log and the curated layer, consolidates, and writes notes —
generalizations, summaries, connections. Superseded notes are edited or deleted
in place; the raw log is untouched; every mutation is logged.

### A recall mid-loop

The agent calls `memory.recall(query)`. The lite retriever runs inline,
firewalled, bounded; it returns a `recall_v1`. Only the result enters the
agent's context; the retriever's own search rounds do not.

### A deep memory investigation

The agent calls `research.investigate(question, mode="memories")`. A
`research_run` task is dispatched; the agent acknowledges and ends its turn; the
heavy memory investigation runs in the worker, firewalled; the finding returns
as an `agent_wake`, tainted, exactly as a `web` or `personal` finding does.

### A proactive wake

A provider push, poll, or scheduled task enqueues an `agent_wake`. The turn
engine appends a `proactive_trigger` event, fires the retriever, and runs the
`main` loop — identical to a user turn. The retriever fires on every wake with
no exception; the old "ambient interpretation gets no retriever" carve-out is
removed.

## Composition With Existing Systems

- **The agent loop.** This cutover extracts `run_agent_loop` from `_wake` and
  `run_research` and adds the three configurations. Async worker-run turns,
  per-program commit, the `scratch` store, worker-run delivery, the wall-clock
  budgets, and stuck-detection are all unchanged and reused.
- **The research subagent.** Gains the `memories` domain and shares
  `run_agent_loop`. The one-domain-per-run rule is unchanged; `memories` is
  mutually exclusive with `web`. The retriever is the `lite` preset of the same
  `investigation` configuration.
- **Proactivity.** Every trigger still wakes through `_wake`. The retriever now
  fires on every wake, proactive ones included.
- **The turn lifecycle.** `turns` and `action_attempts` are unchanged —
  they remain operational rails (turn status, the audit/idempotency spine).
  `memory_log` is the memory-facing substrate written alongside them; the modest
  text overlap between a `turns` row and its `memory_log` events is intentional,
  not duplication to collapse (different owners — operational vs. memory).
- **Sessions.** The `sessions.digest` column is dropped. The context-pressure
  rotation trigger (`auto_rotate_context_pressure_tokens`) and the dead
  `max_context_tokens` / `_count_context_tokens` machinery are deleted —
  context is reconstructed every turn, not accumulated, so there is nothing to
  rotate for pressure. Session rotation on turn-count and age is otherwise out
  of scope and unchanged.
- **The worker & `background_tasks`.** `task_type` drops `memory_remember` and
  `memory_sweep` and gains `memory_encode` and `memory_dream`. The worker gains
  a dispatch arm for each.
- **Taint.** Every `memory_log` and `memory_notes` row carries the taint of its
  content. A `recall_v1` finding is tainted; the main agent treats recalled
  memory as untrusted exactly as it treats a research finding or a fetched page;
  any action it motivates routes through `requires_approval`. Code owns taint
  propagation — it is a security rail, not a memory judgment.
- **Audit.** Every model call in every configuration writes one `ai_judgments`
  row, as today. `judgment_type` becomes `memory_recall` (retriever),
  `memory_encode` and `memory_dream` (rememberer), and the research type for
  `research.investigate`. `ai_judgments` is the complete memory audit trail.
- **Embeddings.** `embed_text` and the `memory_embedding_*` settings are reused.
  Both substrate tables carry `embedding`; population may be synchronous on
  write or by a background pass (`embedding` nullable, null = pending).

## Capability Contract

The model-facing memory surface, by configuration.

### `main` configuration

- **`cap.memory.recall`** — `allow_inline`, `impact_level` read. Run-callable
  `memory.recall(query: str)`. Runs the lite retriever inline, host-side,
  firewalled; returns a `recall_v1` finding. The lite preset is budgeted to fit
  the inline syscall backstop.
- **`cap.memory.remember`** — `allow_inline`, `impact_level` `write_reversible`.
  Run-callable `memory.remember(note: str)`. Enqueues a `memory_encode` task;
  returns `{status: "queued", encode_id}`. Fire-and-forget.
- **`cap.research.investigate`** — unchanged except the `mode` enum gains
  `memories`. Run-callable `research.investigate(question, mode)`.

### `investigation` configuration (`memories` domain)

The retriever and the `memories`-domain researcher whitelist:

- **`cap.memory.search`** — `impact_level` read. `memory.search(query: str,
  limit: int = ..., since: datetime | None = None, kinds: list[str] | None =
  None)`. A hybrid semantic + keyword search over `memory_log` and
  `memory_notes`. Returns matching rows as `{id, layer, kind, created_at,
  snippet, taint}`. `since`/`kinds` are mechanical filters (a temporal/typed
  query), never a relevance ranking — results are returned for the model to
  judge.
- **`cap.memory.read`** — `impact_level` read. `memory.read(id: str)`. Returns
  the full `content` (and metadata, taint) of a log event or a note by id —
  the "follow up" primitive, including following a note's references to other
  ids.

### `rememberer` configuration

The rememberer whitelists `cap.memory.search`, `cap.memory.read`, and:

- **`cap.memory.note.create`** — `memory.note.create(content: str)`. Inserts a
  `memory_notes` row; the handler computes its embedding and appends a
  `note_create` event to `memory_log`.
- **`cap.memory.note.edit`** — `memory.note.edit(id: str, content: str)`.
  Rewrites a note's content; recomputes the embedding; appends a `note_edit`
  event recording the note id and the new content.
- **`cap.memory.note.delete`** — `memory.note.delete(id: str)`. Deletes the
  note row; appends a `note_delete` event.

The `web` and `personal` investigation whitelists are unchanged.

Capability mode whitelists are module-level frozensets in
`capability_registry.py`, beside the existing `RESEARCH_WEB_CAPABILITY_IDS` /
`RESEARCH_PERSONAL_CAPABILITY_IDS`:
`RESEARCH_MEMORIES_CAPABILITY_IDS = {cap.memory.search, cap.memory.read}` and
`REMEMBERER_CAPABILITY_IDS = {cap.memory.search, cap.memory.read,
cap.memory.note.create, cap.memory.note.edit, cap.memory.note.delete}`.

## API Design

### Findings and outputs

- **`recall_v1`** — the retriever's output and the payload the turn engine
  renders into context: `{ summary: str (the reconstructed working context),
  items: list[{ id, layer: "log"|"note", created_at, content, taint }],
  status: "complete" | "partial" }`. `partial` on budget exhaustion. The text
  is model-authored over (possibly tainted) substrate content; it is carried
  and rendered with tainted provenance.
- **`research_finding_v1`** — unchanged (agent-loop cutover); reachable now for
  `mode="memories"`.
- The rememberer's output mode is `operations` — a validated list of
  `note.create` / `note.edit` / `note.delete` calls applied by the rail.

### Syscalls

`memory.recall`, `memory.remember`, `memory.search`, `memory.read`,
`memory.note.create`, `memory.note.edit`, `memory.note.delete`,
`research.investigate`. The model's tool surface stays exactly `run`; these are
syscalls, whitelisted per configuration.

### HTTP

The operator inspection surface: `GET /v1/memory/log` and `GET /v1/memory/notes`
(read-only, paginated) replace `GET /v1/memory/facts`. No other memory HTTP
routes. Genuine privacy/legal erasure is a privileged operator route,
`DELETE /v1/memory/log/{id}` — the one sanctioned exception to log immutability.

## Data Model And Configuration

Schema delta: drop `memory_facts`, `memory_profile`, and the `sessions.digest`
column; create `memory_log` and `memory_notes` with their HNSW (`embedding`) and
GIN (`search_vector`) indexes. Net `memory_*` table count is unchanged (two
before, two after). Foreign keys follow [database.md](database.md)
(`ondelete=RESTRICT`).

`background_tasks.task_type` CHECK enum: drop `memory_remember`, `memory_sweep`;
add `memory_encode`, `memory_dream`. `ai_judgments.judgment_type` CHECK enum:
keep `memory_recall`; drop `memory_remember`; add `memory_encode`,
`memory_dream`.

Configuration (`config.py`, `ARIEL_` prefix, mirrored in `.env.example`):

- **Removed:** `memory_recall_candidate_limit`, `memory_sweep_interval_seconds`,
  `max_recent_turns`, `max_context_tokens`, `auto_rotate_context_pressure_tokens`
  and its validator.
- **Added:** `memory_recall_budget_seconds` (float — the lite retriever; small
  enough to run inline), `memory_dream_budget_seconds` (float),
  `memory_encode_budget_seconds` (float), `memory_dream_interval_seconds`
  (float), `agent_loop_live_rounds` (int — the within-turn verbatim round
  window). Each validated positive.
- **Reused, unchanged:** `memory_embedding_provider` / `_model` / `_dimensions`,
  `main_turn_budget_seconds`, `research_run_budget_seconds`,
  `agent_loop_max_model_calls`.

## Key Decisions

- **Two layers — append-only raw log, editable curated layer.** Splitting
  *what happened* (immutable history) from *what the agent currently
  understands* (an editable projection) dissolves the edit-vs-append question:
  the raw log is never edited; the curated layer is freely edited; nothing is
  lost because the log holds the history, including every logged note mutation.
- **Edit the curated layer in place.** A superseded note is edited, not
  version-stacked — cleaner, and more aligned with how semantic memory updates.
  History is not lost: the rememberer's edits are themselves `note_edit` events
  in the raw log. A wrong edit self-heals — the next `dream` pass re-derives the
  note correctly from the immutable log. No versioning machinery is needed.
- **Read-time, not write-time.** Raw capture is automatic and lossless; there is
  no automatic post-turn extraction. Interpretation happens at recall time, by
  the newest model, agentically. The curated layer is an enrichment, never the
  only copy.
- **One loop, three configurations.** The retriever, researcher, rememberer, and
  main agent are one `run_agent_loop` with a configuration. The agent-loop
  cutover's "one loop, configurations" model, completed. No orchestration layer.
- **Agentic recall, not a gather.** Retrieval is search → read → follow up →
  repeat until satisfied. Deterministic candidate-gathering, RRF, similarity
  thresholds, and relevance ranking are deleted — search is a primitive the
  model drives, not a pipeline code tunes.
- **Recall is bounded.** "Until satisfied" is the model's call; a wall-clock
  budget and `agent_loop_max_model_calls` and stuck-detection are the backstop —
  not for cost, but because an unbounded loop is a reliability hazard, the
  lesson the agent-loop cutover already encoded.
- **No working note.** The only always-present context is the static system
  prompt. The profile and digest are deleted with no document replacing them;
  the retriever reconstructs the working context every wake. Persistence lives
  in long-term memory; "loading" is reconstruction.
- **The retriever fires on every wake.** No exceptions, ambient triage included.
  The old ambient carve-out was a cost optimization; cost is deferred.
- **`memories` is a research domain under the one-domain-per-run rule**, and is
  mutually exclusive with `web` — memory carries content derived from private
  sources, so memory-read plus open-web-reach in one run is the lethal-trifecta
  exfiltration path. This is a security rail and it stays.
- **Code is the floor, never the guide.** Code owns durable storage, the
  embedding/keyword index, the search primitives, the loop, taint, and audit. It
  never ranks, scores, classifies for relevance, decays, weights, or summarizes.
  `kind` on a log event is mechanical event-type provenance — code records and
  filters on it; code never judges relevance or meaning with it.
- **The bet.** The design's quality scales with model capability and context
  economics, not with hand-tuned code. Cost and latency are deferred — "perfect
  but slow first, optimize later." This is deliberate and is the whole point.

## Rules

Standing rules for the memory module after the cutover; they belong in the
rewritten `docs/modules/memory.md`.

- Memory is two layers: an append-only immutable `memory_log` and an editable
  `memory_notes`. The log is never edited or deleted by any normal code path or
  by the model.
- Every memory judgment — what to recall, what to encode, what to consolidate,
  what to supersede — is the model's, made by `run_agent_loop` in the
  `investigation` or `rememberer` configuration.
- Deterministic code stores the substrate, maintains the embedding and keyword
  indexes, exposes the search/read/write primitives, runs the loop, propagates
  taint, and writes audit rows. It performs no relevance, importance,
  categorization, ranking, decay, or "worth remembering" judgment, and it
  summarizes nothing.
- The raw log is written only by rails capturing events. The curated layer is
  written only by the rememberer. The main agent never writes either directly.
- Every note mutation appends a `note_*` event to the raw log.
- The retriever fires as a pre-turn step on every wake and is also the
  `memory.recall` syscall. It reconstructs the working context; there is no
  profile, digest, working note, or verbatim recent-turns window.
- Every retriever, rememberer, and researcher model call writes one
  `ai_judgments` row, on success and failure.
- Recall is bounded by a wall-clock budget, the model-call backstop, and
  stuck-detection. Recall failure is non-fatal — the turn proceeds on the
  system prompt alone.
- Recalled memory is tainted; any action it motivates routes through
  `requires_approval`.
- New memory machinery — schemas on a fact, scorers, rankers, decay functions,
  graph algorithms, projection tables, candidate-gather pipelines, importance
  weights, consolidation schedules with cognitive math — is forbidden. A product
  need is met by a configuration's prompt, never by code.

## Non-Goals

- Do not keep `memory_facts`, `memory_profile`, `sessions.digest`,
  `gather_candidates`, RRF, similarity thresholds, the single-call retriever, or
  the automatic post-turn rememberer.
- Do not add a working note, profile, digest, or any always-loaded document
  beyond the static system prompt.
- Do not add a category/kind/type field to a note, or any relevance score,
  decay function, activation model, or graph algorithm. "Connections" are
  content the agent writes (a note referencing other ids); traversal is the
  agent reading.
- Do not redesign the turn lifecycle, `turns`, `action_attempts`, or sessions
  beyond dropping `sessions.digest` and the context-pressure rotation trigger.
- Do not change the agent-loop cutover's async-turn, per-program-commit,
  `scratch`, delivery, or budget mechanics — build on them.
- Do not make the raw log mutable. Genuine privacy/legal erasure is a single
  privileged operator route, outside the normal flow, and is the only exception.
- Do not weight-level / fine-tune memory — Ariel runs a hosted model.
- No compatibility layer, dual path, feature flag, fallback, or legacy mode.

## The Cutover

Eight phases. Intermediate phases are not independently `make verify`-green — a
hard schema-plus-code cutover cannot be — but the merged final state is. Each
migration runs up and down.

### Phase 1 — Failing contract tests

Tests that fail against `main` and define the target: `memory_log` and
`memory_notes` exist and `memory_facts`/`memory_profile`/`sessions.digest` do
not; the log rejects update and delete; a note edits in place; `run_agent_loop`
exists and runs in three configurations; the retriever fires pre-turn and
returns a `recall_v1`; `memory.recall`/`memory.remember`/`memory.search`/
`memory.read`/`memory.note.*` exist and the old `cap.memory.*` set does not;
`research.investigate` accepts `mode="memories"`; a turn injects no profile and
no digest; an encoded note and a dreamed note appear; recall failure is
non-fatal.

### Phase 2 — Schema

Migration: drop `memory_facts`, `memory_profile`, `sessions.digest`; create
`memory_log` and `memory_notes` with indexes; amend the `background_tasks` and
`ai_judgments` CHECK enums. Update `persistence.py` models, `db.py`
`REQUIRED_TABLES`. Working `downgrade()`.

### Phase 3 — Extract the shared loop

Extract `run_agent_loop` from `_wake` and `run_research`; introduce the
configuration object (whitelist, budget, output mode, prompt) and the
`main` / `investigation` / `rememberer` configurations. `investigation` gains
the `domain` and `preset` axes. `_wake` and `run_research` become thin drivers.
Round-history eviction to a live window is added to the loop.

### Phase 4 — The memory module

Rewrite `memory.py`: `memory_log` and `memory_notes` reads/writes (rails); the
event-append rail; the hybrid search behind `memory.search` and `memory.read`;
the retriever (`run_agent_loop` in `investigation`/`memories`/`lite`) and its
`recall_v1` validation; the rememberer (`run_agent_loop` in `rememberer`,
`encode` and `dream` triggers) and its `operations` validation and apply path;
`embed_text`; the `memory_encode`/`memory_dream` enqueuers.

### Phase 5 — Capabilities

`capability_registry.py` / `action_runtime.py`: define `cap.memory.recall`,
`cap.memory.remember`, `cap.memory.search`, `cap.memory.read`,
`cap.memory.note.create/edit/delete`; delete the old `cap.memory.*` set; add the
`memories` mode to `research.investigate` and the
`RESEARCH_MEMORIES_CAPABILITY_IDS` / `REMEMBERER_CAPABILITY_IDS` whitelists.

### Phase 6 — Turn integration

`app.py`: fire the retriever as a pre-turn step on every wake; rewrite
`_build_responses_input_items` to inject system prompt + `recall_v1`
reconstruction + wake input only — delete profile, digest, recalled-facts, and
verbatim-window injection; append `memory_log` events for the user message,
each round, the assistant message, and proactive triggers; wire round-history
eviction. Delete the context-pressure rotation trigger, `max_context_tokens`,
and `_count_context_tokens`.

### Phase 7 — The writer worker path

`memory.remember` enqueues a `memory_encode` task; add a scheduled
`memory_dream` enqueuer; the worker gains `memory_encode` and `memory_dream`
dispatch arms; delete the `memory_remember` and `memory_sweep` dispatch and
enqueuers.

### Phase 8 — Delete the old surface and reconcile docs

Delete `run_retriever`, `run_rememberer`, `gather_candidates`, the profile/digest
code paths, dead config, and replace `GET /v1/memory/facts` with
`/v1/memory/log` and `/v1/memory/notes`. Rewrite `docs/modules/memory.md` to the
standing module doc; reconcile `ai-first.md` (the Memory section), `database.md`,
`docs/index.md`, `docs/modules/index.md`; delete `docs/modules/memory-cutover.md`
and this cutover doc. Run `make verify` and the acceptance suite.

## Files

- `src/ariel/memory.py` — rewritten: substrate reads/writes, event-append rail,
  hybrid search, the retriever, the rememberer, embedding, enqueuers.
- `src/ariel/app.py` — `run_agent_loop` extraction; the pre-turn retriever step;
  `_build_responses_input_items` rewrite; `memory_log` event appends;
  round-history eviction; deletion of profile/digest/window/rotation-pressure.
- `src/ariel/research_runtime.py` — `run_agent_loop` adoption; the `memories`
  domain.
- `src/ariel/run_runtime.py` — the loop configuration plumbing if the shared
  loop lands here rather than in `app.py`.
- `src/ariel/capability_registry.py` — the new memory capability set, the
  `memories` mode, the two new whitelists.
- `src/ariel/action_runtime.py` — handlers for the new memory capabilities.
- `src/ariel/worker.py` — `memory_encode` / `memory_dream` dispatch; deletion of
  `memory_remember` / `memory_sweep`.
- `src/ariel/persistence.py` — `MemoryLogRecord`, `MemoryNoteRecord`; deletion
  of `MemoryFactRecord`, `MemoryProfileRecord`, `sessions.digest`; CHECK enums.
- `src/ariel/config.py`, `.env.example` — the config delta.
- `src/ariel/db.py` — `REQUIRED_TABLES`.
- `src/ariel/response_contracts.py` — `recall_v1`; the memory HTTP contracts.
- `alembic/versions/` — the schema migration.
- `docs/modules/memory.md` — rewritten; `docs/modules/memory-cutover.md` and
  this doc — deleted in Phase 8.
- `tests/integration/test_memory.py` — rewritten for the new subsystem.

## Acceptance Criteria

The cutover is complete only when all hold:

- `memory_log` and `memory_notes` are the only `memory_*` tables; `memory_facts`,
  `memory_profile`, and `sessions.digest` are gone.
- `memory_log` is append-only — no code path updates or deletes a row except the
  privileged operator erasure route.
- `run_agent_loop` is one function, run in exactly three configurations.
- The retriever fires as a pre-turn step on every wake and returns a `recall_v1`;
  a turn's starting context contains the system prompt, the reconstruction, and
  the wake input, and nothing else — no profile, no digest, no verbatim window.
- Memory retrieval is agentic — search, read, follow up, repeat — bounded by a
  budget, the model-call backstop, and stuck-detection; `gather_candidates`,
  RRF, and any relevance ranking are gone.
- The rememberer runs in `encode` (agent-invoked, dispatched) and `dream`
  (scheduled); there is no automatic post-turn extraction; every note mutation
  appends a `note_*` event to the log.
- `research.investigate` accepts `mode="memories"`; a `memories` run cannot
  reach the web, and `memories` is mutually exclusive with `web`.
- A long turn's round history evicts to the log and is recoverable by
  `memory.recall`.
- Every retriever, rememberer, and researcher model call writes one
  `ai_judgments` row; recalled memory is tainted.
- The model-facing memory syscalls are exactly `memory.recall`,
  `memory.remember`, `memory.search`, `memory.read`, `memory.note.create`,
  `memory.note.edit`, `memory.note.delete`, plus `research.investigate`; no
  `cap.memory.*` from the crystallization survives.
- No relevance score, decay function, activation model, graph algorithm,
  candidate-gather, ranking, importance weight, profile, digest, or working note
  exists anywhere in memory code.
- `docs/modules/memory.md` is the standing doc; `memory-cutover.md` and this doc
  are deleted; no doc references a removed path.
- `make verify` passes; every migration runs up and down.

## Risks

- **Recall is now the whole game.** With no always-loaded profile or digest,
  every turn's competence depends on the retriever. Mitigation: the retriever is
  agentic (the strongest current pattern), covers both relevance and recency,
  and can re-search; dreaming keeps the curated layer recall-friendly; recall
  failure is non-fatal; the bet is deliberate and explicitly accepted.
- **Pure reconstruction may miss recent continuity.** No verbatim recent-turns
  window means "what the user just said" is recalled, not pinned. Mitigation:
  the retriever does recency-based recall of recent session events; the just-
  finished turn is maximally relevant; if it proves unreliable a recency recall
  is a prompt fix, not a schema change.
- **The loop extraction is invasive.** Extracting `run_agent_loop` from `_wake`
  and `run_research` touches the core. Mitigation: contract tests first; the
  agent-loop cutover just worked this code, so it is fresh and well-understood.
- **Latency.** Every wake now begins with a bounded agentic retrieval, and the
  retriever fires on proactive and ambient wakes too. Mitigation: the `lite`
  preset is small; cost and latency are deferred by explicit decision; if a wake
  class proves too slow the retriever preset is tunable.
- **The raw log grows without bound.** Append-only forever, every round
  included. Mitigation: storage cost is deferred; archival/cold-tiering of the
  log is a future optimization, not a launch blocker; nothing in the design
  breaks as the log grows — search degrades gracefully and dreaming keeps the
  curated layer the common recall path.
- **A wrong rememberer edit.** Mitigation: self-healing — the next `dream` pass
  re-derives the note from the immutable log; the log always holds the truth.
- **Prompt injection via recalled memory.** Memory holds tainted content.
  Mitigation: taint propagates on every row and into the `recall_v1` finding;
  the main agent treats recalled memory as untrusted; every action routes
  through `requires_approval`.
- **The lite retriever must fit an inline syscall budget.** `memory.recall` runs
  the retriever synchronously within the calling program. Mitigation:
  `memory_recall_budget_seconds` is set and validated against the sandbox
  host-call backstop in Phase 4; heavy memory investigation is the async
  `research.investigate(mode="memories")` path, not `memory.recall`.
