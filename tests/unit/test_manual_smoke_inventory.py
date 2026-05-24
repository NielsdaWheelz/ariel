from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any, cast

from sqlalchemy import CheckConstraint, Table

from ariel.app import PUBLIC_LOCAL_AUTH_BYPASS_ROUTES, create_app
from ariel.capability_registry import (
    internal_callable_capability_ids,
    run_callable_name_for_capability_id,
)
from ariel.config import AppSettings
from ariel.config import ENV_FILE_SELECTOR_ENV_VAR
from ariel.persistence import BackgroundTaskRecord
from ariel.dev_db import DEV_DB_ENV_VARS


ROOT = Path(__file__).resolve().parents[2]
MANUAL_SMOKE_DOC = ROOT / "docs/manual-smoke-test.md"
ENV_EXAMPLE = ROOT / ".env.example"
WORKER = ROOT / "src/ariel/worker.py"
SRC = ROOT / "src/ariel"
DISCORD_BOT = SRC / "discord_bot.py"
ENV_ACCESS_OWNER_FILES = {
    SRC / "config.py",
    SRC / "dev_db.py",
}
ENV_SELECTOR_VARS = {ENV_FILE_SELECTOR_ENV_VAR}
DOCS_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
NON_CAPABILITY_SYSCALLS = {
    "agent.emit_message",
    "agent.emit_value",
    "agent.pause_until_input",
    "agent.emit_finding",
    "agent.emit_done",
    "scratch.set",
    "scratch.get",
}
DISCORD_NON_SLASH_ACTIONS = {
    "Owner DM",
    "Home-guild ambient message",
    "Non-owner message",
    "Wrong-guild message",
    "Bot mention",
    "Attachment-only message",
    "Wrong-user slash command",
    "Wrong-guild slash command or button",
    "Approval button",
}
EVIDENCE_STATES = {"not_run", "passed", "failed", "blocked", "not_enabled"}


def _manual_text() -> str:
    return MANUAL_SMOKE_DOC.read_text(encoding="utf-8")


def _documented_env_vars(text: str) -> set[str]:
    return set(re.findall(r"\b(ARIEL_[A-Z0-9_]+)\b", text))


def _documented_table_env_vars(text: str) -> set[str]:
    return set(re.findall(r"^\| `(ARIEL_[A-Z0-9_]+)` \|", text, re.MULTILINE))


def _documented_pytest_targets(text: str) -> set[str]:
    return set(re.findall(r"\b(tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?)\b", text))


def _test_function_names(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def _discord_slash_command_names() -> set[str]:
    return set(
        re.findall(
            r"app_commands\.Command\(\s+name=\"([a-z_]+)\"",
            DISCORD_BOT.read_text(encoding="utf-8"),
        )
    )


def _os_env_accesses(path: Path) -> list[int]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    os_module_names: set[str] = set()
    os_env_names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Import):
            os_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "os"
            )
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            os_env_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"environ", "getenv", "putenv"}
            )

    accesses: list[int] = []
    for walk_node in ast.walk(module):
        if (
            isinstance(walk_node, ast.Attribute)
            and isinstance(walk_node.value, ast.Name)
            and walk_node.value.id in os_module_names
            and walk_node.attr in {"environ", "getenv", "putenv"}
        ):
            accesses.append(walk_node.lineno)
        if isinstance(walk_node, ast.Name) and walk_node.id in os_env_names:
            accesses.append(walk_node.lineno)
    return sorted(set(accesses))


def _expected_env_vars() -> set[str]:
    return (
        {"ARIEL_" + field_name.upper() for field_name in AppSettings.model_fields}
        | DEV_DB_ENV_VARS
        | ENV_SELECTOR_VARS
    )


def _section(heading: str) -> str:
    text = _manual_text()
    start = text.index(heading)
    heading_level = len(heading) - len(heading.lstrip("#"))
    next_heading = re.search(rf"\n#{{1,{heading_level}}} ", text[start + len(heading) :])
    if next_heading is None:
        return text[start:]
    return text[start : start + len(heading) + next_heading.start()]


