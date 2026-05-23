# Personal Agent SOTA Roadmap

## Scope

This document owns Ariel's roadmap for matching the useful breadth of
OpenClaw-class systems and surpassing them with a stricter personal-agent
operating system.

It is a planning artifact, not an implementation spec. Each item below must get
its own research pass, module spec, acceptance criteria, security analysis, and
implementation sequence before code changes land.

The goal is not to clone OpenClaw, NanoClaw, ZeroClaw, CellClaw, or any other
agent framework. The goal is to absorb the product and ecosystem lessons that
make them useful while preserving Ariel's stricter architecture: single `run`
tool, sandboxed typed syscalls, Postgres audit, taint propagation, approval
gates, egress rails, receipts, agentic memory, durable worker execution, and
operator control.

## Source Snapshot

This comparison was made on 2026-05-20 from public docs, project pages,
benchmarks, papers, product announcements, and user reports.

OpenClaw baseline:

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

Claw-variant and adjacent systems:

- ZeroClaw: https://github.com/zeroclaw-labs/zeroclaw
- NanoClaw: https://nanoclaws.io/
- CellClaw: https://cellclaw.com/
- SemaClaw: https://arxiv.org/abs/2604.11548
- VisionClaw: https://arxiv.org/abs/2604.03486
- OpenClaw safety analysis: https://arxiv.org/abs/2604.04759

Frontier product and infrastructure references:

- ChatGPT agent: https://openai.com/index/introducing-chatgpt-agent/
- OpenAI computer-using agent: https://openai.com/index/computer-using-agent/
- Anthropic computer use: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- Claude Code: https://docs.anthropic.com/en/docs/claude-code/overview
- Google Agentspace: https://cloud.google.com/agentspace/agentspace-enterprise/docs/overview
- Google Agent Development Kit: https://cloud.google.com/vertex-ai/generative-ai/docs/agent-development-kit/overview
- Apple Intelligence and Foundation Models: https://www.apple.com/newsroom/2025/06/apple-intelligence-gets-even-more-powerful-with-new-capabilities-across-apple-devices/
- Devin: https://docs.devin.ai/
- Replit Agent: https://docs.replit.com/core-concepts/agent
- Browserbase Stagehand: https://www.browserbase.com/stagehand/

Protocols and frameworks:

- MCP: https://modelcontextprotocol.io/docs/getting-started/intro
- A2A: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- OpenHands SDK: https://docs.openhands.dev/sdk/index
- Letta stateful agents: https://docs.letta.com/guides/core-concepts/stateful-agents
- Browser-use: https://github.com/browser-use/browser-use
- BrowserGym: https://github.com/ServiceNow/BrowserGym
- Temporal: https://docs.temporal.io/

Evaluation and safety references:

- HAL: https://hal.cs.princeton.edu/
- WebArena: https://webarena.dev/og/
- OSWorld: https://os-world.github.io/
- SWE-bench: https://www.swebench.com/
- GAIA: https://arxiv.org/abs/2311.12983
- tau-bench: https://arxiv.org/abs/2406.12045
- LongFuncEval: https://arxiv.org/abs/2505.10570
- TheAgentCompany: https://arxiv.org/abs/2412.14161
- OWASP LLM Top 10: https://genai.owasp.org/llm-top-10/
- OWASP MCP Top 10: https://owasp.org/www-project-mcp-top-10/
- OpenAI agent safety: https://developers.openai.com/api/docs/guides/agent-builder-safety
- NCSC prompt-injection guidance: https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection

User-report themes came from Reddit, Hacker News, GitHub issues, product review
sites, and incident coverage. They are not treated as authoritative feature
specs, but they are treated as evidence about trust failures: fake completion,
runaway loops, setup friction, permission anxiety, brittle integrations, credit
burn, interruption fatigue, and weak support when automation breaks.

Relevant Ariel standing docs:

- [north-star-cutover.md](north-star-cutover.md)
- [modules/agent-loop.md](modules/agent-loop.md)
- [modules/proactivity.md](modules/proactivity.md)
- [modules/memory.md](modules/memory.md)
- [production-runbook.md](production-runbook.md)

## Ariel Baseline

