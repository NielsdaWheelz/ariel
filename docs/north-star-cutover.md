# North-Star Cutover

## Scope

This document owns the hard cutover plan for Ariel's new product and
architecture north-star.

It converts the repository-wide rules in [ai-first.md](ai-first.md) and
[agent-tooling.md](agent-tooling.md) into an implementation spec for this
codebase.

The cutover is intentionally incompatible with the current broad tool-catalog
runtime. There is no compatibility layer, no legacy mode, no fallback path, and
no long-term feature flag. Work may be sequenced across commits, but the merged
final state contains only the new surfaces.

## Thesis

Ariel is a Discord control plane for an AI operator.

Coding and repository work happen through a governed executable environment.
Repeated procedures become skills or reviewed procedural memory. Structured
tools exist only for authority, safety, audit, credentials, trust boundaries, or
domain side effects.

The model must not receive the full capability registry. The registry is an
internal authority catalog. The model sees a small, task-scoped working set.

## Target Behavior

### User-Facing Product

Discord is the primary human surface.

The Discord product presents user actions in plain language:

- start a coding task
- inspect a job
- approve or reject an action
- review artifacts
- create or update a pull request
- read an attachment
- send or draft email
- archive, label, trash, or undo email changes
- create a calendar event
- remember a preference or procedure
- acknowledge or give feedback on proactive behavior

Discord never exposes internal capability IDs such as `cap.email.send` in normal
copy. Capability IDs remain visible only in API payloads, logs, audit records,
and developer diagnostics.

The HTTP API is an operator and integration surface, not a second public product
surface. Authority-bearing routes require local authentication. Loopback binding
is not an authentication boundary.

### Agent Behavior

For ordinary turns, Ariel runs a tool strategy pass before the answer pass.

The strategy pass is an AI judgment with a strict output contract. It receives:

- the user message
- bounded context
- available capability families
- current surface metadata
- connector and runtime availability
- attachment and job presence
- hard policy exclusions

It returns either:

- no tools
- a small list of selected capability IDs
- an auditable reason that the task cannot proceed without unavailable authority

The answer pass receives only the selected tool definitions. It cannot call
anything else.

Deterministic code may filter eligibility by hard facts: connector availability,
attachment presence, policy, runtime binding, source surface, proactive case
type, trust boundary, and environment configuration. Deterministic code must not
perform semantic intent classification to choose tools.

### Coding Work

Coding work routes through Agency.

The terminal belongs inside a sandboxed Agency/Codex execution environment. Ariel
does not grow separate GitHub, filesystem, test, linter, build, or shell tools
when Agency can do the work.

Ariel's direct responsibilities for coding work are:

- approve starting work
- pass a bounded prompt and repo root to Agency
- record job identity and sandbox policy
- inspect status and artifacts
- present results in Discord
- approve PR landing
- reconcile side effects

Agency work is not opaque. Ariel persists command or invocation summaries,
working directory, exit status, artifact refs, sandbox policy version, egress
policy version, side-effect receipts, and verification status.

### Personal Assistant Work

Google, email, calendar, Drive, attachment, and selected web operations remain
structured tools only where their boundary is justified.

Justified structured tools include:

- OAuth-backed Google reads and writes
- email send, archive, trash, label mutation, undo, draft, and thread watch
- calendar event creation
- Drive sharing
- Discord attachment reading and extraction
- public URL extraction with SSRF, size, and provenance rails
- Agency task start and PR request
- Discord no-response behavior

Generic search and news are retained only if Ariel needs server-side credentials,
grounded artifact capture, citation normalization, or operation in an environment
without shell/browser access. Otherwise research belongs to the executable
environment or a skill.

### Proactivity

Proactive deliberation gets case-scoped tools, not all read tools.

Rules:

- Read-only is necessary but not sufficient.
- A job case may inspect that job's status and artifacts.
- An attachment case may inspect the referenced attachment.
- An email thread-watch case may inspect the relevant email/thread state.
- A connector case may inspect only the connector-bound source that produced the
  case.
- Most proactive cases start with no tools.
- Proactivity never receives shell-like authority.
- Proactive writes must pass autonomy scope validation and action policy.

Proactive model contracts use one shape for memory updates and one shape for
actions. There is no duplicate `remember` surface and no bespoke
`send_discord_message` action path outside the capability/action system.

