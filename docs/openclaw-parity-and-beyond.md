# OpenClaw Parity And Beyond

## Scope

This document owns Ariel's OpenClaw parity and beyond-OpenClaw direction
backlog.

It is a planning artifact, not an implementation spec. Each item below must get
its own research pass, module spec, acceptance criteria, and implementation
sequence before code changes land.

The goal is not to clone OpenClaw. The goal is to absorb the product breadth
that makes OpenClaw useful while preserving Ariel's stricter architecture:
single `run` tool, sandboxed typed syscalls, Postgres audit, taint propagation,
approval gates, egress rails, receipts, agentic memory, and Discord-first
operator control.

## Source Snapshot

This comparison was made on 2026-05-20 from:

- OpenClaw README: https://github.com/openclaw/openclaw
- OpenClaw gateway architecture: https://docs.openclaw.ai/concepts/architecture
- OpenClaw agent runtime: https://docs.openclaw.ai/concepts/agent
- OpenClaw agent loop: https://docs.openclaw.ai/concepts/agent-loop
- OpenClaw channels: https://docs.openclaw.ai/channels
- OpenClaw tools, skills, and plugins: https://docs.openclaw.ai/tools
- OpenClaw models: https://docs.openclaw.ai/concepts/models
- OpenClaw multi-agent routing: https://docs.openclaw.ai/multi-agent
- OpenClaw nodes: https://docs.openclaw.ai/nodes
- OpenClaw security: https://docs.openclaw.ai/gateway/security

Relevant Ariel standing docs:

- [north-star-cutover.md](north-star-cutover.md)
- [run-program-cutover.md](run-program-cutover.md)
- [modules/agent-loop.md](modules/agent-loop.md)
- [modules/proactivity.md](modules/proactivity.md)
- [modules/memory.md](modules/memory.md)
- [production-runbook.md](production-runbook.md)

## Ariel Baseline

Ariel is already ahead of OpenClaw on the authority model.

Current Ariel strengths:

- The normal model-facing surface is exactly one `run` tool.
- `run` source is a sandboxed Python program with typed host syscalls.
- Capability execution passes through policy, approval, taint, egress, audit,
  idempotency, receipts, and typed failure surfaces.
- The agent loop is worker-run, long, adaptive, and bounded by budget and
  stuck-detection.
- A per-turn scratch store carries large intermediate data without flooding
  model context.
- Proactive wakes use the same `_wake` loop as user messages.
- Memory is a two-layer substrate: immutable raw log plus curated notes.
- Research, memory recall, and rememberer work are agentic loop configurations,
  not deterministic product judgment.
- Coding work routes through Agency instead of ad hoc terminal tools.

OpenClaw is ahead on product breadth and ecosystem:

- Channel count.
- Model/provider count.
- Multi-agent routing.
- Plugin and skill ecosystem.
- Onboarding and operator tooling.
- Native, mobile, voice, browser, canvas, and node surfaces.
- Public distribution and community extension flow.

## Direction Rules

- Preserve Ariel's single-`run` model-facing surface.
- Add breadth as ingress, delivery, provider, plugin, node, and capability rails;
  do not add direct model tools.
- Every new external action must declare schema, impact, policy, egress,
  idempotency, receipts, audit fields, taint behavior, retry safety, and
  failure modes.
- Every new channel normalizes into `WakeContext` and delivers through a
  channel adapter after a committed turn.
- Every new ecosystem surface must be reviewable, revocable, versioned, and
  observable before it is convenient.
- Do not trade OpenClaw parity for weaker trust boundaries.

## Needed To Match OpenClaw

### 1. Channel Gateway And Routing

OpenClaw parity means Ariel can receive and reply through more than Discord.

Needed:

- A channel adapter contract for ingress, delivery, replies, attachments,
  reactions, approvals, and deterministic operator commands.
- Telegram, Slack, WhatsApp, Signal or iMessage, WebChat, and Matrix as first
  candidate adapters.
- Per-channel allowlists, pairing, group mention policy, and context visibility.
- Channel-specific attachment capture that still flows through
  `attachment.read`.
- Channel-specific delivery receipts.

Ariel constraint:

- All human or provider messages enqueue `user_message` or `agent_wake` tasks
  and reach `_wake`.
- Channel adapters never run model judgment.

### 2. Model Provider Registry And Model Ops

OpenClaw parity means Ariel can use multiple model providers and choose among
them safely.

Needed:

- Provider registry with OpenAI, Anthropic, Google, OpenRouter, local
  OpenAI-compatible endpoints, Ollama, and vLLM candidates.
- Per-agent or per-session primary model and fallback list.
- Provider health checks, auth status, expiry warnings, and live probe commands.
- Model capability metadata: tool support, image input, context window,
  streaming support, reasoning controls, cost, and trust tier.
- A model-switch control surface that changes future turns without corrupting
  an active run.

Ariel constraint:

- The selected model still sees exactly `run`.
- Provider differences are normalized behind the model adapter.

### 3. Multi-Agent Scope And Routing

OpenClaw parity means Ariel can host multiple isolated agents.

Needed:

- `agent_id` on sessions, turns, memory, credentials, channel bindings, model
  settings, and capability policy.
- Per-agent memory namespace and optional shared-memory grants.
- Per-agent provider auth and connector credentials.
- Per-agent capability eligibility and sandbox policy.
- Routing rules by channel, account, peer, group, command, or explicit mention.

Ariel constraint:

- Multi-agent does not mean hidden deterministic routing. Routing selects an
  agent boundary; the selected agent still owns judgment through `run`.

### 4. Plugin SDK

OpenClaw parity means third parties can add channels, providers, tools,
automations, and UI surfaces.

Needed:

- Plugin manifest format with version, permissions, capabilities, migrations,
  config schema, secrets, and uninstall behavior.
- Plugin registration for capabilities, channel adapters, model providers,
  provider-ingestion hooks, operator UI panels, and deterministic commands.
- Capability conformance tests generated from plugin metadata.
- Static scan, dependency scan, secret scan, and egress review before enablement.
- Runtime isolation policy for plugin code.

Ariel constraint:

- Plugins register internal capabilities and rails. They do not add direct
  model tools or bypass `process_one_call`.

### 5. Skills And Procedures

OpenClaw parity means users can install reusable workflows.

Needed:

- A first-class procedure artifact that can be authored by the agent, reviewed
  by the user, versioned, and activated.
- Import support for `SKILL.md` style instruction packs as untrusted candidate
  procedures.
- Procedure evals and examples.
- Procedure conflict detection and rollback.
- A local registry before any public registry.

Ariel constraint:

- Procedures teach the model how to use existing authority. They do not grant
  authority by themselves.

### 6. Operator Onboarding, Doctor, And Security Audit

OpenClaw parity means setup and repair are product surfaces.

Needed:

- `ariel onboard` for env, database, migrations, Discord, runsc, Agency, Google,
  search, maps, weather, and worker services.
- `ariel doctor` for local health, schema state, sandbox launch, worker queue,
  connector health, channel delivery, and Agency daemon reachability.
- `ariel security audit` for auth, webhook exposure, weak secrets, world-readable
  credentials, open channels, risky tools, plugin drift, and missing approval
  gates.
- Machine-readable JSON for automation.

Ariel constraint:

- The audit reports rails and configuration. It does not judge user intent.

### 7. Web Control Surface

OpenClaw parity means Ariel has a browser-accessible operator surface.

Needed:

- WebChat or control UI for active conversation.
- Timeline view for turns, events, action attempts, approvals, receipts, memory,
  research, Agency jobs, provider events, and worker queue state.
- Approval and denial controls with signed state binding.
- Artifact viewer for attachments, retrieval artifacts, diffs, research
  findings, and Agency outputs.
- Read-only mode and authenticated local-only default.

Ariel constraint:

- Discord stays primary until the web surface proves operational value.
- The web surface exposes state and controls; it does not become a second
  judgment path.

### 8. Device Nodes

OpenClaw parity means Ariel can operate paired devices and remote execution
surfaces.

Needed:

- Node pairing protocol with device identity, approved command set, token
  rotation, revocation, and status.
- Node commands for screen snapshot, camera listing, location, notifications,
  local file/media import, and optional system execution.
