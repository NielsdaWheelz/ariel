# Run Program Cutover

## Scope

This document owns Ariel's hard cutover from the flat-JSON `run` call list
executed in-process to a sandboxed Python program executed in a gVisor sandbox,
with capabilities exposed as typed syscall host functions.

It supersedes `single-run-tool-cutover.md`, which it deletes. It narrows
[north-star-cutover.md](north-star-cutover.md): where this document conflicts
with `run`-source or terminal language in `north-star-cutover.md`, this
document wins.

The cutover is incompatible with the flat-JSON `{"calls":[...]}` source format
and with in-process host-function execution. There is no compatibility layer, no
legacy mode, no fallback path, and no feature flag that restores the flat call
list or in-process execution. Work may be sequenced across commits, but the
merged final state runs every `run` program in the sandbox.

## Thesis

The model still sees one direct tool: `run`. What changes is the source.

`run` source is a Python program — with variables, conditionals, loops, and data
flow between calls — executed inside a gVisor (`runsc`) sandbox. Every effect the
program can cause is a typed syscall to a host function. The host runs each
syscall through Ariel's existing rails — schema validation, policy, taint,
approval, egress preflight, audit — before any side effect.

The boundary stays model-facing, not implementation-facing:

- model-facing surface: one `run` tool whose source is a Python program
- sandbox boundary: the program runs in gVisor with no network and a read-only
  rootfs; its only channel to the host is the typed syscall protocol
- host authority: capability definitions, policy, approvals, taint, egress,
  audit, receipts, workers, and provider runtimes — unchanged

This copies the programming model of `codapt2` — a sandboxed snippet that calls
typed host-function syscalls — adapted to Python and gVisor. It does not copy
codapt2's TypeScript, Effect, Lua runtime, durable-operation journaling,
workspace-machine executor, or billing.

## Goals

- Replace the flat list of at most 20 calls with a Python program: control flow,
  variables, and data flow from one syscall's result into another's input.
- Execute every `run` program inside a persistent gVisor sandbox: no network,
  read-only rootfs, bounded CPU, memory, wall-clock, syscall count, and output.
- Keep every capability a typed internal syscall. Keep the policy, approval,
  taint, provenance, egress, and audit rails at the syscall boundary, host-side.
- Preserve `action_attempts` as the durable action ledger and preserve the
  existing approval lifecycle.
- Let a program compose mechanical answers — confirmations, counts, formatted
  data — from inline syscall results within one turn.
- Close the same-turn taint gap: a syscall proposed after an untrusted read in
  the same program is evaluated with that taint.
- Treat the model-facing syscall API and the program-authoring prompt as a
  first-class design artifact.
- Remove the terminal subsystem. Agency remains the only path to code execution.
- Make invalid `run` programs observable and recoverable through model feedback,
  not hidden fallbacks.

## Non-Goals

- Do not keep the flat-JSON `{"calls":[...]}` source format.
- Do not keep any in-process host-function execution path. The program always
  runs in the sandbox.
- Do not suspend a program mid-execution to wait for human approval. Approval
  stays a turn boundary.
- Do not implement durable syscall journaling or program replay. A program runs
  once within a turn.
- Do not adopt Firecracker. The production host exposes no KVM. Do not adopt
  codapt2's TypeScript, Effect, or Lua.
- Do not give the program network access or a writable root filesystem.
- Do not expose capabilities as direct model tools. The model tool surface stays
  exactly `run`.
- Do not give a proactive wake a tool surface other than `run`. A proactive
  wake runs the same `run` program as a user turn.
- Do not build a separate skills system or a skill-loading syscall. Repeated
  workflows live in procedural memory.
- Do not add capabilities, syscalls, or program features speculatively.

## Current State To Replace

- `src/ariel/run_runtime.py` — `unpack_run_source()` parses `source` as JSON
  `{"calls":[...]}`, at most 20 calls, each `{name, input}`. There is no control
  flow, no variable binding, and no call can consume another call's output.
