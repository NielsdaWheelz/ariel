# Single Run Tool Cutover

## Scope

This document owns Ariel's hard cutover from task-scoped Responses function
tools to one direct model-facing execution tool.

It narrows the agent tooling direction in [agent-tooling.md](agent-tooling.md)
and the broad product cutover in [north-star-cutover.md](north-star-cutover.md).
Where this document conflicts with older selected-tool language in
`north-star-cutover.md`, this document wins for normal user turns.

The cutover is incompatible with the previous answer-pass surface that exposed
selected capability IDs as Responses tools. There is no compatibility layer, no
legacy mode, no fallback path, and no feature flag that restores per-capability
model tools. Work may be sequenced across commits, but the merged final state has
one direct model tool.

## Thesis

The model should see one direct tool: `run`.

`run` is a tiny execution protocol. It is not a broad shell escape and it is not
a generic API mirror. It accepts a small source program in Ariel's chosen control
language and executes that program inside a governed runtime. The runtime exposes
internal callable operations for terminal work, skills, memory, provider actions,
and user-visible output.

The important boundary is model-facing, not implementation-facing:

- model-facing surface: one `run` tool
- runtime callable surface: small typed functions exposed inside `run`
- internal authority catalog: capability definitions, policy, approvals, audit,
  receipts, workers, and provider runtimes

This copies the architectural shape of `codapt2`, not its TypeScript, Effect,
Lua, executor, workspace-machine, billing, or persistence implementation.

## Goals

- Reduce the normal-turn model-facing tool surface to exactly one tool.
- Make terminal execution the ordinary work path.
- Keep side effects behind policy, approval, idempotency, audit, and receipts.
- Preserve `action_attempts` or an equivalent durable action ledger.
- Keep Ariel Python-native until a separate sandbox runtime earns its cost.
- Make skills and procedural memory the default home for repeated workflows.
- Remove broad Responses tool generation from normal turns.
- Make invalid model protocol behavior observable and recoverable through
  feedback input, not hidden fallbacks.

## Non-Goals

- Do not copy `codapt2` implementation verbatim.
- Do not introduce TypeScript, Effect, Lua, executor RPC, workspace machines, or
  billing concepts into Ariel for this cutover.
- Do not build Python-in-Firecracker execution in this cutover.
- Do not delete the action ledger before an equivalent replacement exists.
- Do not expose Google, email, calendar, memory, web, Agency, attachment, or
  Discord operations as separate answer-pass model tools.
- Do not keep the existing strategy-pass plus selected-Responses-tools flow.
- Do not add compatibility tests that preserve the old tool surface.
- Do not rely on prompt text as the security boundary.

## Target Behavior

### Normal Turn

1. Ariel ingests a terminal, Discord, or API user message.
2. Ariel builds bounded context, memory context, open work, artifacts, and hard
   runtime facts.
3. Ariel calls the answer model with exactly one direct tool definition: `run`.
4. The model either calls `run` exactly once or receives protocol feedback.
5. The `run` program invokes internal callable operations as needed.
6. Internal operations create action attempts, approval requests, receipts,
   artifacts, memory events, and terminal command records.
7. User-visible output is emitted through an explicit runtime function, not
   plain assistant text.
8. The turn ends only after the runtime emits output, requests approval, or
   pauses for new input.

Plain assistant text is not a user-facing output channel in the final protocol.
If the model returns only text, Ariel records the response and feeds back that
the user did not see it.

### Tool Protocol

The model-call protocol has one allowed direct tool:

```text
run({ "source": "..." })
```

Rules:

- Every answer-pass model response must call exactly one direct tool.
- Multiple direct tool calls are a model protocol defect.
- No direct tool call is retryable protocol failure.
- Unsupported tool names are retryable protocol failure.
- Invalid `run` input is retryable protocol failure.
- Do not set provider `tool_choice` to required if it degrades reasoning.
- Enforce the protocol with validation, stored model-response feedback, and a
  subsequent model call.
- Persist model responses that violate the protocol before retry feedback.

### Run Source

The first cutover uses JSON only:

```json
{"calls":[{"name":"agent.emit_message","input":{"text":"..."}}]}
```

Rules:

