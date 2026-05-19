# Memory

## Scope

This document owns Ariel's memory subsystem: the fact store, the profile and
session digest documents, the retriever and rememberer subagents, and the two
memory syscalls.

Memory follows [../ai-first.md](../ai-first.md): every memory judgment is an AI
judgment; deterministic code owns only rails.

## Data Model

Two tables and one column:

- `memory_facts` — the flat fact store. A fact is a rich, plain-language
  statement. It has no kind, type, category, or tag column. A fact carries its
  `content`, a `status` (`active` or `forgotten`), provenance
  (`source_turn_id`, `source_excerpt`), an `embedding` (pgvector, HNSW-indexed),
  and a generated `search_vector` (GIN-indexed). `forgotten` is a reversible
  soft-delete; the sweep hard-deletes rows left `forgotten` long enough.
- `memory_profile` — a single row holding the profile document: who the user
  is, how they work, durable preferences, key relationships, the shape of their
  ongoing work, and standing guardrails including privacy instructions.
- `sessions.digest` — a nullable text column holding one session's conversation
  digest: the working state of the current conversation.

The profile and the digest are AI-authored prose with whatever internal
structure their author chooses. See [../database.md](../database.md) for schema
conventions.

## Subagents

Every memory judgment is made by one of two bounded AI subagents — stateless,
audited, single model calls in the `_*_with_model` shape, run host-side.

The **retriever** decides which facts are relevant to the current wake. It
receives the wake context and a deterministically gathered candidate set, and
returns the subset that matters now. Its selection is rendered into a `recalled
memory` system message, and the selected facts' `last_recalled_at` is updated.

The **rememberer** decides what to write and keeps the documents current. It
receives a conversation (a completed turn or a closing session) or the fact
store (the sweep), plus the current profile, the current digest, and a gathered
candidate set of existing facts. From one call it emits fact operations
(`write`, `edit`, `forget`), an optional rewritten profile, and an optional
rewritten digest. Facts land `active` immediately; there is no candidate or
review state.

## Syscalls

The main Jarvis agent's entire memory surface is two `allow_inline` syscalls,
exposed to the `run` program:

- `memory.recall(query)` — runs the retriever; returns the facts it judged
  relevant.
- `memory.remember(note)` — runs the rememberer over `note`; returns the
  operations it applied.

The main agent never touches `memory_facts`, `memory_profile`, or
`sessions.digest` directly; it only delegates.

## Wakes

- Every wake — a human, API, or capture turn, and a proactive wake from a
  non-human trigger — injects the profile and the session digest and runs the
  retriever as a pre-turn step. A proactive wake is a normal turn
  ([proactivity.md](proactivity.md)); memory treats it no differently.

The rememberer runs as a background task after every turn, on session rotation
(writing a carry-forward digest), on a periodic sweep, and on demand via
`memory.remember`. The session digest is the running continuity; there is no
summarization or compaction machinery.

## Rules

- Memory code stores facts and the two documents, gathers candidates, injects
  the profile and digest, runs the subagents, and writes audit records. It
  makes no relevance, importance, categorization, conflict, ranking, or "worth
  remembering" decision, and it summarizes nothing.
- A fact is a plain-language statement. There is no kind, type, category, tag,
  predicate vocabulary, or fact schema beyond the `memory_facts` columns.
- The profile and the session digest are AI-authored documents. No code
  composes, edits, or summarizes them.
- The main agent's only memory surface is `memory.recall` and `memory.remember`.
  No other code path mutates `memory_facts`, `memory_profile`, or
  `sessions.digest` except the rememberer's applied output.
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
