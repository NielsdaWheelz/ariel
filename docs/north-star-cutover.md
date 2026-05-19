# North-Star Cutover

## Scope

This document owns the hard cutover plan for Ariel's new product and
architecture north-star.

It converts the repository-wide rules in [ai-first.md](ai-first.md) into an
implementation spec for this codebase. The `run` execution model is narrowed by
[run-program-cutover.md](run-program-cutover.md).

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
internal authority catalog. The `run` prompt/runtime exposes only eligible
internal callable authority for the current turn.

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
- reply to a message Ariel sent on its own initiative

Discord never exposes internal capability IDs such as `cap.email.send` in normal
copy. Capability IDs remain visible only in API payloads, logs, audit records,
and developer diagnostics.

The HTTP API is an operator and integration surface, not a second public product
surface. Authority-bearing routes require local authentication. Loopback binding
is not an authentication boundary.

### Agent Behavior

For ordinary turns, Ariel calls the answer model with exactly one direct tool:
`run`.

The model does not receive selected capability IDs as Responses tools. It writes
a `run` program that emits user-visible output or calls internal Ariel
operations. Internal capability calls still pass through policy, approval,
idempotency, audit, and receipts before execution.

Deterministic code may filter eligibility by hard facts: connector availability,
attachment presence, policy, runtime binding, source surface, trigger kind,
trust boundary, and environment configuration. Deterministic code must not
perform semantic intent classification to choose tools or direct work.

### Coding Work

Coding and repository work routes through Agency. Ariel has no terminal;
implementation jobs, PR ownership, and repository inspection all route through
`agency.*`.

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
- email send, archive, trash, label mutation, undo, and draft
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

Proactivity is the main agent loop reached by a non-human trigger, not a
separate cognition path. A proactive wake is a normal turn: it receives the
same single `run` tool and the same memory as a user message.

Rules:

- Every trigger — a user message, a provider push, a poll result, a due
  scheduled task — invokes one shared agent-loop entrypoint.
- A proactive wake writes a `run` program like any turn; it does not get a
  bespoke decision contract.
- Proactivity never receives shell-like authority beyond what `run` already
  rails.
- A proactive wake's writes pass the same per-capability `requires_approval`
  policy as a user turn's; there is no separate autonomy-scope check.
- A proactive wake delivers through `agent.emit_message`, the same path a user
  turn uses. There is no duplicate `remember` surface and no bespoke
  `send_discord_message` action path outside the capability system.

See [modules/proactivity.md](modules/proactivity.md).

### Memory And Skills

Memory relevance, extraction, continuity, and procedure selection are AI
judgments. A correction to proactive behavior is an ordinary memory write, not
a feedback-learning subsystem.

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
3. Build the single strict `run` Responses tool.
4. Run answer model.
5. Validate exactly one `run` call and feed back protocol failures.
6. Execute the run source through internal host operations.
7. Process internal capability calls through policy, approval, execution, audit, and result
   interpretation.
8. Produce final Discord/API response.
9. Extract memory/procedural candidates as separate audited AI judgments.

Proactive wake:

1. A non-human trigger fires — a provider push, a poll result, or a due
   scheduled task — and enqueues a wake.
2. The worker dispatches the `agent_wake` row to the shared agent-loop
   entrypoint.
3. The entrypoint runs the normal turn: bounded context, the single `run`
   tool, the answer model, program execution.
4. Internal capability calls pass through policy, approval, execution, and
   audit, exactly as on a user turn.
5. The wake emits through `agent.emit_message`, or ends without emitting.

Agency coding job:

1. User asks for repo/coding work.
2. The answer model calls `run`; the internal callable starts an Agency task or
   asks for missing approval/context.
3. Policy requires approval for task start.
4. Agency daemon starts sandboxed work.
5. Worker syncs status, artifacts, timeline, verification, and sandbox receipts.
6. Discord presents job state and review actions.
7. PR request requires approval and uses crash-safe side-effect receipts.

### Module Structure

The final codebase stays flat unless a split removes real complexity. See
[codebase.md](codebase.md): no sub-packages unless unavoidable.

Current module ownership:

- `src/ariel/capability_registry.py`: internal capability contracts, schemas, and
  callable metadata.
- `src/ariel/app.py`: FastAPI composition, local auth, and the `_wake`
  agent-loop entrypoint that serves every trigger.
- `src/ariel/action_runtime.py`: proposal intake, policy, approval lifecycle,
  execution orchestration, and side-effect receipts.
- `src/ariel/agency_daemon.py`: Agency daemon client, sandbox policy persistence,
  and PR request handling.
- `src/ariel/worker.py`: the single-threaded `background_tasks` worker —
  scheduled-wake dispatch, provider push/poll ingestion, and shared background
  tasks.
- `src/ariel/memory.py`: the fact store, the profile and digest documents, and
  the retriever and rememberer subagents.
- `src/ariel/discord_bot.py`: Discord presentation and deterministic operator
  commands.

