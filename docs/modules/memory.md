# Memory

## Scope

This document owns Ariel's memory subsystem: the two-layer substrate, the memory
loop drivers, the capability surface, the background tasks, and the HTTP
inspection routes.

Memory follows [../ai-first.md](../ai-first.md): every memory judgment is the
model's, made by `run_agent_loop` in the retriever or rememberer driver;
deterministic code owns only rails.

## The Two Layers

### `memory_log` — the raw log

Append-only, immutable. One row per event. No code path updates or deletes a row.

| Column | Notes |
|---|---|
| `id` | String(32) PK, prefix `mev` |
| `created_at` | DateTime(tz), not null — when the event occurred |
| `kind` | String(32), not null — mechanical event-type: `user_message`, `agent_round`, `assistant_message`, `tool_observation`, `proactive_trigger`, `note_create`, `note_edit`, `note_delete`, `recall`, `research_finding` |
| `content` | Text, not null — the event payload |
| `embedding` | Vector, null — pgvector, HNSW-indexed; null = pending |
| `search_vector` | TSVECTOR — generated from `content`, GIN-indexed |
| `session_id` | String(32), null, FK→`sessions` |
| `turn_id` | String(32), null, FK→`turns` |
| `taint` | trust label of the event's content |
| `source_ref` | Text, null — pointer to the originating record |

No `updated_at`, no `status`, no version column. Events are written by rails
only: the turn engine appends each user message, agent round, and assistant
message; proactive triggers append on wake; the rememberer's note mutations each
append a `note_create`/`note_edit`/`note_delete` event. The model never writes
the log directly.

`append_log_event` skips one specific class of `assistant_message` rows at the
write site: assistant failure prose such as "Calendar fetch failed", "Email
errored", "re-link in settings", and similar. `memory.py` owns the tight
predicate so that legitimate "no X found" messages still write. The same check
runs at the retrieval site (`search_memory`) so directly inserted failure rows
already present in the append-only log never surface in `memory.search` /
`memory.recall` results. Without this, those rows would surface in the
retriever as authoritative evidence of failure even after the capability has
recovered, making the model repeat the same failure prose.

### `memory_notes` — the curated layer

Flat, editable. One row per note. The agent authors and shapes it.

| Column | Notes |
|---|---|
| `id` | String(32) PK, prefix `mno` |
| `content` | Text, not null — plain language, whatever shape the agent chooses |
| `embedding` | Vector, null — pgvector, HNSW-indexed; null = pending |
| `search_vector` | TSVECTOR — generated from `content`, GIN-indexed |
| `created_at` | DateTime(tz), not null |
| `updated_at` | DateTime(tz), not null |
| `taint` | trust label of the note's content |

No `kind`, category, tag, or status column. A note is live; editing rewrites it;
deleting removes it; the log preserves the full mutation trail.

See `alembic/versions/` for the migration that creates these two tables and
enforces append-only log behavior.

## Memory Loop Drivers

`run_agent_loop` is the shared loop. Memory owns two thin drivers around it:

- **retriever** — read-only memory recall. It searches and reads the substrate,
  emits a `recall_v1` finding, writes `memory_recall` judgments, and runs before
  every wake.
- **rememberer** — curated-layer mutation. It searches and reads the substrate,
  calls `memory.note.*`, emits an operations summary, and runs on the `encode`
  and `dream` triggers.

The main agent (`_wake`) and the research subagent (`run_research`) are owned by
their modules. They can call memory capabilities; they are not memory drivers.

## Capability Surface

### Main-Agent Memory Capabilities

- **`memory.recall(query)`** — runs the retriever inline, host-side, firewalled;
  returns a `recall_v1` finding. `allow_inline`, read impact.
- **`memory.remember(note)`** — enqueues a `memory_encode` task and returns
  `{status: "queued", encode_id}`. Fire-and-forget. `allow_inline`, write
  reversible impact.
- **`research.investigate(question, mode)`** — unchanged except `mode` now
  accepts `memories`. `memories` is mutually exclusive with `web` (see Rules).

### Memory Read Capabilities

- **`memory.search(query, limit, since?, kinds?)`** — hybrid semantic + keyword
  search over `memory_log` and `memory_notes`. Returns `{id, layer, kind,
  created_at, snippet, taint}` per match. `since`/`kinds` are mechanical
  filters; results are returned for the model to judge. Read impact.
