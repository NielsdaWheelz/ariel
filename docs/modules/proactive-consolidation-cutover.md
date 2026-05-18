# Proactive Consolidation Cutover

## Scope

Phase 3 of the schema consolidation ([../schema-consolidation-cutover.md](../schema-consolidation-cutover.md)).
The proactive subsystem holds 11 `proactive_*` tables. This cutover consolidates
it to 8: it folds two 1:1 satellites into `proactive_decisions`, merges
`proactive_turns` into `notifications`, and de-duplicates the AI-call record
between `proactive_decisions` and `ai_judgments`.

A table-by-table deep-dive confirmed the rest of the subsystem earns its place:
`proactive_cases` is a sound aggregate root, `proactive_observations` is a
genuine ingress entity, the plan/execution split is replay-critical, and
`proactive_learning_records` is a live policy entity read into the deliberation
prompt. `proactive_feedback` is deferred to Phase 4, where the cross-cutting
event-log family is decided.

Unlike the memory subsystem, proactivity has **no module doc and no doc mandates
its tables** — so these moves need no spec amendments. The pipeline
`docs/north-star-cutover.md` describes (observation → case → context snapshot →
decision → validation → notify/act) is a *runtime* sequence; it does not mandate
one table per stage, and folding rows preserves every step. `docs/ai-first.md`
makes policy validation a deterministic *rail* — that is a rule about logic
ownership, not table count; Move 2 keeps the folded validation fields explicitly
rail-owned and append-only.

## Cutover Policy

Inherits [../schema-consolidation-cutover.md](../schema-consolidation-cutover.md).
Each PR is one Alembic migration, the `persistence.py` model change, and the
code change — no compatibility shim, no dual-write, no feature flag. Ariel holds
no production data; migrations drop and recreate tables freely. Every migration
has a working `downgrade()`. Each PR runs `ruff`, `mypy`, and the full `pytest`
suite to green, and tests the migration up and down.

## Moves

| # | Move | Tables | PR |
|---|---|---|---|
| 1 | Fold `proactive_context_snapshots` into `proactive_decisions` | −1 | A |
| 2 | Fold `proactive_policy_validations` into `proactive_decisions` | −1 | A |
| 3 | Merge `proactive_turns` into `notifications` | −1 | B |
| 4 | De-duplicate the AI-call record (`proactive_decisions` ↔ `ai_judgments`) | 0 | C |

Moves 1 and 2 ship as one migration/PR — both restructure `proactive_decisions`.
Move 3 and Move 4 are separate PRs. Result: 11 → 8 `proactive_*` tables;
`proactive_feedback` is deferred to Phase 4.

### Move 1 — Fold `proactive_context_snapshots` into `proactive_decisions`

`proactive_context_snapshots` is a strict 1:1 satellite of `proactive_decisions`.
Each deliberation run mints one snapshot, then one `ProactiveDecisionRecord` with
a mandatory FK `context_snapshot_id`. Re-deliberation builds a fresh snapshot and
a fresh decision — a snapshot never drives two decisions. `snapshot_key` (a
synthetic uniqueness key) has zero readers.

Schema: add to `proactive_decisions` — `context` `JSONB`, `model_input` `JSONB`,
`omitted_context` `JSONB`, `context_taint` `JSONB` (rename of the snapshot's
`taint`, disambiguated from observation taint). Drop the FK column
`proactive_decisions.context_snapshot_id`. Drop `proactive_context_snapshots`,
its `snapshot_key` unique index, and its indexes. Delete
`ProactiveContextSnapshotRecord` and `serialize_proactive_context_snapshot`.

Code (`proactivity.py`): in `process_proactive_deliberation_due` the
`ProactiveContextSnapshotRecord(...)` build (~`1183`) is deferred — `context`/
`model_input` become locals carried into the decision build (~`1361`).
`_update_snapshot_tool_context` (~`1675`) is rewritten to set the columns on the
decision (or folded into the decision build). The two
`db.get(ProactiveContextSnapshotRecord, …)` re-fetches (~`1265`, `1301`)
disappear. `_record_invalid_decision` (~`1689`) drops its snapshot parameter. The
feedback-learning read (~`4730`) reads `context`/`omitted_context` off the
decision.

API: `GET /v1/proactive/cases/{id}/context-snapshots` (`app.py:9587`) is removed;
the `inspect-why` endpoint (`app.py:9698`) reads the columns off the decision.
Delete `SurfaceProactiveContextSnapshotContract` and its list contract/builder.

### Move 2 — Fold `proactive_policy_validations` into `proactive_decisions`

`proactive_policy_validations` is a small fixed-shape rail verdict written 1:1
with a decision; there is no re-validation path (`policy_version` is a constant,
stamped once). It is the JSONB-vs-table call in `docs/database.md` resolved
toward fold.

