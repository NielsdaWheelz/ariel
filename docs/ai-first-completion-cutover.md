# AI-First Completion Cutover

## Scope

This document defines the hard-cutover plan for closing the remaining gaps in
Ariel's AI-first judgment refactor.

It completes the direction in [ai-first.md](ai-first.md),
[ai-first-judgment-cutover.md](ai-first-judgment-cutover.md), and
[proactive-ai-deliberation-cutover.md](proactive-ai-deliberation-cutover.md).

[ai-first-verification-gap-cutover.md](ai-first-verification-gap-cutover.md)
owns the final verified gap list and acceptance plan found after this cutover's
first implementation pass.

[ai-first-sota-gap-cutover.md](ai-first-sota-gap-cutover.md) owns the remaining
post-verification SOTA closure plan for model-output failures, tool-result
failure provenance, proactive memory-failure audit, strict continuity validation,
ambient source honesty, failure-code constraints, and public doc cleanup.

The completion cutover covers:

- typed, auditable fail-closed AI judgment failures
- durable AI judgment audit records
- tool-result interpretation
- memory curation auditability
- continuity and context compaction durability
- proactive feedback learning auditability
- ambient source coverage
- autonomy scope enforcement
- taint and egress rails for autonomous actions
- legacy-surface and grep-backed removal tests

This is not a compatibility project. It is the final removal of reachable
deterministic judgment behavior from the AI-owned paths.

## Cutover Policy

- This is a hard cutover.
- Do not keep old judgment paths reachable.
- Do not add compatibility routes, compatibility task payloads, compatibility
  response fields, compatibility migrations, feature flags, or fallback prose.
- Do not add deterministic replacement logic for memory relevance, tool-result
  meaning, continuity meaning, feedback meaning, proactive importance, or
  interruption value.
- Invalid, missing, malformed, or low-confidence required AI output fails closed
  with a typed, inspectable record.
- A required AI judgment may be retried by worker rails. It must not be replaced
  by deterministic judgment during retry or exhaustion.
- Deterministic code can gather candidates, execute tools, enforce policy,
  validate schemas, persist records, retry, dead-letter, and explain rail
  failures.
- Deterministic code cannot decide semantic relevance, final answer wording,
  event importance, feedback meaning, continuity meaning, or whether to speak.
- No intermediate state is production-shippable while an old deterministic
  judgment path and its AI replacement are both reachable.

## Goals

- Make every required AI judgment durable enough to answer what the model saw,
  selected, omitted, decided, validated, persisted, and failed.
- Make tool-result interpretation a real AI judgment stage for outputs that
  should not be handed directly to the master model.
- Make memory curation failure a typed turn failure with an audit trail, not an
  uncaught server error.
- Make context-pressure compaction produce a durable AI continuity record.
- Make proactive feedback learning records preserve model id, prompt version,
  source feedback id, parse status, validation status, and failures.
- Make autonomy scopes enforce target, recipient, payload shape, max impact,
  taint, and egress rails before any autonomous write.
- Make ambient sensing cover only configured durable sources, but route each
  enabled source through AI ambient interpretation before proactive cases open.
- Add tests that prove old deterministic judgment paths are unreachable.
- Keep the implementation direct, local, and readable. Add an abstraction only
  when it removes real complexity or protects a rail.

## Non-Goals

- No generic subagent framework.
- No planner DSL, workflow engine, automation builder, or prompt registry.
- No provider-hosted memory as source of truth.
- No scheduled prompts, daily briefings, or digest semantics.
- No full transcript replay as a memory or continuity substitute.
- No deterministic ranking, feedback mapping, continuity summary, tool-result
  prose synthesis, or source-local proactive meaning.
- No model authority over policy, auth, egress, taint, autonomy grants, or
  side-effect boundaries.
- No broad module split done only to match an aspirational file plan. Split a
  file only when the split makes the current implementation easier to read.

## Target Behavior

### User Turns

- A user turn is recorded before required AI memory curation can fail.
- Memory curation failures become typed turn failures with an event timeline.
- The master model receives AI-curated memory bundles and structured subagent
  failures. It does not receive deterministic relevance decisions.
- Tool calls execute through policy and action rails.
- Tool results return to the model as audited data.
- Tool results that are large, multi-source, contradictory, modality-specific,
  or over budget are interpreted by a narrow AI tool-result interpreter before
  the master model continues.
- Final user-facing text is model-authored.
- If the model cannot produce a final response within budget, the turn records a
  typed model-output failure and emits no deterministic answer.

### Memory

