# AI-First Verification Gap Cutover

## Scope

This document owns the final hard-cutover plan for the gaps found during manual
verification of [ai-first-completion-cutover.md](ai-first-completion-cutover.md).

It closes these verified gaps:

- feedback learning is not persisted as an `ai_judgments` record
- proactive AI judgment failure codes do not use the repository-wide typed set
- memory curation and continuity compaction drop provider response ids
- session-rotation continuity is not audited and typed like context-pressure
  continuity
- autonomy scope selection can false-deny when multiple scopes match the same
  action and target system
- ambient interpretation sweeps with no candidates leave no audit trail
- ambient source coverage is narrower than the product spec
- tests do not assert every required AI judgment row directly
- docs and test names still contain legacy derivation or fallback wording

This document does not reopen the broader architecture. The AI-first rule remains
[ai-first.md](ai-first.md): AI owns judgment; deterministic code owns rails.

[ai-first-sota-gap-cutover.md](ai-first-sota-gap-cutover.md) owns the remaining
post-verification gap closure after manual verifier review found stale public
docs, missing provider-response provenance on tool-result failures, generic
model-output exhaustion, proactive memory-failure audit gaps, strict continuity
validation gaps, ambient source placeholder types, and failure-code schema gaps.

## Cutover Policy

- This is a hard cutover.
- Do not add compatibility behavior for old feedback mapping, old continuity
  summaries, old ambient derivation, old task names, old event names, or old
  response fields.
- Do not add fallback prose, fallback summaries, fallback rankings, or fallback
  feedback instructions.
- Invalid, missing, malformed, or unavailable required AI output fails closed
  with a typed `AIJudgmentRecord`.
- Every required AI judgment path writes exactly one success or failure judgment
  record for the attempt.
- Deterministic code can validate, persist, retry, dead-letter, dedupe, enforce
  policy, enforce taint, enforce egress, and explain failures.
- Deterministic code cannot decide semantic feedback meaning, proactive event
  meaning, memory relevance, continuity meaning, interruption value, or final
  wording.
- No feature flags, dual paths, compatibility routes, or legacy fallback code are
  allowed.

## Goals

- Make `feedback_learning` a first-class durable AI judgment with the same audit
  quality as memory, tool-result interpretation, continuity, ambient
  interpretation, and proactive deliberation.
- Normalize every AI judgment failure code to the typed set already documented in
  the completion spec.
- Preserve provider response ids for memory curation, continuity compaction,
  session rotation continuity, tool-result interpretation, ambient
  interpretation, proactive deliberation, and feedback learning whenever the
  provider or model adapter supplies one.
- Make every continuity path typed, auditable, and fail-closed.
- Evaluate all candidate autonomy scopes for an action before denying authority.
- Record explicit audit state for ambient interpretation tasks that find no
  candidates.
- Route every enabled durable ambient source through AI ambient interpretation
  before a proactive observation or case is opened.
- Add tests that prove required audit rows exist and legacy wording/surfaces are
  gone.
- Keep the implementation local, linear, and direct. Add no generic agent
  framework, planner, registry, adapter layer, or reusable workflow abstraction.

## Non-Goals

- No new product mode for scheduled prompts, daily briefings, digests, or
  deterministic notifications.
- No provider-hosted memory as source of truth.
- No generic ambient sensor framework.
- No generic autonomy policy engine.
- No broad file split unless the current file becomes harder to read without it.
- No deterministic replacement for AI feedback learning, AI continuity, AI
  ambient interpretation, or AI scope selection judgment.
- No implementation for unconfigured external systems. Unconfigured local,
  location, repository, CI, or incident sources remain absent until explicitly
  configured and persisted.

## Target Behavior

### Feedback Learning

- Every feedback type queues `proactive_feedback_learning_due`.
- The feedback learner call writes one `AIJudgmentRecord` with
  `judgment_type = "feedback_learning"` on success or failure.
- Success records include source feedback id, case id, selected learning record
  ids, omitted or rejected model items, model id, prompt version, provider
  response id, parse status, validation status, confidence when present, and the
  raw validated model output.
- Failure records include source feedback id, case id, model id when known,
  provider response id when known, prompt version, parse status, validation
  status, typed failure code, safe failure reason, and the safe provider output
  shape when available.
