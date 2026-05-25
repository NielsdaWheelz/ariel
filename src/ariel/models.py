"""Concrete model references used by the adapter.

A ``ModelRef`` is the (provider, model) pair the adapter needs to dispatch
a call. Callsites pick a named constant — ``MAIN``, ``RESEARCH``, ``VISION``,
``EMBEDDING`` — rather than naming a provider directly, so that swapping the
model used by a role is a one-line edit here. There is no env override: model
identity is part of the code, not deployment config.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

Provider = Literal["openai", "anthropic", "google", "openrouter", "cloudflare"]


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Provider
    model: str


# Conversational loop: strong default for tool-heavy assistant turns.
MAIN: Final[ModelRef] = ModelRef(provider="openrouter", model="openai/gpt-5.5")

# Read-only research loop: high-volume investigation with a cheaper reasoning model.
RESEARCH: Final[ModelRef] = ModelRef(provider="openrouter", model="deepseek/deepseek-v3.2")

# Vision: attachment OCR and image understanding.
VISION: Final[ModelRef] = ModelRef(provider="google", model="gemini-2.5-flash")

# Embeddings: memory recall and search index.
EMBEDDING: Final[ModelRef] = ModelRef(provider="openai", model="text-embedding-3-small")

PROVIDER_REQUIRED_ENV_VARS: Final[dict[Provider, tuple[str, ...]]] = {
    "anthropic": ("ARIEL_ANTHROPIC_API_KEY",),
    "cloudflare": ("ARIEL_CLOUDFLARE_API_TOKEN", "ARIEL_CLOUDFLARE_ACCOUNT_ID"),
    "google": ("ARIEL_GOOGLE_API_KEY",),
    "openai": ("ARIEL_OPENAI_API_KEY",),
    "openrouter": ("ARIEL_OPENROUTER_API_KEY",),
}


def required_model_provider_env_vars(
    refs: tuple[ModelRef, ...] = (MAIN, RESEARCH, VISION, EMBEDDING),
) -> tuple[str, ...]:
    required: list[str] = []
    for ref in refs:
        for env_name in PROVIDER_REQUIRED_ENV_VARS[ref.provider]:
            if env_name not in required:
                required.append(env_name)
    return tuple(required)