- `src/ariel/executor.py`, `src/ariel/action_runtime.py` — host functions
  execute in-process: `capability.execute(normalized_input)` runs in the Ariel
  host process. There is no isolation boundary between model-chosen work and the
  host.
- `src/ariel/terminal_runtime.py`, `src/ariel/terminal_safety.py` — the terminal
  subsystem: `terminal.run`, `terminal.run_background`, `terminal.status`,
  `terminal.read_output`, `terminal.cancel`, the `policy_engine.py` terminal
  block, `TerminalCommandRecord`, and the `terminal_commands` table.
- `src/ariel/app.py` — the per-turn taint snapshot is computed once from prior
  turns; a syscall proposed after a same-turn untrusted read is evaluated clean.

These are replacement targets, not compatibility promises.

## Target Behavior

### Normal Turn

1. Ariel ingests a Discord or API user message.
2. Ariel builds bounded context, memory context, open work, artifacts, and
   runtime facts.
3. Ariel calls the answer model with exactly one direct tool definition: `run`.
4. The model calls `run` exactly once with a Python program as `source`, or
   receives protocol feedback.
5. Ariel runs the program as a fresh process inside the persistent gVisor
   sandbox.
6. Each syscall is dispatched to the host, run through the rails, and its result
   returned into the program.
7. The program may branch, loop, bind variables, and pass syscall results into
   later syscalls.
8. Inline syscalls return real results. Approval-gated syscalls stage a proposal
   and return a typed pending value.
9. On clean completion, the program's emitted output, action attempts, and
   staged approval proposals are committed.
10. The turn ends; committed approvals, action attempts, artifacts, and emitted
    output are persisted; approval-required actions are surfaced to the user.

A turn may run several programs: the answer model can run a program, observe its
emitted values, and run another, bounded by the wall-clock budget
(`main_turn_budget_seconds`) and the model-call backstop
(`agent_loop_max_model_calls`). The sandbox boundary is per program execution,
not per turn.

### The `run` Source — a Python Program

`source` is a Python program, not JSON.

- `source` is a single Python program, bounded in size.
- It runs as sandboxed CPython inside gVisor.
- Syscalls are exposed to the program as namespaced callables
  (`email.search(...)`, `memory.search(...)`, `agent.emit_message(...)`). The
  eligible syscalls for the turn are provided to the program and named in the
  prompt.
- The program may use variables, `if`/`elif`/`else`, `for`/`while`,
  comprehensions, local function definitions, exception handling, and the safe
  standard-library compute surface (`json`, `re`, `datetime`, `math`, and
  string, list, and dict operations).
- The program has no network, no filesystem writes outside discarded scratch
  space, and no access to `os`, `sys`, `socket`, or `subprocess`. Module import
  is restricted to the safe compute surface. gVisor, the read-only rootfs, and
  the absent network are the enforced boundary; the restricted interpreter
  environment is defense in depth.
- Each syscall call blocks the program until the host returns. From the
  program's view it is an ordinary function call returning a typed value or
  raising a typed error.
- The program runs under the limits defined in The Sandbox.

### Syscalls and the Host Boundary

A syscall is the only way a program causes an effect or reads state.

- The program calls a syscall function; the call `{name, input}` is marshalled
  as JSON across the sandbox channel to the host.
- The guest worker executes the model's untrusted program. Every message it
  sends the host across the channel is untrusted input. The host-side channel
  reader is an ingress trust boundary: it enforces a maximum message size,
  parses against a strict schema, and narrows to typed values before dispatch.
  See [boundaries.md](boundaries.md).
- The host runs the existing per-call lifecycle for that capability:
  `validate_input`, then `evaluate_proposal` (policy, taint, provenance), then
  preflight and egress intent, then execute, then output guardrails, then the
  `ActionAttemptRecord` and events.
- `allow_inline` syscalls execute immediately; the typed result is marshalled
  back into the program.
