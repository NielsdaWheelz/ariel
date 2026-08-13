"""Pinned llm-tools consumer contract."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json

from llm_tools import BraveSearchProvider, WebSearchRequest, WebSearchResponse


_LLM_TOOLS_SHA = "667e5121268189d6fe1202c244d5ce64e8b096d1"


def test_llm_tools_exact_source_public_facade_and_legacy_hard_cut() -> None:
    distribution = importlib.metadata.distribution("llm-tools")
    assert distribution.version == "0.1.0"

    direct_url = distribution.locate_file("llm_tools-0.1.0.dist-info/direct_url.json")
    source = json.loads(direct_url.read_text())
    assert source == {
        "url": "https://github.com/NielsdaWheelz/llm-tools.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": _LLM_TOOLS_SHA,
            "requested_revision": _LLM_TOOLS_SHA,
        },
    }

    # Ariel consumes only the package facade; the provider execution contract is
    # exercised hermetically in test_search_web_uses_llm_tools_public_brave_provider.
    assert BraveSearchProvider.__module__.startswith("llm_tools.")
    assert WebSearchRequest.__module__.startswith("llm_tools.")
    assert WebSearchResponse.__module__.startswith("llm_tools.")

    assert importlib.util.find_spec("web_search_tool") is None
    try:
        importlib.metadata.distribution("web-search-tool")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("legacy web-search-tool distribution remains installed")