### Memory And Skills

Memory relevance, extraction, continuity, feedback learning, and procedure
selection are AI judgments.

Repeated workflows become reviewed procedural memory or skills. New structured
tools are forbidden for workflow knowledge alone.

Procedure records are the durable in-product form of "how Ariel should do this
next time." Skills are the repository/runtime form of repeated agent workflows.

Memory candidate ordering is transport order. It is not a deterministic
relevance score. Fields and docs must not imply otherwise.

## Final Architecture

### Runtime Flow

Normal user turn:

1. Ingest Discord/API message.
2. Build bounded context and eligibility facts.
3. Run tool strategy AI judgment.
4. Filter selected IDs through deterministic eligibility rails.
5. Build strict Responses tool definitions for selected IDs only.
6. Run answer model.
7. Process tool calls through policy, approval, execution, audit, and result
   interpretation.
8. Produce final Discord/API response.
9. Extract memory/procedural candidates as separate audited AI judgments.

Proactive case:

1. Ingest ambient observation.
2. Build case and context snapshot.
3. Select case-scoped read tools by hard source facts and optional strategy
   judgment.
4. Run deliberation model.
5. Validate decision shape.
6. Apply memory, notify, wait, observe, or propose action.
7. Validate action against autonomy scope and policy.
8. Queue side effects and feedback loops.

Agency coding job:

1. User asks for repo/coding work.
2. Strategy selects Agency task start, or answer asks for missing approval/context.
3. Policy requires approval for task start.
4. Agency daemon starts sandboxed work.
5. Worker syncs status, artifacts, timeline, verification, and sandbox receipts.
6. Discord presents job state and review actions.
7. PR request requires approval and uses crash-safe side-effect receipts.

### Module Structure

The final codebase splits capability definition, presentation, and execution.

Target modules:

- `src/ariel/capabilities/core.py`
  - `CapabilityDefinition`
  - impact levels
  - policy metadata
  - admission metadata
  - common schema helpers
  - registry construction primitives

- `src/ariel/capabilities/google.py`
  - Google capability definitions
  - Google validators
  - Google schema names
  - no runtime stubs that pretend to execute without a Google runtime

- `src/ariel/capabilities/retrieval.py`
  - web extract, search, news, maps, weather
  - egress destination calculation
  - bounded output contracts

- `src/ariel/capabilities/attachments.py`
  - attachment read tool definition and schema metadata

- `src/ariel/capabilities/agency.py`
  - Agency task, status, artifact, and PR capability definitions
  - Agency admission rules
  - sandbox policy metadata

- `src/ariel/capabilities/discord.py`
  - Discord no-response and notification-adjacent model-facing capabilities

- `src/ariel/capabilities/test_fixtures.py`
  - test-only framework capabilities
  - never imported into production registry construction

- `src/ariel/tool_surface.py`
  - `ToolSurfaceContext`
  - deterministic eligibility filtering
  - strategy judgment contract
  - selected tool definition builder
  - exposure tests and counters

- `src/ariel/action_runtime.py`
  - proposal intake
  - action attempt persistence
  - policy calls
  - approval lifecycle
  - generic execution orchestration
  - no provider-specific branching beyond runtime interface lookup

- `src/ariel/action_executors.py`
  - execution runtime protocol
  - provider runtime dispatch
  - read/write execution receipts

- `src/ariel/agency_daemon.py`
  - daemon client
  - Agency runtime execution
  - outbox/receipt handling
  - sandbox policy persistence

- `src/ariel/proactivity.py`
  - ambient interpretation
  - case deliberation
  - case-scoped tool surface
  - action validation
  - feedback learning
  - no duplicated action/memory contracts

- `src/ariel/memory.py`
  - evidence lifecycle
  - candidate pool construction
  - AI curation
  - procedure promotion
  - transport-order naming

- `src/ariel/app.py`
  - FastAPI composition and routes only
  - turn execution orchestration moves out to a runtime module
  - authority-bearing routes use shared local auth dependency

- `src/ariel/discord_bot.py`
  - Discord presentation only
  - user-facing labels for actions
  - no internal capability IDs in normal messages

The package split is part of the hard cutover. Update [codebase.md](codebase.md)
when the new package exists.

## Capability Rules

Every capability must declare:

