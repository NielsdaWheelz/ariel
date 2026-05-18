# Schema Consolidation Cutover

## Scope

This doc owns the plan to consolidate Ariel's PostgreSQL schema. The schema has
86 application tables at Alembic head `20260517_0034`, all defined as ORM models
in `src/ariel/persistence.py`. A subject-matter survey found the schema
disciplined and largely doc-mandated, but over-decomposed for a single-user
assistant: 1:1 satellite tables, stage-as-table pipelines, parallel audit
event-logs, parallel job queues, duplicate AI-call records, and dead tables.

Target: 86 tables to roughly 72, with no capability loss. (The first estimate
was ~60-65, set from the breadth survey. The per-subsystem deep-dives — Phases
2-4 — each found its area less consolidatable than the survey assumed: most of
the schema, table by table, earns its place. ~72 is the deep-dive-verified end
state.)

`memory_*` (31 tables) and `proactive_*` (11) are about half the schema and are
each addressed by a dedicated per-subsystem cutover doc.

## Cutover Policy

- Ship as a sequence of hard cutovers, one per consolidation move.
- Each move is one PR: one Alembic migration that transforms the schema, the
  `persistence.py` model change, and the code change.
- No dual-write, no compatibility shim, no migration-era branch, no feature flag.
- Ariel is not live and holds no production data. Migrations drop and recreate
  tables freely; no data-migration steps are required.
- Foreign keys are `ondelete=RESTRICT`. A migration that removes or merges a
  table drops dependent FK columns and child rows in order, in the same
  migration.
- Every migration has a working `downgrade()`.
- Each consolidation cites the rule it satisfies: `simplicity.md`,
  `cleanliness.md`, `database.md`, or `ai-first.md`.
- `docs/database.md` gains a table-family inventory. It currently documents none
  of the 86 tables.

## Verification

Each PR runs `ruff`, `mypy`, and the full `pytest` suite to green, and tests the
migration up and down.

## Phases

```
Phase 1 (this doc)  ->  Tier 0  ->  Phase 2 memory  ->  Phase 3 proactive  ->  Phase 4 cross-cutting
```

Tier 0 has no dependencies and can land at any time. Phases 2-4 each produce
their own cutover doc, approved before any code. Phase 4 follows the subsystem
phases because Phase 2's outcome for `memory_events` and `memory_projection_jobs`
determines the cross-cutting event-log and job-queue designs.

### Tier 0 - Dead schema

No deep-dive required. One or two small PRs.

- Drop `ai_judgment_type_cutover_20260514_0027` - a migration scratch table
  leaked onto the upgrade path by migration `0027`. It has no ORM model.
- Drop `work_people` - orphan table, never read or written outside its model.
  Also drop the two always-null FK columns on `work_commitments` that reference
  it.
- Drop `connector_subscriptions` - the Google push-notification channel
  registry. The renewal worker and read endpoints exist; the path that registers
  a watch with Google was never built. Polling-based sync (`sync_cursors`,
  `provider_sync_due`) is the live mechanism. Remove the table, the
  `_process_provider_subscription_renewal_due` worker, the
  `provider_subscription_renewal_due` task type, the two `app.py` read
  endpoints, and `serialize_connector_subscription`. Push notifications, if
  wanted later, ship as one whole cutover.
- Wire `attachment_extractions` instead of dropping it. The table persists
  extracted attachment content but is never read. Add a lookup in
  `attachment_content.py` keyed by `(blob_id, extractor, extractor_version)`
  that reuses a cached extraction before calling `_extract_attachment`. The
  table then earns its place as an extraction cache.
- Fix `db.py` `REQUIRED_TABLES`: it omits `memory_events`. One line; not a
  schema change.

### Phase 2 - Memory subsystem

31 `memory_*` tables to roughly 24-26. Produces
`docs/modules/memory-consolidation-cutover.md`. Candidate moves under deep-dive:

- Fold `memory_salience` (1:1 with `memory_assertions`) into `memory_assertions`.
- Collapse `memory_episodes`, `memory_reasoning_traces`, `memory_action_traces`
  into one table with an `episode_class` discriminator.
- Resolve the projection layer: six `*_projection*` tables plus
  `memory_projection_jobs`. Confirm which projection kinds are still live after
  the memory-completion cutover; keep, replace with PostgreSQL materialized
  views, or move embeddings to a `pgvector` column.
- Drop `memory_reviews` (write-only; duplicated by `memory_versions` and
  `memory_events`).
- Resolve `memory_deletions` against `memory_versions`.
- Resolve `memory_eval_runs` (production table vs. test fixtures).

### Phase 3 - Proactive subsystem

11 `proactive_*` tables to roughly 8. Produces
`docs/modules/proactive-consolidation-cutover.md`. Candidate moves:

- Fold `proactive_context_snapshots` and `proactive_policy_validations` into
  `proactive_decisions`.
- Merge `proactive_turns` into `notifications` - same concept, written in one
  transaction.
- Resolve `ai_judgments` vs. `proactive_decisions` ownership of the AI-call
  record.
- Fold `proactive_feedback` into proactive case events.

### Phase 4 - Cross-cutting

Consolidations that span subsystems. Produces
`docs/schema-cross-cutting-cutover.md`.

- Event-log family: six audit `*_events` tables (`job_events`, `memory_events`,
  `proactive_case_events`, `google_connector_events`, `workspace_item_events`,
  `work_follow_up_events`) - shared event table, JSONB-on-parent, or keep.
  `events` and `agency_events` are out of scope: core turn stream and webhook
  ingress.
- Job-queue family: unify `background_tasks` and `memory_projection_jobs`.
  `jobs` stays separate - it mirrors external Agency state, it is not a queue.
- Dual write-ledger: reconcile `email_actions` and `provider_write_receipts`.
- Speculative generality: narrow `workspace_items` and `workspace_item_events`
  to their actual Discord-only use.

## Decisions

- `connector_subscriptions`: dropped, not finished. Finishing requires a full
  Google watch-API integration; polling already covers sync.
- `attachment_extractions`: wired as a cache, not dropped. The read path is a
  small change and the table's design is sound.
- Phase 4 is a separate pass, not folded into the subsystem phases.
- Each phase's plan is a `*-cutover.md` doc, approved before code.

## Final State

Roughly 72 tables. No dead schema, no fully-derivable tables, no near-duplicate
tables, no duplicate AI-call record; one job-queue mechanism. The six per-table
event logs are kept — `docs/database.md`'s mandatory foreign keys and CHECK
enums make a shared or JSONB-folded event log structurally impossible. The
satellite tables that remain (`action_private_payloads`, `turn_idempotency_keys`,
`provider_evidence_blocks`) are deliberate, audited design, not over-decomposition.
`docs/database.md` documents every table family.