- **`memory.read(id)`** — returns the full `content` and metadata of a log event
  or note by id. Read impact.

### Rememberer Write Capabilities

Whitelists `memory.search` and `memory.read`, plus:

- **`memory.note.create(content)`** — inserts a `memory_notes` row; the handler
  computes the embedding and appends a `note_create` event to `memory_log`.
- **`memory.note.edit(id, content)`** — rewrites the note's content; recomputes
  the embedding; appends a `note_edit` event.
- **`memory.note.delete(id)`** — deletes the note row; appends a `note_delete`
  event.

Capability whitelists are frozensets in `capability_registry.py`:
`RESEARCH_MEMORIES_CAPABILITY_IDS` and `REMEMBERER_CAPABILITY_IDS`.

## Background Tasks

- **`memory_encode`** — dispatched by `memory.remember`, fire-and-forget. The
  worker runs the rememberer in the `encode` trigger: it reads the relevant
  substrate and writes or edits notes in `memory_notes`. Every mutation is logged.
- **`memory_dream`** — scheduled, self-gating. The worker runs the rememberer in
  the `dream` trigger: it reads swaths of the raw log and the curated layer,
  consolidates, and writes generalisations, summaries, and connections as notes.
  Superseded notes are edited or deleted; every mutation is logged; the raw log
  is never touched.

`background_tasks.task_type` CHECK enum: `memory_encode` and `memory_dream`.

## Mode Exclusivity

`research.investigate` runs in exactly one domain per run. `memories` is
mutually exclusive with `web`: memory holds content derived from private sources,
so a single run that reads memory and reaches the open web is the lethal-trifecta
exfiltration path. This is a hard security rail, not a cost optimisation.
`memories` and `personal` can coexist only if explicitly designed; today each run
is in exactly one domain.

## Audit

Every memory model call writes one `ai_judgments` row, on both success and
failure. The memory `judgment_type` values are `memory_recall` (retriever),
`memory_encode`, and `memory_dream` (rememberer). Main-agent model calls write
`model_output`; no `research` judgment type exists.

## HTTP Routes

Operator inspection surface (read-only, paginated):

- `GET /v1/memory/log` — paginates `memory_log` rows.
- `GET /v1/memory/notes` — paginates `memory_notes` rows.

There is no write HTTP route for memory.

## Rules

- Memory is two layers: an append-only immutable `memory_log` and an editable
  `memory_notes`. The log is never edited or deleted by any code path or by the
  model.
- Every memory judgment — what to recall, what to encode, what to consolidate,
  what to supersede — is the model's, made by `run_agent_loop` in the
  retriever or rememberer driver.
- Deterministic code stores the substrate, maintains the embedding and keyword
  indexes, exposes the search/read/write primitives, runs the loop, propagates
  taint, and writes audit rows. It makes no relevance, importance,
  categorisation, ranking, decay, or "worth remembering" judgment, and it
  summarises nothing. `kind` on a log event is mechanical event-type provenance;
  code records and filters on it but never judges relevance or meaning with it.
- The raw log is written only by rails capturing events. The curated layer is
  written only by the rememberer. The main agent never writes either directly.
- Every note mutation appends a `note_*` event to the raw log.
- The retriever fires as a pre-turn step on every agent wake — user message,
  proactive trigger, or research completion — with no exception. Capture ingress
  stores durable data without waking the agent. Its `recall_v1`
  finding is the working-context reconstruction consumed by the main-agent
  prompt.
- Recall is bounded by `memory_recall_budget_seconds`, the
  `agent_loop_max_model_calls` backstop, and stuck-detection. Recall failure is
  non-fatal — the turn proceeds on the system prompt alone.
- Recalled memory is tainted; any action it motivates routes through
  `requires_approval`.
- Every retriever and rememberer model call writes one `ai_judgments` row, on
  success and failure.
- `memories` is mutually exclusive with `web` in `research.investigate`. This is
  a security rail and cannot be relaxed.
- New memory machinery — schemas on a note, scorers, rankers, decay functions,
  graph algorithms, projection tables, candidate-gather pipelines, importance
  weights — is forbidden. A product need is met by a driver or trigger prompt,
  never by code.
