# AI-First SOTA Gap Cutover

## Scope

This document owns the hard-cutover plan for the remaining gaps found after
manual verification of [ai-first-verification-gap-cutover.md](ai-first-verification-gap-cutover.md).

It closes these verified gaps:

- stale public docs and environment examples still describe old ambient
  derivation surfaces
- tool-result interpreter failures drop provider response ids
- exhausted model attempts after tool output return generic turn-limit behavior
  instead of a typed AI model-output failure
- proactive deliberation can lose memory-curation failures before a
  proactive-case audit row exists
- rotation continuity accepts incomplete model output
- memory recall candidate bundles do not expose enough retrieval features,
  trust, taint, or ordering evidence for AI curation audit
- ambient source enums expose unconfigured source types without real ingress,
  persistence, and tests
- `AIJudgmentRecord.failure_code` is not schema-constrained to the typed set
- event/API/status vocabulary can diverge from `ai_judgments`
- feedback learning lacks schema-invalid coverage
- `.env.example` is incomplete under the repo environment rule
- grep-backed legacy cleanup does not cover README, env examples, and stale
  fallback-shaped test names

The goal is not another architecture proposal. This is the final cleanup pass
that makes the current AI-first architecture precise, auditable, and shippable.

## SOTA Posture

Ariel's target state follows the current agent pattern:

- the model owns interpretation, tool strategy, memory selection, continuity,
  proactive judgment, and final wording
- deterministic code owns isolation, execution, validation, policy, taint,
  authorization, idempotency, audit, retry, and recovery
- agent loops return tool results to AI instead of synthesizing deterministic
  final prose
- memory is a write-manage-read loop coupled to perception and action
- proactive assistance starts from durable sensed context, then AI decides
  whether to speak, wait, remember, ask, inspect, or act
- subagents or task-specific model calls run in bounded contexts with strict
  output contracts and audit rows
- autonomous writes are free only inside explicit scopes; prompt-injection and
  taint rails remain deterministic hard stops

Reference baselines:

- OpenAI ChatGPT agent combines research, tool use, browser execution,
  terminal-limited work, connectors, and safety controls.
- Claude computer use documents the agent loop: model requests tool use,
  deterministic code executes, tool results return to the model, and the model
  continues or answers.
- Claude subagents isolate bounded work in independent contexts.
- Claude memory separates persistent written rules from model-authored auto
  memory, with audit and edit controls.
- Recent memory-agent research frames agent memory as a write-manage-read loop
  across context compression, retrieval stores, reflection, hierarchical
  virtual context, and policy-learned management.
- Context-aware proactive-agent research uses sensory context plus persona and
  history to decide whether to offer proactive service and call tools.

## Cutover Policy

- This is a hard cutover.
- Do not preserve old derivation routes, task names, response fields, event
  terms, docs, examples, or fallback-shaped tests.
- Do not add compatibility aliases for removed task names, response fields,
  error codes, or environment variables.
- Do not add deterministic prose when model output is missing, malformed, over
  budget, or exhausted.
- Required AI judgment attempts must write exactly one success or failure
  `AIJudgmentRecord`.
- Every failure code in `AIJudgmentRecord` must be either `null` or one of the
  typed AI judgment codes.
- Provider response ids are audit provenance. Preserve them when supplied; never
  invent them.
- Ambient source types exist only when they have real configuration,
  persistence, ingress, trust/taint labeling, dedupe, interpretation, and tests.
- Unconfigured ambient source families are documented as absent. They are not
  allowed in runtime enums or constraints.
- Deterministic code can explain rail failures. It cannot decide semantic
  importance, final wording, relevance, continuity meaning, or interruption
  value.
- No feature flags, dual paths, compatibility routes, or legacy fallback code are
  allowed.

## Goals

- Make every remaining verifier finding impossible to regress.
- Make model-output exhaustion a typed, auditable AI failure instead of a generic
  turn-limit response.