- the source is a JSON object with exactly one `calls` key
- `calls` is a non-empty list of at most 20 call objects
- each call object has exactly `name` and `input`
- `name` is a run-callable name such as `terminal.run` or `memory.search`, never
  a `cap.*` capability id
- `input` is the callable's typed input object
- invalid sources execute no effects
- no secret material belongs in source or callable docs

Until the Firecracker direction is implemented, the run runtime executes host
functions in-process and delegates unsafe work to governed external runtimes such
as Agency or provider connectors.

### Terminal Work

Terminal work is the default for bounded in-turn inspection, local documentation
lookup, verification, tests, and command-line workflows. Durable coding tasks,
long-running implementation work, and PR ownership route through `agency.*`.

The first terminal callable operation is:

```json
{"calls":[{"name":"terminal.run","input":{"cwd":"...","command":"...","purpose":"..."}}]}
```

Rules:

- `cwd` is required.
- Commands run in a clean non-login shell.
- Shell input rejects NUL bytes.
- Command output is bounded and persisted.
- Exit code, stdout, stderr, working directory, duration, and purpose are
  recorded.
- Nonzero exits and foreground timeouts are command results with exit codes, not
  host-function transport failures.
- Long-running processes use a separate background operation.
- Destructive, networked, production, or cross-repo commands require policy and
  approval.
- The terminal runtime must not read `.env`, credential stores, or configured
  denylisted paths.

Long-term, `terminal.run` moves behind Python running inside ephemeral
Firecracker microVMs. That is an explicit future architecture, not part of this
cutover.

### User Output

User-visible output is emitted inside `run` through an internal function:

```json
{"calls":[{"name":"agent.emit_message","input":{"text":"..."}}]}
```

The runtime may later support richer structured blocks, but the first cutover
uses text unless an existing surface contract requires structured output.

Rules:

- Plain assistant text is stored for audit only.
- `agent.emit_message` is the normal user output path.
- Approval-required actions may surface host-authored approval text because the
  user must review the pending action before execution.
- A run that emits user-visible text must not also call internal operations.
  Internal operations return results to the model first; a later run emits the
  final user-visible text.
- `agent.pause_until_input` ends the turn without visible output.
- `agent.emit_value` stores internal structured data for later model context.
- Deferred output is applied only if the turn remains caught up with input
  events.

### Approvals

Approval-required operations do not execute inside the model turn.

Rules:

- The host function creates an action attempt and approval request.
- The runtime returns an approval-required result to the run program.
- The user-facing surface shows the pending action in plain language.
- Approval resolution revalidates payload hash, policy, actor, expiry, and
  contract hash.
- Approved side effects run through the worker/action runtime and write receipts.

### Proactivity

Proactive deliberation keeps no direct model tools.

The single `run` tool is for normal user turns only until a proactive case earns
a source-scoped read operation with a written justification. Proactivity never
receives terminal authority.

## Final Architecture

### Model Call Flow

Normal user turn:

1. Append user input event.
2. Build prompt sections from active session, memory, work, artifacts, and
   runtime facts.
3. Package prior transcript and any model-response feedback.
4. Call model with exactly one direct tool definition, `run`.
5. Persist provider response.
6. Validate exactly one `run` call.
7. On invalid protocol, append model-response feedback and repeat within the
   model-call budget.
8. Execute `run`.
9. Apply emitted output, action attempts, approvals, artifacts, and pause state.
10. Queue memory/procedure extraction as separate audited AI judgments.

### Run Runtime Flow

`run` execution:

1. Parse and validate source.
2. Build host-function table from allowed internal callable operations.
3. Execute source within compute, output, and host-call budgets.
4. Each host call validates input at the boundary.
5. Each side-effecting host call goes through policy, approval, idempotency, and
   audit before execution.
6. Return run effects: emitted messages, emitted values, action attempts, approval
   refs, artifacts, pause request, and runtime errors.
7. Persist all effects before user-facing projection.

### Internal Callable Families

The initial callable families are:

- `agent.*`: emit message, emit value, pause
- `terminal.*`: run foreground command, start background command, inspect
  background command, read bounded output, cancel background command
- `memory.*`: inspect/search/propose/review/correct with existing policy rails
- `agency.*`: start coding job, inspect job, inspect artifacts, request PR
- `email.*`, `calendar.*`, `drive.*`: Google Workspace reads/writes through the
  Google runtime
