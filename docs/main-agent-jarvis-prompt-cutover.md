# Main Agent Jarvis Prompt Cutover

## Scope

This document owns the hard cutover that wires Ariel's Jarvis-style main-agent
system prompt into production.

The cutover replaces the inline main-agent prompt tuple in `src/ariel/app.py`
with a versioned, code-owned prompt artifact in `src/ariel/prompts.py`. It
keeps `docs/jarvis-system-prompt.md` as the design and research artifact, not a
runtime dependency.

This document is limited to the **main user-facing agent**: the loop driven by
`_wake`, whose final output is `agent.emit_message` or
`agent.pause_until_input`. The memory retriever, memory rememberer, research
subagent, attachment extraction, and Agency downstream prompts are adjacent
prompt surfaces with their own contracts. They compose with the main prompt but
are not rewritten by this cutover.

The cutover is hard. There is no compatibility layer, no legacy prompt mode, no
feature flag, no environment override, no markdown runtime loader, and no
fallback to the old inline instructions. Work may be sequenced across commits,
but the merged final state has one production main-agent prompt path.

## Thesis

Ariel's main prompt is not a personality paragraph. It is a product contract.

The prompt tells the model what kind of operator it is, what authority it has,
how to treat tools and retrieved context, when to speak, when to stay silent,
how to handle uncertainty and failure, and how to express the Jarvis voice
without sacrificing correctness. Runtime rails remain responsible for actual
authority: schemas, taint, policy, approvals, egress, sandboxing, audit, and
receipts.

The professional pattern is:

- a cache-stable static prompt prefix in code
- a dynamic context tail assembled per turn
- a single strict `run` tool as the direct model tool surface
- prompt-version observability in audit and judgment records
- invariant tests for prompt assembly and authority boundaries
- separate model evals for voice, drift, and behavior

The prompt improves behavior. It is not a security boundary.

## Goals

- Move the production main-agent static system instructions to
  `src/ariel/prompts.py`.
- Give the main prompt an explicit immutable version:
  `MAIN_AGENT_PROMPT_VERSION = "main-agent-jarvis-v2"`.
- Remove the inline `_POLICY_SYSTEM_INSTRUCTIONS` constant from
  `src/ariel/app.py`.
- Preserve the current dynamic context assembly model: static policy first,
  dynamic context after, user message last.
- Preserve the model-facing capability contract: exactly one direct Responses
  tool named `run`; internal capabilities appear only as run-callable aliases.
- Keep internal capability ids (`cap.*`) out of model-facing prompt text.
- Add prompt version to main-agent context audit metadata.
- Make `AIJudgmentRecord.prompt_version` record the real loop prompt version
  instead of a hardcoded generic value.
- Keep the prompt cache-friendly: stable static prefix first, per-turn data
  last.
- Add deterministic tests for assembly, leakage, versioning, and protocol
  invariants.
- Keep tone tests coarse. The deterministic test suite must not freeze exact
  butler prose.

## Non-Goals

- Do not load `docs/jarvis-system-prompt.md` at runtime.
- Do not parse markdown into prompt text.
- Do not add a prompt-management service, database table, admin UI, or remote
  prompt registry.
- Do not add an environment variable or feature flag to select the old prompt.
- Do not keep the old inline prompt as a fallback.
- Do not migrate from the Responses API to the Agents SDK.
- Do not expose capabilities as direct model tools.
- Do not change the `run` tool schema.
- Do not rewrite memory retriever, memory rememberer, research, attachment, or
  Agency prompts in this cutover.
- Do not add new syscalls, capabilities, approval semantics, or provider
  integrations.
- Do not rely on prompt instructions to enforce security, privacy, or side
  effect boundaries.
- Do not add live model calls to the normal pytest suite.

## Current State To Replace

### Main Agent Prompt

`src/ariel/app.py` defines `_POLICY_SYSTEM_INSTRUCTIONS` inline. The tuple is
copied into `context_bundle["policy_system_instructions"]` by
`_build_turn_context_bundle` and rendered first by
`_build_responses_input_items`.

