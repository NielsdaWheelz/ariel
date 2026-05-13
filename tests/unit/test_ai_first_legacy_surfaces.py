from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PATHS = ("src/ariel", "alembic/versions")
PUBLIC_DOC_ENV_PATHS = ("README.md", ".env.example")
ACTIVE_AI_FIRST_DOC_PATHS = (
    "README.md",
    ".env.example",
    "docs/index.md",
    "docs/ai-first.md",
    "docs/modules",
    "docs/production-runbook.md",
)
LEGACY_SURFACE_PATHS = (*ACTIVE_AI_FIRST_DOC_PATHS, *RUNTIME_PATHS, "tests")

AI_JUDGMENT_FAILURE_CODES = {
    "E_AI_JUDGMENT_REQUIRED",
    "E_AI_JUDGMENT_CREDENTIALS",
    "E_AI_JUDGMENT_TIMEOUT",
    "E_AI_JUDGMENT_INVALID_JSON",
    "E_AI_JUDGMENT_SCHEMA",
    "E_AI_JUDGMENT_VALIDATION",
    "E_AI_JUDGMENT_BUDGET",
}
REMOVED_AI_JUDGMENT_FAILURE_CODES = {
    "E_AI_JUDGMENT_MODEL",
    "E_AI_JUDGMENT_JSON",
}
UNCONFIGURED_AMBIENT_SOURCE_FAMILIES = {
    "ci": ("ci", "CI"),
    "location": ("location",),
    "local_activity": ("local_activity", "local activity", "local"),
    "repository": ("repository", "repo"),
    "incident": ("incident",),
}
UNCONFIGURED_AMBIENT_SOURCE_TYPES = (
    "'ci'",
    "'location'",
    "'local_activity'",
    "'repository'",
    "'incident'",
)
UNCONFIGURED_AMBIENT_SOURCE_MIGRATION_ALLOWLIST = {
    "WHERE source_type IN ('ci', 'location', 'local_activity')": (
        "data cleanup of removed source values before replacing the constraint"
    ),
    "'provider_event', 'connector_event', 'ci', 'location', 'local_activity')": (
        "downgrade restores the prior source constraint"
    ),
}
FALLBACK_TEST_NAME_ALLOWLIST = {
    (
        "tests/integration/test_s2_pr01_acceptance.py::"
        "test_s2_pr01_invalid_or_denied_proposals_are_blocked_with_explicit_reason_and_safe_fallback"
    ): "approval proposal rail exposes a safe action denial, not AI-first replacement prose",
    (
        "tests/integration/test_s4_pr01_acceptance.py::"
        "test_s4_pr01_attendee_slot_fallback_is_explicit_and_recoverable_without_freebusy_scope"
    ): "calendar free/busy scope recovery is connector behavior outside AI-first judgment fallback",
    (
        "tests/integration/test_s4_pr03_acceptance.py::"
        "test_s4_pr03_attendee_reconnect_intent_requests_freebusy_and_closes_fallback_path"
    ): "calendar reconnect flow verifies a removed connector fallback path closes",
}
_TEST_DEF_RE = re.compile(r"^(?P<path>[^:]+):\d+:def (?P<name>test_[^(]+)\(")
_AI_JUDGMENT_CODE_RE = re.compile(r"E_AI_JUDGMENT_[A-Z_]+")


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