Schema: add to `proactive_decisions` — `policy_result` `String(32)` (the
validation's `result`, with its CHECK re-created as
`ck_proactive_decision_policy_result`), `policy_version` `String(64)`,
`action_plan_hash` `String(128)` nullable, `policy_constraints` `JSONB` (rename
of `constraints`), `denial_reason` `Text` nullable. Drop
`proactive_action_plans.policy_validation_id` (its column, index, FK) — it is
dead linkage, never dereferenced; the plan already carries `decision_id`. Drop
`proactive_policy_validations`. Delete `ProactivePolicyValidationRecord` and
`serialize_proactive_policy_validation`.

These five columns are a **deterministic rail's audit record** — per
`docs/ai-first.md`, policy validation is the side-effect authorization boundary.
Folding the table does not dissolve the rail: the columns are rail-written,
append-only (set once per decision, never mutated), and attributable. Document
them as such; do not let later code treat them as model output.

Code (`proactivity.py`): `_validate_and_apply_decision` (~`2065`) writes the
policy fields onto the decision instead of a `ProactivePolicyValidationRecord`
(~`2172`); `validation.result`/`validation.id` references (~`2189`, `2194`,
`2249`, `2400`, `2470`) become `decision.policy_result` or are dropped. The
inline invalid-decision validation (~`1440`) and the one in
`_record_invalid_decision` (~`1759`) set the decision's policy columns.
`_create_proactive_turn` / `_create_action_plan` (~`2370`/`2442`) drop the
`validation` parameter.

ORM/API: drop `policy_validation_id` from `ProactiveActionPlanRecord` and
`SurfaceProactiveActionPlanContract`; update `serialize_proactive_action_plan`.
`GET /v1/proactive/cases/{id}/validations` (`app.py:9639`) is removed; in
`inspect-why` the validation dereference — including the defensive
`ORDER BY created_at DESC LIMIT 1` query — collapses to reading the decision's
columns. Delete `SurfaceProactivePolicyValidationContract` and its
list contract/builder.

### Move 3 — Merge `proactive_turns` into `notifications`

`_create_proactive_turn` (`proactivity.py:2370`) inserts a `ProactiveTurnRecord`
and, in the same transaction, a `NotificationRecord` with
`source_type='proactive_turn'` copying `channel`/`status`/`body`. The two rows
are a 1:1 pair. **No foreign key anywhere points at `proactive_turns`** — there
is nothing to re-point. `notifications` already has the `proactive_turn`
`source_type` and unifies agency/approval/connector deliveries; it is the right
single owner of an outbound message.

Schema: add to `notifications` — `proactive_case_id` (FK → `proactive_cases`,
`ondelete=RESTRICT`, nullable, indexed) and `proactive_decision_id` (FK →
`proactive_decisions`, `ondelete=RESTRICT`, nullable, indexed); add
`ck_notification_proactive_shape` (a partial CHECK: `source_type='proactive_turn'`
⇒ both FK columns NOT NULL, else both NULL). Widen `dedupe_key` to `String(220)`
(the proactive dedupe key is longer than the current `String(160)`). Drop
`proactive_turns`. Delete `ProactiveTurnRecord` and `serialize_proactive_turn`.

`case_id`/`decision_id` become real FK columns, not JSONB, because they are
queried as predicates (feedback-learning filters proactive notifications by
case). The dead single-valued `origin` field and the never-written `cancelled`
status are not carried over.

Code: `_create_proactive_turn` collapses to a single `NotificationRecord` insert
(rename it `_create_proactive_notification`); it keeps the `with_for_update`
dedupe probe, now against `notifications.dedupe_key`.
`process_proactive_feedback_learning_due` (~`4734`) queries
`select(NotificationRecord).where(source_type=='proactive_turn',
proactive_case_id==case.id)`. `mark_proactive_turn_acknowledged`
(`proactivity.py:5101`) is dead code — delete it; `mark_proactive_turn_delivered`
(~`5083`) is removed (the worker already sets `notification.status` directly).
`worker.py` drops the `mark_proactive_turn_delivered` import and call.
`record_proactive_feedback` and `ack_notification` (`app.py`) read
`notification.proactive_case_id` directly. `db.py` drops `proactive_turns` from
its table list. Delete `SurfaceProactiveTurnContract` and its list
contract/builder.

This move runs after Moves 1–2, so the turn's old `delivery_payload`
`policy_validation_id` reference is already gone; the notification `payload`
carries only `{case_id, decision_id}`.

Open fork: `GET /v1/proactive/turns` (`app.py:9836`) becomes
`GET /v1/notifications` filtered by `source_type='proactive_turn'`. Recommended:
delete the endpoint and add a `source_type` filter to `/v1/notifications` —
`docs/cleanliness.md` forbids a duplicate API for one capability.

### Move 4 — De-duplicate the AI-call record

