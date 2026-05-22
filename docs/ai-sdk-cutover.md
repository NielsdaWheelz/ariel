# Spec — Ariel Model Adapter & Tiered Inference

## 1. Mission

Replace Ariel's bespoke OpenAI Responses HTTP adapter with a **thin typed adapter over Pydantic AI's per-provider Models**, expose a **single tier-shaped call contract** (`tier → ModelCall → ModelResponse`) that every subsystem in the codebase routes through, and make **conversation history stateless and caller-owned** throughout. Hard cutover: no shim, no legacy class kept around, no compatibility branch in `agent_loop`. After cutover, swapping the bulk-tier model from GPT to DeepSeek is a one-line config change with no code edits.

## 2. Target final state

When the work is done, running Ariel looks like this:

- `app.py` instantiates exactly **one** `ModelAdapter` and wires it into every subsystem. No more `_build_default_model_adapter` returning a hand-rolled HTTP client.
- The agent loop, research runtime, memory subagents, ai_judgments, and attachment processing all call `await adapter.call(tier=..., messages=..., tools=..., response_format=...)`. Nothing else.
- Each call carries its **full conversation history** as `list[ModelMessage]`. There is no `previous_response_id`, no server-side cursor, no per-session statefulness anywhere in the model layer.
- Each call names its **tier** (`MAIN`, `BULK`, `STRUCTURED`, `CODING`, `VISION`, `EMBEDDING`). The adapter resolves tier → concrete `provider:model` via a single Python config object, instantiates the right Pydantic AI `Model` once at startup, and dispatches.
- Switching tiers' models is done by editing one config file and restarting. There is no DB row, no `/admin` endpoint, no runtime mutation surface.
- A single failure (network error, 5xx, 429 past retry budget) **raises**. No model-tier fallback chain, no circuit breaker, no provider failover. The agent loop's existing `model_failed` exhaustion path catches it.
- Telemetry events (`evt.model.started/completed/failed`) continue to fire with `provider`, `model`, `tier`, token usage, latency. Their schema does not change. (This is the only "side rail" we keep — it already exists.)

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Subsystems                                                 │
│  • agent_loop          (tier=MAIN)                          │
│  • research_runtime    (tier=BULK)                          │
│  • memory.rememberer   (tier=BULK)                          │
│  • memory.retriever    (tier=BULK)                          │
│  • memory.dreamer      (tier=BULK)                          │
│  • ai_judgments.*      (tier=STRUCTURED or BULK)            │
│  • attachment_content  (tier=VISION)                        │
│                                                              │
│              ▼  await adapter.call(ModelCall)               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ ArielModelAdapter (src/ariel/model_adapter.py)         │ │
│  │   • tier → pydantic_ai.Model resolver (built at boot)  │ │
│  │   • Ariel ModelCall → pydantic_ai.ModelRequest         │ │
│  │   • pydantic_ai.ModelResponse → Ariel ModelResponse    │ │
│  │   • emits evt.model.* via injected event sink          │ │
│  └────────────────────────────────────────────────────────┘ │
│              ▼                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ Pydantic AI Models (substrate)                         │ │
│  │   • OpenAIResponsesModel  (gpt-5.4, gpt-5.4-mini, ...)  │ │
│  │   • AnthropicModel        (claude-sonnet-4-6, ...)     │ │
│  │   • GoogleModel           (gemini-2.5-flash-lite, ...) │ │
│  │   • OpenAIModel + base_url= OpenRouter (DeepSeek/GLM)  │ │
│  └────────────────────────────────────────────────────────┘ │
│              ▼                                               │
│  Provider HTTPS endpoints                                   │
└─────────────────────────────────────────────────────────────┘
```

Three layers, one direction of dependency:

1. **Substrate** — Pydantic AI per-provider `Model` classes. Owned by the pydantic team. Speaks each provider's native wire format. Single point of upgrade when providers change shapes.
2. **Contract** — `ArielModelAdapter` and the `ModelCall` / `ModelResponse` pydantic models. Owned by Ariel. ~200 lines. The stable surface every subsystem depends on.
3. **Subsystems** — call sites. Each names a tier; everything else is identical.

## 4. Capability contract

```python
# src/ariel/model_tiers.py
from enum import StrEnum