- Candidate retrieval remains deterministic rail work.
- Candidate retrieval records the bounded candidate bundle with ids, provenance,
  trust, taint, lifecycle state, validity, source evidence, and budget omissions.
- The AI memory curator decides relevance and order.
- The curator output preserves selected order in the bundle shown to the master
  model.
- Selected and omitted candidates, rationale, uncertainty, confidence, model id,
  prompt version, parse status, and validation status are recorded.
- Invalid curator output fails closed with a typed event and no deterministic
  memory bundle.

### Tool Results

- The model decides which tools to call.
- Deterministic code validates and executes tool calls.
- The tool-result interpreter receives only bounded audited tool output,
  artifacts, citations, taint, provenance, and typed failures.
- The interpreter returns structured findings, contradictions, uncertainty,
  citation refs, artifact refs, omitted output ids, and confidence.
- The master model authors the final response from the interpreter output and
  any direct audited tool output that was small enough to skip interpretation.
- The interpreter never writes final user prose.

### Continuity And Compaction

- Context pressure is a resource rail, not a semantic summarizer.
- When context pressure requires compaction, an AI continuity curator writes a
  durable continuity record before the compacted context is used.
- The record includes source turn ids, preserved items, omitted items and
  reasons, user commitments, assistant commitments, decisions, unresolved
  uncertainty, tool/action outcomes, model id, prompt version, parse status, and
  validation status.
- The next turn reads continuity records through the memory/context curation
  path.
- No no-op compaction path is reachable when context exceeds budget.
- No last-N deterministic summary path exists.

### Feedback Learning

- Every proactive feedback type runs through the AI feedback learner:
  `stop_pattern`, `more_aggressive`, `useful`, `wrong`, `correct`, `ack`, and
  `automatic_next_time`.
- The learner receives feedback, case, observation, decision, context snapshot,
  action plans, action executions, delivery state, turns, and related learning
  records.
- The learner can create `instruction`, `example`, `calibration`, `preference`,
  `source_preference`, `prompt_instruction`, and `autonomy_request` records.
- Feedback can propose autonomy. It cannot grant autonomy.
- Each learning record stores source feedback id, model id, prompt version,
  parse status, validation status, and content.
- Invalid learner output fails closed and records a typed worker failure.

### Ambient Interpretation

- Enabled ambient sources write durable source events or raw observations first.
- AI ambient interpretation decides whether those records become proactive
  observations or case updates.
- Discord ambient messages, Google workspace events, captures, jobs, approvals,
  memory changes, connector health, Agency events, and any configured local,
  location, repository, CI, or incident source enter through the same durable
  interpretation task once enabled.
- Transport handlers do not directly open proactive cases or notify users.
- Invalid interpreter output fails closed and leaves source records replayable.

### Autonomous Action

- The model proposes exact action plans.
- Deterministic rails authorize or deny exact plans.
- Autonomy scopes enforce actor, source context, action type, target system,
  target, recipient, allowed payload shape, allowed payload values, max impact,
  revocation rule, notification rule, taint, and audit visibility.
- Empty `allowed_payload` does not mean any payload is allowed. It means no exact
  values are constrained, and the payload still must satisfy the allowed shape.
- Missing `allowed_payload_shape` means no autonomous payload is authorized.
- Prompt-injection-bearing or tainted external content cannot authorize or
  execute autonomous writes.
- Proactive writes use the same preflight, egress, taint, idempotency, and
  provider execution rails as prompted writes.
- The case is not resolved as acted until the exact side effect is durably
  recorded.

## Architecture

The completed AI-first pipeline is:

1. Ingress records the user turn, proactive source event, feedback event, or
   context-pressure event.
2. Deterministic rails gather bounded candidates and provenance.
3. The exact AI judgment call for the product path runs:
   - memory curator
   - tool-result interpreter
   - continuity curator
   - feedback learner
   - ambient interpreter
   - proactive deliberator
4. Deterministic rails parse, validate, and persist the AI judgment record.
5. Invalid output records a typed failure and stops that path.
6. The master model receives selected AI judgment outputs and typed failures.
7. The master model authors user-facing text or structured proactive decisions.
8. Deterministic rails enforce policy, autonomy, taint, egress, idempotency,
   persistence, delivery, execution, retry, and recovery.

There is no generic orchestration layer. Each path has a local explicit model
call and local explicit validation.

## Structure

### AI Judgment Records

Every AI judgment record has:

- `id`
- `judgment_type`
- `source_type`
- `source_id`
- `status`
- `model`
- `prompt_version`
- `provider_response_id`
- `input_summary`
- `input_refs`
- `selected`
- `omitted`
- `output`
- `rationale`
- `uncertainty`
- `confidence`
- `parse_status`
- `validation_status`
- `failure_code`
- `failure_reason`
- `created_at`
- `updated_at`