- Invalid feedback learner output persists no `ProactiveLearningRecord`.
- Feedback can propose autonomy through an `autonomy_request` learning record. It
  cannot activate or grant an autonomy scope.

### Typed AI Judgment Failures

- The only AI judgment failure codes are:
  - `E_AI_JUDGMENT_REQUIRED`
  - `E_AI_JUDGMENT_CREDENTIALS`
  - `E_AI_JUDGMENT_TIMEOUT`
  - `E_AI_JUDGMENT_INVALID_JSON`
  - `E_AI_JUDGMENT_SCHEMA`
  - `E_AI_JUDGMENT_VALIDATION`
  - `E_AI_JUDGMENT_BUDGET`
- Proactive model transport or provider failures use `E_AI_JUDGMENT_REQUIRED`
  unless a narrower code applies.
- Malformed JSON uses `E_AI_JUDGMENT_INVALID_JSON`.
- Parsed JSON that fails the local output contract uses
  `E_AI_JUDGMENT_SCHEMA`.
- Parsed JSON that references unknown ids, impossible state, denied values, or
  invalid cross-record relationships uses `E_AI_JUDGMENT_VALIDATION`.
- All events, task errors, API errors, and judgment rows use the same code for
  the same failed attempt.

### Provider Response Ids

- Every model adapter and direct provider call returns or preserves
  `provider_response_id` when available.
- Memory curation success and failure rows write `provider_response_id` when the
  model response has one.
- Context-pressure continuity compaction success and failure rows write
  `provider_response_id` when the model response has one.
- Session-rotation continuity success and failure rows write
  `provider_response_id` when the model response has one.
- Missing provider ids are recorded as `null`; deterministic code does not invent
  synthetic provider ids.

### Continuity

- Context-pressure compaction and session rotation use the same AI continuity
  output contract.
- Session rotation writes a `continuity_compaction` `AIJudgmentRecord` on success
  and failure.
- Rotation success still writes the existing durable continuity snapshot, but the
  snapshot state links to the AI judgment id.
- Rotation failure writes a typed AI judgment failure and stops the rotation path
  before any deterministic continuity summary can be used.
- Empty rotation inputs can record `parse_status =
  "not_required_no_candidates"` only when there are truly no source turns to
  preserve. This is an auditable rail outcome, not a semantic summary.
- No `RuntimeError` from continuity model calls escapes without being converted
  into a typed AI judgment failure at the product boundary.

### Autonomy Scope Selection

- A model-proposed action is checked against every active scope matching actor,
  action type, and target system.
- A scope matches only when target, recipients, payload shape, payload values,
  risk tier, notification rule, expiration, taint, and preflight checks all pass.
- If at least one scope fully authorizes the exact action, the action proceeds
  under that scope.
- If no scope authorizes the action, the denial records the most specific safe
  reason and the considered scope ids.
- Multiple scopes cannot broaden authority by unioning partial permissions. One
  scope must authorize the whole exact action.
- Tainted or prompt-injection-bearing context still blocks autonomous writes even
  when a scope otherwise matches.

### Ambient Interpretation

- Every ambient interpretation task writes an `AIJudgmentRecord`.
- When no candidates exist, the task writes a succeeded
  `ambient_interpretation` record with `parse_status =
  "not_required_no_candidates"`, `validation_status = "not_validated"`,
  empty selected/omitted lists, and input refs explaining the empty sweep.
- Enabled durable sources enter the candidate bundle only after their source
  record is persisted with provenance, trust boundary, taint, dedupe key, and
  observed time.
- Discord ambient messages are persisted as durable ambient source records before
  interpretation. Discord transport does not open proactive cases directly.
- Local, location, repository, CI, and incident sources are represented as
  configured source records only when the repo has real configuration and
  persistence for them. Until then, the spec requires explicit non-configuration
  docs and tests, not placeholder sensors.
- Sensor failure is an ambient source event. The model decides whether the
  missing sensor matters.

### Legacy Wording And Surfaces

- Docs use `ambient interpretation`, `ambient sensing`, or `source-event
  interpretation`; they do not use `ambient derivation` for the AI-first product.
- Test names do not use `fallback` for allowed rail behavior.
- Runtime and migrations still have no reachable old attention, ranking, derive,
  synthesis, last-N summary, scheduled prompt, daily briefing, or deterministic
  notification paths.