- `requires_approval` syscalls do not execute. The host stages a proposal — an
  action attempt and an approval request — and returns a typed
  `approval_required` value carrying the approval ref. Staged proposals are
  committed only when the program completes cleanly; see Program Failure.
- `deny` syscalls return a typed denial.
- Taint accumulates within the program: once an inline read returns
  untrusted-influenced content, every later syscall in the same program is
  evaluated with that taint.
- The ordered syscall trace — each syscall's name, input, result, and rail
  decision — is persisted and is the audit record for the program. The program
  source is recorded, but the executed syscall sequence, not the source, is the
  audit spine.

### Approvals

Approval is a turn boundary.

- A `run` program cannot suspend mid-execution to wait for a human.
- An approval-gated syscall stages a proposal and returns a typed pending value;
  the program treats the action as proposed, not done.
- A program cannot consume the result of an approval-gated syscall in the same
  turn. To act on an approved action's outcome, the model writes a new program
  on a later turn after approval resolves.
- Approval resolution revalidates payload hash, policy, actor, expiry, and
  contract hash, then executes through the worker and action runtime and writes
  receipts.

### Program Failure

A program that does not complete cleanly commits no proposals.

- A program that fails to parse, exceeds a limit, or violates the protocol
  executes no syscalls, commits no effects, and produces model feedback.
- A program that raises an unhandled exception after executing syscalls: inline
  read results already returned to the program are side-effect-free and stand;
  staged approval proposals are discarded; emitted messages and values are
  discarded; the exception is fed back to the model.
- Failure feedback records the program source, the syscall trace up to the
  failure, and the error.

### User Output

- `agent.emit_message` is the user-visible output path. A program may compute
  the message from inline syscall results within the same turn — for
  confirmations, counts, and formatted data. The message can carry static text
  the model authored and mechanical derivations of syscall results; it cannot
  carry a model-authored prose summary of fetched content, because the model
  wrote the program before seeing that content. At most one `agent.emit_message`
  call per program.
- `agent.emit_value` records internal structured data for later model context.
  The model uses it when it needs another turn to read and reason over results
  before answering — the normal path for content the model must interpret. At
  most 10 emitted values per program, each at most 12 KB when JSON-encoded.
- `agent.pause_until_input` ends the turn with no visible output and must be the
  program's only agent-output call when used.
- Plain assistant text outside `agent.emit_message` is stored for audit only and
  is not user-visible.

### The Sandbox

- A single persistent gVisor (`runsc`) sandbox runs for the life of the service.
  Each `run` program executes as a fresh process inside it, with clean
  interpreter state and scratch space discarded when the program completes. The
  sandbox is not created or destroyed per turn.
- The sandbox has no network interface.
- The sandbox root filesystem is read-only and minimal: a CPython runtime and
  its standard library, and the guest worker. The only writable space is
  per-process scratch discarded with the process.
- The only host channel is the syscall protocol over a single pipe or socket.
- Program limits, all enforced by the host:
  - max source size
  - guest compute, bounded by cgroup CPU-time and memory limits on the program
    process; time blocked in host syscalls consumes neither
  - a wall-clock timeout on total program duration, as a backstop
  - cumulative host-call time across all of the program's syscalls
  - max syscalls per program, set for the cost and rate limits of real external
    APIs and not inherited from a local-call sandbox; capabilities may carry
    per-capability sub-limits
  - max emitted output
- gVisor uses the Systrap platform, which needs no KVM and no nested
  virtualization.

### Terminal and Coding Work

- The terminal subsystem is removed. There is no `terminal.*` syscall.
- Durable coding work and any repository execution route through `agency.*`.
  Agency is the only path to code execution.
- The `run` program is for orchestration and personal-assistant work, not for
  running shell commands.

### Procedural Memory

Repeated workflows are not a separate skills system. A procedure is an ordinary
plain-language fact in `memory_facts` — there is no separate procedure memory
type. The retriever surfaces relevant procedures as context and the model
applies them when authoring a `run` program. This cutover adds no skills
runtime, no skills store, and no skill-loading syscall.