- capability ID
- owner module
- impact level
- policy decision
- input schema
- output schema
- idempotency model
- admission reason
- why a skill or terminal workflow is insufficient
- model exposure class: `runtime`, `proactive`, `internal`, or `test_only`
- allowed surfaces
- side effects
- approval requirement
- retry safety
- audit fields
- common failure modes

Capabilities are not model tools by default.

Production response tool generation excludes:

- `test_only`
- `internal`
- provider-unbound capabilities
- capabilities blocked by current policy
- capabilities whose required source artifact is absent
- proactive-disallowed writes
- Agency capabilities when no Agency repo root/runtime is configured

Test fixtures do not live in the production registry. Tests import their fixture
registry explicitly.

## Security Rules

Local API:

- Authority-bearing routes require authentication.
- Loopback bind is defense in depth only.
- Discord-origin actions use signed controls or a server-side state binding.
- Provider callbacks keep provider-specific verification.
- Test clients use explicit test auth helpers.

Agency:

- `cap.agency.run` requires approval.
- Agency records sandbox policy version, filesystem scope, egress policy,
  runner identity, environment redaction state, and resource limits.
- Env values are denylisted/redacted before persistence.
- Destructive or networked Agency actions are controlled by daemon policy.
- PR landing and sync use an outbox/receipt pattern.
- Retries reconcile by idempotency key or daemon request ID.

Audit:

- Events that authorize or perform side effects are append-only at the application
  level.
- Side-effect records store request ID, actor, policy decision, input hash,
  contract hash, approval ref, external receipt, and reconciliation status.
- Add tamper-evident event hashing for action attempts, approvals, job events,
  connector events, and proactive policy validations.

Credentials:

- Production startup fails if connector encryption uses dev defaults.
- Credential-bearing routes and daemon runs never expose raw secrets to model
  context.

Attachments:

- Persist content hash, extractor version, prompt version, model response ID,
  source URL, taint classification, and bounded extraction refs.

## Hard-Cutover Decisions

- Delete broad `response_tool_definitions()` use from normal turns.
- Delete production exposure of `cap.framework.*`.
- Delete Google execution stubs that only return `google_runtime_not_bound`.
- Delete deterministic contradiction/source-count tool-result routing as semantic
  judgment. Keep only budget, taint, modality, and explicit AI-requested
  interpretation triggers.
- Delete bespoke proactive action types that duplicate capability execution.
- Delete duplicate proactive `remember` shapes.
- Delete user-facing capability IDs from Discord copy.
- Delete unauthenticated authority-bearing local route behavior.
- Delete stale cutover docs and replace them with this document plus narrow module
  docs.
- Rename or document memory `retrieval_rank` as transport order; do not preserve
  deterministic relevance semantics.
- Do not keep feature flags that restore old broad-catalog behavior.

## Implementation Plan

The sequence below is for engineering order only. It does not authorize merged
legacy runtime behavior.

### Phase 1: Lock The Contract

Files:

- `docs/north-star-cutover.md`
- `docs/index.md`
- `tests/unit/test_agent_tooling_policy.py`
- `tests/unit/test_tool_surface_cutover.py`
- `tests/integration/test_proactive_tool_surface_cutover.py`
- `tests/integration/test_api_auth_cutover.py`
- `tests/integration/test_agency_security_cutover.py`

Work:

- Add static policy tests for capability admission metadata.
- Add tests that normal turns never receive the full registry.
- Add tests that test-only tools are absent from production surfaces.
- Add tests that proactive tool sets are case-scoped.
- Add tests that authority-bearing routes reject unauthenticated calls.
- Add tests that Agency task starts persist sandbox policy metadata.

Acceptance:

- The new tests fail on the current code for the intended reasons.
- The tests encode final behavior, not transitional behavior.

### Phase 2: Split Capability Definition From Presentation

Files:

- `src/ariel/capability_registry.py`
- `src/ariel/capabilities/core.py`
- `src/ariel/capabilities/google.py`
- `src/ariel/capabilities/retrieval.py`
- `src/ariel/capabilities/attachments.py`
- `src/ariel/capabilities/agency.py`
- `src/ariel/capabilities/discord.py`
- `src/ariel/capabilities/test_fixtures.py`
- `src/ariel/tool_surface.py`
- `tests/unit/test_responses_tool_contract.py`
- `tests/unit/test_email_decluttering_cutover.py`
- `tests/unit/test_capability_registry_search.py`