Ariel is already ahead of OpenClaw-style systems on the authority model.

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

OpenClaw and adjacent systems are ahead on product breadth and ecosystem:

- Channel count.
- Model/provider count.
- Multi-agent routing.
- Plugin and skill ecosystem.
- Onboarding and operator tooling.
- Native, mobile, voice, browser, canvas, wearable, and node surfaces.
- Public distribution and community extension flow.

## Direction Rules

- Preserve Ariel's single-`run` model-facing surface.
- Add breadth as ingress, delivery, provider, plugin, node, protocol, and
  capability rails; do not add direct model tools.
- Every new external action must declare schema, impact, policy, egress,
  idempotency, receipts, audit fields, taint behavior, retry safety, and
  failure modes.
- Every new channel normalizes into `WakeContext` and delivers through a
  channel adapter after a committed turn.
- Every external protocol implementation is a gateway into Ariel capabilities,
  not a bypass around Ariel policy.
- Every new ecosystem surface must be reviewable, revocable, versioned, and
  observable before it is convenient.
- Do not trade parity or feature breadth for weaker trust boundaries.

## Landscape Read

### OpenClaw

OpenClaw's useful contribution is breadth: many channels, tools, providers,
skills, plugins, nodes, and a public extension story.

Ariel should match that breadth through adapters and capability rails, not by
making the model-facing tool surface wider.

### ZeroClaw

ZeroClaw's useful contribution is a small Rust runtime and one-machine
ownership model: one agent binary, provider choice, many channels, and local
execution.

Ariel should steal the shape, not the implementation:

- Small trusted core.
- Strict adapter boundaries.
- Provider and channel plugins.
- Easy service installation.
- Local ownership of keys, data, and runtime state.

### NanoClaw

NanoClaw's useful contribution is minimalism: a small TypeScript assistant built
around Claude Agent SDK, containers, scheduled jobs, memory, WhatsApp, and
agent swarms.

Ariel should steal:

- Container-first execution for untrusted work.
- Tiny install path.
- Procedures and skills as simple files before a public marketplace.
- Bounded agent teams only when they are easier to inspect than one giant run.

Ariel should not copy NanoClaw's narrow provider dependency. Provider choice is
part of the personal-agent OS.

### CellClaw

CellClaw's useful contribution is device-native agency: Android accessibility
control, screenshots, SMS/calls/messaging, wake word, persistent local memory,
and explicit autonomy profiles.

Ariel should steal:

- Device nodes as first-class paired agents.
- Per-device capability policy.
- Push-to-talk and wake-word paths after privacy indicators exist.
- Phone control as a narrow node capability, not as global authority.

### SemaClaw, VisionClaw, And Research Variants

The research direction is no longer "better prompt." It is harness engineering:
orchestration, safety bridges, context tiers, personal knowledge construction,
and always-on situated perception.

Ariel should treat these as warnings:

- Always-on perception multiplies privacy and permission risk.
- Multi-agent orchestration without authority partitions is just more attack
  surface.
- Harness quality is now the main product differentiator.

### Frontier Products

The strongest products are converging on supervised autonomy:

- OpenAI ChatGPT agent combines deep research, browser action, terminal, APIs,
  connectors, and human interruption.
- OpenAI and Anthropic computer-use systems use screenshot/action loops as a
  fallback for human-designed interfaces.
- Claude Code, Codex, Cursor, Devin, and Replit Agent make coding agents useful
  through repo sandboxes, tests, diffs, PRs, background jobs, and review.
- Microsoft, Google, and Lindy focus on enterprise connectors, identity,
  permissions, and workflows.
- Browserbase and Stagehand provide browser execution infrastructure, replay,
  and observability rather than trying to be the whole assistant.
- Apple Intelligence points toward OS-native personal context, App Intents,
  Shortcuts, on-device models, and private cloud escalation.

The product lesson is that Ariel needs a command center for supervised
autonomy, not a louder chat app.

### Benchmark And User Reality

Benchmarks show fast progress on static browser, desktop, coding, and general
assistant tasks, but they also show the limits:

- Static tasks are not enough; live sites, sessions, UI drift, and multi-site
  workflows remain brittle.