- Preserve provider response ids through all tool-result interpreter success and
  failure paths.
- Audit proactive-deliberation memory curation failures as proactive case
  failures.
- Tighten continuity output validation so incomplete summaries fail closed.
- Give memory curation enough candidate evidence to judge relevance without
  relying on SQL order as hidden semantics.
- Remove placeholder ambient source enum values until a real source exists.
- Constrain failure codes in ORM and migration schema.
- Normalize event/API/judgment status vocabulary.
- Make README and `.env.example` enforce AI-first terminology and configuration.
- Keep the implementation local, linear, and direct.

## Non-Goals

- No generic subagent framework.
- No workflow engine, prompt registry, automation builder, planner DSL, or
  generic sensor framework.
- No provider-hosted memory as source of truth.
- No broad computer-use environment or desktop agent in this pass.
- No scheduled prompt, daily briefing, digest, or deterministic notification
  product.
- No implementation for location, local activity, repository, CI, or incident
  sensing unless each has real configuration, persistence, ingress, trust/taint
  labels, dedupe, interpretation, and tests in the same cutover.
- No high-impact autonomous action expansion.
- No backward compatibility for stale public docs, old task names, old routes,
  or old env wording.

## Target Behavior

### Documentation And Environment

- `README.md` describes only AI-first ambient interpretation and proactive
  deliberation.
- `README.md` does not mention ambient derivation, derive endpoints, old derive
  task names, or deterministic proactive ranking.
- `.env.example` includes every environment variable read by `src/ariel/config.py`.
- Each `.env.example` entry states whether it is required or optional and gives
  the default when code has one.
- `.env.example` uses `ambient interpretation` and `ambient sensing`
  terminology.
- Grep tests cover active docs, README, `.env.example`, source, migrations, and
  tests for banned AI-first legacy surfaces.

### Tool-Result Interpretation

- Tool-result interpreter calls preserve provider, model, provider response id,
  usage, prompt version, parse status, validation status, and safe raw output
  shape on success and failure.
- Invalid JSON writes a failed `tool_result_interpretation` judgment with
  `E_AI_JUDGMENT_INVALID_JSON` and the provider response id when present.
- Schema-invalid output writes a failed `tool_result_interpretation` judgment
  with `E_AI_JUDGMENT_SCHEMA` and the provider response id when present.
- Tool-result interpreter failure emits no deterministic final answer.
- The turn fails closed with an API error and timeline event that use the same
  code, parse status, validation status, and provider response id as the
  `AIJudgmentRecord`.

### Model-Output Exhaustion

- If the model consumes all allowed attempts after tool output or tool-result
  interpretation without producing final model-authored output, the turn writes a
  failed `model_output` AI judgment.
- The failure code is `E_AI_JUDGMENT_BUDGET`.
- The failure includes attempt count, limit, last provider response id when
  present, last tool-result interpretation id when present, and safe last output
  shape.
- The user receives an error envelope, not deterministic prose.
- No `E_TURN_LIMIT_REACHED` path remains for model-authored output exhaustion.
  Generic turn-limit rails may remain only for non-AI resource limits that are
  not judgment output.

### Proactive Memory Curation Failure

- Proactive deliberation creates or identifies the proactive case before memory
  curation runs.
- If memory curation fails while building proactive deliberation context, the
  case receives:
  - failed `memory_curation` `AIJudgmentRecord`
  - failed `proactive_deliberation` `AIJudgmentRecord` that cites the memory
    failure
  - case event with the same code and status vocabulary
  - failed case status
  - retryable task error when the failure is retryable
- No uncaught `AIJudgmentFailure` leaves the worker without a case audit trail.
- No proactive decision, turn, action plan, or delivery is created after failed
  required memory curation.

### Continuity Contract

- Rotation and context-pressure continuity use one required output contract.
- Source turns exist means the AI output must include:
  - `summary`
  - `preserved_turn_refs`
  - `omitted_turn_refs`
  - `user_commitments`
  - `assistant_commitments`
  - `decisions`
  - `open_loops`
  - `tool_action_outcomes`
  - `unresolved_uncertainty`
  - `important_omissions`
  - `confidence`