class ModelTier(StrEnum):
    MAIN       = "main"        # agent loop, tool use, planning
    BULK       = "bulk"        # summarization, consolidation, judgments at scale
    STRUCTURED = "structured"  # pydantic-schema extraction
    CODING     = "coding"      # code edits, code reasoning
    VISION     = "vision"      # multimodal input
    EMBEDDING  = "embedding"   # retrieval embeddings (separate dispatch path)
```

```python
# src/ariel/model_adapter.py
from typing import Protocol, Any
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelMessage  # use pydantic-ai's message type directly

class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

class ReasoningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effort: Literal["minimal", "low", "medium", "high"] | None = None
    max_thinking_tokens: int | None = None

class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0

class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: str
    name: str
    arguments: dict[str, Any]

class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: ModelTier
    messages: list[ModelMessage]               # full history, caller-owned
    tools: list[ToolSpec] = []
    tool_choice: Literal["auto", "required", "none"] = "auto"
    response_format: type[BaseModel] | None = None   # for STRUCTURED tier
    reasoning: ReasoningConfig | None = None
    max_output_tokens: int | None = None
    metadata: dict[str, Any] = {}              # opaque, surfaces in telemetry

class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None
    tool_calls: list[ToolCall]
    structured_output: BaseModel | None        # populated iff response_format set
    reasoning_summary: str | None
    usage: TokenUsage
    provider: str                              # e.g. "openai"
    model: str                                 # e.g. "gpt-5.4"
    tier: ModelTier
    latency_ms: int
    raw_response_id: str | None                # provider-side id, for trace correlation only

class ModelAdapter(Protocol):
    async def call(self, request: ModelCall) -> ModelResponse: ...
    async def embed(self, texts: list[str], tier: ModelTier = ModelTier.EMBEDDING) -> list[list[float]]: ...
```

**Invariant.** `ModelCall` is the **only** way to invoke a model in Ariel. Subsystems do not import `pydantic_ai`, do not import `openai`, do not import `anthropic`, do not import `httpx` for model calls. They import `ModelAdapter`, `ModelCall`, `ModelTier`.

## 5. Tier mapping (one model per tier, period)

```python
# src/ariel/model_tiers.py (continued)

@dataclass(frozen=True)
class TierBinding:
    provider: str           # "openai" | "anthropic" | "google" | "openrouter"
    model: str              # provider-native id
    max_context_tokens: int # advisory, used by the agent loop's context packer
    supports_tools: bool
    supports_structured_output: bool
    supports_vision: bool