def _rg_regex(pattern: str, *paths: str) -> list[str]:
    result = subprocess.run(
        ["rg", "--line-number", pattern, *paths],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def _read_repo_text(path: str) -> str:
    return (ROOT / path).read_text()


def _section(path: str, heading: str) -> str:
    text = _read_repo_text(path)
    start = text.index(heading)
    next_heading = text.find("\n### ", start + len(heading))
    if next_heading == -1:
        next_heading = text.find("\n## ", start + len(heading))
    return text[start:] if next_heading == -1 else text[start:next_heading]


def _without_this_test(lines: list[str]) -> list[str]:
    return [line for line in lines if "tests/unit/test_ai_first_legacy_surfaces.py:" not in line]


def _assert_absent(pattern: str, *paths: str) -> None:
    matches = _rg_fixed(pattern, *(paths or RUNTIME_PATHS))
    assert not matches, "legacy AI-first surface remains:\n" + "\n".join(matches[:20])


def _assert_absent_except_this_test(pattern: str, *paths: str) -> None:
    matches = _without_this_test(_rg_fixed(pattern, *(paths or RUNTIME_PATHS)))
    assert not matches, "legacy AI-first surface remains:\n" + "\n".join(matches[:20])


def _assert_absent_except_allowed_substrings(
    pattern: str,
    paths: tuple[str, ...],
    allowlist: dict[str, str],
) -> None:
    matches = [
        line
        for line in _rg_fixed(pattern, *paths)
        if not any(allowed in line for allowed in allowlist)
    ]
    assert not matches, "legacy AI-first surface remains:\n" + "\n".join(matches[:20])


@pytest.mark.parametrize(
    "pattern",
    [
        "workspace_observation_derivation_due",
        "proactive_observation_derivation_due",
        "ambient_observation_derivation_due",
        "derive_proactive_observations",
        "/v1/proactive/observations/derive",
        "ambient observation derivation",
        "ambient derivation",
        "observation derivation",
        "proactive_derivation",
    ],
)
def test_old_ambient_derivation_route_task_and_wording_are_absent(pattern: str) -> None:
    _assert_absent_except_this_test(pattern, *LEGACY_SURFACE_PATHS)


def test_old_proactive_attention_runtime_surfaces_are_absent() -> None:
    _assert_absent("attention_ranking_due")
    _assert_absent("attention_item")


@pytest.mark.parametrize(
    "pattern",
    [
        "ambient observation derivation",
        "ambient derivation",
        "observation derivation",
        "workspace_observation_derivation_due",
        "proactive_observation_derivation_due",
        "ambient_observation_derivation_due",
        "derive_proactive_observations",
        "/v1/proactive/observations/derive",
    ],
)
def test_readme_and_env_example_do_not_describe_legacy_ambient_derivation(
    pattern: str,
) -> None:
    _assert_absent_except_this_test(pattern, *PUBLIC_DOC_ENV_PATHS)


@pytest.mark.parametrize(
    "pattern",
    [
        "evt.memory.recalled",
        "included_memory_count",
        "candidate_memory_count",
        "max_recalled_items",
        "evt.turn.limit_reached",
    ],
)
def test_old_deterministic_memory_event_and_response_fields_are_absent(pattern: str) -> None:
    _assert_absent(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "_synthesize_",
        "fallback_text",
        "fallback_message",
        "fallback_response",
        "I couldn't",
        "I could not",
        "unable to answer",
    ],
)
def test_old_tool_result_synthesis_and_deterministic_prose_are_absent(
    pattern: str,
) -> None:
    _assert_absent(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "E_AI_JUDGMENT_" + "MODEL",
        "E_AI_JUDGMENT_" + "JSON",
    ],
)
def test_removed_ai_judgment_failure_codes_are_absent(pattern: str) -> None:
    _assert_absent_except_this_test(pattern, "src/ariel", "tests")


def test_sota_gap_doc_lists_exact_typed_ai_judgment_failure_codes() -> None:
    section = _section(
        "docs/ai-first-sota-gap-cutover.md",
        "### Failure Code And Status Vocabulary",
    )

    assert set(_AI_JUDGMENT_CODE_RE.findall(section)) == AI_JUDGMENT_FAILURE_CODES


def test_ai_judgment_failure_code_schema_uses_typed_vocabulary() -> None:
    orm_text = _read_repo_text("src/ariel/persistence.py")
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
        for code in sorted(REMOVED_AI_JUDGMENT_FAILURE_CODES):
            assert code not in text, f"{label} still contains removed failure code {code}"


def test_fallback_shaped_test_names_are_explicitly_allowlisted() -> None:
    observed: dict[str, str] = {}
    for line in _without_this_test(_rg_regex(r"def test_[^(]*fallback[^(]*\(", "tests")):
        match = _TEST_DEF_RE.match(line)
        assert match is not None, line
        observed[f"{match.group('path')}::{match.group('name')}"] = line

    unexpected = sorted(set(observed) - set(FALLBACK_TEST_NAME_ALLOWLIST))
    stale_allowlist = sorted(set(FALLBACK_TEST_NAME_ALLOWLIST) - set(observed))

    assert not unexpected, "fallback-shaped test names need explicit allowlist:\n" + "\n".join(
        observed[name] for name in unexpected
    )
    assert not stale_allowlist, "stale fallback-shaped test allowlist entries:\n" + "\n".join(
        stale_allowlist
    )


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("ambient observation derivation", "docs"),
        ("proactive_derivation", "tests"),
        ("_budget_fallback", "tests/integration/test_s5_pr02_session_management_acceptance.py"),
        ("_summary_fallback", "tests/integration/test_s5_pr02_session_management_acceptance.py"),
    ],
)
def test_legacy_ai_first_wording_is_absent_from_active_docs_and_tests(
    pattern: str,
    path: str,
) -> None:
    _assert_absent_except_this_test(pattern, path)


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


@pytest.mark.parametrize("pattern", UNCONFIGURED_AMBIENT_SOURCE_TYPES)
def test_unconfigured_ambient_source_types_are_absent_from_runtime_constraints(
    pattern: str,
) -> None:
    _assert_absent(pattern, "src/ariel/persistence.py")
    _assert_absent_except_allowed_substrings(
        pattern,
        ("alembic/versions",),
        UNCONFIGURED_AMBIENT_SOURCE_MIGRATION_ALLOWLIST,
    )


def test_provider_sync_runtime_does_not_queue_ambient_interpretation() -> None:
    sync_runtime = ROOT / "src/ariel/sync_runtime.py"
    source = sync_runtime.read_text()

    assert 'task_type="ambient_interpretation_due"' not in source
    assert "workspace_item_event_id" not in source
    for pattern in (
        "ProactiveCaseRecord",
        "ProactiveObservationRecord",
        "upsert_proactive_observation",
        'task_type="proactive_deliberation_due"',
    ):
        _assert_absent(pattern, "src/ariel/sync_runtime.py")