### Proactivity

A proactive wake receives the same `run` tool and runs the same agent loop as a
user turn. This is the unified-loop model established by the proactivity
crystallization (see [modules/proactivity-cutover.md](modules/proactivity-cutover.md))
and confirmed by the agent-loop cutover. The earlier statement that "proactive
deliberation receives no `run` program and no tools" is superseded.

## Key Decisions

### Python, Not Lua or Extended JSON

The model authors Python most fluently, it is the repository language, and its
standard library covers in-program compute — formatting, filtering, date math —
without new syscalls. Python cannot be safely contained in-process, which forces
a real OS-level sandbox; see the gVisor decision.

### gVisor, Not Firecracker

Firecracker requires `/dev/kvm`. Hetzner Cloud exposes no nested virtualization
on CPX or CCX instances, and the production host is a CPX11. gVisor (`runsc`)
intercepts guest syscalls in a user-space kernel and needs no KVM. It is
production-proven for untrusted code at E2B, Modal, and GKE Sandbox, and is
lighter than a microVM, which suits the 2 GB host. Firecracker remains a
possible future substrate if the host moves to bare metal; the host-function
boundary does not change if it does.

### Persistent Sandbox, Fresh Process Per Program

One gVisor sandbox runs for the life of the service; each program runs as a
fresh process inside it. A turn runs several programs, so a per-turn or
per-program container would pay container cold-start latency many times per user
turn. A persistent sandbox pays gVisor start once at service start; the
per-program cost is a process spawn. The single-user model means no cross-tenant
isolation need justifies per-program containers; clean interpreter state and
discarded scratch space give the needed freshness between programs.

### Copy codapt2's Programming Model, Not Its Code

Copy: one tool runs a program; effects are typed syscalls; the program has real
control flow; an invalid program produces feedback and another model call. Do
not copy: TypeScript, Effect, the Lua runtime, durable-operation journaling and
replay, the workspace-machine executor, billing.

### Keep Ariel's Rails

codapt2 host functions run with no policy layer. Ariel's syscalls keep policy,
approval, taint, provenance, egress preflight, output guardrails, and the action
ledger. The syscall boundary is exactly where these rails sit. This cutover
changes the source language and the execution substrate; it does not weaken a
rail.

### Approval Stays a Turn Boundary

A program runs once, within a turn. Approval-gated syscalls stage a proposal and
return a pending value; they never block the program for a human. No durable
journaling or replay is built — a crashed turn re-runs from the model, and the
action ledger provides idempotency, as today.

### Close the Same-Turn Taint Gap

Because syscalls in a program execute sequentially through the host, the host
updates taint as the program runs: a syscall proposed after a same-turn
untrusted read is evaluated with that taint. The flat-list model evaluated every
proposal against a single per-turn snapshot and could not do this.

### Compose Mechanical Answers in One Turn

The flat list forbade `agent.emit_message` alongside internal calls because the
message text was fixed before results were known. A program computes the message
after inline reads return, so the rule is relaxed for mechanical answers:
confirmations, counts, and formatted data composed from syscall results in one
program. It does not remove the second turn for answers that require the model
to read and interpret fetched content — the model authored the program before
seeing that content, so it uses `agent.emit_value` and a later turn for those.

### Test the Sandbox Boundary in Two Layers

Host-side logic — the syscall protocol, dispatch, the rails, run effects — is
tested with the sandbox boundary mocked by a fake transport that exercises the
same protocol. A smaller suite runs real programs in a real gVisor sandbox. CI
provides `runsc`; the Systrap platform needs no special host capability.

### Remove the Terminal

The terminal subsystem is deleted, not sandboxed. Ariel is not a coding agent;
Agency owns code execution. Removing the terminal removes the largest
unsandboxed-execution and prompt-injection surface and roughly 2,200 lines,
including the bulk of `policy_engine.py`.

## Implementation Plan