## Architecture

The final AI-first judgment pipeline is:

1. Ingress records the user turn, source event, feedback event, rotation event,
   context-pressure event, or ambient sweep request.
2. Deterministic rails gather bounded candidates and provenance.
3. The exact AI judgment call for that path runs.
4. Deterministic rails parse and validate the model output.
5. The attempt writes one `AIJudgmentRecord`.
6. Success continues with the validated AI output.
7. Failure stops the product path or lets worker retry according to the existing
   task budget.
8. Policy, taint, egress, idempotency, persistence, delivery, execution, replay,
   inspection, and dead-letter rails run deterministically.

There is no generic judgment dispatcher. Each path owns its local prompt,
provider call, validation, and audit write.

## Structure

### AI Judgment Record Use

Required judgment rows:

- `memory_curation`: turn memory recall
- `tool_result_interpretation`: unsuitable tool output interpretation
- `continuity_compaction`: context-pressure and session-rotation continuity
- `feedback_learning`: proactive feedback interpretation
- `ambient_interpretation`: ambient source batches and empty sweeps
- `proactive_deliberation`: proactive case decisions

All rows use the existing `AIJudgmentRecord` shape. Add columns only if an
acceptance criterion cannot be answered with the existing fields.

### Failure Shape

Every typed failure includes:

- judgment type
- source type
- source id
- prompt version
- retryability at the task/API boundary
- parse status
- validation status
- typed failure code
- safe failure reason
- provider response id when available
- safe input refs
- safe output refs when available

Failure records never include unredacted tainted content.

### Ambient Source Records

Durable ambient source records must expose enough data for AI interpretation:

- source type
- source id
- source provider
- observed time
- actor when known
- subject when known
- raw or normalized payload
- trust boundary
- taint
- dedupe key
- source URI or external id when available

Use existing records where they already satisfy this shape. Add the smallest
local table only when a source has no durable record.

## Rules

- Required AI judgment attempts must be inspectable even when they produce no
  product output.
- Empty candidates are auditable judgment outcomes.
- One successful autonomy scope must authorize the whole exact action.
- Scope checks cannot merge permissions across scopes.
- Failure code normalization is mandatory before new behavior is added.
- Provider response ids are provenance, not product logic. Preserve them; do not
  depend on them for decisions.
- Session rotation is a continuity path and follows the same AI judgment rules as
  context-pressure compaction.
- Ambient transport code records source facts only. It does not decide proactive
  meaning.
- Tests must assert absence of legacy product language in runtime docs where that
  language would imply reachable behavior.

## Files

### Docs

- `docs/ai-first-verification-gap-cutover.md`: this spec.
- `docs/index.md`: link this spec.
- `docs/ai-first-completion-cutover.md`: point final implementation work here.
- `docs/proactive-ai-deliberation-cutover.md`: replace derivation wording with
  interpretation wording.
- `docs/production-runbook.md`: add recovery notes for feedback-learning
  judgments, empty ambient sweeps, and rotation-continuity failures.

### Source

- `src/ariel/proactivity.py`
  - Add `AIJudgmentRecord` writes for feedback learning success and failure.
  - Normalize proactive AI judgment failure codes.
  - Evaluate all matching autonomy scopes before denying an action.
  - Record empty ambient sweeps.
  - Add durable Discord ambient source ingestion if no existing source record
    covers ambient messages.
- `src/ariel/memory.py`
  - Preserve provider response ids in memory curation output.
  - Return typed continuity failures instead of plain runtime failures.
  - Preserve provider response ids in rotation continuity output.
- `src/ariel/app.py`
  - Persist provider response ids for memory and continuity judgment rows.
  - Persist `AIJudgmentRecord` rows for session-rotation continuity.
  - Convert rotation continuity failures into typed API/task failures at the
    product boundary.
- `src/ariel/persistence.py`
  - Add only the smallest schema surface needed for missing ambient source
    records or missing audit links.
- `src/ariel/db.py`
  - Require any new tables added for ambient source durability.
- `src/ariel/response_contracts.py`
  - Expose AI-native inspection fields only when an API response needs them.
- `src/ariel/discord_bot.py`
  - Persist ambient Discord messages as source records before worker
    interpretation.