Work:

- Move capability families into owner modules.
- Add admission metadata.
- Build an internal production registry without test fixtures.
- Build an explicit test fixture registry for framework capabilities.
- Replace `response_tool_definitions()` with selected-ID builders.
- Add context-aware eligibility filters.

Acceptance:

- Production registry contains no framework capabilities.
- Capability definitions cannot be model-exposed without admission metadata.
- No code path can accidentally export every capability as a model tool.

### Phase 3: Tool Strategy And Turn Runtime

Files:

- `src/ariel/app.py`
- `src/ariel/tool_surface.py`
- `src/ariel/action_runtime.py`
- `src/ariel/response_contracts.py`
- `tests/unit/test_responses_tool_contract.py`
- `tests/integration/test_pr01_acceptance.py`
- session and turn integration tests

Work:

- Move turn execution out of nested `create_app` scope.
- Add audited tool strategy model call.
- Pass only selected tools to the answer model.
- Fail closed on invalid strategy output.
- Preserve AI ownership of final response and tool-result interpretation.
- Remove broad catalog calls from normal turns.

Acceptance:

- A no-tool conversational turn receives zero function tools.
- A coding turn receives Agency tools only when Agency is configured and selected.
- An attachment turn receives attachment read only when attachment refs exist.
- Email/calendar/Drive tools appear only in selected, provider-bound contexts.
- Invalid strategy output produces a typed auditable failure, not fallback tool
  exposure.

### Phase 4: Proactivity Cutover

Files:

- `src/ariel/proactivity.py`
- `src/ariel/tool_surface.py`
- `src/ariel/policy_engine.py`
- `tests/integration/test_proactive_ambient_sources.py`
- `tests/integration/test_proactive_runtime_completion.py`
- `tests/integration/test_proactive_api_controls.py`

Work:

- Replace read-all tool loading with case-scoped selection.
- Remove duplicate memory/action model contract shapes.
- Route all proactive side effects through capabilities and policy.
- Keep autonomy scope as the write boundary.
- Add provider-aware read execution for proactive reads or exclude unbound reads.

Acceptance:

- Proactive deliberation never receives all read tools.
- Proactive cases expose only source-relevant read tools.
- Proactive writes cannot bypass capability policy.
- Proactive memory updates use one contract.
- Proactive Discord sends use one action path.

### Phase 5: Agency As Coding Boundary

Files:

- `src/ariel/agency_daemon.py`
- `src/ariel/action_runtime.py`
- `src/ariel/persistence.py`
- `src/ariel/worker.py`
- `alembic/versions/*`
- `tests/integration/test_agency_security_cutover.py`
- worker/job integration tests

Work:

- Persist sandbox policy and egress policy metadata.
- Redact and validate run environment values.
- Add outbox/receipt state before PR land/sync side effects.
- Reconcile PR side effects by idempotency key or daemon request ID.
- Ensure job status/artifact reads are scoped to tracked jobs.

Acceptance:

- Agency task start cannot run without approval and configured repo allowlist.
- Every Agency job has sandbox policy metadata.
- PR land/sync is crash-recoverable and idempotent.
- Agency artifacts are available through job-scoped reads only.

### Phase 6: Local Auth And Audit

Files:

- `src/ariel/app.py`
- `src/ariel/config.py`
- `src/ariel/persistence.py`
- `src/ariel/discord_bot.py`
- `src/ariel/google_connector.py`
- `tests/integration/test_api_auth_cutover.py`
- `tests/integration/test_no_ai_ops_acceptance.py`

Work:

- Add a shared local auth dependency for authority-bearing routes.
- Keep provider callbacks separately verified.
- Add tamper-evident audit hashes for side-effect records.
- Fail startup on production credential defaults.
- Keep deterministic slash commands model-free.

Acceptance:

- Unauthenticated local approval, memory mutation, connector control, autonomy,
  and proactive-control calls fail.
- Authenticated test helpers and Discord controls pass.
- Audit hash chains verify for action and job events.
- Production config rejects dev connector encryption defaults.

### Phase 7: Memory, Procedures, And Evals

Files:

- `src/ariel/memory.py`
- `src/ariel/proactivity.py`
- `docs/modules/memory.md`
- `tests/integration/test_memory_eval_acceptance.py`
- `tests/integration/test_north_star_memory_pass.py`