- `attachment.*`: read bounded attachment content
- `search.*`, `web.extract`: search/extract only when server-side credentials,
  SSRF rails, and artifact capture justify a structured operation
- `maps.*`, `weather.forecast`: provider-backed local information calls when
  configured

Callable families are internal. They are not direct model tools.

### Module Ownership

Keep the flat module rule from [codebase.md](codebase.md).

Expected ownership:

- `src/ariel/capability_registry.py`: internal callable/capability contracts,
  callable names, schemas, documentation, and contract hashes.
- `src/ariel/app.py`: model-call orchestration, protocol validation, feedback,
  prompt packaging, and surface response construction.
- `src/ariel/action_runtime.py`: action attempts, approvals, provider execution,
  receipts, and worker execution.
- `src/ariel/run_runtime.py`: source parsing, host-function dispatch, runtime
  budgets, run effects, and protocol-level runtime errors.
- `src/ariel/terminal_runtime.py`: clean shell execution, background process
  handles, command logs, deny-read/egress policy hooks, and output bounding.
- `src/ariel/agency_daemon.py`: Agency daemon host functions and PR receipts.
- `src/ariel/memory.py`: memory host functions and memory AI judgments.
- `src/ariel/proactivity.py`: no-tool proactive deliberation and action planning.
- `src/ariel/discord_bot.py`: Discord projection and approval controls.
- `src/ariel/response_contracts.py`: surface response validation and action
  lifecycle projection.

Do not add packages or routing layers unless a concrete module becomes too large
to reason about.

## Key Decisions

### Copy Shape, Not Code

Copy these decisions from `codapt2`:

- one direct model tool
- direct tool executes a small control program
- internal callable functions are typed and documented
- terminal command execution is an internal callable function
- user output is emitted through a runtime function
- invalid model protocol produces feedback and another model call

Do not copy:

- Effect durable operation framework
- Lua runtime
- workspace-machine executor system
- billing and credit admission
- TypeScript capability loader
- Codapt-specific IDs or persistence

### Preserve The Action Ledger

`action_attempts` remains the action ledger unless replaced by an equivalent
ledger in the same cutover.

The ledger must preserve:

- session and turn linkage
- proposal index or equivalent ordering
- capability/callable name
- version and contract hash
- normalized input and payload hash
- policy decision and reason
- approval requirement and approval ref
- execution status, output, and error
- event linkage and artifacts
- provider-write receipts and reconciliation state

### Hide Capabilities From The Model

Capabilities are internal callable operations, not direct Responses tools.

`response_tool_definitions()` must not be used for normal answer turns after the
cutover. Tests may retain helpers for migration only if they assert that normal
turns cannot expose per-capability tools.

### Terminal Before Structured Provider Tools

If a workflow can be done transparently and safely through terminal commands,
local scripts, CLIs, or skills, do not add a new internal callable operation.

Add an internal callable operation only when it needs:

- credentials not available to terminal safely
- typed approval or side-effect receipts
- provider-specific idempotency
- SSRF, taint, or provenance rails
- bounded attachment/provider extraction
- durable domain audit records

### Firecracker Later

The desired long-term execution substrate is Python inside ephemeral Firecracker
microVMs.

This cutover must not fake that architecture. It creates the protocol and host
function boundary that can later move from in-process execution to Firecracker
without changing the model-facing tool contract.

## Implementation Plan

### Phase 1: Failing Contract Tests

Add tests that fail against current behavior:

- normal answer-pass model calls receive exactly one tool named `run`
- normal turns never call per-capability Responses tool generation
- model responses with no tool call append protocol feedback
- model responses with multiple tool calls defect or feedback according to the
  final protocol decision
- unsupported tool names append feedback and do not execute
- plain assistant text is not user-visible
- `agent.emit_message` is user-visible
- unadvertised per-capability function calls are denied and audited

These tests replace old assertions that production model tools include memory,
email, search, attachment, or Agency capability schemas.

### Phase 2: Run Tool Skeleton

Implement:

- strict `run` tool schema
- model response protocol validator
- model-response feedback input event
- run effect type
- minimal source parser
- `agent.emit_message`
- `agent.pause_until_input`
- `agent.emit_value`

At this phase, no provider or terminal host function is required.

### Phase 3: Terminal Host Functions

Implement:

- `terminal.run`
- `terminal.run_background`
- background status/read-output function
- command output artifact records
- clean shell invocation
- deny-read and egress policy hooks
- command tests for cwd, exit code, stdout/stderr, timeout, output truncation,
  and denied paths

### Phase 4: Capability Host Functions

Move existing runtime capability execution behind host functions:

- Agency run/status/artifacts/request PR
- attachment read
- memory inspect/search/mutation operations
- Google reads/writes
- selected web read/search
- Discord no-response as `agent.pause_until_input` or a surface-specific host
  function

Keep policy, approval, receipts, private payload sealing, and worker execution in
`action_runtime.py`.

### Phase 5: Delete Old Tool Surface

Delete or retire:

- normal-turn selected capability tool generation
- strategy-pass selected-tool orchestration
- per-capability answer-pass Responses tools
- tests that require production model exposure of individual capabilities
- stale docs that say normal answer turns receive selected capability tools

Keep:

- internal capability definitions
- input/output schemas
- policy evaluation
- action attempts
- approvals
- receipts
- worker execution
- no-tool proactive deliberation

### Phase 6: Surface And Docs

Update:

- `docs/north-star-cutover.md`
- `docs/agent-tooling.md` if terminology needs clarification
- `docs/production-runbook.md`
- `.env.example` for any terminal runtime settings
- tests and runbooks for terminal smoke flows

## Acceptance Criteria

The cutover is complete only when all are true:

- Normal answer-pass model calls expose exactly one direct tool: `run`.
- No normal turn exposes capability IDs as direct Responses tools.
- The old selected-tool strategy pass is gone from normal turns.
- Invalid model protocol is persisted and fed back to the model.
- Plain assistant text is not user-visible output.
- User-visible output goes through `agent.emit_message`.
- Terminal foreground and background host functions exist and are audited.
- Terminal commands record cwd, command, purpose, exit code, stdout/stderr refs,
  duration, and policy decision.
- Destructive/networked/production/cross-repo commands require approval.
- Existing provider writes still require approval and produce receipts.
- Google, memory, attachment, Agency, and web operations are internal host
  functions or removed with explicit tests.
- Proactive deliberation still receives no direct tools.
- `action_attempts` or an equivalent ledger remains the authority/audit spine.
- Tests prove old per-capability model tool exposure is absent.
- Tests prove runtime capability execution still preserves policy, approval,
  idempotency, redaction, receipts, and audit.
- Docs contain no normal-turn instructions that preserve the old selected
  Responses tool surface.
- `make verify` passes.

## Key Risks

- A single `run` tool can become an unaudited shell escape. Mitigation: small
  source language, typed host functions, command policy, deny-read rules, output
  bounds, and persisted command records.
- Deleting per-capability model tools can accidentally delete provider rails.
  Mitigation: move capability execution behind host functions; do not delete the
  action ledger.
- Protocol feedback loops can hide model failures. Mitigation: persist every
  invalid model response and cap retries.
- Terminal-first can overexpose secrets. Mitigation: clean environment, deny-read
  paths, redaction, scoped credentials, and future Firecracker isolation.
- Tests can preserve legacy behavior. Mitigation: replace model-visible tool
  tests with internal-runtime and absent-model-surface tests.

## Source Findings

This spec is based on a read-only survey of:

- `docs/agent-tooling.md`
- `docs/north-star-cutover.md`
- `docs/ai-first.md`
- `docs/simplicity.md`
- `src/ariel/app.py`
- `src/ariel/capability_registry.py`
- `src/ariel/action_runtime.py`
- `src/ariel/executor.py`
- `src/ariel/agency_daemon.py`
- `src/ariel/persistence.py`
- `tests/unit/test_responses_tool_contract.py`
- `tests/unit/test_capability_registry_search.py`
- `tests/integration/test_google_workspace_follow_up_acceptance.py`
- `tests/integration/test_email_decluttering_action_runtime.py`
- `/home/niels/src/work/codapt2/docs/modules/main-agent.md`
- `/home/niels/src/work/codapt2/src/main/server/internal/agent/tools/main.ts`
- `/home/niels/src/work/codapt2/src/main/server/internal/agent/tools/run.ts`
- `/home/niels/src/work/codapt2/src/main/server/internal/agent/capabilities/registry.ts`