DEFAULT_TIERS: dict[ModelTier, TierBinding] = {
    ModelTier.MAIN: TierBinding(
        provider="anthropic",
        model="claude-sonnet-4-6",
        max_context_tokens=200_000,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=True,
    ),
    ModelTier.BULK: TierBinding(
        provider="google",
        model="gemini-2.5-flash-lite",
        max_context_tokens=1_000_000,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=True,
    ),
    ModelTier.STRUCTURED: TierBinding(
        provider="openai",
        model="gpt-5.4-mini",
        max_context_tokens=400_000,
        supports_tools=True,
        supports_structured_output=True,  # strict mode
        supports_vision=False,
    ),
    ModelTier.CODING: TierBinding(
        provider="openai",
        model="gpt-5.3-codex",
        max_context_tokens=400_000,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=False,
    ),
    ModelTier.VISION: TierBinding(
        provider="google",
        model="gemini-3-flash",
        max_context_tokens=1_000_000,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=True,
    ),
    ModelTier.EMBEDDING: TierBinding(
        provider="openai",
        model="text-embedding-3-small",
        max_context_tokens=8_192,
        supports_tools=False,
        supports_structured_output=False,
        supports_vision=False,
    ),
}
```

Tiers can be **overridden by env var** at startup (one env var per tier; see §8). No DB. No runtime mutation. To change a tier in production: edit env, restart.

## 6. The rules (invariants — enforced by code, not convention)

1. **One adapter instance per process.** Constructed in `app.py` once, passed by reference to every subsystem. No globals, no late binding.
2. **Stateless conversation.** Every `ModelCall` carries full `messages`. The adapter never reads or writes any per-conversation server-side state. `previous_response_id` is forbidden. (Enforced: it's not on the request type.)
3. **One model per tier.** No fallback list, no chain. If the tier's model fails, the call raises. The agent loop's existing exhaustion path handles it.
4. **No retries inside the adapter beyond Pydantic AI's defaults.** Pydantic AI does sensible exponential backoff on connection errors; we accept those defaults and do not add a custom retry loop. A 5xx that survives Pydantic AI's retry is a hard failure for the call.
5. **Telemetry is mandatory.** Every call emits `evt.model.started` before dispatch and `evt.model.completed` or `evt.model.failed` after. The event sink is injected at construction; the adapter doesn't import the event bus directly.
6. **Tiers are typed enums, not strings.** No `tier="bluk"` typos that ship. mypy strict catches this.
7. **No mid-call mutation of `ModelCall`.** It's `frozen` via pydantic `ConfigDict(frozen=True)` (add to ConfigDict above).
8. **Embeddings get their own method (`embed`).** Different shape, different contract, same adapter object. Do not shoehorn into `call`.
9. **The adapter does not parse tool-call arguments against tool schemas.** That's the agent loop's job (it already does this in `process_one_call`). The adapter delivers `ToolCall` objects with raw `arguments: dict`.
10. **Structured output validation lives in the adapter.** If `response_format=` is set, the adapter validates the response against that pydantic model before returning. Validation failure → raises `ResponseContractViolation` (existing exception type). Caller sees a typed object or an exception, never a hallucinated string.

## 7. Composition with existing subsystems

| Subsystem                                   | Current call shape                   | New call shape                                                                                                  | Tier                                                               |
| ------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `agent_loop.py` (the main loop)             | `model_adapter.create_response(...)` | `await adapter.call(ModelCall(tier=MAIN, messages=..., tools=..., reasoning=ReasoningConfig(effort="medium")))` | `MAIN`                                                             |
| `research_runtime.py`                       | same                                 | `await adapter.call(ModelCall(tier=BULK, messages=..., response_format=ResearchFindings))`                      | `BULK` for synthesis, `STRUCTURED` for the final report            |
| `memory.py` rememberer                      | direct `openai` call (probably)      | `await adapter.call(ModelCall(tier=BULK, messages=..., response_format=MemoryFactCandidate))`                   | `BULK`                                                             |
| `memory.py` retriever                       | same                                 | `await adapter.call(ModelCall(tier=BULK, ...))`                                                                 | `BULK`                                                             |
| `memory.py` dreamer                         | same                                 | `await adapter.call(ModelCall(tier=BULK, ...))`                                                                 | `BULK`                                                             |
| `ai_judgments.py` (each judgment)           | same                                 | `await adapter.call(ModelCall(tier=STRUCTURED, messages=..., response_format=<JudgmentType>))`                  | `STRUCTURED` (default) or `BULK` (for high-volume cheap judgments) |
| `attachment_content.py` (image/PDF parsing) | OpenAI vision SDK                    | `await adapter.call(ModelCall(tier=VISION, messages=..., response_format=AttachmentExtraction))`                | `VISION`                                                           |

The `model_adapter` parameter on `run_agent_loop` and `run_research` already exists structurally — only the call shape inside changes. Subsystems that today hold their own client (memory, attachments) lose that client and take the adapter as a constructor arg.

**Tier selection is per-call-site, not per-step.** I considered allowing the agent loop's next step to pick a different tier (Vercel's `prepareStep` model) but that's premature for one user — call sites are fixed enough that hardcoded tiers per subsystem capture the intent. If we want this later, `MAIN` can accept a `tier_override` arg on `run_agent_loop`; it's not in scope now.

## 8. Configuration

`config.py` adds these settings (and **only** these, related to models):

```python
# Provider auth (existing settings stay; renamed for clarity)
openai_api_key: SecretStr | None
anthropic_api_key: SecretStr | None
google_api_key: SecretStr | None
openrouter_api_key: SecretStr | None