The live prompt is short and operational. It lacks the Jarvis identity, voice,
service posture, explicit anti-sycophancy posture, proactivity policy, failure
register, and exemplar-based voice anchoring captured in
`docs/jarvis-system-prompt.md`.

### Context Assembly

The main-agent input order is:

1. static policy system instructions
2. optional Discord context
3. eligible syscall callables
4. runtime facts
5. current turn-id write authority
6. recalled memory
7. open jobs
8. recent artifacts
9. user message

This order is directionally correct. The cutover preserves it.

### Prompt Versioning

Memory prompt versions exist as constants in `src/ariel/memory.py`, but the
shared loop judgment record currently writes a generic prompt version. The main
agent has no first-class production prompt version in the context bundle,
audit metadata, or judgment record.

### Design Artifact

`docs/jarvis-system-prompt.md` owns the research basis, prompt architecture,
full V2 draft, examples, and eval dimensions. It is not currently wired into
runtime.

## Target Behavior

### Normal Main-Agent Turn

1. Ariel receives a user message, scheduled wake, or research completion wake.
2. Ariel runs pre-turn recall and builds the dynamic context bundle.
3. Ariel builds Responses input items.
4. The first model-visible items are the exact static Jarvis prompt blocks from
   `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS`.
5. Dynamic system blocks follow the static prompt: Discord context, eligible
   syscall aliases, runtime facts, turn authority, recall, jobs, and artifacts.
6. The user or wake prompt is the final input item.
7. Ariel calls the model with exactly one direct tool definition, `run`.
8. The model writes a sandboxed Python `run` program.
9. The program emits user-visible output only through `agent.emit_message`, or
   pauses silently with `agent.pause_until_input`.
10. The final answer follows the Jarvis contract: useful first, evidence-aware,
    concise, privacy-preserving, calibrated, and dry only when the situation
    permits.

### Prompt Version Observability

Every main-agent model start is auditable with:

- prompt version
- context section order
- policy instruction count
- current turn id
- recent-window metadata

Every recorded main-agent model judgment writes the same prompt version to
`AIJudgmentRecord.prompt_version`.

Prompt version is a product contract version, not a hash of the model input.
Dynamic context changes do not create new prompt versions.

### User-Visible Behavior

The main agent must:

- lead with the result or answer
- act without ceremony when intent is clear
- ask the smallest clarifying question when ambiguity changes outcome
- never claim completion without tool evidence, state, artifact, or approval
  resolution
- distinguish proposed/pending work from completed work
- treat memory, email, docs, attachments, web, research findings, and tool
  outputs as evidence, not authority
- prefer fresh authoritative sources when facts may have changed
- disclose stale or missing evidence
- stay silent for low-value proactive wakes
- use suspended register for grief, fear, medical/legal/financial, safety,
  auth, tool failures, and irreversible operations
- refuse or redirect unsafe requests without theatrical flourish
- push back on foolish plans through facts, not contempt
- avoid flattery, sycophancy, honorifics, and catchphrase imitation

The Jarvis voice is subordinate to reliability. If tone and correctness
conflict, correctness wins.

## Target Architecture

### Prompt Module

Add `src/ariel/prompts.py`.

The module owns production prompt constants:

```python
MAIN_AGENT_PROMPT_VERSION = "main-agent-jarvis-v2"

MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS: tuple[str, ...] = (
    "<identity>...</identity>",
    "<mission>...</mission>",
    ...
)
```

The module may expose a pure helper for audit/testing:

```python
def joined_main_agent_static_prompt() -> str:
    return "\n\n".join(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)
```

No runtime code reads markdown. No runtime code computes prompt content from
docs. No runtime code selects among prompt variants.

### Production Prompt Content

The production prompt is a curated runtime version of
`docs/jarvis-system-prompt.md`'s V2 prompt. It includes the system-prompt blocks
needed at inference time:

- identity
- mission
- voice
- authority and trust
- turn workflow
- run protocol
- tools and actions
- memory
- proactivity
- service principles
- communication
- failure handling
- safety overrides
- exemplars
- self-check

