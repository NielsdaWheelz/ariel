# AI-First Architecture

## Scope

This document owns Ariel's repository-wide AI-first architecture rule: AI owns
judgment, interpretation, synthesis, and delegation; deterministic code owns
service rails.

This document covers model and subagent ownership, when deterministic helpers are
allowed, how helpers must be shaped, and how to keep AI behavior auditable.

## Thesis

Ariel is an AI operator, not a deterministic workflow engine with model garnish.

The master assistant owns the user-facing turn and delegates bounded cognitive
work to task-specific AI subagents. Deterministic code exists to provide services
the AI cannot yet perform safely or directly. Those services must be narrow,
auditable, replaceable, and removable when model capability improves. Code exists
only to enable and facilitate AI judgment; it never performs product judgment on
its own.

## Judgment Ownership

AI owns judgment-shaped work:

- deciding whether information matters
- deciding which memories are relevant
- deciding whether something is worth remembering
- deciding which tools or sources to inspect
- deciding what a non-human trigger means and whether it warrants acting
- synthesizing final user-facing text
- compacting context and continuity
- choosing whether to ask, wait, ignore, speak, remember, or act
- proposing exact action plans inside available authority

Deterministic code must not replace AI judgment with hidden heuristics,
thresholds, keyword routing, handcrafted priority scores, fallback prose,
rule-based summaries, or delivery decisions.

## Rail Ownership

Deterministic code owns rails:

- schema validation
- parsing values whose malformedness is locally knowable
- authentication and authorization
- policy, taint, provenance, and trust boundaries
- capability approval gating and hard safety blocks
- idempotency, dedupe, replay, and recovery
- transactions, locks, migrations, and persistence
- egress allowlists and side-effect boundaries
- resource budgets, timeouts, and retry limits
- audit records, inspection APIs, and operator controls
- typed failure surfaces

Rails can authorize, deny, constrain, fail closed, persist, replay, recover, and
explain. Rails must not decide semantic importance, user intent, usefulness,
response content, interruption value, or memory relevance.

## Subagent Rule

When a judgment task can be expressed with bounded inputs, bounded authority, and
a strict output contract, the master assistant delegates it to a subagent or
task-specific model call.

Subagents are used for:

- memory relevance and recall curation
- memory extraction and "worth remembering" decisions
- run-source choice and evidence source selection
- tool-result interpretation
- final answer synthesis from evidence
- context compaction and continuity summaries

The master assistant sees subagent outputs, provenance, confidence, omissions,
and failures. The master assistant does not need every intermediate token or raw
candidate unless the subagent output is insufficient and the next step requires
inspection.

## Service-Based Determinism

Deterministic helpers are services for current model limitations.

A deterministic helper is allowed only when it:

- protects safety, privacy, authority, or data integrity
- exposes a concrete capability to the model
- normalizes or validates a known boundary shape
- retrieves bounded candidate evidence with provenance
- enforces resource or side-effect limits
- records durable state needed for replay and audit

Every helper must have a narrow contract and a clear removal path. Do not add
deterministic helpers that become the product brain.

## Context

Context assembly is a service to AI judgment.

Deterministic code may gather eligible candidates, enforce lifecycle exclusions,
apply access checks, enforce budgets, label taint, attach provenance, and record
omissions. AI decides which candidates matter, how to use them, what uncertainty
means, and what to say.

If deterministic code must order candidates for budget reasons, the order is a
transport order, not the final relevance decision.

## Tool Use

Tool execution is a rail; run-source choice and result interpretation are
AI-owned.

Deterministic code validates the `run` protocol, executes authorized internal
callables, captures outputs, labels taint, records artifacts, and returns typed
failures. AI decides which internal callables to invoke, whether more evidence
is needed, how outputs affect the answer, and whether uncertainty should be
surfaced.

Deterministic code must not author final answers from tool output except typed
failure envelopes that stop the turn.

## Memory

Memory is a flat fact store plus two AI-maintained documents — the profile and
the per-session digest. Storage is canonical in Ariel-owned persistence; every
memory judgment is AI-owned.

Two subagents own that judgment: the retriever decides which facts matter for
the current wake; the rememberer decides what to write and keeps the profile and
digest current. Deterministic code stores facts and the two documents, gathers
candidate facts, injects the profile and digest into context, runs the
subagents, and writes audit records. No extraction, curation, conflict,
projection, or review machinery exists. See [modules/memory.md](modules/memory.md).

## Research

Read-only investigation is a third named subagent: the research subagent. The
main agent dispatches it with the `research.investigate` syscall; it runs the
same agent loop in a read-only configuration, in one of two mutually exclusive
modes (web XOR personal), and returns a typed, tainted `research_finding_v1`.
The main agent owns synthesis; the research subagent owns breadth-first,
read-heavy, independent investigation whose context is discarded once the
finding returns.

Deterministic code owns only rails: the `background_tasks` queue, the
mode-capability whitelists, taint propagation on the finding, and the
`requires_approval` routing that any tainted-finding-motivated action must pass
through. See [modules/agent-loop.md](modules/agent-loop.md).

## Proactivity

Proactive behavior follows the same rule.

Proactivity is not a subsystem. It is the main agent loop reached by non-human
triggers — a provider push, a poll result, a due scheduled task — plus one
durable scheduler. Every trigger wakes the same agent loop that serves a user
message, with the same memory and the same `run` tool.

The model decides, on every wake, whether the event matters, whether to inspect
more context, whether to interrupt, whether to wait, whether to remember,
whether to act, and when to look again. A wake may end without emitting.
Deterministic code owns only rails: the durable queue, ingress normalization,
provider auth, capability policy, approval, and delivery. Per-capability
`requires_approval` policy remains the side-effect authorization boundary. See
[modules/proactivity.md](modules/proactivity.md).

## Testing

Tests for rails assert deterministic invariants: schemas, auth, policy, taint,
idempotency, replay, transactions, and audit records.

Tests for judgment use model fixtures, subagent contracts, evals, or stored
decision examples. They must not reintroduce deterministic judgment as a test
oracle unless the test is proving that deterministic judgment is absent.

## Non-Goals

- Do not replace policy, auth, taint, or side-effect safety with model judgment.
- Do not add hidden deterministic product brains under names like helper,
  synthesizer, planner, scorer, ranker, router, or classifier.
- Do not keep deterministic fallback behavior because model output might fail.
  Invalid or missing model output fails closed with an auditable error.
- Do not add generic reusable abstractions for hypothetical future subagents.
  Add the narrow subagent contract required by the current product path.