- One-shot success is not reliability. Production agents need repeated success.
- Tool catalogs can hurt. LongFuncEval shows that more tools, longer tool
  responses, and longer conversations degrade function-calling performance.
- Scaffolds matter. The SOTA is model plus planner, tools, retries,
  checkpoints, verification, budget, and traces.
- Long-horizon work fails through compounding small errors.

User reports add the product failure modes:

- Users like bounded delegation, inspectable artifacts, familiar tool fit, and
  human-in-driver-seat coding workflows.
- Users hate hallucinated completion, hidden loops, surprise costs, noisy
  proactivity, fragile setup, privacy ambiguity, and automations that break
  without useful recovery paths.

## Gold-Standard SOTA Architecture

The target architecture is a local-first personal agent OS. These are the
minimum SOTA pillars.

### 1. Agent Harness, Not Prompt Magic

Target:

- Model, planner loop, typed capabilities, memory, retries, budgets,
  checkpoints, and verifier are treated as one harness.
- The harness owns state and safety; the model owns judgment.
- Every significant run is replayable enough to debug.

Ariel implication:

- Keep `run` as the only normal model tool, but make the host runtime a richer
  operating layer around it.

### 2. Capability OS

Target:

- Every read and write is a typed syscall.
- Every syscall has a formal authority contract.
- Every side effect has an idempotency key, receipt, reconciliation path, and
  audit trail.
- Every action can be explained after the fact: who authorized it, what evidence
  influenced it, what policy allowed it, what external receipt proves it, and
  how to undo or reconcile it.

Ariel implication:

- Treat capabilities as Ariel's kernel API.

### 3. Durable Objective Engine

Target:

- A goal has owner, scope, state, checkpoints, artifacts, blockers, next wake,
  approvals, and success criteria.
- Goals survive restarts and span research, Agency, calendar/email actions,
  memory updates, and follow-up wakes.
- The user can inspect, pause, resume, cancel, or narrow a goal.

Ariel implication:

- Add durable objectives before adding more autonomous behavior.

### 4. Personal Event Fabric

Target:

- Email, calendar, Drive, Discord, channels, web watches, repositories,
  location, device state, bills, receipts, health checks, and incidents feed one
  event fabric.
- Each event carries source, trust, taint, freshness, dedupe identity, and
  privacy classification.
- Events wake the main loop only after deterministic coalescing and access
  checks.

Ariel implication:

- Proactivity should be an event-routing problem before it is an agent-persona
  problem.

### 5. Memory OS

Target:

- Immutable raw events, curated notes, procedure memory, episodic summaries,
  preference memory, and rejection memory are separate stores.
- Memory carries provenance, freshness, confidence, privacy class, and
  supersession links.
- The system can forget, redact, quarantine, and explain memory.

Ariel implication:

- Extend the current raw-log plus curated-notes substrate without collapsing it
  into a single vector database.

### 6. Trust-Partitioned Agent Cells

Target:

- Web research, personal-data research, memory work, coding, review, and device
  control run in separate cells.
- A cell is defined by data class, outbound reach, write authority, model,
  budget, retention, and approval policy.
- No cell receives the lethal trifecta of private data, untrusted content, and
  outbound reach unless an explicit reviewed exception exists.
- The main agent owns synthesis and every write proposal.

Ariel implication:

- Multi-agent work is primarily a security boundary, not a cast of
  personalities.

### 7. API-First, Computer-Use Fallback

Target:

- Structured APIs and MCP-style servers are preferred for stable operations.
- Browser, desktop, and mobile control are fallback executors for interfaces
  built for humans.
- Screenshots, DOM state, actions, and account use produce receipts.
- Credential entry and consequential actions require tightly scoped takeover or
  payload-bound approval.

Ariel implication:

- Browser/device control must be a tainted capability family with strict rails.

### 8. Protocol Gateway

Target:

- MCP is supported as a tool/context integration standard.
- A2A-style protocols are supported for agent-to-agent delegation.
- Protocol discovery never grants authority by itself.
- Every imported tool, agent, and context source maps to an Ariel capability,
  policy, data class, and audit path.

Ariel implication:

- Ariel should be a protocol gateway, not a protocol free-for-all.