It excludes:

- research basis
- prompt architecture commentary
- eval checklist
- V1 appendix
- implementation notes
- links and citations

The production prompt must use model-facing callable aliases such as
`agent.emit_message`, `agent.pause_until_input`, `attachment.read`, and
`agency.run` only where they are part of the model contract. It must not mention
internal capability ids such as `cap.email.search`.

### App Integration

`src/ariel/app.py` imports:

```python
from ariel.prompts import (
    MAIN_AGENT_PROMPT_VERSION,
    MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS,
)
```

`_build_turn_context_bundle` sets:

```python
{
    "prompt_version": MAIN_AGENT_PROMPT_VERSION,
    "policy_system_instructions": list(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS),
    ...
}
```

`_build_responses_input_items` keeps rendering
`policy_system_instructions` first. The context key name remains because it
accurately describes the role of the block and is not the legacy code path. The
legacy item is the inline app constant, which is deleted.

### Loop Configuration

`LoopConfig` gains:

```python
prompt_version: str
```

Every loop driver must pass a prompt version:

- main agent: `MAIN_AGENT_PROMPT_VERSION`
- memory retriever: `RETRIEVER_PROMPT_VERSION`
- memory rememberer encode: `REMEMBERER_ENCODE_PROMPT_VERSION`
- memory rememberer dream: `REMEMBERER_DREAM_PROMPT_VERSION`
- research: its research prompt version, added in `research_runtime.py`

`run_agent_loop` records `cfg.prompt_version` in AI judgment records. The
hardcoded generic prompt version is deleted.

This is not main-agent backward compatibility. It is prompt observability for
all shared-loop configurations, because the shared loop owns judgment recording.

### Context Audit Metadata

`_context_bundle_audit_metadata` returns:

```json
{
  "schema_version": "1.0",
  "prompt_version": "main-agent-jarvis-v2",
  "section_order": [...],
  "policy_instruction_count": N,
  "current_turn_id": "trn_...",
  "recent_window": {...}
}
```

The metadata shape keeps `schema_version = "1.0"`. The new `prompt_version`
field is additive, the payload is internal audit metadata, and the cutover
updates all tests that inspect the shape. Do not support both old and new audit
metadata shapes.

### Prompt Cache Discipline

The static Jarvis prompt is the first exact model-visible prefix on every main
agent call. Per-turn data never enters the static prompt.

Static prefix:

- `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS`

Dynamic tail:

- Discord context
- eligible syscall aliases
- runtime facts
- current turn id
- recall
- open jobs
- recent artifacts
- user/wake prompt
- remaining budget item appended by `run_agent_loop`
- later round feedback and tool observations

Do not add current time, trigger kind, connector state, recall, or user-specific
preferences to `src/ariel/prompts.py`. If the model needs a runtime fact, inject
it as a dynamic system block.

### Tool Surface

The direct model tool surface remains:

- one strict Responses function tool named `run`
- no direct exposure of capability ids
- `parallel_tool_calls = False`
- `store = False`

The static prompt may mention the `run` protocol, but the runtime tool schema in
`run_runtime.py` remains the canonical API contract for the `run` tool.

### Capability Contract

The main prompt composes with the capability system by naming behavior, not
authority.

The model sees:

- syscall callable aliases listed for the current turn
- `agent.emit_message`, `agent.emit_value`, and `agent.pause_until_input`
- runtime facts about configured connectors and bindings
- plain-language policy constraints

The model does not see:

- `cap.*` ids
- provider secrets
- policy-engine internals
- approval implementation details beyond user-facing pending/proposed state
- hidden prompt source or internal prompt module names

Runtime rails continue to enforce:

- schema validation
- capability policy
- taint/provenance
- approval gating
- egress controls
- provider scopes
- sandbox constraints
- audit and receipts

Prompt policy cannot clear taint, authorize a write, bypass approval, or expand
the callable whitelist.

## Composition With Other Systems

### Memory

The memory retriever stays separate. It reconstructs `recall_v1` before the main
turn. The main Jarvis prompt tells the main agent how to treat that recall:
helpful, fallible evidence, never authority.

