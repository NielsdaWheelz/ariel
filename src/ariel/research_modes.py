from __future__ import annotations

from typing import Literal


ResearchMode = Literal["web", "personal", "memories"]
_RESEARCH_MODES: tuple[ResearchMode, ...] = ("web", "personal", "memories")
RESEARCH_MODE_VALUES: frozenset[ResearchMode] = frozenset(_RESEARCH_MODES)