- Every source turn id must appear in either `preserved_turn_refs` or
  `omitted_turn_refs`.
- Every preserved or omitted turn ref must include a string reason.
- Unknown turn ids, duplicate turn ids, missing accounting, out-of-range
  confidence, or missing required keys produce `E_AI_JUDGMENT_SCHEMA` or
  `E_AI_JUDGMENT_VALIDATION`.
- Invalid continuity output writes a failed `continuity_compaction` judgment and
  stops rotation or compaction.
- Empty source-turn rotation remains the only no-candidates continuity path.

### Memory Candidate Evidence

- Candidate retrieval remains deterministic rail work.
- Each memory candidate exposed to AI curation includes:
  - candidate id and kind
  - lifecycle state
  - source evidence ids and snippets or source refs
  - trust boundary
  - taint state
  - validity interval
  - confidence or review state when present
  - retrieval features used to place it in the candidate set
  - retrieval order index
  - projection version
  - conflict status when applicable
- SQL order is recorded as a transport feature, not treated as final relevance.
- AI curation still chooses selected order and omitted reasons.
- Tests prove the memory bundle can be selected by AI against candidate features,
  not by deterministic order alone.

### Ambient Source Coverage

- Runtime source constraints include only configured durable sources.
- `ci`, `location`, `local_activity`, repository, and incident source types are
  removed from runtime constraints unless implemented end-to-end in this cutover.
- Documentation explicitly lists unconfigured source families as absent.
- No placeholder source type can be inserted into `proactive_observations`.
- Every allowed source type has at least one test proving:
  - source record exists
  - trust boundary exists
  - taint exists
  - dedupe key exists
  - ambient interpretation sees it before any case opens

### Failure Code And Status Vocabulary

- `AIJudgmentRecord.failure_code` is constrained in ORM and Alembic:
  - `null`
  - `E_AI_JUDGMENT_REQUIRED`
  - `E_AI_JUDGMENT_CREDENTIALS`
  - `E_AI_JUDGMENT_TIMEOUT`
  - `E_AI_JUDGMENT_INVALID_JSON`
  - `E_AI_JUDGMENT_SCHEMA`
  - `E_AI_JUDGMENT_VALIDATION`
  - `E_AI_JUDGMENT_BUDGET`
- Events, API errors, task errors, and judgment rows use the same code for the
  same failed attempt.
- `validation_status` values are exactly `valid`, `invalid`, or
  `not_validated` everywhere.
- Event payloads do not use `validation_status = "failed"`.
- The codebase has one typed vocabulary table in docs and one schema constraint
  in code. No aliases.

### Feedback Learning Coverage

- Schema-invalid feedback learner output writes a failed `feedback_learning`
  `AIJudgmentRecord` with `E_AI_JUDGMENT_SCHEMA`.
- Invalid JSON feedback learner output writes `E_AI_JUDGMENT_INVALID_JSON`.
- Both paths persist no learning records.
- Tests assert the failure row directly from the database.

### Legacy Cleanup

- Active docs, README, env examples, runtime, migrations, and tests do not imply
  old compatibility behavior.
- Test names do not use `fallback` for accepted rail behavior.
- Allowed non-AI-first uses of the word `fallback` must be explicitly scoped in
  the grep test allowlist with a reason.
- Old route, task, and event names are absent from active docs and runtime.

## Architecture

The final pipeline is:

1. Ingress persists a user turn, source event, proactive case, feedback event,
   rotation request, or context-pressure trigger.
2. Deterministic rails gather bounded candidate context and provenance.
3. The exact AI judgment call runs in its local product path.
4. Deterministic rails parse and validate the model output.
5. The attempt writes one `AIJudgmentRecord`.
6. Success continues with AI-authored output.
7. Failure stops the path or lets the worker retry through existing task budgets.
8. Deterministic rails enforce policy, taint, egress, idempotency, persistence,
   execution, replay, inspection, and dead-lettering.

