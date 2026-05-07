# AI-First Judgment Cutover

## Scope

This document defines Ariel's hard cutover from deterministic judgment-shaped
runtime behavior to AI/subagent-owned judgment with deterministic service rails.

The cutover covers five refactor areas:

- memory/context curation
- tool-result interpretation and final answer synthesis
- feedback learning
- session continuity and context compaction
- ambient observation interpretation

This document implements the direction in [ai-first.md](ai-first.md). It does not
replace that rule doc; it turns the rule into an implementation plan.

[ai-first-completion-cutover.md](ai-first-completion-cutover.md) owns the final
completion plan for the remaining verified gaps: typed AI judgment failures,
durable audit records, tool-result interpretation, continuity durability,
feedback auditability, autonomy rails, ambient source completion, and
legacy-surface tests.

## Cutover Policy

- This is a hard cutover.
- Remove deterministic judgment code instead of wrapping it.
- Do not keep compatibility paths for old memory ranking, tool-result synthesis,
  feedback mapping, session summaries, or ambient observation interpretation.
- Do not add feature flags that keep old and new judgment systems reachable in
  the same deployment.
- Do not add deterministic fallback behavior when model output is missing,
  malformed, slow, or low confidence.
- Do not preserve old response fields, event concepts, task payloads, or tests
  unless they are native to the new AI-first contract.
- Invalid AI output fails closed with an auditable typed error.
- Deterministic code may gather candidates, validate contracts, enforce policy,
  persist state, retry, recover, and explain. It must not decide relevance,
  usefulness, final wording, interruption value, continuity meaning, or feedback
  meaning.
- No intermediate state is production-shippable while old deterministic judgment
  and new AI judgment are both reachable.

## Product Thesis

Ariel should behave like one master operator with bounded specialist delegates.

The master assistant owns the user-facing turn and final decision. Whenever the
turn needs judgment that can be separated from the master context, Ariel asks a
task-specific AI subagent to do that work and returns only the subagent's
structured result, provenance, omissions, and failures to the master.

Deterministic services are not the product brain. They are replaceable rails that
make current AI work safely: candidate retrieval, validation, policy, idempotency,
taint, storage, replay, and audit.

## Goals

- Make memory relevance AI-curated instead of deterministic ranked recall.
- Make final user-facing answers model-authored instead of deterministic
  tool-output prose.
- Make proactive feedback learning AI-authored instead of hardcoded feedback
  mappings.
- Make session continuity and compaction AI-authored instead of threshold-driven
  last-turn summaries.
- Make ambient event meaning AI-interpreted instead of source-local observation
  templates.
- Keep deterministic rails strict: auth, policy, taint, egress, schema,
  transactions, idempotency, replay, resource budgets, and audit.
- Preserve inspectability: every AI judgment records input candidates, selected
  items, omitted items, rationale, model id, prompt version, and parse outcome.
- Keep control flow explicit and local. Add narrow subagent calls for concrete
  product paths, not a generic agent framework.
- Fail closed when a required AI judgment is unavailable.

## Non-Goals

- No deterministic replacement brain.
- No generic workflow engine, automation builder, prompt DSL, subagent registry,
  planner framework, or reusable orchestration layer.
- No provider-hosted memory as Ariel's source of truth.
- No silent model fallback to deterministic summaries, deterministic answers, or
  deterministic priority decisions.
- No full transcript replay strategy.
- No compatibility with old memory recall response semantics.
- No compatibility with deterministic `_synthesize_*` final-answer behavior.
- No compatibility with deterministic feedback-to-learning mappings.
- No compatibility with old session rotation summary behavior.
- No sensors that are not explicitly configured.
- No side-effect policy delegated to model judgment.

## Target Behavior

### Master Assistant

- The master assistant receives the user request or proactive case.
- The master assistant delegates bounded judgment tasks before the final response.
- The master assistant sees structured subagent outputs, not raw candidate floods.
- The master assistant authors the final user-facing answer.
- The master assistant can explain which subagents ran, what they selected, what
  they omitted, and which rails authorized or denied actions.

### Memory And Context

- Deterministic code retrieves eligible memory candidates with provenance,
  lifecycle state, taint, source trust, validity windows, and budget metadata.
- An AI memory curator selects relevant memories for the current turn or
  proactive case.
- The memory curator returns selected items, omitted relevant candidates,
  uncertainty notes, conflict handling, and rationale.
- The master assistant receives the curated memory bundle.
- Deterministic candidate ordering can bound the candidate pool; it is not the
  final relevance decision.
- If AI memory curation fails when memory is required, the turn fails closed.

### Tool Results

- The model decides which tools to call.
- Deterministic code validates and executes authorized tool calls, records
  action attempts, artifacts, taint, typed failures, and provenance.