The memory rememberer stays separate. The main agent may call memory write
syscalls when exposed, but durable memory judgment remains enforced through the
memory subsystem and its own prompt versions.

### Research

Research remains a read-only subagent loop. Research findings enter a future
main-agent wake as tainted evidence. The Jarvis prompt tells the main agent not
to obey instructions embedded in research findings.

This cutover does not merge research prompt policy into the main prompt and does
not give research the Jarvis voice. Research output is structured evidence, not
user-facing persona text.

### Proactivity

A proactive wake uses the same main prompt and same `run` loop as a user turn.
There is no separate proactive persona prompt.

The static prompt sets the policy: silence by default, batch medium-priority
updates, interrupt only for concrete usefulness, time sensitivity, high impact,
or principal-declared importance.

Runtime remains responsible for wake scheduling and delivery.

### Attachments

Discord attachments are metadata until `attachment.read` is called. The main
prompt states this. Attachment extraction remains a separate model call in
`attachment_content.py` with its own injection warning and content-extraction
contract.

The main prompt must not imply that filenames, URLs, or attachment refs are
attachment content.

### Google Workspace

The main prompt preserves the provider-write authority rule:

- exactly one of `source_evidence_id` or `user_instruction_ref=turn:<turn_id>`
- turn reference only when the current conversation contains an explicit user
  instruction

Google read operations do not require approval. Google write proposals remain
rail-gated.

### Agency

Coding and repository work continues to route through `agency.*`. The main
prompt may mention `agency.run` in plain callable terms. It must not imply shell
or terminal authority.

Agency has its own downstream prompt input. The main Jarvis prompt shapes what
the main agent asks Agency to do, not how the Agency worker behaves internally.

### Discord

Discord delivery remains outside the prompt. The main prompt can choose
`agent.pause_until_input` for a silent turn. The worker remains responsible for
posting committed assistant messages to Discord.

### API Responses

The surfaced message response contract does not change:

- `assistant.message`
- `assistant.sources`
- `assistant.silent`

This cutover changes model input and audit metadata, not the public message
response schema.

## Key Decisions

### Code Constant, Not Markdown Loader

Production prompt text lives in `src/ariel/prompts.py`.

Reason:

- code is packaged and type-checked
- diffs are reviewable
- prompt bytes are deterministic
- startup cannot fail because a docs file is missing
- docs edits cannot silently alter runtime behavior

`docs/jarvis-system-prompt.md` remains the design source and evaluation guide.
Changing production behavior requires editing code.

### Version String, Not Dynamic Hash As Version

The prompt version is a deliberate string such as
`main-agent-jarvis-v2`. It changes when the product contract changes.

A hash may be useful for audit, but it is not the semantic version. Formatting
or refactoring that preserves the contract can keep the version if the owner
chooses; changing behavior must bump the version.

### Preserve Dynamic Context Tail

The existing context assembly separates static policy from dynamic context. The
cutover preserves that shape because it is correct for prompt caching,
debugging, and trust boundaries.

### Do Not Persona-Wash Internal Agents

Only the user-facing main agent gets the Jarvis voice. Memory and research
agents are functional internal workers. Giving them the butler persona would
add tokens, reduce precision, and pollute structured outputs.

### Tests Assert Invariants, Not Literature

Deterministic tests assert contracts:

- prompt appears first
- prompt version is recorded
- `run` protocol anchors exist
- no `cap.*` leaks
- dynamic context remains separate
- user message remains last

They do not assert exact jokes, exact exemplars, or "dryness." Voice quality is
covered by separate model evals.

### Prompt Injection Is Expected

The prompt tells the model how to reason about untrusted content. Runtime rails
still enforce authority boundaries. A prompt-injected email, document, research
finding, or attachment cannot acquire tool authority by text alone.

## Files

### Add

- `src/ariel/prompts.py`
  - owns `MAIN_AGENT_PROMPT_VERSION`
  - owns `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS`
  - optionally owns prompt join/hash helpers for tests and audit