### Phase 1: Failing Contract Tests

Add tests that fail against current behavior:

- flat-JSON `run` source is rejected; Python-program `run` source is accepted
- a program with a conditional and a loop executes and dispatches syscalls in
  order
- a program executes inside the sandbox, has no network, and cannot write the
  root filesystem
- no `terminal.*` syscall exists
- an approval-gated syscall stages a proposal and returns a pending value
  without executing inline
- a program that raises mid-run commits no proposals
- a syscall after a same-turn untrusted read is evaluated as tainted

These run against the mocked sandbox boundary, except a small real-sandbox
suite; see the two-layer test decision.

Acceptance: the tests fail against current `main` and define the target.

### Phase 2: Remove the Terminal Subsystem

Delete `terminal_runtime.py`, `terminal_safety.py`, the five terminal
capabilities and their validators, the `policy_engine.py` terminal block,
`TerminalCommandRecord`, the terminal config, and the terminal tests. Add a
migration that drops `terminal_commands`.

Acceptance: no terminal callable or capability exists; `policy_engine.py` has no
terminal branch; `make verify` passes.

### Phase 3: The gVisor Sandbox Runtime

Install `runsc`. Build a minimal container image with CPython, its standard
library, and the guest worker. Run one persistent sandbox; execute each program
as a fresh process inside it. Implement the host-to-guest syscall channel as
JSON over one pipe or socket, with the host-side reader as a strict ingress
boundary. Implement the guest worker that runs the program and marshals
syscalls. Enforce no network, a read-only rootfs, and the program limits. Build
the mocked-transport test layer.

Acceptance: a trivial Python program runs as a fresh process in the persistent
sandbox, calls a stub syscall, and returns a result; network and root-filesystem
writes are denied; oversized channel messages are rejected; CPU, memory,
wall-clock, and syscall limits are enforced.

### Phase 4: Rewrite `run_runtime`

Change the `run` tool definition and the `source` contract to a Python program.
Design the program-authoring prompt: the eligible syscalls, worked program
examples, and the protocol rules. Implement program validation and protocol
feedback. Dispatch syscalls from the sandbox channel to the host. Collect run
effects and commit them only on clean completion.

Acceptance: a program with control flow and data flow executes end to end;
flat-JSON source is rejected with feedback; a program that raises mid-run
commits no proposals.

### Phase 5: Re-seat Capabilities as Syscalls

Design the model-facing syscall API as a deliberate surface: stable namespaced
names, typed signatures, per-syscall documentation, and worked examples. Expose
the capabilities as the program's syscall functions. Each syscall, host-side,
runs the existing per-call lifecycle. Implement `agent.emit_message`,
`agent.emit_value`, and `agent.pause_until_input`. Implement within-program
taint accumulation and the persisted syscall trace.

Acceptance: every capability is callable as a syscall; tests prove policy,
approval, taint, egress, guardrails, and audit still apply; approval-gated
syscalls stage proposals and return pending values; the same-turn taint test
passes; the syscall trace is persisted.

### Phase 6: Delete the Old Surface

Delete `unpack_run_source` flat-JSON parsing and the in-process execution path.
Confirm no code path executes a host function outside the sandbox.

Acceptance: no flat-JSON parsing and no in-process execution remain; tests prove
both are absent.

### Phase 7: Deployment, Docs, Verification

Install `runsc` on the production host and run the persistent sandbox under
systemd alongside the services. Update `deploy/systemd` and the production
runbook. Complete the docs cutover: this spec, the deletions, and the updates.
Run `make verify` and the acceptance suite.

Acceptance: the stack runs on the production host with `runsc`; `make verify`
and the acceptance suite pass.

## Acceptance Criteria

The cutover is complete only when all are true:

- Normal turns expose exactly one direct model tool, `run`.
- `run` source is a Python program; flat-JSON source is rejected with model
  feedback.
- Every `run` program executes as a fresh process inside the persistent gVisor
  sandbox; there is no in-process execution path.