The deliberation path writes the same model call to two tables: a
`proactive_decisions` row and an `ai_judgments` row
(`judgment_type='proactive_deliberation'`). `ai_judgments.output` equals
`proactive_decisions.raw_model_output`; `model` and `provider_response_id` sit on
both. `ai_judgments` is the canonical cross-subsystem AI-call audit log (10
`judgment_type` values, shared with memory). This move makes it the sole owner of
the call-audit facts. It removes **no table** — it is a normalization — and is a
separate PR sequenced after Moves 1–2.

Schema: remove from `proactive_decisions` — `provider`, `model`,
`provider_response_id`, `raw_model_output`. Add `proactive_decisions.ai_judgment_id`
(FK → `ai_judgments.id`, `ondelete=RESTRICT`, NOT NULL). `ai_judgments` itself is
**not** altered — only newly referenced — so this stays inside the proactive
cluster and does not touch the memory subsystem. `provider` is dropped outright
(the model name implies it); it is not added to `ai_judgments`.

Snag: `_apply_remember_decision` reads `decision.raw_model_output["memory"]` and
`decision.model`. Removing `raw_model_output` breaks it. Resolution: keep a
narrow `memory_payload` `JSONB` nullable column on `proactive_decisions` for the
remember-decision payload, and drop the full `raw_model_output`; `ai_judgments`
retains the complete `output`. The extraction model name is read from the linked
`ai_judgments` row.

Code: in the three decision-writing paths, flush the `AIJudgmentRecord` before
the `ProactiveDecisionRecord` so `decision.ai_judgment_id` can be set (today the
decision is flushed first — reorder). Drop the four shed fields from the two
`ProactiveDecisionRecord(...)` builds. `confidence` and `rationale` stay on
`proactive_decisions` as domain fields; the `ai_judgments` copies are the audit
copies.

### Kept tables

`proactive_cases` (aggregate root, 9-state lifecycle, FK target of the cluster),
`proactive_observations` (genuine multi-source ingress entity with its own
dedupe identity), `proactive_case_events` (clean append-only log — owned by the
Phase 4 event-log family), `proactive_action_plans` + `proactive_action_executions`
(the plan/execution split is replay-critical: the execution row is the
idempotency anchor across a multi-session worker), and `proactive_learning_records`
(a durable, mutable policy entity read live into the deliberation prompt).

`proactive_feedback` is **deferred to Phase 4**. It is thin, but it is the parent
of the learning chain (`proactive_learning_records.feedback_id` FKs it). Folding
it into `proactive_case_events` would create a new FK into a table Phase 4's
event-log-family work is about to restructure. Phase 4 decides feedback and the
event-log family together.

## Minor cleanup

`proactive_observations.status` allows `IN ('new','linked','ignored')` but
`'ignored'` is never written. Drop it from the CHECK and narrow the matching
`Literal` on the `GET /v1/proactive/observations` `status` parameter. Risk-free;
bundle with PR A or skip.

## Sequencing

Three PRs, a linear Alembic chain:

- **PR A** — Moves 1 + 2, one migration. Both restructure `proactive_decisions`;
  doing them in one migration avoids two consecutive `ALTER`s of the same table.
- **PR B** — Move 3, after PR A.
- **PR C** — Move 4, after PR A (it removes columns from `proactive_decisions`
  that PR A does not touch; the `_apply_remember_decision` snag gets its own
  attention).

All of Phase 3 runs before Phase 4 — Phase 4's `notifications` /
`notification_deliveries` work then operates on the post-merge `notifications`
table.

## Acceptance criteria

- 11 → 8 `proactive_*` tables; `ProactiveContextSnapshotRecord`,
  `ProactivePolicyValidationRecord`, and `ProactiveTurnRecord` are gone from
  `persistence.py`.
- `proactive_decisions` carries the folded context and policy-validation columns,
  an `ai_judgment_id` FK, and no longer stores the duplicated call-audit fields.
- The proactive deliberation, notification, feedback-learning, and action-execution
  paths work unchanged in behaviour; `inspect-why` returns the same information.
- `db.py` `REQUIRED_TABLES` and the constraint registries match the new schema.
- Each migration runs up and down; `ruff`, `mypy`, and the full `pytest` suite
  are green.

## Open forks

- **Decision ↔ validation 1:1** (Move 2): the fold hard-locks this to 1:1.
  No re-validation path exists today; the `inspect-why` `LIMIT 1` was defensive.
  If re-validating an unchanged decision under a newer `policy_version` ever
  becomes a feature, it needs a new decision row.
- **`GET /v1/proactive/turns`** (Move 3): delete the endpoint in favour of a
  `source_type` filter on `/v1/notifications` (recommended), or keep it as a thin
  filtered view.
- **`proactive_feedback`**: deferred to Phase 4 — folded into the event-log
  family or kept, decided there.