# Optional upstream override — if set, OpenAI-compatible calls go here.
# Set to "https://ai-gateway.vercel.sh/v1" to route via Vercel AI Gateway.
# Set to "https://openrouter.ai/api/v1" to route OpenAI-shape calls via OpenRouter.
# Default unset → direct to provider.
openai_base_url: str | None = None

# Per-tier overrides. Format: "<provider>:<model>".
# Examples: "openai:gpt-5.4", "anthropic:claude-sonnet-4-6", "openrouter:deepseek/deepseek-chat-v4"
model_tier_main: str | None = None
model_tier_bulk: str | None = None
model_tier_structured: str | None = None
model_tier_coding: str | None = None
model_tier_vision: str | None = None
model_tier_embedding: str | None = None
```

Resolution order at startup:
1. If `model_tier_<x>` is set, parse it and override `DEFAULT_TIERS[<x>]`.
2. For each tier in the resolved table, look up the matching provider creds. Missing creds → startup error with a clear message ("tier BULK requires google_api_key").
3. Instantiate one Pydantic AI `Model` per tier, store in a `dict[ModelTier, Model]` on the adapter.

**No `.env` reload, no SIGHUP, no hot-swap.** Change config → restart.

## 9. Files

**New:**
- `src/ariel/model_tiers.py` — `ModelTier` enum, `TierBinding`, `DEFAULT_TIERS`, env-resolution.
- `src/ariel/model_adapter.py` — `ArielModelAdapter` (the only `ModelAdapter` implementation), `ModelCall`, `ModelResponse`, `ToolSpec`, `ToolCall`, `TokenUsage`, `ReasoningConfig`.
- `tests/unit/test_model_adapter.py` — replaces the old test file. Covers: tier resolution from env, ModelCall validation, telemetry event emission, structured-output validation (success + failure), tool-call extraction, embedding dispatch, mypy-strict typing of the public surface.
- `tests/unit/test_model_tiers.py` — config parsing, default fallback when env unset, error on bad format.

**Edited:**
- `src/ariel/app.py` — delete the inline `OpenAIResponsesAdapter` class (lines ~437-555) and the helper at line 823. Replace with a one-line `adapter = ArielModelAdapter.from_settings(settings, event_sink=...)`.
- `src/ariel/agent_loop.py` — change `model_adapter.create_response(...)` to `await model_adapter.call(ModelCall(tier=ModelTier.MAIN, ...))`. Update type hints from `Any` to `ModelAdapter`. Drop the structural comment.
- `src/ariel/research_runtime.py` — same migration, `tier=BULK`.
- `src/ariel/memory.py` — three subagents migrate. Each takes the adapter at construction.
- `src/ariel/ai_judgments.py` — each judgment function takes adapter + emits its own ModelCall with the right tier.
- `src/ariel/attachment_content.py` — vision tier.
- `src/ariel/config.py` — new settings as in §8.
- `src/ariel/response_contracts.py` — no changes; `ResponseContractViolation` is reused.
- `pyproject.toml` — add `pydantic-ai = "^1.99"`, `anthropic = "^0.X"` if not already there, `google-genai = "^X"` ditto. (OpenAI is presumably already a dep.) Remove any unused HTTP-client dependencies that were only there for the old adapter.

**Deleted:**
- `tests/unit/test_openai_responses_adapter.py` — its scope is subsumed by `test_model_adapter.py`. Do not rename it; delete and write fresh.
- The inline HTTP adapter class in `app.py`. Do not leave it commented out, do not leave a `# removed` stub. Delete and move on.
- Any `*_response_id` plumbing for OpenAI server-side conversation state if it exists. Stateless cutover.