Do not add `tool_surface.py`, capability sub-packages, executor wrappers, or test
fixture registries until the existing modules have a concrete complexity problem
that a split will reduce.

## Capability Rules

Every capability must declare:

- capability ID
- impact level
- policy decision
- input schema
- output schema
- idempotency model
- why a skill or terminal workflow is insufficient
- allowed surfaces
- side effects
- approval requirement
- retry safety
- audit fields
- common failure modes

Capabilities are internal callables, not normal-turn model tools.

Normal answer turns expose only `run`. Internal callable eligibility excludes:

- `test_only`
- `internal`
- provider-unbound capabilities
- capabilities blocked by current policy
- capabilities whose required source artifact is absent
- Agency capabilities when no Agency repo root/runtime is configured

Test-only capabilities must never be callable from `run`.

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
- Add hash chains only when the audit store crosses an untrusted persistence
  boundary. Until then, keep side-effect events append-only and receipt-backed.

Credentials:

- Production startup fails if local auth is disabled, the local auth token is weak,
  connector encryption uses dev defaults, the connector keyring is absent, or the
  active connector key version is missing from the keyring.
- Credential-bearing routes and daemon runs never expose raw secrets to model
  context.

Attachments:

- Persist content hash, extractor version, prompt version, model response ID,
  source URL, taint classification, and bounded extraction refs.

## Hard-Cutover Decisions

- Delete broad `response_tool_definitions()` use from normal turns.
- Delete `cap.framework.*` from the production capability registry, not just from
  model-tool exposure.
- Require exactly one direct `run` call from the answer model.
- Build capability eligibility from durable runtime facts: connected providers,
  granted scopes, attachments present on the current turn, configured provider
  backends, and Agency repo allowlists. Do not infer authority from compacted
  model-owned context.
- Default-deny internal run calls outside the eligible turn capability
  set and emit `evt.action.call_denied`.
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

## Implementation Checklist

The cutover stays in the existing flat modules. Do not create package splits or
routing layers to make this checklist look tidy.

- Normal turns expose exactly one direct model tool, `run`.
- The answer model must call `run` exactly once.
- Plain assistant text is protocol feedback only, not user-visible output.
- Runtime execution denies unadvertised internal function calls before action attempts exist.
- A proactive wake runs the normal `run` turn; it has no separate tool surface
  or decision contract.
- Google capabilities execute only through the Google runtime, never local stubs.
- Agency PR land/sync uses durable provider-write receipts and daemon
  idempotency request IDs.
- Local authority routes require bearer auth outside provider-owned callbacks.
- Discord copy uses user-facing action labels.
- Tests cover absence of broad model tool exposure, local auth, Agency policy
  metadata, the unified proactive wake path, and strict run protocol validation.

## Acceptance Criteria

The cutover is complete only when all of these are true:

- Normal turns cannot receive the full capability catalog or per-tool strategy
  descriptions.
- The normal-turn model tool surface contains no test fixtures.
- Every internal callable has a current justification.
- Normal turns expose exactly one direct model tool, `run`.
- The selected-tool strategy pass is absent from normal turns.
- User-visible output goes through `agent.emit_message`.
- Deterministic filtering is limited to hard eligibility rails.
- A proactive wake reaches the same `_wake` entrypoint and `run` tool as a user
  turn.
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
- Do not expose framework tools through production model tool surfaces.
- Do not add new structured tools for coding workflows that Agency can perform.
- Do not build a generic MCP/API catalog for hypothetical future workflows.
- Do not use deterministic keyword classifiers as product judgment.
- Do not give a proactive wake authority beyond what the `run` sandbox rails
  for a user turn.
- Do not rely on prompt instructions as a security boundary.
- Do not treat phases as runtime modes.

## Key Risks

- The single `run` tool can become an unaudited execution surface. Mitigation:
  the program runs in a gVisor sandbox, every effect is a typed syscall through
  policy and approval, output is bounded, and action attempts are durable. See
  [run-program-cutover.md](run-program-cutover.md).
- Agency can become too broad. Mitigation: sandbox, egress, approval, transcript,
  and outbox receipts are required before terminal-first is safe.
- Tests can preserve legacy shape accidentally. Mitigation: write failing
  cutover tests first and remove fixture capabilities from production imports.
- Product copy can leak implementation. Mitigation: user-facing action-label
  registry is separate from capability IDs.
- A proactive wake can act on its own initiative on tainted input. Mitigation:
  every high-impact, irreversible, or externally-visible capability is
  `requires_approval`, so an autonomous action the user has not seen cannot run
  irreversibly. See [modules/proactivity.md](modules/proactivity.md).

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

The implemented cutover must stay guarded against regressions in these areas:

- normal turns receiving the full response tool catalog
- a proactive wake gaining a tool surface separate from the normal `run` turn
- test-only framework capabilities leaking into production model tool surfaces
- Agency PR requests losing receipt-derived idempotency IDs
- authority-bearing local routes bypassing bearer auth
- Discord copy exposing internal capability IDs
- a proactive wake reintroducing a duplicate action or `remember` shape outside
  the capability system