Work:

- Treat candidate order as transport order in names/docs.
- Promote feedback-derived durable behavior into reviewed procedure candidates.
- Keep autonomy requests separate from procedures.
- Add memory eval cases promised by docs.
- Add no-memory mode coverage.

Acceptance:

- Memory curation remains AI-owned.
- Procedure promotion requires evidence and review state.
- No-memory mode performs no extraction and no recall.
- Evals cover vector-wrong, keyword-wrong, temporal, conflict, abstention,
  correction, deletion, no-memory, proactive feedback, and procedure cases.

### Phase 8: Product Presentation Cleanup

Files:

- `src/ariel/discord_bot.py`
- `src/ariel/response_contracts.py`
- `docs/production-runbook.md`
- Discord unit tests

Work:

- Replace capability IDs in Discord copy with action labels.
- Consolidate proactive inspection endpoints or document one inspection envelope.
- Keep deterministic ops slash commands.
- Update runbook smoke tests to north-star behavior.

Acceptance:

- Discord copy contains no internal capability IDs outside developer diagnostics.
- Job, approval, memory, and capture commands remain deterministic.
- Runbook smoke tests match new auth and tool-surface behavior.

## Acceptance Criteria

The cutover is complete only when all of these are true:

- Normal turns cannot receive the full capability catalog.
- Production model tool surfaces contain no test fixtures.
- Every exposed tool has admission metadata and a current justification.
- Tool strategy is AI-owned and audited.
- Deterministic filtering is limited to hard eligibility rails.
- Proactive tools are case-scoped.
- Coding work routes through Agency, not new granular repo tools.
- Agency runs record sandbox and egress policy metadata.
- Authority-bearing local API routes are authenticated.
- PR landing and external side effects are crash-recoverable.
- Connector credential defaults fail closed in production.
- Tool-result interpretation does not use semantic keyword heuristics.
- Memory candidate order is not represented as deterministic relevance.
- Durable behavioral learning flows into reviewed procedure memory or skills.
- Discord user copy hides internal capability IDs.
- Tests prove the absence of legacy broad-catalog behavior.
- Docs contain no stale cutover links that describe removed paths.

## Non-Goals

- Do not preserve the old broad registry-as-tool-surface behavior.
- Do not add a compatibility mode for old tests.
- Do not keep framework tools in production behind policy denial.
- Do not add new structured tools for coding workflows that Agency can perform.
- Do not build a generic MCP/API catalog for hypothetical future workflows.
- Do not use deterministic keyword classifiers as product judgment.
- Do not let proactive ambient flows gain shell authority.
- Do not rely on prompt instructions as a security boundary.
- Do not treat phases as runtime modes.

## Key Risks

- Tool selection can become hidden deterministic intent routing. Mitigation:
  strategy is AI-owned; deterministic code filters only hard eligibility facts.
- Agency can become too broad. Mitigation: sandbox, egress, approval, transcript,
  and outbox receipts are required before terminal-first is safe.
- Tests can preserve legacy shape accidentally. Mitigation: write failing
  cutover tests first and remove fixture capabilities from production imports.
- Product copy can leak implementation. Mitigation: user-facing action-label
  registry is separate from capability IDs.
- Memory procedures can become unauthorized autonomy. Mitigation: procedures
  store behavior preferences; autonomy scopes remain explicit and approved.

## Source Findings

This spec is based on the May 2026 code survey of:

- `src/ariel/app.py`
- `src/ariel/capability_registry.py`
- `src/ariel/action_runtime.py`
- `src/ariel/proactivity.py`
- `src/ariel/memory.py`
- `src/ariel/agency_daemon.py`
- `src/ariel/discord_bot.py`
- `src/ariel/executor.py`
- `src/ariel/policy_engine.py`
- `src/ariel/config.py`
- current unit and integration tests

The most important current-state defects are:

- normal turns load every response tool
- proactive deliberation loads every read tool
- test-only framework capabilities are in the production tool catalog
- Agency is the right terminal-first boundary but needs stronger sandbox and
  side-effect receipts
- authority-bearing local routes rely too heavily on loopback
- action runtime and capability registry are monolithic
- Discord copy leaks internal capability IDs
- tests do not yet enforce tool minimalism or skills-before-tools