Use an existing event or table only when it can answer every inspection
question. Otherwise add the smallest durable table needed for that judgment.

### Typed Failure Shape

Required AI judgment failures use one typed shape:

- `E_AI_JUDGMENT_REQUIRED`
- `E_AI_JUDGMENT_CREDENTIALS`
- `E_AI_JUDGMENT_TIMEOUT`
- `E_AI_JUDGMENT_INVALID_JSON`
- `E_AI_JUDGMENT_SCHEMA`
- `E_AI_JUDGMENT_VALIDATION`
- `E_AI_JUDGMENT_BUDGET`

The failure record includes the judgment type, prompt version, retryability,
source id, and safe reason. It never includes unredacted tainted content.

### Tool-Result Interpreter Contract

Input:

- action attempt ids
- capability ids
- audited tool output ids
- artifact refs
- citation refs
- taint and provenance
- typed tool failures
- token and output budgets

Output:

- interpreted findings
- contradictions
- uncertainty
- selected output refs
- omitted output refs and reasons
- citation refs
- artifact refs
- recommended next evidence, if any
- confidence

The interpreter does not decide final wording or side effects.

### Continuity Contract

Input:

- source turn ids
- active session context
- memory curation output
- tool/action outcomes
- open commitments
- configured omission budget
- current user request when compaction is in-turn

Output:

- continuity summary
- preserved turn refs
- omitted turn refs and reasons
- user commitments
- assistant commitments
- decisions
- unresolved uncertainty
- tool/action outcomes
- validity hints
- confidence

### Autonomy Policy Contract

The model may propose an action. Deterministic policy validates:

- actor
- source context
- action type
- target system
- target
- recipient
- payload shape
- payload values
- impact
- taint
- egress
- idempotency
- scope expiration
- revocation

Every denied field records the exact denial reason. No denied action is silently
dropped as if the model chose not to act.

## Files

### Docs

- `docs/ai-first-completion-cutover.md`: this completion spec.
- `docs/ai-first-judgment-cutover.md`: link this spec and keep the broad
  architecture.
- `docs/proactive-ai-deliberation-cutover.md`: align autonomy, taint, ambient,
  and feedback acceptance text.
- `docs/modules/memory.md`: align file ownership with the simplicity rule; do
  not require extra modules unless the split clearly earns its keep.
- `docs/production-runbook.md`: document recovery for AI judgment failures and
  dead-lettered tasks.
- `.env.example`: add or remove settings only when implementation uses them.

### Source

- `src/ariel/app.py`
  - Create turn/audit context before required memory curation can fail.
  - Route context-pressure compaction through durable AI continuity records.
  - Route tool-output loops through the tool-result interpreter when required.
  - Return typed AI judgment failures instead of uncaught internal errors.
- `src/ariel/action_runtime.py`
  - Keep tool execution rails.
  - Mark tool outputs that require interpretation.
  - Preserve artifacts, citations, taint, provenance, and typed failures.
- `src/ariel/memory.py`
  - Keep direct candidate retrieval and AI curation unless a split makes the
    current code easier to understand.
  - Persist or emit full memory curation audit records.
  - Raise typed AI judgment failures for invalid curation output.
- `src/ariel/proactivity.py`
  - Persist feedback learning audit metadata.
  - Enforce autonomy scope target, recipient, payload shape, taint, egress, and
    preflight rails.
  - Ensure ambient interpretation covers enabled durable sources.
- `src/ariel/sync_runtime.py`
  - Keep provider sync as durable source-event ingestion only.
- `src/ariel/worker.py`
  - Retry and dead-letter AI judgment tasks with typed failure records.
  - Treat unsupported legacy task names as deployment leftovers, not runnable
    compatibility paths.
- `src/ariel/persistence.py`
  - Add the smallest durable audit fields/tables needed for AI judgments,
    feedback learning, and continuity records.
- `src/ariel/response_contracts.py`
  - Expose AI-native inspection fields only.
  - Remove old deterministic judgment fields and event concepts.
- `src/ariel/executor.py`
  - Keep side-effect rails shared between prompted and proactive writes.

### Tests

- `tests/unit/test_ai_first_legacy_surfaces.py`
  - Grep-backed absence tests for old deterministic judgment names, events,
    fields, task payloads, and fallback prose.
- `tests/unit/test_responses_tool_contract.py`
  - Tool-result interpreter routing and no deterministic synthesis.