- `src/ariel/worker.py`
  - Keep legacy task names unsupported and dead-lettered.
  - Retry only typed AI judgment task failures within existing budgets.

### Migrations

- Add one hard-cutover Alembic migration only if new persistence is required.
- Do not add compatibility columns or legacy views.
- Downgrade can restore schema shape for development rollback; runtime code must
  not preserve compatibility behavior.

### Tests

- `tests/unit/test_ai_first_legacy_surfaces.py`
  - Add grep checks for legacy derivation/fallback wording where it implies
    product behavior.
  - Add grep checks banning `E_AI_JUDGMENT_MODEL` and `E_AI_JUDGMENT_JSON`.
- `tests/integration/test_proactive_feedback_learning.py`
  - Assert every feedback type writes a `feedback_learning` judgment row.
  - Assert invalid learner output writes a failed judgment row and no learning
    record.
- `tests/integration/test_ai_first_judgment_audit.py`
  - Assert memory curation, tool-result interpretation, continuity compaction,
    feedback learning, ambient interpretation, and proactive deliberation each
    write direct `ai_judgments` rows.
- `tests/integration/test_ai_first_continuity.py`
  - Assert session rotation writes a continuity judgment row and preserves
    provider response id.
  - Assert invalid rotation continuity fails closed.
- `tests/integration/test_proactive_autonomy_policy.py`
  - Assert multiple scopes are considered and a later fully matching scope can
    authorize.
  - Assert partial scope permissions are not unioned.
- `tests/integration/test_proactive_ambient_sources.py`
  - Assert empty ambient sweeps are audited.
  - Assert Discord ambient messages persist source records and enter AI ambient
    interpretation.
- Existing proactive, memory, tool, and worker tests stay green.

## Key Details

### Feedback Learning Audit

The success audit row uses:

- `source_type = "proactive_feedback"`
- `source_id = feedback.id`
- `selected = [{"learning_record_id": ..., "record_type": ...}]`
- `omitted = rejected or ignored model items with reasons`
- `output = validated learner output`
- `input_refs = feedback id, case id, observation id, decision id, snapshot id,
  action plan ids, execution ids, turn ids, related learning record ids`

The failure audit row uses the same source and input refs with empty selected
records.

### Failure Code Normalization

Replace proactive-only model/json codes:

- `E_AI_JUDGMENT_MODEL` becomes `E_AI_JUDGMENT_REQUIRED`
- `E_AI_JUDGMENT_JSON` becomes `E_AI_JUDGMENT_INVALID_JSON`

Do not keep aliases. Tests fail if old codes remain in `src/ariel`.

### Provider Response Id Preservation

Direct OpenAI response helpers return a payload containing:

- parsed AI output
- provider
- model
- provider response id

Model adapter paths pass through the same metadata if the fixture or provider
supplies it. Tests must cover both direct adapter metadata and missing metadata.

### Rotation Continuity

Session rotation should call the same AI continuity contract as context-pressure
compaction. If source turns exist and the model cannot produce a valid
continuity record, rotation fails closed and records a typed judgment failure.

Rotation may proceed without a model only when there are no source turns. That
path writes an auditable no-candidates judgment row.

### Scope Matching

Scope selection is not a ranker. It is an authorization search:

1. Load active scopes for actor, action type, and target system.
2. Evaluate each scope independently against the exact normalized action.
3. Keep the first fully authorizing scope by stable created/id order.
4. If none authorize, persist the strongest denial reason and considered scope
   ids.

The model still decides what action to propose. Rails only decide whether the
exact proposal is authorized.

### Ambient Source Coverage

Configured source means the repo has:

- settings or explicit runtime configuration
- durable persistence
- ingress code
- trust and taint labeling
- dedupe
- tests

Do not create placeholder source types without a real source event path.

## Key Decisions

- The completion gap is audit completeness and rail precision, not a new
  architecture.
- `AIJudgmentRecord` is the canonical cross-cutting audit table for AI judgment
  attempts.
- Proactive feedback learning is a required AI judgment.
- Session rotation continuity is a required AI judgment when source turns exist.
- Empty ambient sweeps are auditable successful rail outcomes.
- Failure code names are part of the contract and must be normalized.
- Autonomy scope authorization considers multiple candidate scopes but never
  unions permissions.
