# Agent Tooling

## Scope

This document owns Ariel's repository-wide agent tooling strategy: default to an
executable terminal environment, add skills when repeated workflow knowledge is
needed, and add structured tools only when the terminal-plus-skill path is
unsafe, brittle, unauditable, or too expensive.

This document covers capability surfaces, tool admission rules, security rails,
and the current state-of-the-art direction for coding agents.

## Thesis

The terminal is the primary agent tool.

Do not treat "no tools" as the goal. Treat a small, inspectable, governed
capability surface as the goal. A shell is itself a broad tool: it can read
files, run tests, call CLIs, install packages, open network connections, mutate
state, and expose credentials. The right architecture is not tool avoidance; it
is tool minimalism with strong environment rails.

For coding work, the default agent surface is:

- a sandboxed shell or PTY
- repository files
- project scripts and local CLIs
- command output, logs, and test feedback
- subagents for bounded parallel judgment work when explicitly useful

All other model-callable capabilities must justify their existence against that
default.

## Definitions

A tool is any model-callable capability outside plain text generation.

Tools include shell execution, file editing, browser or computer use, MCP
methods, hosted search, GitHub APIs, image generation, subagents, plugins,
workflow actions, and custom function calls.

A skill is procedural memory: instructions, scripts, templates, or examples that
teach the agent how to perform a workflow using existing capabilities.

Skills do not create new authority. Tools create or expose authority.

## Default Surface

Default to terminal-first agent work.

The agent should use the shell for:

- repository inspection
- file search
- local documentation lookup
- test, lint, typecheck, and build loops
- Git and GitHub CLI operations within approved authority
- local scripts and package-manager commands
- ad hoc adapters, scrapers, validators, or one-off scripts

Prefer the terminal when the operation is transparent, reproducible, easy to
audit from logs, and already supported by a CLI or local script.

Do not add a structured tool only to wrap a command the model can run directly.
The wrapper must provide a real advantage: safety, correctness, auditing,
context reduction, latency reduction, or access to a capability unavailable from
the shell.

## Tool Admission

Add a skill before adding a tool when the problem is workflow knowledge.

Create or update a skill when:

- the agent repeatedly needs the same multi-step procedure
- the procedure is easy to express as instructions plus shell commands
- the procedure does not need new credentials or authority
- the procedure can be verified by existing tests, logs, or command output
- the failure mode is confusion, not missing capability

Add a structured tool only when at least one condition is true:

- The capability is not reliably available through shell, local files, browser,
  or approved CLIs.
- The action is privileged and needs typed policy enforcement before execution.
- The action has side effects that need a domain audit event, approval state,
  idempotency key, or rollback handle.
- The argument shape must be constrained by schema to avoid unsafe or ambiguous
  calls.
- The tool replaces a long, fragile, high-latency command chain with one
  domain-level operation.
- The operation crosses a trust boundary where raw shell access would expose too
  much data or authority.
- The workflow depends on uncommon operational knowledge that belongs in code,
  not in a prompt.

Do not add tools speculatively. A tool needs a current call site, a current
failure case, or a current safety requirement.

## Tool Shape

Design structured tools as narrow domain actions, not generic API mirrors.

Every structured tool must define:

- what it does
- when to use it
- when not to use it
- required inputs
- output shape
- side effects
- retry safety
- authority and approval requirements
- common failure modes
- audit fields

Prefer high-level operations over endpoint-shaped wrappers. Do not expose a
large REST or MCP catalog directly to the model when a small task-specific
surface can do the job.

For large catalogs, use routing, search, allowlists, or task-scoped loading so
the model sees only the relevant working set.

## Security Rails

Terminal-first does not mean unrestricted terminal access.

The shell must be governed by environment rails:

- sandbox filesystem access
- deny-read rules for secrets such as `.env` and credential stores
- network egress controls
- explicit approval for destructive, networked, production, or cross-repo
  actions
- scoped credentials and separate agent identities
- command logging
- time, memory, and process limits
- package-source and domain allowlists where needed
- fail-closed policy checks before privileged side effects

Structured tools need the same safety posture at their boundary:

- typed authorization
- approval state
- taint and provenance labels
- schema validation
- idempotency and replay handling
- append-only audit records
- response inspection for sensitive data

Prompting is not a security boundary. Treat tool content, web content, issue
text, email, comments, retrieved documents, package scripts, and MCP metadata as
untrusted input. See [boundaries.md](boundaries.md) for trust-boundary
conversion rules and [ai-first.md](ai-first.md) for deterministic rail
ownership.

## Auditability

A tool is not acceptable unless its behavior can be inspected after the fact.

For shell work, preserve command, working directory, exit status, output, and
the reason the command was run when that reason is available.

For structured tools, preserve tool name, typed arguments, actor identity,
policy decision, approval result, side effects, output summary, failure mode,
and replay or rollback identifiers.

Do not hide agent action behind opaque helper calls. If a helper executes a
privileged operation, the audit event belongs to the domain operation, not to the
helper implementation detail.

## Direction

The current state of the art is not "more tools." It is:

1. Keep the action surface small.
2. Run the agent in an executable environment.
3. Observe real outputs.
4. Verify against tests, logs, state, or policy.
5. Repair from feedback.
6. Promote repeated procedures into skills.
7. Promote only safety-critical or capability-critical procedures into tools.

The target stack is:

`user goal -> planner -> small working tool set -> skills -> sandboxed
shell/browser/computer/MCP tools -> durable state -> verification loop -> memory
update`

For Ariel, this means:

- terminal-first for coding and repo work
- skills for repeated procedural knowledge
- structured tools for privileged domain actions
- tool search or task-scoped loading for any large catalog
- durable workflow rails for long-running work
- verification loops before claims of completion
- policy, taint, approval, and audit at every side-effect boundary