- `tests/integration/test_ai_first_memory_fail_closed.py`
  - Invalid memory curator output produces typed audited failure.
- `tests/integration/test_ai_first_tool_interpreter.py`
  - Large, multi-source, contradictory, and modality-specific outputs route
    through interpreter.
- `tests/integration/test_ai_first_continuity.py`
  - Context pressure persists AI continuity records and rejects invalid output.
- `tests/integration/test_proactive_feedback_learning.py`
  - Every feedback type routes through AI learner and persists audit metadata.
- `tests/integration/test_proactive_autonomy_policy.py`
  - Scope, target, recipient, payload shape, taint, egress, and idempotency
    denials are enforced.
- `tests/integration/test_proactive_ambient_sources.py`
  - Enabled sources enter durable ambient interpretation before cases open.

## Key Decisions

- Keep the master assistant as the only user-facing author.
- Use narrow product-path AI calls, not a generic subagent framework.
- Persist AI judgment records where events alone cannot answer inspection
  questions.
- Treat invalid AI output as a product-visible typed failure, not an internal
  exception.
- Require a real tool-result interpreter for unsuitable tool output.
- Require durable continuity records for context-pressure compaction.
- Enforce autonomous writes through the same rails as prompted writes.
- Treat tainted external content as unable to grant write authority at any risk
  tier.
- Prefer local explicit code over new modules. Split files only when the current
  implementation becomes less readable without the split.

## Acceptance Criteria

### Global

- `make verify` passes.
- Alembic upgrade and downgrade smoke tests pass.
- Grep tests fail on old deterministic judgment helper names, old task names,
  old response fields, old event concepts, and old fallback prose.
- Every required AI judgment has model id, prompt version, provider response id
  when available, parse status, validation status, selected output, omitted
  output, and failure state.
- Invalid AI output never produces deterministic judgment output.

### Memory

- The turn record exists before required memory curation can fail.
- Invalid memory curation returns a typed audited failure.
- Full candidate refs and curation output are inspectable.
- Curator-selected order is preserved in the memory bundle.
- No deterministic rank score or candidate order alone decides the final bundle.

### Tool Results

- Tool outputs below interpretation thresholds return as audited data.
- Large, multi-source, contradictory, and modality-specific outputs route
  through the AI tool-result interpreter.
- Interpreter output is structured and not final prose.
- Exhausted model attempts after tool output record typed failure.
- No `_synthesize_*` or equivalent deterministic final-answer path exists.

### Continuity

- Context pressure writes a durable AI continuity record.
- The record cites source turn ids and omitted turn ids with reasons.
- Invalid continuity output records typed failure.
- No no-op compaction path is reachable over budget.
- No last-N deterministic summary path exists.

### Feedback

- All supported feedback types enqueue and process AI feedback learning.
- Learning records include model id, prompt version, source feedback id, parse
  status, and validation status.
- Invalid learner output records typed failure and persists no learning record.
- Feedback cannot grant autonomy.

### Ambient

- Enabled ambient sources first create durable source records.
- AI ambient interpretation creates or updates proactive observations and cases.
- Transport code never opens proactive cases directly.
- Invalid interpreter output leaves source records replayable.

### Autonomy And Rails

- Action plans outside target, recipient, payload shape, payload values, impact,
  egress, or scope are denied with exact reasons.
- Tainted or prompt-injection-bearing content cannot execute autonomous writes.
- Proactive Google/write actions use the same preflight and egress rails as
  prompted writes.
- Completed side effects are durably recorded before cases resolve.

## Implementation Plan

1. Add typed AI judgment failure helpers and audit persistence.
2. Move turn creation before required memory curation and wire typed failure
   events.
3. Add memory curation audit persistence for candidate refs, selected, omitted,
   parse status, and validation status.
4. Add tool-result interpreter routing, contract validation, and tests.
5. Persist context-pressure continuity records and remove in-memory-only
   over-budget compaction.
6. Add feedback learning audit metadata and invalid-output failure handling.
7. Tighten autonomy policy for target, recipient, payload shape, taint, egress,
   and shared preflight execution.
8. Complete ambient source ingestion for enabled durable sources and prevent
   transport-owned proactive case creation.
9. Remove or rename old deterministic judgment response/event/task surfaces.
10. Add grep-backed and acceptance tests for every removed deterministic path.
11. Run ruff, mypy, targeted PostgreSQL integration tests, Alembic smoke, full
    pytest, and `make verify`.

No step is complete until old deterministic behavior is unreachable and invalid
AI output fails closed with an inspectable typed record.
