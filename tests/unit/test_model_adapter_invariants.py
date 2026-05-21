"""Spec invariants for the model-adapter cutover (docs/ai-sdk-cutover.md §10).

The adapter is the *only* place where provider SDKs are touched and where
HTTP traffic to model providers originates. These tests guard the boundary
by scanning ``src/ariel/`` source. Each assertion mirrors one acceptance
criterion in the spec; if a future change widens the surface, the failing
test forces an explicit acknowledgement here rather than silent leakage.
"""

from __future__ import annotations

import re
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[2] / "src" / "ariel"


def _src_files() -> list[Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _grep(pattern: str) -> list[tuple[Path, int, str]]:
    rx = re.compile(pattern)
    hits: list[tuple[Path, int, str]] = []
    for path in _src_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if rx.search(line):
                hits.append((path.relative_to(_PKG_DIR.parents[1]), lineno, line))
    return hits


def test_no_provider_sdk_imports_in_subsystems() -> None:
    """Acceptance §10.1 — provider SDKs (openai / anthropic / google-genai) are
    routed exclusively through ``ariel.model_adapter``. pydantic-ai pulls the
    real SDKs as transitive dependencies; subsystems never import them.
    """
    hits = _grep(
        r"^\s*(from openai|import openai|from anthropic|import anthropic|from google\.genai|import google\.genai)\b"
    )
    assert hits == [], f"unexpected provider-SDK imports: {hits}"


def test_no_stateful_conversation_cursor_anywhere() -> None:
    """Acceptance §10.10 — conversation history is caller-owned and stateless.
    ``previous_response_id`` is the OpenAI Responses server-side cursor; we
    re-send full history every call instead. ``provider_response_id`` is the
    telemetry id of a *single* response and remains in the event schema.
    """
    hits = _grep(r"\bprevious_response_id\b")
    assert hits == [], f"stateful conversation cursor reintroduced: {hits}"


def test_httpx_to_model_providers_is_only_the_audio_exception() -> None:
    """Acceptance §10.2 — no subsystem calls a model provider via ``httpx``.
    The single documented exception is ``attachment_content._extract_with_openai_audio``
    (pydantic-ai 1.99 ships no STT contract; see ``justify-direct-httpx-audio``).
    Everything else hits providers through ``ModelAdapter``.
    """
    files_with_httpx = {hit[0].as_posix() for hit in _grep(r"^\s*import httpx\b")}
    # Non-model uses of httpx (URL fetching, RPC clients, OAuth) are allowed.
    allowed = {
        "src/ariel/agency_daemon.py",  # Unix-socket RPC to the agency daemon
        "src/ariel/attachment_content.py",  # URL fetcher + audio-STT exception
        "src/ariel/capability_registry.py",  # web extract, maps, weather
        "src/ariel/discord_bot.py",  # Discord REST
        "src/ariel/google_connector.py",  # Google OAuth + Workspace APIs
        "src/ariel/worker.py",  # Discord delivery
    }
    leaks = files_with_httpx - allowed
    assert not leaks, f"httpx introduced in unexpected module(s): {leaks}"

    # Within attachment_content, the only ``api.openai.com`` URL is the audio
    # endpoint — the vision path now flows through ``ModelAdapter``.
    non_audio_openai = [
        hit for hit in _grep(r"https://api\.openai\.com/") if "audio/transcriptions" not in hit[2]
    ]
    assert non_audio_openai == [], f"unexpected direct OpenAI URLs: {non_audio_openai}"