No generic judgment dispatcher is introduced. Each product path keeps its local
prompt, model call, validation, audit write, and fail-closed behavior.

## Structure

### AI Judgment Types

Required judgment types after this cutover:

- `memory_curation`
- `tool_result_interpretation`
- `continuity_compaction`
- `feedback_learning`
- `ambient_interpretation`
- `proactive_deliberation`
- `model_output`

`model_output` is reserved for required final model-authored output failures.
It is not used for rail limits unrelated to model-authored output.

### Failure Shape

Every failed AI judgment row includes:

- judgment type
- source type
- source id
- model when known
- prompt version
- provider response id when supplied
- input refs
- safe output shape when available
- parse status
- validation status
- typed failure code
- safe failure reason
- retryability at the API or task boundary

### Ambient Source Shape

Every allowed ambient source must have:

- source provider or internal owner
- source id
- source type
- observed time
- durable record id
- trust boundary
- taint
- dedupe key
- replayable payload
- AI ambient interpretation task coverage

### Memory Candidate Shape

Memory curation candidates use local dictionaries. Do not add a reusable model
class unless the same shape is written to the database or validated at more than
one product boundary.

Required local keys:

- `id`
- `kind`
- `value` or `summary`
- `evidence_refs`
- `trust_boundary`
- `taint`
- `lifecycle_state`
- `retrieval_features`
- `retrieval_rank`
- `projection_version`

## Rules

- Every required AI output failure is an AI judgment failure, not generic turn
  failure prose.
- Every provider response id exposed by the model adapter survives into the
  success or failure audit row.
- Every source enum value must correspond to real runtime ingress.
- Every allowed no-candidates path must write an auditable judgment row.
- Every schema-invalid model response must fail closed before any product state
  derived from it is trusted.
- Every old surface removed from runtime must be removed from active docs and
  env examples.
- Tests may use model fixtures. Tests must not reintroduce deterministic
  semantic or relevance logic as an oracle.
- Keep control flow linear and local. Extract functions only when the extracted
  code prevents real duplication or isolates a true boundary.

## Files

### Docs

- `docs/ai-first-sota-gap-cutover.md`
  - This spec.
- `docs/index.md`
  - Link this spec.
- `docs/ai-first-completion-cutover.md`
  - Point final remaining work here.
- `docs/ai-first-verification-gap-cutover.md`
  - Point post-verification remaining work here.
- `docs/proactive-ai-deliberation-cutover.md`
  - Align ambient source and model-output failure language if stale.
- `docs/modules/memory.md`
  - Align candidate feature and continuity contract details.
- `docs/production-runbook.md`
  - Add operator recovery for model-output, tool-result interpretation, and
    proactive memory-curation failures.
- `README.md`
  - Remove old ambient derivation surfaces and stale fallback-shaped prose.
- `.env.example`
  - Include every `AppSettings` environment variable with required/default
    notes.

### Source

- `src/ariel/persistence.py`
  - Add `model_output` judgment type.
  - Add `failure_code` check constraint.
  - Remove unconfigured proactive observation source types.
- `alembic/versions/20260501_0020_proactive_ai_deliberation_cutover.py`
  - Match hard-cutover schema.
- New Alembic migration if altering existing databases is required.
- `src/ariel/app.py`
  - Preserve tool-result interpreter provider response ids on failures.
  - Record typed `model_output` budget failures.
  - Normalize event validation statuses.
  - Keep turn path fail-closed without deterministic prose.
- `src/ariel/proactivity.py`
  - Catch proactive memory curation failures and write case-linked audit rows.
  - Remove unconfigured ambient source types from runtime paths.
- `src/ariel/memory.py`
  - Validate continuity contract fully.
  - Add memory candidate retrieval feature metadata.