- Tool outputs are returned to the model or a tool-result subagent.
- A tool-result interpreter is required when raw tool output is too large,
  multi-source, contradictory, modality-specific, or otherwise unsuitable to
  hand directly to the master model within budget.
- The model authors final user-facing text from audited tool output.
- Deterministic code never creates final answer prose from tool output.
- If model attempts are exhausted after tool output, the turn fails with a typed
  model-output error instead of emitting deterministic fallback text.

### Feedback Learning

- Feedback records remain durable operator/user inputs.
- An AI feedback learner receives the case, decision, context snapshot, action
  results, delivery state, feedback, and related learning records.
- The learner writes exact durable learning records:
  - preference
  - example
  - calibration
  - source preference
  - prompt instruction
  - autonomy-scope proposal
- The learner can propose autonomy scopes but cannot grant them.
- Deterministic code validates the learner output and persists or rejects it.
- Hard policy blocks cannot be changed by feedback learning.

### Session Continuity And Compaction

- Deterministic thresholds remain rails for resource pressure: age, turn count,
  token budget, and wall time.
- When continuity or compaction is needed, an AI continuity curator receives the
  relevant turns, memory candidates, tool/action outcomes, open commitments, and
  omission budget.
- The curator writes a continuity record with summary, decisions, open loops,
  unresolved uncertainty, important omitted context, and source turn ids.
- The next turn reads AI-authored continuity records through the memory/context
  curation path.
- No deterministic last-N-turn rolling summary remains.
- No no-op compaction path is reachable when context exceeds the configured
  budget.

### Ambient Event Interpretation

- Source ingestion remains deterministic and writes raw or normalized source
  records with access, taint, and dedupe metadata.
- An AI ambient interpreter receives source events and nearby context.
- The interpreter decides event meaning, case key, observation subject,
  enrichment needs, likely relevance, and whether a proactive case should open or
  update.
- Ambient observers never notify or act directly.
- Source-local event templates can label provenance; they cannot decide
  importance or user interruption value.
- If interpretation fails, the raw source event is retained and the interpreter
  task fails closed for replay.

## Architecture

The AI-first turn pipeline is:

1. Ingress records the user turn, proactive case, or raw ambient source event.
2. Deterministic services gather bounded candidate context and provenance.
3. Narrow AI subagents perform task-specific judgment:
   - memory curator
   - tool-result interpreter when needed
   - feedback learner
   - continuity curator
   - ambient interpreter
4. Deterministic services validate subagent contracts and persist audit records.
5. The master model receives only the curated bundles and typed rail outcomes.
6. The master model authors the final response or structured proactive decision.
7. Deterministic rails validate, authorize, persist, deliver, execute, retry, and
   recover.

There is no generic orchestration framework. Each product path calls the exact
subagent it needs with an explicit local contract.

## Structure

### Subagent Contracts

Each subagent call has:

- task name
- prompt version
- model id
- bounded input bundle
- allowed read-only tools, when any
- strict JSON output schema
- selected ids
- omitted ids and reasons
- rationale
- confidence
- parse diagnostics
- rail validation result

Subagent outputs are append-only audit records. They can be superseded by later
subagent outputs but not overwritten.

### Deterministic Services

Deterministic services may:

- load candidate rows
- enforce access and lifecycle filters
- bound candidate count and token budget
- execute tools
- label taint and provenance
- validate JSON contracts
- persist state and events
- retry transient work
- dead-letter exhausted work
- expose inspection APIs

Deterministic services may not:

- choose final memory relevance
- write final answer prose
- summarize continuity semantically
- map feedback to behavior changes semantically
- decide event importance
- decide whether Ariel should interrupt

### Canonical Records

Add or reuse records so every AI judgment is inspectable:

- memory curation records for selected and omitted memory candidates
- tool-result interpretation records when tool output requires a separate
  subagent
- feedback learning records with source feedback and AI-authored content
- continuity compaction records with source turn ids and omissions
- ambient interpretation records with source event ids, case key, and enrichment
  requests
- existing turn, event, proactive case, action, artifact, and policy records

Use existing tables and event records where they already express the concept
directly. Add a new table only when no existing record can answer "what did the
AI judge and why?"

## Rules

- AI judgment is mandatory for the five cutover areas.
- Deterministic output ordering is candidate ordering only.
- Candidate omission for relevance requires AI rationale.
- Final answers are model-authored.
- Typed rail failures can stop a turn; they are not fallback answers.
- Subagent failures are visible to the master only as structured failure records.
- Subagent calls may use read-only tools only when the task contract allows them.
- Prompt-injection-bearing content remains tainted across subagent boundaries.
- Subagent output cannot grant permissions, change policy, or bypass taint.
- Action execution still requires deterministic policy validation.
- Feedback can propose autonomy; only explicit user/operator confirmation grants
  autonomy.
- Every deleted deterministic judgment path gets a grep-backed test.