- Literal ambient source coverage means configured, durable, tested sources only.
- Legacy wording cleanup is part of the cutover because docs guide future code.

## Acceptance Criteria

### Global

- `make verify` passes.
- Full `uv run pytest -q --maxfail=10` passes.
- Ruff and mypy pass.
- Alembic upgrade and downgrade smoke tests pass if a migration is added.
- Grep tests fail if old AI judgment codes, old proactive attention/ranking
  surfaces, old derive task names, old synthesize helpers, old last-N summary
  names, old scheduled prompt semantics, or old fallback prose paths appear in
  runtime.
- Every required AI judgment path has a direct test that selects its
  `ai_judgments` row from the database.

### Feedback Learning

- Every supported feedback type writes a succeeded `feedback_learning`
  `AIJudgmentRecord`.
- Invalid feedback learner JSON writes a failed `feedback_learning`
  `AIJudgmentRecord` with `E_AI_JUDGMENT_INVALID_JSON`.
- Schema-invalid learner output writes a failed `feedback_learning`
  `AIJudgmentRecord` with `E_AI_JUDGMENT_SCHEMA`.
- Invalid learner output persists no learning record.
- `automatic_next_time` can create an `autonomy_request` learning record but no
  active autonomy scope.

### Failure Codes

- `rg "E_AI_JUDGMENT_MODEL|E_AI_JUDGMENT_JSON" src/ariel tests` returns no
  matches except migration or changelog text explicitly describing removed codes.
- Proactive ambient, deliberation, and feedback failures use the typed set.
- API errors, task errors, case events, and judgment rows agree on failure code
  for the same attempt.

### Provider Response Ids

- Memory curation success records provider response id when supplied.
- Memory curation failure records provider response id when supplied.
- Context-pressure continuity success and failure record provider response id
  when supplied.
- Session-rotation continuity success and failure record provider response id
  when supplied.
- Missing provider ids remain `null`.

### Continuity

- Context-pressure compaction still fails closed when over budget and no valid
  AI continuity record exists.
- Session rotation with source turns writes a succeeded continuity judgment row
  and a linked continuity snapshot.
- Session rotation with invalid AI output writes a failed continuity judgment row
  and does not write a deterministic summary.
- Session rotation with no source turns writes an auditable no-candidates
  continuity judgment row.

### Autonomy

- When two scopes match action/system and only the second matches
  target/recipient/payload, the action is authorized under the second scope.
- When two scopes each match only part of an action, the action is denied.
- Denials include considered scope ids and the most specific denial reason.
- Tainted autonomous writes remain denied even with an otherwise matching scope.
- Preflight still runs before any external side effect.

### Ambient

- Empty ambient interpretation sweeps write a succeeded no-candidates judgment
  row.
- Provider workspace events still write source records before interpretation.
- Discord ambient messages write durable source records before interpretation.
- Discord transport does not open proactive cases directly.
- Configured local/location/repo/CI/incident sources are either implemented with
  durable records and tests or explicitly documented as unconfigured.

### Legacy Cleanup

- Docs use interpretation/sensing terminology for the AI-first ambient product.
- Test names do not call allowed rail behavior fallback.
- Legacy no-op migration names can remain as historical revision identifiers, but
  active runtime, tests, and docs must not imply compatibility behavior.

## Implementation Plan

1. Normalize proactive AI judgment failure codes and add grep coverage.
2. Add feedback-learning `AIJudgmentRecord` success and failure writes.
3. Preserve provider response ids in memory curation and continuity outputs.
4. Add session-rotation continuity judgment rows and typed fail-closed handling.
5. Change autonomy scope authorization to evaluate all candidate scopes
   independently.
6. Record no-candidate ambient interpretation audit rows.
7. Add durable Discord ambient source ingestion and route it through ambient
   interpretation.
8. Document unconfigured ambient source classes or implement only those with real
   persistence and tests.
9. Add direct database assertion tests for all required AI judgment rows.
10. Clean legacy derivation/fallback wording in docs and tests.
11. Run ruff, mypy, targeted integration tests, full pytest, grep checks,
    Alembic smoke if needed, and `make verify`.

No step is complete until every remaining verified gap has an acceptance test and
the old behavior remains unreachable.
