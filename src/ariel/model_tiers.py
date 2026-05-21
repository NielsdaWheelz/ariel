from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModelTier(StrEnum):
    MAIN = "main"
    BULK = "bulk"
    STRUCTURED = "structured"
    CODING = "coding"
    VISION = "vision"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class TierBinding:
    provider: str
    model: str
    max_context_tokens: int
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
        supports_structured_output=True,
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

_SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic", "google", "openrouter", "cloudflare"})


def parse_tier_override(value: str) -> tuple[str, str]:
    """Parse a ``"<provider>:<model>"`` override string.

    Raises ``ValueError`` with a clear message on malformed input.
    """
    provider, sep, model = value.partition(":")
    if not sep or not provider or not model:
        raise ValueError(f"tier override must be '<provider>:<model>' (got {value!r})")
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported provider {provider!r}; expected one of {sorted(_SUPPORTED_PROVIDERS)}"
        )
    return provider, model


def resolve_tier(tier: ModelTier, override: str | None) -> TierBinding:
    """Return the binding for ``tier``, applying ``override`` if set.

    An override preserves the default tier's capability flags and context
    window — the override string only changes provider+model. Operators
    overriding to a model with different capabilities are responsible for
    matching the tier's role.
    """
    base = DEFAULT_TIERS[tier]
    if override is None:
        return base
    provider, model = parse_tier_override(override)
    return TierBinding(
        provider=provider,
        model=model,
        max_context_tokens=base.max_context_tokens,
        supports_tools=base.supports_tools,
        supports_structured_output=base.supports_structured_output,
        supports_vision=base.supports_vision,
    )