- The sandbox has no network and a read-only rootfs; CPU, memory, wall-clock,
  syscall-count, and output limits are enforced.
- The host-side syscall channel rejects oversized and schema-invalid guest
  messages before dispatch.
- A program can branch, loop, bind variables, and pass one syscall's result into
  another.
- Every capability is an internal syscall; no capability is a direct model tool.
- Policy, approval, taint, provenance, egress preflight, output guardrails, and
  the `action_attempts` ledger are preserved and tested at the syscall boundary.
- Approval-gated syscalls stage a proposal and return a pending value; staged
  proposals are committed only when the program completes cleanly.
- A program that raises an unhandled exception commits no proposals and feeds
  the error back.
- The ordered syscall trace is persisted as the program's audit record.
- A syscall after a same-turn untrusted read is evaluated with that taint.
- The terminal subsystem is fully removed; no `terminal.*` syscall or capability
  exists.
- Tests prove the flat-JSON path and the in-process path are absent.
- Host-side tests run against the mocked sandbox boundary; a real-sandbox suite
  runs under `runsc`.
- The stack deploys and runs on the production host with `runsc`.
- `make verify` passes.

## Key Risks

- Authoring a correct Python program against the syscall API is a substantially
  harder task for the model than emitting a flat call list; unreliable authoring
  degrades every turn. Mitigation: a deliberately small, well-documented syscall
  API; worked program examples and patterns in the prompt; strict protocol
  validation with feedback; the model-facing API and prompt are a first-class
  design artifact, not a byproduct.
- The guest worker runs the model's untrusted program, so its channel messages
  are untrusted input. Mitigation: the host-side channel reader enforces a
  maximum message size, strict schema parsing, and typed narrowing before
  dispatch; the guest is never trusted because it is Ariel's own worker.
- gVisor is a user-space kernel, not a hardware-isolated microVM; a gVisor
  escape would reach the host. Mitigation: no network in the sandbox, a
  read-only minimal rootfs, a restricted interpreter environment as defense in
  depth, nothing of value inside the container, and `runsc` kept patched.
  Firecracker remains a future upgrade on bare metal with no change to the
  host-function boundary.
- A Python program is more expressive than a flat list, widening the surface a
  prompt-injected model can drive. Mitigation: the program can only call typed
  syscalls; every syscall passes the existing rails; the sandbox contains the
  program; the terminal — the worst sink — is removed; within-program taint
  propagation denies untrusted-influenced side effects.
- Re-seating capabilities could silently drop a rail. Mitigation: syscall
  execution reuses the existing per-call lifecycle code; contract tests assert
  policy, approval, taint, egress, guardrails, and audit on every capability.
- A program-process spawn adds per-program latency. Mitigation: the gVisor
  sandbox is persistent, so only a process spawn — not a container start — is
  paid per program; measure against the turn budget and use a warm interpreter
  if needed.
- The 2 GB production host is constrained. Mitigation: gVisor is lighter than a
  microVM; a single-user system runs one program at a time; measure footprint
  alongside PostgreSQL and the services.
- A program cannot read back an approval-gated result in the same turn.
  Mitigation: this matches current behavior and is not a regression; the model
  proposes in one turn and continues after approval in a later turn.

## Source Findings

This spec is based on a read-only survey of:

- `src/ariel/run_runtime.py`, `capability_registry.py`, `action_runtime.py`,
  `policy_engine.py`, `executor.py`, `terminal_runtime.py`,
  `terminal_safety.py`, `persistence.py`, `app.py`
- `docs/single-run-tool-cutover.md`, `north-star-cutover.md`,
  `agent-tooling.md`, `ai-first.md`, `simplicity.md`, `index.md`
- `/home/niels/src/work/codapt2` — the run tool, the Lua and Firecracker
  sandboxes, the durable-execution model, and the host-function registry
- Web research on Firecracker host requirements, Hetzner Cloud
  nested-virtualization support, and gVisor as the KVM-free alternative
