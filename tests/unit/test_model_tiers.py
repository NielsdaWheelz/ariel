from __future__ import annotations

import pytest

from ariel.model_tiers import DEFAULT_TIERS, ModelTier, parse_tier_override, resolve_tier


def test_default_tiers_cover_every_member() -> None:
    assert set(DEFAULT_TIERS.keys()) == set(ModelTier)


def test_parse_tier_override_returns_provider_and_model() -> None:
    assert parse_tier_override("openai:gpt-5.4") == ("openai", "gpt-5.4")
    assert parse_tier_override("openrouter:deepseek/deepseek-chat-v4") == (
        "openrouter",
        "deepseek/deepseek-chat-v4",
    )
    assert parse_tier_override("cloudflare:@cf/baai/bge-m3") == (
        "cloudflare",
        "@cf/baai/bge-m3",
    )


@pytest.mark.parametrize(
    "bad",
    ["", "openai", ":gpt-5.4", "openai:", "openai gpt-5.4"],
)
def test_parse_tier_override_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match="tier override"):
        parse_tier_override(bad)


def test_parse_tier_override_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        parse_tier_override("xai:grok")


def test_resolve_tier_without_override_returns_default() -> None:
    assert resolve_tier(ModelTier.MAIN, None) is DEFAULT_TIERS[ModelTier.MAIN]


def test_resolve_tier_with_override_swaps_provider_and_model_only() -> None:
    default = DEFAULT_TIERS[ModelTier.BULK]
    resolved = resolve_tier(ModelTier.BULK, "openrouter:deepseek/deepseek-chat-v4")

    assert resolved.provider == "openrouter"
    assert resolved.model == "deepseek/deepseek-chat-v4"
    # Capability flags + context window track the tier role, not the override.
    assert resolved.max_context_tokens == default.max_context_tokens
    assert resolved.supports_tools == default.supports_tools
    assert resolved.supports_structured_output == default.supports_structured_output
    assert resolved.supports_vision == default.supports_vision


def test_resolve_tier_rejects_bad_override() -> None:
    with pytest.raises(ValueError):
        resolve_tier(ModelTier.MAIN, "not-a-spec")