### 9. Evidence And Receipts Command Center

Target:

- Every answer can show evidence, omitted evidence, tool outputs, memory notes,
  taint state, approvals, receipts, and unresolved uncertainty.
- Every proactive message can show why it spoke now.
- Every silent wake can still be inspected.
- Every side effect can show execution state, external receipt, reconciliation
  status, and undo path.

Ariel implication:

- Build a control surface around traces and receipts before building cosmetic
  dashboards.

### 10. Attention Policy

Target:

- Every proactive wake can end silently.
- Interruptions require an agent-authored reason tied to urgency,
  reversibility, deadline, personal preference, or risk.
- Bundled low-urgency observations are summarized at chosen review times.
- User corrections become memory or procedure updates.
- The product tracks false positives, false negatives, and ignored alerts.

Ariel implication:

- Attention is a scarce resource and needs policy, telemetry, and review.

### 11. Model Fleet And Provider Ops

Target:

- Providers, models, local endpoints, fallbacks, health, cost, context windows,
  modalities, and safety tiers are explicit runtime state.
- Model changes do not corrupt active turns.
- Local models handle private low-stakes recall, redaction, and offline work
  when quality is sufficient.
- Frontier cloud models are used for high-judgment work with minimum necessary
  context.

Ariel implication:

- Add provider ops as infrastructure, not as a chat setting.

### 12. Continuous Evals And Red-Team Harness

Target:

- Prompt-injection suites cover email, web, attachments, memory, research
  findings, and channel history.
- Approval-bypass and egress-bypass tests are mandatory.
- Multi-agent data-leak tests are mandatory.
- Proactivity interruption-quality evals are mandatory.
- Procedure-following and model-provider compatibility evals are mandatory.
- Benchmarks track cost, latency, repeatability, and trace quality, not only
  success rate.

Ariel implication:

- Unsafe regressions should be hard to merge.

### 13. Local-First Private Intelligence

Target:

- Private memory, credentials, transcripts, and artifacts stay in Ariel-owned
  storage.
- Cloud calls receive the minimum context needed for the current judgment.
- Device nodes can work with local models and local policy when disconnected.
- Sensitive data flows are visible in audit.

Ariel implication:

- Privacy is a dataflow property, not a provider promise.

### 14. Procedure Compiler

Target:

- The rememberer detects repeated workflows and drafts procedure candidates.
- The user reviews procedure scope, examples, required capabilities, approval
  gates, and failure behavior.
- Accepted procedures become versioned artifacts with eval examples.
- Procedures can be demoted, edited, superseded, retired, and rolled back.
- Procedure retrieval is agentic and taint-aware.

Ariel implication:

- Skills should evolve from lived usage, not only from public packages.

## Needed To Match OpenClaw-Class Breadth

### 1. Channel Gateway And Routing

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

Needed:

- `agent_id` on sessions, turns, memory, credentials, channel bindings, model
  settings, and capability policy.
- Per-agent memory namespace and optional shared-memory grants.
- Per-agent provider auth and connector credentials.
- Per-agent capability eligibility and sandbox policy.
- Routing rules by channel, account, peer, group, command, or explicit mention.

Ariel constraint:

- Routing selects an agent boundary; the selected agent still owns judgment
  through `run`.
- Agent boundaries must be compatible with trust-partitioned cells.

### 4. Plugin SDK

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

Needed:

- `ariel onboard` for env, database, migrations, Discord, runsc, Agency, Google,
  search, maps, weather, and worker services.
- `ariel doctor` for local health, schema state, sandbox launch, worker queue,
  connector health, channel delivery, and Agency daemon reachability.
- `ariel security audit` for auth, webhook exposure, weak secrets,
  world-readable credentials, open channels, risky capabilities, protocol
  servers, plugin drift, and missing approval gates.
- Machine-readable JSON for automation.

Ariel constraint:

- The audit reports rails and configuration. It does not judge user intent.

### 7. Web Command Center

Needed:

- WebChat or control UI for active conversation.
- Timeline view for turns, events, action attempts, approvals, receipts, memory,
  research, Agency jobs, provider events, and worker queue state.