## 10. Acceptance criteria

A reviewer should be able to verify all of these:

1. **No subsystem imports a provider SDK.** `grep -r "import openai\|import anthropic\|import google" src/ariel/` returns exactly two hits, both in `model_adapter.py`. Anything else is a regression.
2. **No subsystem imports `httpx` to call a model.** `grep -r "httpx" src/ariel/` returns nothing on the model-calling path.
3. **`make verify` (or whatever the existing CI command is) passes** — type check, lint, unit, integration.
4. **The agent loop runs end-to-end** against a live model with the new adapter, completing at least one tool-using turn. (Manual; documented in PR description.)
5. **A tier swap requires zero code changes.** Demonstrated by setting `MODEL_TIER_BULK=openrouter:deepseek/deepseek-chat-v4` in `.env` and running research without editing source.
6. **Structured-output validation fails loudly.** A test inserts a response_format and forces the model to emit invalid JSON; the adapter raises `ResponseContractViolation`, not a malformed `ModelResponse`.
7. **Telemetry events match the existing schema.** `evt.model.started/completed/failed` events parse against `SurfaceEventType` in `response_contracts.py` exactly as they did before.
8. **`ModelCall` is `frozen`.** Mutation raises `pydantic.ValidationError`.
9. **The adapter handles all six tiers in tests.** Each tier has at least one happy-path test that exercises the full request → response path against a Pydantic AI test model.
10. **No `previous_response_id` anywhere in the codebase.** `grep -r "previous_response_id\|response.id.*conversation" src/` returns nothing.

## 11. Non-goals (explicit)

The following are **out of scope** for this work. If a reviewer flags them as missing, the answer is "intentional, see spec §11":

- DB-backed provider/model registry. (Config object only.)
- Per-tier fallback chains. (One model per tier; failure raises.)
- Circuit breakers, health probes (passive or active), provider liveness tracking.
- Daily/monthly spend caps, cost budgets, kill switches.
- Per-user / per-tenant anything (Ariel is single-user).
- Runtime model-switch control surface (no admin endpoint, no CLI command to swap tiers without restart).
- Active conversation hot-swap mid-step. (We restart; calls in-flight raise.)
- Multi-agent orchestration. The adapter exposes one call shape, not an agent framework.
- OpenInference / OTEL instrumentation. The existing `evt.model.*` event schema is what we already have; we keep it; we do not add a parallel OTEL layer.
- Inspect/DeepEval evaluation harness wiring. Adopt later if needed; not in this work.
- Local-tier (vLLM, MLX, Ollama). The `LOCAL` tier is not defined and no local-serving code is added. When we want it, we add a `LOCAL` tier and another Pydantic AI Model binding — the contract supports it without changes.
- Vercel AI Gateway as a required dependency. It's a one-env-var optional upstream; the adapter works fine pointing direct.
- A `/model/status` API endpoint, a Discord command to list models, an operator UI panel. None of these.
- Migrating the OpenAI Responses surface to something else. We keep using Responses on OpenAI calls (cache wins) but stateless.

## 12. Cutover plan

One PR per phase. Each phase ends with `make verify` green and the system functionally identical from the user's POV (except where the phase explicitly changes behavior).

**Phase 1 — Land the new contract, dark.**
Add `model_tiers.py` and `model_adapter.py`. New code only; not wired up anywhere. Full unit-test coverage. Existing system still uses the old HTTP adapter. Goal: review the contract surface in isolation.

**Phase 2 — Cut over `agent_loop` and `app.py`.**
Replace `_build_default_model_adapter` with `ArielModelAdapter`. Update `agent_loop.py`'s call sites. Delete the old `OpenAIResponsesAdapter` class. Delete `test_openai_responses_adapter.py`. After this PR, the main loop runs on the new adapter and nothing else does. Verify end-to-end manually.