## File Plan

### Docs

- `docs/ai-first.md`: repository-wide rule, already present.
- `docs/ai-first-judgment-cutover.md`: this cutover spec.
- `docs/index.md`: link this spec.
- `docs/modules/memory.md`: align with memory curation contract.
- `docs/proactive-ai-deliberation-cutover.md`: align proactive feedback and
  ambient interpretation details.
- `docs/production-runbook.md`: add operation and recovery notes for AI judgment
  tasks.
- `README.md`: summarize AI-first product behavior after implementation.

### Source

- `src/ariel/app.py`
  - Remove no-op compaction as a production path.
  - Route model loops through AI-authored memory curation and final answer
    synthesis.
  - Fail closed when required subagent output is invalid.
- `src/ariel/action_runtime.py`
  - Remove deterministic `_synthesize_*` final-answer functions.
  - Return audited tool outputs and typed failures to the model.
  - Keep policy, taint, action attempt, artifact, and execution rails.
- `src/ariel/memory.py`
  - Keep canonical lifecycle operations.
  - Remove deterministic memory relevance decisions from runtime recall.
- `src/ariel/memory_retrieval.py`
  - Own candidate retrieval, filtering, ordering, and provenance.
- `src/ariel/memory_curation.py`
  - Own AI memory curation and context bundle construction.
- `src/ariel/proactivity.py`
  - Replace hardcoded feedback mapping with AI feedback learning.
  - Replace source-local ambient observation meaning with AI ambient
    interpretation.
  - Keep proactive decision parsing, policy validation, action planning, and
    replay rails.
- `src/ariel/sync_runtime.py`
  - Persist source deltas and provenance.
  - Stop source-local templates from deciding case meaning or importance.
- `src/ariel/worker.py`
  - Dispatch new AI judgment tasks.
  - Recover stale failed AI judgment tasks under retry budgets.
- `src/ariel/persistence.py`
  - Add only the audit records required for new AI judgments.
- `src/ariel/response_contracts.py`
  - Surface inspection contracts for curation, interpretation, compaction, and
    learning records.
- `src/ariel/config.py` and `.env.example`
  - Add only required model/tool budgets for concrete subagent calls.

### Migrations

- Add one hard-cutover Alembic migration.
- Remove columns, constraints, and records that only exist for old deterministic
  judgment semantics.
- Add audit tables only for AI judgment records that need durable inspection.
- Downgrade can restore schema shape for development rollback, but runtime code
  must not preserve compatibility behavior.

### Tests

- Add integration tests for AI memory curation.
- Add integration tests proving deterministic tool synthesis is unreachable.
- Add tool-result interpreter tests for large, multi-source, contradictory, and
  modality-specific outputs.
- Add feedback learning tests with AI-authored learning records.
- Add session compaction tests proving no deterministic last-N summary remains.
- Add ambient interpretation tests proving source-local templates do not open
  proactive cases directly.
- Add legacy-surface removal tests for old response fields, event concepts, and
  task payloads.
- Add grep tests for removed deterministic judgment functions and old fallback
  phrases.
- Keep rail tests for policy, taint, idempotency, replay, and audit.

## Key Details

### Memory Curation

The candidate retrieval service returns more candidates than the master model
should see. The AI memory curator reduces that pool to the actual memory bundle.

The curation output must include:

- selected memory ids
- selected evidence ids
- omitted candidate ids
- omission reasons
- conflict notes
- uncertainty notes
- token budget used
- model id and prompt version

### Tool Result Handling

Tool outputs are never converted into final prose by deterministic code.

The model loop continues with:

- original model output items
- function call outputs
- audited tool summary as data, not final text
- retrieval artifact ids
- taint and provenance
- typed tool failures

If the model cannot produce a valid final answer within budget, the turn fails
closed with an auditable model-output error.

### Feedback Learning

Feedback learning runs as a worker task. The AI feedback learner reads the
feedback and the linked case/turn records, then returns one or more exact
learning records.

`automatic_next_time` can create an autonomy-scope proposal. It cannot activate
the scope.

### Continuity

Continuity compaction is a required AI judgment under context pressure.

The continuity curator output must include:

- source turn ids
- user commitments
- assistant commitments
- unresolved decisions
- active tool/action outcomes
- important user preferences
- omitted context notes
- expiration or validity hints

### Ambient Interpretation

Ambient interpretation runs after source events are durably recorded. The AI
interpreter decides:

- what happened
- which existing case, if any, it updates
- whether more read-only context is needed
- whether a proactive deliberation should run
- what evidence refs should be attached
- what uncertainty should be preserved

## Key Decisions

