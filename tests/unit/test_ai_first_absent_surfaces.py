from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PATHS = ("src/ariel", "alembic/versions")

AI_JUDGMENT_FAILURE_CODES = {
    "E_AI_JUDGMENT_REQUIRED",
    "E_AI_JUDGMENT_CREDENTIALS",
    "E_AI_JUDGMENT_TIMEOUT",
    "E_AI_JUDGMENT_INVALID_JSON",
    "E_AI_JUDGMENT_SCHEMA",
    "E_AI_JUDGMENT_VALIDATION",
    "E_AI_JUDGMENT_BUDGET",
}
UNCONFIGURED_AMBIENT_SOURCE_FAMILIES = {
    "ci": ("ci", "CI"),
    "location": ("location",),
    "local_activity": ("local_activity", "local activity", "local"),
    "repository": ("repository", "repo"),
    "incident": ("incident",),
}


def _rg_fixed(pattern: str, *paths: str) -> list[str]:
    result = subprocess.run(
        ["rg", "--line-number", "--fixed-strings", pattern, *paths],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def _section(path: str, heading: str) -> str:
    text = (ROOT / path).read_text()
    start = text.index(heading)
    next_heading = text.find("\n### ", start + len(heading))
    if next_heading == -1:
        next_heading = text.find("\n## ", start + len(heading))
    return text[start:] if next_heading == -1 else text[start:next_heading]


def _assert_absent(pattern: str, *paths: str) -> None:
    matches = _rg_fixed(pattern, *(paths or RUNTIME_PATHS))
    assert not matches, "unexpected AI-first surface remains:\n" + "\n".join(matches[:20])


def test_proactivity_runtime_has_no_attention_ranking_surfaces() -> None:
    _assert_absent("attention_ranking_due")
    _assert_absent("attention_item")


@pytest.mark.parametrize(
    "route",
    [
        "/v1/proactive/",
        "/v1/work/commitments",
        "/v1/email/thread-watches",
        "/v1/notifications",
    ],
)
def test_proactivity_has_no_separate_api_routes(route: str) -> None:
    _assert_absent(route, "src/ariel")


@pytest.mark.parametrize(
    "pattern",
    [
        "_synthesize_",
        "I couldn't",
        "I could not",
        "unable to answer",
    ],
)
def test_runtime_has_no_deterministic_tool_result_synthesis_or_fallback_prose(
    pattern: str,
) -> None:
    _assert_absent(pattern)


def test_sota_gap_doc_lists_exact_typed_ai_judgment_failure_codes() -> None:
    section = _section(
        "docs/ai-first-sota-gap-cutover.md",
        "### Failure Code And Status Vocabulary",
    )

    assert set(re.findall(r"E_AI_JUDGMENT_[A-Z_]+", section)) == AI_JUDGMENT_FAILURE_CODES


def test_ai_judgment_failure_code_schema_uses_typed_vocabulary() -> None:
    orm_text = (ROOT / "src/ariel/persistence.py").read_text()
    migration_text = "\n".join(
        path.read_text() for path in sorted((ROOT / "alembic/versions").glob("*.py"))
    )

    for label, text in {
        "src/ariel/persistence.py": orm_text,
        "alembic/versions": migration_text,
    }.items():
        assert "ck_ai_judgment_failure_code" in text, (
            f"{label} does not constrain AI judgment failure codes"
        )
        for code in sorted(AI_JUDGMENT_FAILURE_CODES):
            assert code in text, f"{label} missing typed failure code {code}"
        for code in ("E_AI_JUDGMENT_JSON", "E_AI_JUDGMENT_MODEL"):
            assert code not in text, f"{label} still contains unsupported failure code {code}"


def test_unconfigured_ambient_source_families_are_documented_as_absent() -> None:
    section = _section(
        "docs/ai-first-sota-gap-cutover.md",
        "### Ambient Source Coverage",
    )
    normalized = section.lower().replace("-", "_")

    assert "unconfigured" in normalized
    assert "absent" in normalized
    for family, aliases in UNCONFIGURED_AMBIENT_SOURCE_FAMILIES.items():
        assert any(alias.lower().replace("-", "_") in normalized for alias in aliases), (
            f"missing unconfigured ambient source family in docs: {family}"
        )


@pytest.mark.parametrize("source_type", tuple(UNCONFIGURED_AMBIENT_SOURCE_FAMILIES))
def test_unconfigured_ambient_source_types_are_absent_from_runtime_constraints(
    source_type: str,
) -> None:
    _assert_absent(f"'{source_type}'", "src/ariel/persistence.py", "alembic/versions")