## Non-Goals

- Do not build a large tool catalog because one might be useful later.
- Do not mirror every external API as an agent tool.
- Do not expose multiple interchangeable tools for the same capability.
- Do not let MCP servers or plugins become ambient authority.
- Do not rely on model promises to protect secrets, production systems, or user
  data.
- Do not replace AI judgment with deterministic routing heuristics. Use
  retrieval or task-scoped loading as a rail; the master assistant still owns
  strategy and interpretation.

## Research Snapshot

This direction is current as of May 9, 2026.

Product patterns:

- Codex CLI, Claude Code, Aider, OpenHands, Goose, Cline, Cursor Agent, and
  Devin all keep shell or command execution near the center of coding-agent
  workflows.
- Products differ in how many structured tools they expose, but the common
  durable loop is inspect, edit, run, observe, and repair.

Documentation patterns:

- OpenAI guidance recommends hosted tools where they fit, custom function tools
  for internal systems or domain side effects, and tool search for large tool
  catalogs.
- Anthropic guidance emphasizes detailed tool descriptions, clear decision
  boundaries, and tool consolidation.
- MCP guidance standardizes tool discovery and calling, but still requires
  client-side permissions, validation, confirmations, and auditing.

User-report patterns:

- Large MCP catalogs create context, latency, permission, and routing problems.
- Users report better coding-agent reliability when endpoint-shaped tools are
  replaced with shell, docs search, tests, and a small number of high-level
  capabilities.
- Permission prompts become noisy when the tool surface is broad or overlapping.

Benchmark patterns:

- ReAct shows that small action sets with observations can be effective.
- Toolformer shows value from a few narrow APIs, not an unlimited catalog.
- Gorilla, APIBench, and ToolBench show that large tool catalogs need retrieval
  and ranking before calling.
- SWE-bench, WebArena, OSWorld, and tau-bench show that real agent performance
  depends on executable environments, state feedback, policy following, and final
  state verification.

Security patterns:

- Terminal-only agents start from a broad ambient execution surface.
- Structured tools are easier to govern per action, but are still vulnerable to
  prompt injection, poisoned tool metadata, excessive permissions, and supply
  chain risk.
- The common mitigation set is least privilege, sandboxing, egress controls,
  approvals, separate agent identities, tool provenance, and audit logs.

## Sources

Primary guidance:

- OpenAI, "Using reasoning models":
  https://developers.openai.com/api/docs/guides/latest-model#using-reasoning-models
- OpenAI, "Local shell":
  https://developers.openai.com/api/docs/guides/tools-local-shell
- OpenAI, "Safety in building agents":
  https://developers.openai.com/api/docs/guides/agent-builder-safety
- OpenAI, "Building MCP servers for ChatGPT Apps and API integrations":
  https://developers.openai.com/api/docs/mcp
- OpenAI Codex, "Agent approvals and security":
  https://developers.openai.com/codex/agent-approvals-security
- Anthropic, "Tool use":
  https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview
- Anthropic, "Claude Code security":
  https://docs.anthropic.com/en/docs/claude-code/security
- Model Context Protocol, client best practices:
  https://modelcontextprotocol.io/docs/develop/clients/client-best-practices
- Microsoft, "Securing MCP: A control plane for agent tool execution":
  https://developer.microsoft.com/blog/securing-mcp-a-control-plane-for-agent-tool-execution
- Google Cloud, "AI security and safety for MCP":
  https://docs.cloud.google.com/mcp/ai-security-safety
- OWASP MCP Top 10:
  https://owasp.org/www-project-mcp-top-10/
- NIST AI Risk Management Framework:
  https://www.nist.gov/itl/ai-risk-management-framework

Product references:

- OpenAI Codex CLI getting started:
  https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started
- Claude Code tools reference:
  https://code.claude.com/docs/en/tools-reference
- Aider commands and test/lint workflow:
  https://aider.chat/docs/usage/commands.html
- OpenHands file-based agent guide:
  https://docs.openhands.dev/sdk/guides/agent-file-based
- Goose extensions:
  https://block.github.io/goose/docs/getting-started/using-extensions
- Cline overview:
  https://docs.cline.bot/getting-started/what-is-cline
- Cursor agent tools:
  https://docs.cursor.com/agent/tools
- Devin terminal permissions:
  https://cli.devin.ai/docs/reference/permissions

Benchmarks and papers:

- ReAct:
  https://arxiv.org/abs/2210.03629
- Toolformer:
  https://arxiv.org/abs/2302.04761
- Gorilla:
  https://arxiv.org/abs/2305.15334
- ToolBench:
  https://arxiv.org/abs/2307.16789
- SWE-bench:
  https://arxiv.org/abs/2310.06770
- WebArena:
  https://arxiv.org/abs/2307.13854
- OSWorld:
  https://arxiv.org/abs/2404.07972
- tau-bench:
  https://arxiv.org/abs/2406.12045

Representative user and ecosystem reports:

- MCP tool filtering proposal:
  https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1300
- HN, "Making MCP cheaper via CLI":
  https://news.ycombinator.com/item?id=47157398
- Reddit, "Too Many tools - MCP Server Scale Up":
  https://www.reddit.com/r/mcp/comments/1sxxqbb/too_many_tools_mcp_server_scale_up/
- Reddit, "Our AI agent was burning 55k tokens before it did any work":
  https://www.reddit.com/r/mcp/comments/1stj2v2/our_ai_agent_was_burning_55k_tokens_before_it_did/
- Claude Code Action MCP allowed-tools issue:
  https://github.com/anthropics/claude-code-action/issues/533