def _python_heredocs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    heredocs: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<'PY'" not in line:
            index += 1
            continue
        start_line = index + 1
        body_lines: list[str] = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if candidate.strip() == "PY":
                assert candidate == "PY", (
                    f"Python heredoc terminator is indented at line {index + 1}"
                )
                break
            body_lines.append(candidate)
            index += 1
        else:
            raise AssertionError(f"Python heredoc starting at line {start_line} is unterminated")
        heredocs.append((start_line, "\n".join(body_lines) + "\n"))
        index += 1
    return heredocs


def _app_route_surface() -> set[tuple[str, str]]:
    app = create_app(database_url="postgresql+psycopg://test:test@localhost/ariel_doc_check")
    try:
        routes: set[tuple[str, str]] = set()
        for route in app.routes:
            raw_methods = getattr(route, "methods", None)
            raw_path = getattr(route, "path", None)
            if not isinstance(raw_methods, set) or not isinstance(raw_path, str):
                continue
            if raw_path in DOCS_ROUTES:
                continue
            for method in raw_methods - {"HEAD", "OPTIONS"}:
                routes.add((str(method), raw_path))
        return routes
    finally:
        cast(Any, app.state).engine.dispose()


def _background_task_types() -> set[str]:
    table = cast(Table, BackgroundTaskRecord.__table__)
    for constraint in table.constraints:
        if constraint.name != "ck_background_task_type":
            continue
        sql = str(cast(CheckConstraint, constraint).sqltext)
        return set(re.findall(r"'([a-z_]+)'", sql))
    raise AssertionError("background task type constraint not found")


def _worker_dispatched_task_types() -> set[str]:
    text = WORKER.read_text(encoding="utf-8")
    start = text.index("def process_one_task(")
    end = text.index("\ndef run_worker(", start)
    return set(re.findall(r'case "([a-z_]+)"', text[start:end]))


def test_manual_smoke_env_inventory_tracks_runtime_env_surface() -> None:
    env_section = _section("## Env Var Inventory")
    documented = _documented_table_env_vars(env_section) | set(
        re.findall(
            r"^- `(ARIEL_DB_[A-Z0-9_]+)`$",
            _section("### Local DB Helper Only"),
            re.MULTILINE,
        )
    )

    assert documented == _expected_env_vars()


def test_env_example_tracks_runtime_and_dev_helper_env_surface() -> None:
    documented = _documented_env_vars(ENV_EXAMPLE.read_text(encoding="utf-8"))

    assert documented == _expected_env_vars()


def test_source_env_access_stays_in_env_owner_modules() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path in ENV_ACCESS_OWNER_FILES:
            continue
        for line_number in _os_env_accesses(path):
            offenders.append(f"{path.relative_to(ROOT)}:{line_number}")

    assert offenders == []


def test_manual_smoke_python_heredocs_are_syntactically_valid() -> None:
    heredocs = _python_heredocs(_manual_text())

    assert heredocs
    for start_line, source in heredocs:
        compile(source, f"{MANUAL_SMOKE_DOC}:{start_line}", "exec")


def test_manual_smoke_pytest_references_point_to_existing_tests() -> None:
    targets = _documented_pytest_targets(_manual_text())

    assert targets
    for target in targets:
        raw_path, separator, test_name = target.partition("::")
        path = ROOT / raw_path
        assert path.exists(), f"{target} references a missing test file"
        if separator:
            assert test_name in _test_function_names(path), f"{target} references a missing test"


def test_manual_smoke_route_inventory_tracks_fastapi_surface() -> None:
    route_section = _section("### HTTP Route Inventory")
    documented = {
        (method, path) for method, path in re.findall(r"\| `([A-Z]+) ([^`]+)` \|", route_section)
    }

    assert documented == _app_route_surface()


def test_manual_smoke_public_auth_bypass_inventory_tracks_app_contract() -> None:
    public_route_section = _section("### Public Local-Auth Bypass Routes")
    documented = {
        (method, path)
        for method, path in re.findall(r"\| `([A-Z]+) ([^`]+)` \|", public_route_section)
    }

    assert documented == set(PUBLIC_LOCAL_AUTH_BYPASS_ROUTES)