- Per-node capability policy and approval requirements.
- Node-host delivery receipts and command audit.

Ariel constraint:

- Device commands are capabilities with explicit authority metadata.
- Remote execution does not replace Agency for repository work.

### 9. Voice, Realtime Conversation, And Mobile

OpenClaw parity means Ariel can be spoken to and can speak back.

Needed:

- Push-to-talk before always-on wake word.
- Speech-to-text ingress that normalizes to `WakeContext`.
- Text-to-speech delivery with interruption handling.
- Mobile and desktop clients for capture, approval, notification, and brief
  review.
- Privacy indicators and per-device recording policy.

Ariel constraint:

- Voice is transport. It does not alter the approval, taint, or action model.

### 10. Browser, Canvas, And Visual Workspace

OpenClaw parity means Ariel can inspect and operate browser/visual tasks.

Needed:

- Browser automation as a capability family with profile isolation, URL policy,
  screenshot artifacts, and DOM/action receipts.
- Canvas or visual workspace for live task state, artifacts, checklists, and
  generated UI.
- Explicit separation between browser reading, browser acting, and account
  credential use.

Ariel constraint:

- Browser content is tainted. Browser-driven external actions require approval
  unless the capability policy proves a narrower safe case.

### 11. Media Capabilities

OpenClaw parity means Ariel can understand, generate, and transform media.

Needed:

- Image understanding and OCR beyond Discord attachments.
- Audio transcription and summarization.
- Image generation, document generation, and PDF extraction as capability
  families.
- Media artifact storage, size bounds, provenance, and redaction.

Ariel constraint:

- Generated or transformed media is an artifact with provenance and policy.

### 12. Distribution And Ecosystem

OpenClaw parity means Ariel is installable and extensible by someone other than
the repository author.

Needed:

- Install script or package.
- Stable CLI.
- Versioned config.
- Migration repair tools.
- Example deployments.
- Plugin/procedure packaging.
- Local registry first; public registry only after security scanning and
  moderation are real.

Ariel constraint:

- Public ecosystem convenience comes after supply-chain controls.

## Surpass Targets

### 1. Capability OS

Ariel's core differentiator should be a capability operating system.

Target:

- Every read and write is a typed syscall.
- Every syscall has a formal authority contract.
- Every side effect has an idempotency key, receipt, reconciliation path, and
  audit trail.
- Every action can be explained after the fact: who authorized it, what evidence
  influenced it, what policy allowed it, what external receipt proves it, and
  how to undo or reconcile it.

This surpasses OpenClaw by making authority inspectable by construction.

### 2. Personal Event Fabric

Ariel should ingest the user's world as normalized event streams.

Target:

- Email, calendar, Drive, Discord, channels, web watches, repositories,
  location, device state, bills, receipts, health checks, and incidents feed one
  event fabric.
- Each event carries source, trust, taint, freshness, dedupe identity, and
  privacy classification.
- Events wake the main loop only after deterministic coalescing and access
  checks.
- The model decides meaning, interruption value, next action, and silence.

This surpasses OpenClaw by turning proactivity into a principled event system
rather than a heartbeat plus many integrations.

### 3. Durable Objectives

Ariel should manage long-running objectives, not only chats and tasks.

Target:

- A goal has owner, scope, state, checkpoints, artifacts, blockers, next wake,
  approvals, and success criteria.
- Goals survive restarts and span research, Agency, calendar/email actions,
  memory updates, and follow-up wakes.
- The model owns strategy; rails own state transitions, receipts, and budgets.
- The user can inspect, pause, resume, cancel, or narrow a goal.

This surpasses OpenClaw by giving autonomous work durable shape without adding a
deterministic planner brain.

### 4. Procedure Compiler

Ariel should turn repeated work into reviewed, testable procedures.

Target:

- The rememberer detects repeated workflows and drafts procedure candidates.
- The user reviews procedure scope, examples, required capabilities, approval
  gates, and failure behavior.
- Accepted procedures become versioned artifacts with eval examples.
- Procedures can be demoted, edited, superseded, or retired.
- Procedure retrieval is agentic and taint-aware.

This surpasses public skill packs by making personal operational knowledge
audited and testable.

### 5. Trust-Partitioned Agent Cells