- Approval and denial controls with signed state binding.
- Artifact viewer for attachments, retrieval artifacts, diffs, research
  findings, and Agency outputs.
- Browser replay and screen/action replay for computer-use tasks.
- Read-only mode and authenticated local-only default.

Ariel constraint:

- Discord stays primary until the web surface proves operational value.
- The web surface exposes state and controls; it does not become a second
  judgment path.

### 8. Device Nodes

Needed:

- Node pairing protocol with device identity, approved command set, token
  rotation, revocation, and status.
- Android node inspired by CellClaw: screen snapshot, app launch, notifications,
  location, SMS/call visibility, messaging, clipboard, and constrained
  accessibility actions.
- Desktop node: screen snapshot, notification delivery, file/media import, and
  optional local execution.
- Per-node capability policy and approval requirements.
- Node-host delivery receipts and command audit.

Ariel constraint:

- Device commands are capabilities with explicit authority metadata.
- Remote execution does not replace Agency for repository work.

### 9. Voice, Realtime Conversation, Mobile, And Wearables

Needed:

- Push-to-talk before always-on wake word.
- Speech-to-text ingress that normalizes to `WakeContext`.
- Text-to-speech delivery with interruption handling.
- Mobile and desktop clients for capture, approval, notification, and brief
  review.
- Wearable capture only after privacy indicators, recording policy, and
  attention policy exist.
- Privacy indicators and per-device recording policy.

Ariel constraint:

- Voice and wearables are transports. They do not alter the approval, taint, or
  action model.

### 10. Browser, Canvas, And Visual Workspace

Needed:

- Browser automation as a capability family with profile isolation, URL policy,
  screenshot artifacts, and DOM/action receipts.
- Canvas or visual workspace for live task state, artifacts, checklists, and
  generated UI.
- Explicit separation between browser reading, browser acting, account
  credential use, and purchase/send/submit actions.
- Evaluation harness for browser tasks before broad release.

Ariel constraint:

- Browser content is tainted. Browser-driven external actions require approval
  unless the capability policy proves a narrower safe case.

### 11. Media Capabilities

Needed:

- Image understanding and OCR beyond Discord attachments.
- Audio transcription and summarization.
- Image generation, document generation, and PDF extraction as capability
  families.
- Media artifact storage, size bounds, provenance, and redaction.

Ariel constraint:

- Generated or transformed media is an artifact with provenance and policy.

### 12. Distribution And Ecosystem

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

### 13. Protocol Integration

Needed:

- MCP client support for local and remote servers.
- MCP server support only for exposing deliberately selected Ariel resources.
- A2A-style task delegation for known remote agents.
- Agent cards or equivalent capability descriptions mapped to local trust
  policy.
- Protocol conformance, auth, revocation, and audit tests.

Ariel constraint:

- Protocol support expands interoperability, not authority. Discovery is not
  permission.

### 14. Evaluation Harness

Needed:

- A local eval runner for Ariel procedures, tools, prompts, and model adapters.
- Browser, coding, memory, proactivity, permission, egress, and prompt-injection
  suites.
- Cost, latency, repeatability, and trace-quality tracking.
- Regression gates for high-risk capability changes.

Ariel constraint:

- Benchmark wins do not override local safety invariants.

## One-Year Target

By mid-2027, Ariel should be a serious local personal-agent control plane.

Target capabilities:

- Provider registry with model health, cost, fallback, and capability metadata.
- `ariel onboard`, `ariel doctor`, and `ariel security audit`.
- Channel gateway with Discord plus at least one non-Discord channel.
- Durable objective table and operator controls for pause, resume, cancel, and
  inspect.
- Evidence command center for turns, approvals, receipts, artifacts, memory, and
  Agency work.
- Browser executor with profile isolation, taint, screenshot/action receipts,
  and a small eval suite.
- MCP-to-capability gateway for vetted local servers.
- Procedure artifact v1 with review, versioning, examples, and rollback.
- Security evals for prompt injection, approval bypass, egress bypass, and
  memory poisoning.
- Push-to-talk voice ingress if the privacy and approval model is already in
  place.

Definition of success:

- Ariel can run real supervised multi-step work across chat, browser, memory,
  research, and coding without hiding state from the operator.