- `src/ariel/response_contracts.py`
  - Expose only AI-native inspection fields needed by API responses.
- `src/ariel/config.py`
  - Stay source of truth for env var names; no compatibility aliases.

### Tests

- `tests/unit/test_ai_first_legacy_surfaces.py`
  - Expand grep coverage to README and `.env.example`.
  - Ban old derive route/task/event names across active docs and runtime.
  - Ban stale fallback-shaped test names outside explicit allowlist.
  - Assert typed failure code constants and no old aliases.
- `tests/integration/test_pr01_acceptance.py`
  - Add tool-result interpreter invalid JSON/schema provider response id tests.
  - Add model-output exhaustion typed failure test.
- `tests/integration/test_proactive_api_controls.py`
  - Add schema-invalid feedback learner failure test.
- `tests/integration/test_proactive_ambient_sources.py`
  - Assert runtime source type allowlist contains only implemented durable
    sources.
  - Assert unconfigured source families are documented as absent.
- `tests/integration/test_s5_pr01_acceptance.py`
  - Assert memory candidate feature fields are included in curation input and
    audit.
- `tests/integration/test_s5_pr02_session_management_acceptance.py`
  - Assert continuity contract rejects missing accounting, unknown turn ids,
    duplicate turn ids, missing reasons, missing required keys, and bad
    confidence.
- `tests/integration/test_worker_proactive_completion.py`
  - Assert proactive deliberation memory curation failure is audited and
    retry/dead-letter behavior is typed.
- Migration/schema tests
  - Assert `failure_code` constraint rejects untyped codes.
  - Assert removed ambient source types cannot be inserted.

## Key Details

### Provider Response Id Failure Preservation

Model adapter errors should carry provider response ids. If a helper parses a
provider response and then fails validation, the raised error must preserve the
response id. Do not recover the id by reparsing logs or output strings.

### Model-Output Failure

The output budget path is a model-output judgment failure because the model was
required to author final output and did not. It is not a tool failure, action
failure, transport failure, or deterministic turn-limit answer.

### Proactive Memory Curation

Proactive deliberation has two required AI judgments when memory candidates
exist: memory curation and proactive deliberation. If the first fails, the second
records a failed dependency outcome rather than pretending deliberation happened.

### Continuity Validation

Continuity is not a loose summary. It is a structured state-transfer contract.
The schema is intentionally strict because any omitted source turn is removed
from future context unless preserved in continuity.

### Ambient Source Honesty

SOTA ambient sensing does not mean placeholder enums. It means configured,
consented, durable sensing paths. A source that cannot be observed, replayed,
taint-labeled, and tested is absent.

### Memory Candidate Features

Candidate retrieval can use deterministic features to build a bounded candidate
set. Those features are evidence for the AI curator, not final relevance
decisions. The curator output owns selected order and omission reasons.

## Key Decisions

- Add `model_output` as a first-class AI judgment type.
- Add a database check constraint for AI judgment failure codes.
- Treat stale public docs and env examples as product-surface bugs.
- Remove unconfigured ambient source enum values rather than leaving aspirational
  placeholders.
- Validate continuity accounting strictly because continuity is future context,
  not optional prose.
- Keep subagents as local task-specific model calls. Do not add a generic
  subagent framework.
- Keep deterministic memory candidate retrieval but expose its features to AI.
- Keep deterministic policy and taint hard stops for autonomous action.

## Acceptance Criteria

### Global

- `make verify` passes.
- `uv run ruff check .` passes.
- `uv run ruff format --check .` passes.
- `uv run mypy src tests` passes.
- `ulimit -n 4096 && uv run pytest -q --maxfail=1` passes.
- `git diff --check` passes.
- Alembic upgrade and downgrade smoke tests pass if a migration is added.
- No old AI-first surfaces remain in active docs, README, env examples, source,
  migrations, or tests except historical migration identifiers explicitly
  allowlisted by the grep test.

### Documentation And Env