Ariel should use multiple agents only when they improve trust boundaries.

Target:

- Web research, personal-data research, memory work, coding, review, and device
  control run in separate cells.
- A cell is defined by data class, outbound reach, write authority, model,
  budget, and retention.
- No cell receives the lethal trifecta of private data, untrusted content, and
  outbound reach unless an explicit reviewed exception exists.
- The main agent owns synthesis and every write proposal.

This surpasses generic multi-agent routing by making agent boundaries security
objects, not just personalities.

### 6. Evidence And Receipts UI

Ariel should make its work legible.

Target:

- Every answer can show evidence, omitted evidence, tool outputs, memory notes,
  taint state, approvals, receipts, and unresolved uncertainty.
- Every proactive message can show why it spoke now.
- Every silent wake can still be inspected.
- Every side effect can show execution state, external receipt, reconciliation
  status, and undo path.

This surpasses chat-first assistants by making the audit trail a product
surface.

### 7. Self-Improving Operations

Ariel should operate itself.

Target:

- Scheduled self-audits for security config, connector drift, worker lag,
  failed tasks, stale approvals, memory health, model failures, and cost.
- Regression evals for key procedures and prompt changes.
- Automatic issue drafts or Agency jobs for fixable defects.
- Proactive warnings only when user attention is justified.

This surpasses manual operator tooling by letting the agent maintain its own
rails under approval.

### 8. Attention Policy

Ariel should become excellent at not bothering the user.

Target:

- Every proactive wake can end silently.
- Interruptions require an agent-authored reason tied to urgency, reversibility,
  deadline, personal preference, or risk.
- Bundled low-urgency observations are summarized at chosen review times.
- User corrections become memory/procedure updates.
- The product tracks false positives, false negatives, and ignored alerts.

This surpasses always-on agents by treating attention as a scarce resource.

### 9. Safety Evals And Red-Team Harness

Ariel should test the behavior that matters.

Target:

- Prompt-injection suites across email, web, attachments, memory, research
  findings, and channel history.
- Approval-bypass and egress-bypass regression tests.
- Multi-agent data-leak tests.
- Proactivity interruption-quality evals.
- Procedure-following evals.
- Model-provider compatibility evals.

This surpasses ad hoc hardening by making unsafe regressions hard to merge.

### 10. Local-First Private Intelligence

Ariel should keep private context close and bounded.

Target:

- Private memory, credentials, transcripts, and artifacts stay in Ariel-owned
  storage.
- Local models are available for low-stakes private recall, redaction, and
  offline operation when quality is sufficient.
- Cloud calls receive the minimum context needed for the current judgment.
- Sensitive data flows are visible in audit.

This surpasses provider-centric assistants by making privacy a dataflow
property, not only a promise.

## Research And Spec Queue

Each backlog item follows the same sequence:

1. Research OpenClaw and adjacent systems.
2. Survey Ariel's current code and docs.
3. Write a module spec with scope, non-goals, data model, capability contracts,
   migration plan, acceptance tests, and security analysis.
4. Implement the smallest vertical slice.
5. Verify with unit tests, integration tests, security tests, and operator
   smoke tests.
6. Update runbooks and remove stale docs.

Initial order:

1. Channel gateway and routing.
2. Model provider registry and model ops.
3. Operator onboarding, doctor, and security audit.
4. Web control surface.
5. Multi-agent scope and routing.
6. Plugin SDK.
7. Skills and procedure registry.
8. Device nodes.
9. Voice and mobile.
10. Browser, canvas, and visual workspace.
11. Durable objectives.
12. Personal event fabric.
13. Procedure compiler.
14. Trust-partitioned agent cells.
15. Evidence and receipts UI.
16. Self-improving operations.
17. Attention policy.
18. Safety evals and red-team harness.
19. Local-first private intelligence.

## Completion Definition

OpenClaw parity is reached when Ariel can be installed, configured, extended,
operated, and used across multiple channels and models without weakening its
single-`run` authority model.

Beyond-OpenClaw status is reached when Ariel can run durable personal
objectives across event streams with inspectable authority, trustworthy
proactivity, audited memory, verified procedures, and receipts for every
side effect.