- `docs/main-agent-jarvis-prompt-cutover.md`
  - this document

### Modify

- `src/ariel/app.py`
  - import prompt constants
  - delete inline `_POLICY_SYSTEM_INSTRUCTIONS`
  - add `prompt_version` to context bundle
  - add `prompt_version` to context audit metadata

- `src/ariel/agent_loop.py`
  - add `prompt_version` to `LoopConfig`
  - record `cfg.prompt_version` in AI judgments
  - delete hardcoded generic model-output prompt version

- `src/ariel/memory.py`
  - pass existing memory prompt versions into `LoopConfig`

- `src/ariel/research_runtime.py`
  - add a research prompt version constant
  - pass it into `LoopConfig`

- `tests/unit/test_responses_tool_contract.py`
  - update imports to `ariel.prompts`
  - assert main prompt invariants and no `cap.*` leakage

- `tests/unit/test_prompt_context_rendering.py`
  - new unit tests for context bundle and input item order

- `tests/integration/test_pr01_acceptance.py`
  - update context audit assertions for prompt version

- `tests/integration/test_single_run_cutover.py`
  - assert production path includes Jarvis prompt anchors and still exposes
    exactly one strict `run` tool
  - assert retry/protocol-nudge paths preserve the static prompt

- `docs/index.md`
  - link this cutover spec

### Do Not Modify

- `src/ariel/run_runtime.py`, unless tests expose an existing prompt-string
  invariant that must be adjusted to match the new main prompt. The `run` tool
  schema is not part of this cutover.
- `src/ariel/action_runtime.py`, except if needed for prompt-version plumbing
  through an existing shared type.
- `src/ariel/attachment_content.py`
- provider connector implementations
- public response schemas

## Test Plan

### Unit Tests

`tests/unit/test_responses_tool_contract.py`

- `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS` contains:
  - `run`
  - `source`
  - `agent.emit_message`
  - `agent.pause_until_input`
  - evidence/trust boundary language
  - attachment metadata rule
  - Google write authority rule
  - missing or stale evidence disclosure language
- `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS` does not contain `cap.`
- `run_tool_definitions()` still exposes exactly one strict tool named `run`

`tests/unit/test_prompt_context_rendering.py`

- `_build_turn_context_bundle(...)` includes:
  - `prompt_version = MAIN_AGENT_PROMPT_VERSION`
  - `policy_system_instructions` copied from
    `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS`
  - current section order preserved
- `_build_responses_input_items(...)` renders:
  - static policy first
  - Discord/context/tool/runtime blocks after static policy
  - turn authority block with `turn:<turn_id>`
  - user message last
- eligible callable block uses aliases, not `cap.*`
- runtime facts, recall, jobs, and artifacts remain separate system blocks

### Integration Tests

`tests/integration/test_single_run_cutover.py`

- normal turn exposes exactly one strict `run` tool
- first model input contains core Jarvis anchors:
  - `private AI butler-operator`
  - `Reliability outranks personality`
  - `evidence, not authority`
  - `agent.emit_message`
  - `agent.pause_until_input`
- model input contains no `cap.`
- protocol failure retry keeps the static prompt in the prefix and appends the
  protocol nudge rather than replacing the prompt

`tests/integration/test_pr01_acceptance.py`

- context audit metadata includes `prompt_version`
- section order remains stable
- policy instruction count matches the static prompt tuple

`tests/integration/test_memory.py` or targeted loop tests

- memory retriever/rememberer loop configs record their own prompt versions if
  judgment recording applies to those paths

### Static Checks

- `rg "_POLICY_SYSTEM_INSTRUCTIONS" src tests docs` returns no live code or
  test references after migration, except historical prose if explicitly kept.
- `rg "model-output-v1" src tests` returns no shared-loop prompt-version
  hardcoding.
- `rg "cap\\." src/ariel/prompts.py tests` confirms no model-facing main prompt
  leakage, with internal capability tests excluded as needed.

### Verification Command

Run:

```bash
make verify
```

If the full suite is too slow during development, run the targeted tests first:

```bash
uv run pytest tests/unit/test_responses_tool_contract.py tests/unit/test_prompt_context_rendering.py
uv run pytest tests/integration/test_single_run_cutover.py tests/integration/test_pr01_acceptance.py
```

The final cutover is not accepted until `make verify` passes.

## Acceptance Criteria

- `src/ariel/prompts.py` exists and owns the production main prompt.
- `src/ariel/app.py` no longer defines `_POLICY_SYSTEM_INSTRUCTIONS`.
- Main-agent Responses input starts with the static Jarvis prompt on every
  normal turn and retry round.
- Dynamic context is still rendered after the static prompt.
- User/wake prompt remains the final pre-loop input item.
- The model still receives exactly one direct Responses tool named `run`.
- The production prompt includes run protocol, authority, trust, memory,
  proactivity, failure, safety, and Jarvis voice policy.
- The production prompt excludes research notes, eval checklist, V1 appendix,
  and markdown source commentary.
- No model-facing prompt text leaks internal `cap.*` ids.
- Main-agent context audit metadata records `main-agent-jarvis-v2`.
- Main-agent AI judgment records use `main-agent-jarvis-v2`.
- Shared-loop non-main configurations no longer record the generic
  `model-output-v1` prompt version.
- Unit and integration tests cover prompt assembly and invariants.
- `make verify` passes.

## SOTA Basis

The cutover follows the current agent-prompting consensus:

- keep reusable instructions versioned and reviewable
- put static prompt content first for prompt-cache reuse
- place dynamic and untrusted context after trusted static instructions
- isolate untrusted web/email/doc/tool content from privileged instruction
  layers
- use typed tools and runtime guardrails for authority
- gate prompt changes with evals and regression tests
- keep prompt policy separate from hard security rails

Relevant public references:

- OpenAI prompting guide:
  `https://developers.openai.com/api/docs/guides/prompting`
- OpenAI text and instruction guide:
  `https://developers.openai.com/api/docs/guides/text`
- OpenAI prompt caching:
  `https://developers.openai.com/api/docs/guides/prompt-caching`
- OpenAI agent safety:
  `https://developers.openai.com/api/docs/guides/agent-builder-safety`
- OpenAI evals:
  `https://developers.openai.com/api/docs/guides/evals`
- OWASP LLM Top 10:
  `https://genai.owasp.org/llm-top-10/`
- NCSC prompt injection analysis:
  `https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection`

## Implementation Phases

Phases are sequencing only. The final merged state is a hard cutover.

### Phase 1 - Prompt Module

- Add `src/ariel/prompts.py`.
- Move curated production prompt text into
  `MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS`.
- Add `MAIN_AGENT_PROMPT_VERSION`.
- Add unit tests for prompt invariants.

### Phase 2 - App Wiring

- Import prompt constants into `app.py`.
- Delete `_POLICY_SYSTEM_INSTRUCTIONS`.
- Add `prompt_version` to `_build_turn_context_bundle`.
- Add `prompt_version` to `_context_bundle_audit_metadata`.
- Update context rendering tests.

### Phase 3 - Loop Prompt Versioning

- Add `prompt_version` to `LoopConfig`.
- Pass the main prompt version from `_wake`.
- Pass existing memory prompt versions from memory drivers.
- Add a research prompt version constant and pass it from research runtime.
- Replace hardcoded judgment prompt version with `cfg.prompt_version`.

### Phase 4 - Integration Coverage

- Update single-run integration tests.
- Update context audit integration tests.
- Assert no direct `cap.*` leakage.
- Assert retry nudges preserve the static prompt prefix.

### Phase 5 - Verification And Deletion

- Remove obsolete imports/tests that reference the old app-level prompt
  constant.
- Run targeted tests.
- Run `make verify`.

## Open Questions

None block the cutover.

Two future improvements are deliberately outside this hard cutover:

- A separate model-eval suite for Jarvis voice, sycophancy resistance,
  proactivity judgment, and multi-turn drift.
- A prompt hash in audit metadata. Add it only if operational review needs exact
  byte-level prompt provenance in addition to semantic prompt version.