## Five-Year Target

By 2031, Ariel should be a personal agent OS.

Target capabilities:

- Personal event fabric for mail, calendar, files, chats, repos, devices,
  watches, bills, receipts, health, and incidents.
- Memory OS with raw log, curated facts, episodic summaries, procedures,
  preferences, rejections, provenance, supersession, and deletion.
- Trust-partitioned agent cells for web research, private-data research, memory,
  coding, review, and device control.
- Cross-device nodes for desktop, phone, and limited wearable capture.
- Local model tier for private recall, redaction, and offline operation.
- Attested or privacy-preserving cloud escalation for hard tasks.
- A2A-style delegation to known external agents under explicit policy.
- Self-improving operations: scheduled audits, connector drift repair,
  procedure evals, model regression checks, and issue/Agency job drafts.
- Attention policy with measured interruption quality and bundled review
  windows.

Definition of success:

- Ariel can manage ongoing personal objectives across time and devices while
  remaining inspectable, revocable, and boringly safe.

## Ten-Year Target

By 2036, Ariel should be a sovereign digital twin control plane, not a provider
assistant.

Target capabilities:

- User-owned identity, memory, credentials, procedures, and authority graph.
- Federated agent mesh where Ariel can delegate to specialized agents without
  surrendering user data or write authority.
- Cryptographic or otherwise verifiable receipts for high-stakes transactions.
- Legal, financial, medical, and professional action contracts that encode
  approval, liability, retention, and rollback requirements.
- Local-first replication across devices with conflict handling and selective
  cloud sync.
- Personal policy language for preferences, risk thresholds, budget,
  interruption, data sharing, and durable objectives.
- Continuous safety and reliability evaluation as part of runtime operations.
- Human-computer interfaces that are ambient and multimodal but sparse:
  situated perception when useful, silence when not.

Definition of success:

- Ariel becomes the user's trusted authority layer over models, tools, devices,
  services, and other agents.

## Research And Spec Queue

Each backlog item follows the same sequence:

1. Research OpenClaw-class systems, frontier products, protocols, benchmarks,
   and user failure reports.
2. Survey Ariel's current code and docs.
3. Write a module spec with scope, non-goals, data model, capability contracts,
   migration plan, acceptance tests, and security analysis.
4. Implement the smallest vertical slice.
5. Verify with unit tests, integration tests, security tests, operator smoke
   tests, and relevant evals.
6. Update runbooks and remove stale docs.

Initial order:

1. Operator onboarding, doctor, and security audit.
2. Provider registry and model ops.
3. Evidence command center.
4. Durable objectives.
5. Channel gateway and routing.
6. Evaluation and red-team harness.
7. Browser executor and visual workspace.
8. MCP gateway and protocol policy.
9. Procedure artifact and local registry.
10. Multi-agent scope and trust-partitioned cells.
11. Device nodes.
12. Voice, mobile, and wearable capture.
13. Personal event fabric.
14. Memory OS expansion.
15. Plugin SDK and distribution.
16. Media capabilities.
17. Attention policy.
18. Self-improving operations.
19. A2A-style delegation.
20. Local-first private intelligence.

Small PR bundles:

- `onboard` + `doctor` can share health-check primitives.
- Provider registry + model health can land together.
- Evidence command center can start read-only with existing turn, event,
  receipt, and worker state.
- Durable objectives should be its own vertical slice.
- Channel adapters should land one at a time after the gateway contract.
- Browser execution should be its own PR because it changes the threat model.
- Protocol support should be split into MCP client, MCP server, and A2A
  delegation.
- Device nodes should start with read-only node capabilities before write or
  accessibility control.

## Completion Definition

OpenClaw-class parity is reached when Ariel can be installed, configured,
extended, operated, and used across multiple channels, models, protocols, and
device nodes without weakening its single-`run` authority model.

SOTA parity is reached when Ariel has durable objectives, evidence surfaces,
protocol gateways, browser/device executors, procedure memory, model ops, and
continuous evals.

Beyond-SOTA status is reached when Ariel can run durable personal objectives
across event streams and devices with inspectable authority, trustworthy
proactivity, audited memory, verified procedures, and receipts for every side
effect.