def test_manual_smoke_discord_action_inventory_tracks_bot_surface() -> None:
    discord_section = _section("## Discord User Actions")
    documented = set(
        re.findall(r"^\| (`?[^|`]+`?) \| [^|]+ \| [^|]+ \|$", discord_section, re.MULTILINE)
    )
    normalized_documented = {
        item.strip("`").strip() for item in documented if item not in {"Action", "---"}
    }
    expected = DISCORD_NON_SLASH_ACTIONS | {f"/{name}" for name in _discord_slash_command_names()}

    assert normalized_documented == expected


def test_manual_smoke_capability_inventory_tracks_registry_and_aliases() -> None:
    capability_section = _section("## Agent Capability Inventory").split(
        "### Capability Evidence Ledger", maxsplit=1
    )[0]
    documented = set(re.findall(r"\| `(cap\.[^`]+)` \| `([^`]+)` \|", capability_section))
    expected: set[tuple[str, str]] = set()
    for capability_id in internal_callable_capability_ids():
        syscall = run_callable_name_for_capability_id(capability_id)
        assert syscall is not None, f"{capability_id} has no run syscall alias"
        expected.add((capability_id, syscall))

    assert documented == expected


def test_manual_smoke_capability_rows_include_preconditions() -> None:
    capability_section = _section("## Agent Capability Inventory").split(
        "### Capability Evidence Ledger", maxsplit=1
    )[0]
    rows = re.findall(
        r"\| `(cap\.[^`]+)` \| `([^`]+)` \| ([^|]+) \| ([^|]+) \|",
        capability_section,
    )

    assert len(rows) == len(internal_callable_capability_ids())
    for capability_id, _syscall, preconditions, _evidence in rows:
        assert preconditions.strip(), f"{capability_id} has no preconditions"


def test_manual_smoke_capability_evidence_ledger_tracks_registry() -> None:
    ledger_section = _section("### Capability Evidence Ledger")
    rows = re.findall(r"\| `(cap\.[^`]+)` \| `([^`]+)` \| ([^|]+) \|", ledger_section)

    assert {capability_id for capability_id, _state, _evidence in rows} == set(
        internal_callable_capability_ids()
    )
    for capability_id, state, evidence in rows:
        assert state in EVIDENCE_STATES, capability_id
        assert evidence.strip(), capability_id


def test_manual_smoke_runtime_syscall_inventory_lists_non_capability_syscalls() -> None:
    syscall_section = _section("## Model Runtime Syscall Inventory")
    documented = set(re.findall(r"\| `((?:agent|scratch)\.[^`]+)` \|", syscall_section))

    assert documented == NON_CAPABILITY_SYSCALLS


def test_manual_smoke_capability_section_documents_eligibility_gates() -> None:
    capability_section = _section("## Agent Capability Inventory").split(
        "### Capability Evidence Ledger", maxsplit=1
    )[0]

    for phrase in (
        "required_scopes",
        "calendar.freebusy",
        "Agency runtime is configured",
        "attachment refs",
        "runtime bindings are configured",
        "worker drainage",
    ):
        assert phrase in capability_section


def test_manual_smoke_background_task_inventory_tracks_schema_constraint() -> None:
    task_section = _section("## Background Task Type Inventory")
    documented = set(re.findall(r"\| `([a-z_]+)` \|", task_section))

    assert documented == _background_task_types()


def test_background_task_schema_types_match_worker_dispatch_arms() -> None:
    assert _background_task_types() == _worker_dispatched_task_types()


def test_manual_smoke_queue_shape_checks_cover_reconcile_identity() -> None:
    smoke_sequence = _section("## Smoke Sequence")

    assert "bad_reconcile_shape" in smoke_sequence
    assert "provider_write_receipt_id is null" in smoke_sequence
    assert "idempotency_key != 'provider_write_reconcile:' || provider_write_receipt_id" in (
        smoke_sequence
    )
    assert "duplicate_reconcile_receipts" in smoke_sequence