**Phase 3 — Migrate `research_runtime`, `memory`, `ai_judgments`, `attachment_content`.**
Each subsystem migrated in one commit (or one PR if they're independent). Tier per the table in §7. Drop any direct provider SDK imports.

**Phase 4 — Lock invariants.**
Add the grep-based invariant tests (acceptance criteria 1, 2, 10) as a CI check. Add the `ModelCall` frozen check. Final pass: search for any leftover `httpx` model code, any leftover `previous_response_id`, any leftover `OpenAIResponsesAdapter` references in docs or tests.

Each phase is independently revertable. Phase 1 alone is harmless. Phase 2 is the only one with user-visible behavior change; if it breaks production, revert just that PR.

## 13. Key decisions (with rationale)

- **Pydantic AI's per-provider `Model` classes as substrate, used via `Model.request()`, not via their `Agent` class.** Their `Agent` is opinionated about loops, retries, message graphs — we have our own loop. `Model.request()` is the minimal contract: messages in, response out. We keep all orchestration.
- **No tier-fallback chain.** A single-user prototype with one developer cannot afford the test surface of fallback semantics. The agent loop already has `model_failed` as a terminal state; that's our fallback. Hard fail, loud, see it in dev, fix it.
- **Stateless conversation history.** Locking the contract to a stateless shape now is the single biggest provider-portability win. The agent loop already maintains its own conversation history in DB; sending it on each call is a few KB of overhead.
- **Embeddings get their own method.** Different inputs (`list[str]`), different output (`list[list[float]]`), different cost shape, no reasoning/tools/structured-output knobs. Forcing them through `call` is bad typing.
- **One adapter instance, dependency-injected.** No globals. The adapter is part of every subsystem's constructor. This is how the existing code already passes `model_adapter` around; we preserve the pattern.
- **Config in env vars, not DB.** Roadmap §11 says "explicit runtime state" — for one user, env is "explicit runtime state." Restart is acceptable. DB introduces migration, admin UI, race conditions, schema versioning. None of that earns its keep yet.
- **`MODEL_TIER_<X>="<provider>:<model>"` string format.** Single env var per tier. Parses trivially. No YAML, no nested config, no overlay system. If you want to set a base_url override, that's a separate env var.
- **Telemetry stays on the existing `evt.model.*` event schema.** Not adding OTEL alongside. The event schema already carries what we need; adding a second layer means two things to keep in sync. If we want OTEL later, we add a translator from the event schema; we do not instrument twice.
- **Vision is a tier, not a flag.** Mixing vision capability into MAIN's TierBinding would force MAIN to a vision-capable model even when most calls don't need it. Separate tier, separate model, called only from attachment processing.

## 14. Risks

- **Pydantic AI version churn.** v1.x is stable but they have shipped breaking changes between minors. Pin to a known-good (`^1.99` with a manual upgrade cadence) and run their test suite against ours on upgrade. Mitigation: the adapter is one file; if Pydantic AI breaks badly, swap it for direct SDK calls — same contract, ~300 lines more code.
- **OpenAI Responses-vs-Chat semantics across providers.** Pydantic AI's `OpenAIResponsesModel` is Responses-native; `OpenAIModel` is Chat Completions; `AnthropicModel` is Anthropic Messages; `GoogleModel` is Gemini. We rely on Pydantic AI's translation to make tools and structured output behave the same across all of them. There will be edge cases — extended thinking quirks, tool-result formatting, parallel-tool-call differences. Mitigation: structured-output validation in the adapter catches the worst class of these and turns them into typed exceptions, not silent corruption.
- **Pydantic AI `ModelMessage` becomes the contract leak.** We use Pydantic AI's `ModelMessage` directly rather than wrapping it. If we want to swap Pydantic AI for something else later, every caller knows that type. Acceptable trade for now; wrapping adds ~100 lines of boilerplate for a swap we may never do.
- **`make verify` may not cover the new structured-output paths.** Phase 4's invariant tests close that gap; until they land, treat the new code as "tested but not enforced."
