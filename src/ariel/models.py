"""Concrete model references used by the adapter.

A ``ModelRef`` is the (provider, model) pair the adapter needs to dispatch
a call. Callsites pick a named constant — ``MAIN``, ``VISION``,
``EMBEDDING`` — rather than naming a provider directly, so that swapping
the model used by a role is a one-line edit here. There is no env
override: model identity is part of the code, not deployment config.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

Provider = Literal["openai", "anthropic", "google", "openrouter", "cloudflare"]


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Provider
    model: str


# Conversational tier: the main agent loop and downstream subloops.
MAIN: Final[ModelRef] = ModelRef(provider="anthropic", model="claude-sonnet-4-6")

# Vision: attachment OCR and image understanding.
VISION: Final[ModelRef] = ModelRef(provider="google", model="gemini-3-flash")

# Embeddings: memory recall and search index.
EMBEDDING: Final[ModelRef] = ModelRef(provider="openai", model="text-embedding-3-small")