- AI-first is the default architecture.
- The master assistant owns final user-facing behavior.
- Subagents are narrow product-path model calls, not a generic framework.
- Deterministic candidate retrieval remains allowed only as a bounded service.
- Deterministic final-answer synthesis is removed.
- Deterministic feedback mapping is removed.
- Deterministic semantic continuity summaries are removed.
- Deterministic ambient event meaning is removed.
- Policy validation remains deterministic.
- Invalid AI output fails closed.
- No fallback provider, fallback prose, fallback ranking, or fallback summary is
  allowed.

## Final State

- Every memory bundle shown to the master assistant is AI-curated.
- Every final user-facing answer after tool use is model-authored.
- Every feedback learning record is AI-authored and rail-validated.
- Every session continuity record is AI-authored and source-linked.
- Every proactive observation/case meaning is AI-interpreted after source
  ingestion.
- Deterministic services expose candidates, tools, policies, persistence, replay,
  and audit only.
- Old deterministic judgment functions, records, tests, and docs are removed.
- Inspection APIs can answer:
  - which subagent ran?
  - what did it see?
  - what did it select?
  - what did it omit?
  - what did it decide?
  - what rail allowed or denied it?
  - what failed closed?

## Acceptance Criteria

### Memory

- A turn with many plausible memories produces a memory curation record with
  selected and omitted candidates.
- The selected memory bundle can include a lower candidate-order item when the
  AI curator explains why it is relevant.
- Deleted, rejected, retracted, and inaccessible memories never enter the
  candidate pool.
- If memory curation returns invalid JSON, the turn fails closed and no
  deterministic ranking bundle is used.
- Tests fail if deterministic rank score alone decides the memory bundle shown
  to the master assistant.

### Tool Results

- Web, news, weather, maps, Google, attachment, and Agency read outputs return to
  the model as audited tool output.
- No `_synthesize_*` deterministic final-answer path is reachable.
- Large, multi-source, contradictory, and modality-specific tool outputs route
  through a tool-result interpreter before reaching the master model.
- If the model cannot produce a final answer after tool output, the turn records
  a typed model-output failure.
- Retrieval citations and artifacts remain available to the model and response
  contract.
- Tests fail if deterministic fallback prose is emitted after tool calls.

### Legacy Surface Removal

- Old response fields for deterministic judgment surfaces are removed or renamed
  to native AI-first fields.
- Old event concepts for deterministic ranking, synthesis, feedback mapping,
  continuity summaries, and source-local ambient meaning are gone.
- Old task payloads for deterministic judgment paths are invalid and
  dead-lettered as unsupported deployment leftovers.
- Tests fail if any old deterministic judgment endpoint, task type, response
  field, event name, or compatibility route remains reachable.

### Feedback

- `stop_pattern`, `more_aggressive`, `useful`, `wrong`, `correct`, `ack`, and
  `automatic_next_time` run through AI feedback learning.
- The learner output is persisted with model id, prompt version, source feedback
  id, and validation result.
- `automatic_next_time` can propose but not grant an autonomy scope.
- Invalid learner output fails closed without hardcoded mapping.
- Tests fail if feedback type maps directly to a canned instruction.

### Continuity

- Context pressure triggers AI continuity compaction.
- The continuity record cites source turn ids and omission reasons.
- The old last-three-turn deterministic summary is unreachable.
- No-op compaction is not reachable when context exceeds budget.
- Invalid continuity output fails closed with an auditable error.

### Ambient Interpretation

- Google, Discord, capture, job, approval, connector, memory, and Agency events
  are first stored as source events or raw observations.
- AI ambient interpretation creates or updates proactive observations and cases.
- Duplicate source events do not duplicate cases or decisions.
- Invalid interpretation output fails closed and leaves the source event
  replayable.
- Tests fail if source-local templates directly decide proactive case meaning or
  interruption value.

### Rails

- Policy, taint, egress, auth, idempotency, replay, transaction, and audit tests
  still pass.
- Prompt-injection content cannot grant authority across any subagent boundary.
- Side effects require deterministic policy validation after AI judgment.
- Every AI judgment record is inspectable through API or event timeline.
- `make verify` passes.

## Implementation Plan

1. Add schema and response contracts for AI judgment audit records.
2. Add explicit model-call helpers only where each product path needs them.
3. Cut over memory runtime to candidate retrieval plus AI memory curation.
4. Delete deterministic tool-result final-answer synthesis and route tool
   outputs back through model-authored responses.
5. Cut over feedback learning to AI-authored learning records.
6. Cut over session continuity to AI-authored compaction records.
7. Cut over ambient observation interpretation to AI-authored source-event
   interpretation.
8. Remove old deterministic judgment functions, task names, tests, and docs.
9. Add acceptance, integration, and grep coverage for the hard cutover.
10. Run ruff, mypy, PostgreSQL integration tests, Alembic upgrade/downgrade
    smoke, full pytest, and grep checks.

No step is production-ready until all five deterministic judgment areas are
unreachable and the full acceptance suite passes.