- README contains no ambient derivation route, task, or product wording.
- `.env.example` contains every `AppSettings` env var.
- `.env.example` uses AI-first ambient interpretation terminology.
- Grep tests cover README and `.env.example`.

### Tool-Result Interpretation

- Invalid JSON interpreter output writes failed `tool_result_interpretation`
  with provider response id preserved.
- Schema-invalid interpreter output writes failed `tool_result_interpretation`
  with provider response id preserved.
- No deterministic final text is emitted after interpreter failure.

### Model Output

- Exhausted model attempts after tool output write failed `model_output`
  `AIJudgmentRecord` with `E_AI_JUDGMENT_BUDGET`.
- The API error and event payload use the same failure code and status
  vocabulary.
- `E_TURN_LIMIT_REACHED` is unreachable for final model-output exhaustion.

### Proactive Deliberation

- Memory curation failure during proactive context build writes failed
  `memory_curation` and failed `proactive_deliberation` rows.
- The proactive case is failed and no decision, turn, action plan, or delivery is
  produced.
- Retryable failures remain retryable through worker task budgets.

### Continuity

- Rotation continuity rejects missing required keys.
- Rotation continuity rejects missing turn accounting.
- Rotation continuity rejects duplicate or unknown source turn ids.
- Rotation continuity rejects missing reasons.
- Rotation continuity rejects invalid confidence.
- Context-pressure continuity follows the same contract.
- Empty no-candidates rotation remains audited and valid.

### Memory

- AI curation input includes trust, taint, lifecycle, evidence, retrieval
  features, retrieval rank, and projection version.
- Curation audit records selected and omitted items with the candidate evidence
  needed to explain the model decision.
- Tests fail if SQL order alone decides the selected memory bundle.

### Ambient

- Runtime source constraints include only implemented durable sources.
- `ci`, `location`, and `local_activity` are rejected unless implemented
  end-to-end.
- Docs explicitly list unconfigured source families as absent.
- Every allowed source type has a durable-source-to-AI-interpretation test.

### Failure Codes And Status

- Database rejects unknown AI judgment failure codes.
- Runtime has no `E_AI_JUDGMENT_MODEL` or `E_AI_JUDGMENT_JSON`.
- Event payloads never use `validation_status = "failed"`.
- API errors, task errors, case events, and judgment rows agree for the same
  failed AI judgment attempt.

### Feedback

- Schema-invalid feedback learner output writes failed `feedback_learning` with
  `E_AI_JUDGMENT_SCHEMA`.
- Invalid learner output persists no `ProactiveLearningRecord`.

## Implementation Plan

1. Update docs, README, and `.env.example`; expand grep tests.
2. Add `model_output` and failure-code constraints to ORM and migrations.
3. Preserve provider response ids in tool-result interpreter failures.
4. Replace model-output exhaustion with typed `model_output` judgment failure.
5. Audit proactive memory curation failures inside proactive deliberation.
6. Tighten continuity validation and tests.
7. Add memory candidate retrieval feature metadata.
8. Remove unconfigured ambient source enum values and document absent sources.
9. Normalize event/API/judgment validation status vocabulary.
10. Add schema-invalid feedback learner coverage.
11. Run focused tests, migration smoke, full pytest, `make verify`, grep checks,
    and `git diff --check`.

No step is complete until the old behavior is unreachable and every remaining
verified gap has a direct acceptance test.

## Final State

Ariel is an AI-first proactive agent with:

- durable event-triggered deliberation
- configured ambient sensing through replayable source records
- model-authored proactive turns
- scoped autonomous actions with deterministic safety rails
- AI-owned memory curation and continuity
- AI-owned feedback learning
- AI-owned tool-result interpretation
- typed model-output failures
- provider-response provenance on success and failure
- strict source, failure-code, and continuity contracts
- public docs and env examples that describe only the current product

There are no legacy proactive derivation surfaces, no fallback prose, no
placeholder ambient source types, no untyped AI judgment failures, and no
deterministic replacement brain.

