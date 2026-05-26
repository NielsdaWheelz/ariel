from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any, cast, get_args

from sqlalchemy import CheckConstraint, Table

from ariel.app import PUBLIC_LOCAL_AUTH_BYPASS_ROUTES, create_app
from ariel.capture_ingress import CaptureRecordRequest
from ariel.capability_registry import (
    EMAIL_MUTATION_CAPABILITY_IDS,
    internal_callable_capability_ids,
    get_capability,
    MAPS_CAPABILITY_IDS,
    run_callable_signature,
    run_callable_name_for_capability_id,
)
from ariel.config import AppSettings
from ariel.config import ENV_FILE_SELECTOR_ENV_VAR
from ariel.models import PROVIDER_REQUIRED_ENV_VARS, required_model_provider_env_vars
from ariel.persistence import AIJudgmentRecord, BackgroundTaskRecord
from ariel.production_posture import (
    REQUIRED_CADDY_PUBLIC_PROXY_ROUTES,
    validate_caddy_config_posture,
)
from ariel.research_modes import RESEARCH_MODE_VALUES
from ariel.dev_db import DEV_DB_ENV_VARS
from ariel.dev_db import load_local_env, resolve_local_postgres_runtime
from ariel.run_runtime import run_tool_definitions


ROOT = Path(__file__).resolve().parents[2]
MANUAL_SMOKE_DOC = ROOT / "docs/manual-smoke-test.md"
DOCS_INDEX = ROOT / "docs/index.md"
MODULE_DOCS_INDEX = ROOT / "docs/modules/index.md"
CLEANLINESS_DOC = ROOT / "docs/cleanliness.md"
ENV_EXAMPLE = ROOT / ".env.example"
DEV_ENV_EXAMPLE = ROOT / ".env.dev.example"
WORKER = ROOT / "src/ariel/worker.py"
CADDYFILE = ROOT / "deploy/caddy/Caddyfile"
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
    "agent.finish_silent",
    "agent.emit_finding",
    "agent.emit_done",
    "scratch.set",
    "scratch.get",
}
DISCORD_NON_SLASH_ACTIONS = {
    "Discord application setup",
    "Bot or self message",
    "Blank owner message",
    "Owner DM",
    "Home-guild ambient message",
    "Non-owner message",
    "Wrong-guild message",
    "Unsupported Discord message type",
    "Bot mention",
    "Mention-only bot ping",
    "Owner DM attachment-only no-instruction message",
    "Home-guild attachment-only no-instruction message",
    "Owner DM attachment read request",
    "Home-guild attachment read request",
    "Origin reply routing",
    "Default notification delivery",
    "Wrong-user slash command",
    "Wrong-guild slash command",
    "Wrong-user approval button",
    "Wrong-guild approval button",
    "Non-Ariel component custom_id",
    "Approval approve button",
    "Approval deny button",
    "Approval stale/replay interaction",
    "Malformed approval button custom_id",
}
EVIDENCE_STATES = {"not_run", "partial", "passed", "failed", "blocked", "not_enabled"}
CURRENT_HOST_CONTRADICTION_PATTERNS = {
    "passed": (
        "not run",
        "was not run",
        "were not run",
        "not a full",
        "historical",
        "must be rerun",
        "capability rows passed",
        "rows are healthy",
        "blocked until",
        "blocked by",
        "blocked",
        "canonical production posture expects",
        "canonical production posture requires",
    ),
    "partial": (
        "not run",
        "was not run",
        "were not run",
        "must be rerun",
        "no fresh",
        "blocked until",
        "blocked by",
        "not a full",
        "rows are healthy",
        "canonical production posture expects",
        "canonical production posture requires",
    ),
}
TRANSIENT_LIVE_ID_PATTERN = re.compile(
    r"\b(?:age|art|cap|evt|job|mem|mno|pev|rot|ses|tsk|trn)_01[0-9a-z]+\b"
)
RESEARCH_MODE_EVIDENCE_ITEMS = set(RESEARCH_MODE_VALUES) | {
    "Invalid research mode",
    "Cross-mode private/web rejection",
}
AGENT_LOOP_RAILS = {
    "Wall-clock budget",
    "Model-call backstop",
    "Stuck detection",
    "Per-program commit",
    "Failed-program approval voiding",
    "Task replay",
    "emit_value eviction",
    "Remaining-budget signal",
    "Run-protocol recovery",
    "Retryable model error retry",
    "Round-history eviction",
    "Scratch bounds/taint",
}
PROVIDER_BOUND_CAPABILITY_PRECONDITIONS = {
    "cap.maps.directions": "Maps binding configured",
    "cap.maps.search_places": "Maps binding configured",
    "cap.search.web": "Brave search binding configured",
    "cap.web.extract": "Extract binding configured",
    "cap.weather.forecast": "Weather binding configured",
}
PROVIDER_BOUND_CURRENT_HOST_PROOF = {
    "cap.maps.directions": ("ARIEL_MAPS_API_KEY", "Google Routes"),
    "cap.maps.search_places": ("ARIEL_MAPS_API_KEY", "Google Places"),
    "cap.search.web": ("ARIEL_SEARCH_WEB_API_KEY", "Brave"),
    "cap.web.extract": ("ARIEL_JINA_API_KEY", "Jina Reader"),
    "cap.weather.forecast": ("ARIEL_WEATHER_PRODUCTION_API_KEY", "Tomorrow.io"),
}
DEV_PROVIDER_CAPABILITY_ENV_VARS = {
    "ARIEL_SEARCH_WEB_API_KEY",
    "ARIEL_JINA_API_KEY",
    "ARIEL_MAPS_API_KEY",
    "ARIEL_WEATHER_PRODUCTION_API_KEY",
}
DEV_ENV_CURATED_ENV_VARS = {
    "ARIEL_ANTHROPIC_API_KEY",
    "ARIEL_BIND_HOST",
    "ARIEL_BIND_PORT",
    "ARIEL_CLOUDFLARE_ACCOUNT_ID",
    "ARIEL_CLOUDFLARE_API_TOKEN",
    "ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION",
    "ARIEL_CONNECTOR_ENCRYPTION_SECRET",
    "ARIEL_DATABASE_URL",
    "ARIEL_DB_CONTAINER_NAME",
    "ARIEL_DB_DOCKER_IMAGE",
    "ARIEL_DB_VOLUME_NAME",
    "ARIEL_DEPLOYMENT_MODE",
    "ARIEL_DISCORD_ARIEL_BASE_URL",
    "ARIEL_DISCORD_BOT_TOKEN",
    "ARIEL_DISCORD_CHANNEL_ID",
    "ARIEL_DISCORD_GUILD_ID",
    "ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS",
    "ARIEL_DISCORD_USER_ID",
    "ARIEL_ENV_FILE",
    "ARIEL_GOOGLE_API_KEY",
    "ARIEL_GOOGLE_OAUTH_CLIENT_ID",
    "ARIEL_GOOGLE_OAUTH_CLIENT_SECRET",
    "ARIEL_GOOGLE_OAUTH_REDIRECT_URI",
    "ARIEL_JINA_API_KEY",
    "ARIEL_LOCAL_AUTH_REQUIRED",
    "ARIEL_MAPS_API_KEY",
    "ARIEL_MEMORY_EMBEDDING_DIMENSIONS",
    "ARIEL_OPENAI_API_KEY",
    "ARIEL_OPENROUTER_API_KEY",
    "ARIEL_OPENROUTER_BASE_URL",
    "ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS",
    "ARIEL_SEARCH_WEB_API_KEY",
    "ARIEL_SUBSCRIBER_HEARTBEAT_INTERVAL_SECONDS",
    "ARIEL_SUBSCRIBER_HEARTBEAT_STALENESS_FACTOR",
    "ARIEL_WEATHER_PRODUCTION_API_KEY",
    "ARIEL_WORKER_POLL_SECONDS",
}
BACKGROUND_WORK_FEATURE_ITEMS = {
    "Worker one-shot failure retry/exhaustion",
    "Worker recurring failure exhaustion re-arm",
    "Recurring maintenance seeders",
    "Google connector-error wake",
    "Stale cursor full-resync recovery",
    "research_run",
    "provider_write_reconcile_due",
    "Provider event ingest",
}
AGENCY_EVENT_BEHAVIOR_ITEMS = {
    "heartbeat",
    "missing-job-id rejection",
    "non-waking job update",
    "waiting-state wake",
    "terminal-state wake",
}
GOOGLE_RECONNECT_BEHAVIOR_ITEMS = {
    "baseline reconnect",
    "single capability_intent",
    "comma-bundled capability_intents",
    "invalid capability_intent",
    "reconnect event payload",
}


def _manual_text() -> str:
    return MANUAL_SMOKE_DOC.read_text(encoding="utf-8")


def _documented_env_vars(text: str) -> set[str]:
    return set(re.findall(r"\b(ARIEL_[A-Z0-9_]+)\b", text))


def _active_env_assignments(text: str) -> set[str]:
    return set(re.findall(r"^(ARIEL_[A-Z0-9_]+)=", text, re.MULTILINE))


def _documented_table_env_vars(text: str) -> set[str]:
    return set(re.findall(r"^\| `(ARIEL_[A-Z0-9_]+)` \|", text, re.MULTILINE))


def _documented_pytest_targets(text: str) -> set[str]:
    return set(re.findall(r"\b(tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?)\b", text))


def _capture_record_kind_values() -> set[str]:
    request_union = get_args(CaptureRecordRequest)[0]
    values: set[str] = set()
    for model in get_args(request_union):
        model_fields = getattr(model, "model_fields")
        values.update(cast(tuple[str, ...], get_args(model_fields["kind"].annotation)))
    return values


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


def _ai_judgment_types() -> set[str]:
    table = cast(Table, AIJudgmentRecord.__table__)
    for constraint in table.constraints:
        if constraint.name != "ck_ai_judgment_type":
            continue
        sql = str(cast(CheckConstraint, constraint).sqltext)
        return set(re.findall(r"'([a-z_]+)'", sql))
    raise AssertionError("AI judgment type constraint not found")


def _worker_dispatched_task_types() -> set[str]:
    text = WORKER.read_text(encoding="utf-8")
    start = text.index("def process_one_task(")
    end = text.index("\ndef run_worker(", start)
    return set(re.findall(r'case "([a-z_]+)"', text[start:end]))


def _four_column_evidence_rows(heading: str) -> list[tuple[str, str, str, str]]:
    rows = re.findall(
        r"^\| (`?[^|`]+`?) \| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$",
        _section(heading),
        re.MULTILINE,
    )
    return [
        (item.strip("`").strip(), contract_state, current_host_state, evidence.strip())
        for item, contract_state, current_host_state, evidence in rows
    ]


def _capability_inventory_rows() -> dict[str, tuple[str, str, str]]:
    capability_section = _section("## Agent Capability Inventory").split(
        "### Capability Evidence Ledger", maxsplit=1
    )[0]
    rows = re.findall(
        r"\| `(cap\.[^`]+)` \| `([^`]+)` \| ([^|]+) \| ([^|]+) \|",
        capability_section,
    )
    return {
        capability_id: (syscall, preconditions.strip(), evidence.strip())
        for capability_id, syscall, preconditions, evidence in rows
    }


def _assert_current_host_evidence_is_coherent(
    *,
    item: str,
    current_host_state: str,
    evidence: str,
) -> None:
    normalized_evidence = evidence.lower()
    offenders = [
        pattern
        for pattern in CURRENT_HOST_CONTRADICTION_PATTERNS.get(current_host_state, ())
        if pattern in normalized_evidence
    ]
    assert offenders == [], f"{item} {current_host_state} evidence contradicts state: {offenders}"


def _assert_four_column_evidence_rows(
    *,
    rows: list[tuple[str, str, str, str]],
    expected_items: set[str],
) -> None:
    assert len(rows) == len(expected_items)
    assert {item for item, *_ in rows} == expected_items
    for item, contract_state, current_host_state, evidence in rows:
        normalized_evidence = evidence.lower()
        assert contract_state in EVIDENCE_STATES, item
        assert current_host_state in EVIDENCE_STATES, item
        assert evidence, item
        if contract_state == "passed":
            assert (
                "fixture" in normalized_evidence
                or "validator" in normalized_evidence
                or "guard" in normalized_evidence
            ), item
        if current_host_state in {"passed", "partial", "blocked"}:
            assert "live" in normalized_evidence or "current" in normalized_evidence, item
        _assert_current_host_evidence_is_coherent(
            item=item,
            current_host_state=current_host_state,
            evidence=evidence,
        )


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


def test_manual_smoke_env_evidence_ledger_tracks_runtime_env_surface() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Env Var Evidence Ledger"),
        expected_items=_expected_env_vars(),
    )


def test_manual_smoke_dev_db_helper_rows_name_local_only_production_rejection() -> None:
    rows = {
        item: evidence
        for item, _contract_state, _current_host_state, evidence in _four_column_evidence_rows(
            "### Env Var Evidence Ledger"
        )
    }

    for env_name in DEV_DB_ENV_VARS:
        evidence = rows[env_name]
        assert "Local-only" in evidence
        assert "production `/etc/ariel/ariel.env` audit rejects it as unknown" in evidence


def test_manual_smoke_env_selector_state_tracks_blocked_default_stack() -> None:
    recent_snapshot = _section("## Recent Evidence Snapshot")
    rows = {
        item: current_host_state
        for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
            "### Env Var Evidence Ledger"
        )
    }

    if "| Env parsing | blocked |" in recent_snapshot:
        assert rows[ENV_FILE_SELECTOR_ENV_VAR] == "blocked"


def test_manual_smoke_env_selector_is_documented_as_local_only() -> None:
    env_section = _section("## Env Var Inventory")
    selector_section = _section("### Local Env Selector")

    assert "`ARIEL_ENV_FILE` is a local/dev selector" in env_section
    assert "do not put it in `/etc/ariel/ariel.env`" in env_section
    assert "| `ARIEL_ENV_FILE` |" in selector_section


def test_manual_smoke_secret_classification_keeps_paths_non_secret() -> None:
    google_env_section = _section("### Google Connector And Push")
    rows = {
        env_name: (secret, evidence)
        for env_name, secret, evidence in re.findall(
            r"^\| `(ARIEL_[A-Z0-9_]+)` \| (yes|no) \| [^|]+ \| ([^|]+) \|$",
            google_env_section,
            re.MULTILINE,
        )
    }

    secret, evidence = rows["ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH"]
    assert secret == "no"
    assert "never print the JSON contents" in evidence


def test_manual_smoke_recent_snapshot_model_key_states_track_env_ledger() -> None:
    env_states = {
        item: current_host_state
        for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
            "### Env Var Evidence Ledger"
        )
    }
    snapshot_rows = {
        area: state
        for area, state, _evidence in re.findall(
            r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$",
            _section("## Recent Evidence Snapshot"),
            re.MULTILINE,
        )
        if area != "Area"
    }

    if env_states["ARIEL_OPENROUTER_API_KEY"] == "blocked":
        assert snapshot_rows["OpenRouter main/research models"] == "blocked"
    if env_states["ARIEL_GOOGLE_API_KEY"] == "blocked":
        assert snapshot_rows["Google vision model"] == "blocked"


def test_env_example_tracks_runtime_and_dev_helper_env_surface() -> None:
    documented = _documented_env_vars(ENV_EXAMPLE.read_text(encoding="utf-8"))

    assert documented == _expected_env_vars()


def test_dev_env_example_documents_curated_dev_surface() -> None:
    documented = _documented_env_vars(DEV_ENV_EXAMPLE.read_text(encoding="utf-8"))

    assert documented == DEV_ENV_CURATED_ENV_VARS
    assert documented < _expected_env_vars()


def test_dev_env_example_selects_isolated_dev_runtime() -> None:
    env = load_local_env(ROOT, environ={ENV_FILE_SELECTOR_ENV_VAR: ".env.dev.example"})
    runtime = resolve_local_postgres_runtime(env)

    assert env["ARIEL_BIND_HOST"] == "127.0.0.1"
    assert env["ARIEL_BIND_PORT"] == "8001"
    assert env["ARIEL_DISCORD_ARIEL_BASE_URL"] == "http://127.0.0.1:8001"
    assert runtime.host == "127.0.0.1"
    assert runtime.host_port == 5435
    assert runtime.container_name == "ariel-postgres-dev"
    assert runtime.volume_name == "ariel-postgres-dev-data"
    assert runtime.database == "ariel"


def test_dev_env_example_tracks_current_model_provider_key_surface() -> None:
    text = DEV_ENV_EXAMPLE.read_text(encoding="utf-8")
    documented = _documented_env_vars(text)

    assert "ARIEL_MODEL_NAME" not in text
    for env_name in required_model_provider_env_vars():
        assert env_name in documented
    assert "ARIEL_OPENROUTER_API_KEY" in documented
    assert "ARIEL_OPENROUTER_BASE_URL" in documented
    for env_name in PROVIDER_REQUIRED_ENV_VARS["cloudflare"]:
        assert env_name in documented


def test_dev_env_example_includes_provider_capability_keys() -> None:
    documented = _documented_env_vars(DEV_ENV_EXAMPLE.read_text(encoding="utf-8"))

    assert DEV_PROVIDER_CAPABILITY_ENV_VARS.issubset(documented)


def test_env_examples_assign_current_required_model_provider_keys() -> None:
    for path in (ENV_EXAMPLE, DEV_ENV_EXAMPLE):
        text = path.read_text(encoding="utf-8")
        assignments = _active_env_assignments(text)

        assert "your_real_key" not in text
        for env_name in required_model_provider_env_vars():
            assert env_name in assignments, path.name


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


def test_manual_smoke_uses_shared_redacted_env_audit() -> None:
    smoke_sequence = _section("## Smoke Sequence")

    assert "scripts/verify_production_posture.py --redacted-env-audit" in smoke_sequence
    assert "--env-file /etc/ariel/ariel.env" in smoke_sequence
    assert "unknown env scanner passed" not in smoke_sequence


def test_manual_smoke_preserves_full_verify_as_merge_gate() -> None:
    text = _manual_text()
    normalized = " ".join(text.split())

    assert "optional pre-release" not in text
    assert "before merge or release, `make verify` remains the required gate" in normalized


def test_docs_use_flat_rule_paths() -> None:
    checked_paths = [
        ROOT / "README.md",
        ROOT / "Makefile",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "scripts").glob("*.sh"),
        *(ROOT / "scripts").glob("*.py"),
        *(SRC.rglob("*.py")),
        *(ROOT / "tests").rglob("*.py"),
    ]
    forbidden_patterns = ("docs/rules/", "rules/cleanliness.md")
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted(set(checked_paths))
        if path != Path(__file__).resolve()
        and any(pattern in path.read_text(encoding="utf-8") for pattern in forbidden_patterns)
    ]

    assert offenders == []


def test_cleanliness_doc_uses_canonical_flat_path() -> None:
    manual_smoke_text = MANUAL_SMOKE_DOC.read_text(encoding="utf-8")

    assert CLEANLINESS_DOC.exists()
    assert not (ROOT / "docs/rules/cleanliness.md").exists()
    assert "[cleanliness.md](cleanliness.md)" in manual_smoke_text


def test_docs_index_lists_every_top_level_doc() -> None:
    text = DOCS_INDEX.read_text(encoding="utf-8")
    top_level_docs = {path.name for path in (ROOT / "docs").glob("*.md") if path.name != "index.md"}
    linked_docs = set(re.findall(r"\]\(([^/)]+\.md)\)", text))

    assert linked_docs == top_level_docs


def test_module_docs_index_lists_every_module_doc() -> None:
    text = MODULE_DOCS_INDEX.read_text(encoding="utf-8")
    module_docs = {
        path.name for path in (ROOT / "docs/modules").glob("*.md") if path.name != "index.md"
    }
    linked_docs = set(re.findall(r"\]\(([^/)]+\.md)\)", text))

    assert linked_docs == module_docs


def test_manual_smoke_pytest_references_point_to_existing_tests() -> None:
    targets = _documented_pytest_targets(_manual_text())

    assert targets
    for target in targets:
        raw_path, separator, test_name = target.partition("::")
        path = ROOT / raw_path
        assert path.exists(), f"{target} references a missing test file"
        if separator:
            assert test_name in _test_function_names(path), f"{target} references a missing test"


def test_manual_smoke_backticked_pytest_references_are_specific_tests() -> None:
    refs = re.findall(r"`(tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?)`", _manual_text())

    assert refs
    assert [ref for ref in refs if "::" not in ref] == []


def test_manual_smoke_fixture_anchor_phrases_point_to_specific_tests() -> None:
    for match in re.finditer(r"Fixture anchors?: (?P<refs>[^|]+)", _manual_text()):
        refs = re.findall(r"`(tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?)`", match["refs"])
        assert refs, f"fixture anchor phrase has no pytest refs: {match[0]}"
        for ref in refs:
            assert "::" in ref, f"fixture anchor is file-only: {ref}"


def test_manual_smoke_route_inventory_tracks_fastapi_surface() -> None:
    route_section = _section("### HTTP Route Inventory")
    documented = {
        (method, path) for method, path in re.findall(r"\| `([A-Z]+) ([^`]+)` \|", route_section)
    }

    assert documented == _app_route_surface()


def test_manual_smoke_email_action_routes_document_mailbox_mutation_scope() -> None:
    route_section = _section("### HTTP Route Inventory")

    assert "cap.email.draft" not in EMAIL_MUTATION_CAPABILITY_IDS
    assert "cap.email.send" not in EMAIL_MUTATION_CAPABILITY_IDS
    assert "Email mailbox mutation action list" in route_section
    assert "Email mailbox mutation action detail" in route_section
    assert "draft/send receipts are not part of this route" in route_section


def test_manual_smoke_route_evidence_ledger_tracks_fastapi_surface() -> None:
    route_section = _section("### HTTP Route Evidence Ledger")
    rows = re.findall(
        r"\| `([A-Z]+) ([^`]+)` \| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|",
        route_section,
    )

    assert len(rows) == len(_app_route_surface())
    assert {
        (method, path) for method, path, _contract_state, _host_state, _evidence in rows
    } == _app_route_surface()
    for method, path, contract_state, host_state, evidence in rows:
        normalized_evidence = evidence.lower()
        assert contract_state in EVIDENCE_STATES, f"{method} {path}"
        assert host_state in EVIDENCE_STATES, f"{method} {path}"
        assert evidence.strip(), f"{method} {path}"
        if contract_state == "passed":
            assert "fixture" in normalized_evidence or "live" in normalized_evidence, (
                f"{method} {path} contract state needs fixture or live route evidence"
            )
        if host_state in {"passed", "partial", "failed", "blocked"}:
            assert "live" in normalized_evidence, (
                f"{method} {path} current-host state needs live evidence"
            )
        _assert_current_host_evidence_is_coherent(
            item=f"{method} {path}",
            current_host_state=host_state,
            evidence=evidence,
        )


def test_manual_smoke_public_auth_bypass_inventory_tracks_app_contract() -> None:
    public_route_section = _section("### Public Local-Auth Bypass Routes")
    documented = {
        (method, path)
        for method, path in re.findall(r"\| `([A-Z]+) ([^`]+)` \|", public_route_section)
    }

    assert documented == set(PUBLIC_LOCAL_AUTH_BYPASS_ROUTES)


def test_manual_smoke_public_caddy_ingress_inventory_tracks_caddyfile() -> None:
    public_route_section = _section("### Public Caddy Ingress Routes")
    caddyfile_text = CADDYFILE.read_text(encoding="utf-8")
    documented = {
        (method, path)
        for method, path in re.findall(r"\| `([A-Z]+) ([^`]+)` \|", public_route_section)
    }

    assert documented == set(REQUIRED_CADDY_PUBLIC_PROXY_ROUTES)
    assert (
        validate_caddy_config_posture(
            config_text=caddyfile_text,
            expected_config_text=caddyfile_text,
            config_is_valid=True,
            config_path=str(CADDYFILE),
        )
        == []
    )
    assert ("GET", "/v1/health") not in documented
    assert ("POST", "/v1/agency/events") not in documented
    assert all(
        (method, path) not in documented for path in DOCS_ROUTES for method in ("GET", "POST")
    )


def test_manual_smoke_model_env_rows_track_current_model_refs() -> None:
    model_section = _section("### Model And Loop")

    assert "ARIEL_MODEL_NAME" not in model_section
    for env_name in required_model_provider_env_vars():
        assert f"| `{env_name}` |" in model_section


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


def test_manual_smoke_discord_action_evidence_ledger_tracks_bot_surface() -> None:
    ledger_section = _section("### Discord User Action Evidence Ledger")
    rows = re.findall(
        r"^\| (`?[^|`]+`?) \| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$",
        ledger_section,
        re.MULTILINE,
    )
    expected = DISCORD_NON_SLASH_ACTIONS | {f"/{name}" for name in _discord_slash_command_names()}

    assert {action.strip("`").strip() for action, *_ in rows} == expected
    for action, contract_state, current_host_state, evidence in rows:
        normalized_action = action.strip("`").strip()
        assert contract_state in EVIDENCE_STATES, normalized_action
        assert current_host_state in EVIDENCE_STATES, normalized_action
        assert evidence.strip(), normalized_action
        if current_host_state in {"passed", "partial", "blocked"}:
            assert "Live" in evidence or "live" in evidence, normalized_action
        if contract_state == "passed":
            assert "Fixture" in evidence or "fixture" in evidence, normalized_action
        _assert_current_host_evidence_is_coherent(
            item=normalized_action,
            current_host_state=current_host_state,
            evidence=evidence,
        )


def test_manual_smoke_capture_kind_evidence_ledger_tracks_request_discriminator() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Capture Kind Evidence Ledger"),
        expected_items=_capture_record_kind_values(),
    )


def test_manual_smoke_google_reconnect_evidence_ledger_tracks_intent_behaviors() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Google Reconnect Evidence Ledger"),
        expected_items=GOOGLE_RECONNECT_BEHAVIOR_ITEMS,
    )


def test_manual_smoke_baseline_reconnect_does_not_overclaim_callback_completion() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### Google Reconnect Evidence Ledger"
        )
    }
    current_host_state, evidence = rows["baseline reconnect"]

    if current_host_state == "passed":
        normalized = evidence.lower()
        assert "callback" in normalized and "connector status" in normalized
    else:
        assert current_host_state == "partial"


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
    rows = _capability_inventory_rows()

    assert len(rows) == len(internal_callable_capability_ids())
    for capability_id, (_syscall, preconditions, _evidence) in rows.items():
        assert preconditions.strip(), f"{capability_id} has no preconditions"


def test_manual_smoke_capability_preconditions_reflect_registry_metadata() -> None:
    rows = _capability_inventory_rows()

    assert set(rows) == set(internal_callable_capability_ids())
    for capability_id, (syscall, preconditions, _evidence) in rows.items():
        capability = get_capability(capability_id)
        assert capability is not None, capability_id
        assert run_callable_signature(syscall), f"{syscall} has no model-facing signature"

        required_scopes = capability.contract_metadata.get("required_scopes")
        if isinstance(required_scopes, list) and required_scopes:
            assert "Google connected" in preconditions, capability_id
            for scope in required_scopes:
                assert isinstance(scope, str), capability_id
                scope_token = scope.rsplit("/auth/", maxsplit=1)[-1]
                assert f"`{scope_token}`" in preconditions, capability_id

        attendee_scope = capability.contract_metadata.get("attendee_intersection_scope")
        if isinstance(attendee_scope, str):
            scope_token = attendee_scope.rsplit("/auth/", maxsplit=1)[-1]
            assert f"`{scope_token}`" in preconditions, capability_id
            assert "all-attendee" in preconditions, capability_id

        if capability.policy_decision == "requires_approval":
            assert "approval" in preconditions.lower(), capability_id

        if capability_id.startswith("cap.agency."):
            assert "Agency runtime configured" in preconditions, capability_id
        if capability_id.startswith("cap.memory."):
            assert "Memory runtime configured" in preconditions, capability_id
        if capability_id == "cap.attachment.read":
            assert "attachment refs" in preconditions, capability_id
        if capability_id == "cap.research.investigate":
            assert "Research mode" in preconditions, capability_id
            assert "bounded non-poll question" in preconditions, capability_id
            assert "child capabilities" not in preconditions, capability_id
        if capability_id in {"cap.memory.remember", "cap.proactive.schedule"}:
            assert "worker drains" not in preconditions, capability_id
        expected_provider_precondition = PROVIDER_BOUND_CAPABILITY_PRECONDITIONS.get(capability_id)
        if expected_provider_precondition is not None:
            assert expected_provider_precondition in preconditions, capability_id


def test_manual_smoke_capability_evidence_ledger_tracks_registry() -> None:
    expected_capability_ids = set(internal_callable_capability_ids())

    rows = _four_column_evidence_rows("### Capability Evidence Ledger")
    _assert_four_column_evidence_rows(rows=rows, expected_items=expected_capability_ids)
    for capability_id, contract_state, _current_host_state, _evidence in rows:
        assert contract_state == "passed", capability_id


def test_manual_smoke_capability_current_host_passed_requires_success_outcomes() -> None:
    forbidden_success_fragments = (
        "read_outcome=too_large",
        "partial=true",
        "remains unproven",
    )
    for capability_id, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
        "### Capability Evidence Ledger"
    ):
        if current_host_state != "passed":
            continue
        normalized = evidence.lower()
        assert all(fragment not in normalized for fragment in forbidden_success_fragments), (
            capability_id
        )


def test_manual_smoke_provider_bound_capability_passed_rows_cite_live_provider_and_env() -> None:
    capability_section = _section("## Agent Capability Inventory")
    assert "direct capability/provider smokes" in capability_section
    assert "not imply main-agent model readiness" in capability_section

    env_states = {
        item: current_host_state
        for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
            "### Env Var Evidence Ledger"
        )
    }
    capability_rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### Capability Evidence Ledger"
        )
    }

    for capability_id, (env_name, provider_name) in PROVIDER_BOUND_CURRENT_HOST_PROOF.items():
        current_host_state, evidence = capability_rows[capability_id]
        if current_host_state == "passed":
            assert env_states[env_name] == "passed", capability_id
            assert "Live " in evidence, capability_id
            assert provider_name in evidence, capability_id


def test_manual_smoke_discord_approval_rows_cite_api_side_effect_fixtures() -> None:
    rows = {
        action: evidence
        for action, _contract_state, _current_host_state, evidence in _four_column_evidence_rows(
            "### Discord User Action Evidence Ledger"
        )
    }

    assert (
        "tests/integration/test_agency_receipt_reconcile.py::"
        "test_agency_run_approval_decision_worker_execution_records_job_once"
    ) in rows["Approval approve button"]
    assert (
        "tests/integration/test_agency_receipt_reconcile.py::"
        "test_approval_decision_api_denies_without_enqueuing_execution"
    ) in rows["Approval deny button"]


def test_manual_smoke_evergreen_ledgers_do_not_pin_transient_live_ids() -> None:
    text = _manual_text()
    evergreen_text = text.split("## Recent Evidence Snapshot", maxsplit=1)[0]

    assert TRANSIENT_LIVE_ID_PATTERN.findall(evergreen_text) == []


def test_manual_smoke_agent_tool_inventory_tracks_run_tool_surface() -> None:
    tool_section = _section("## Agent Tool Inventory").split(
        "### Agent Tool Evidence Ledger", maxsplit=1
    )[0]
    documented = set(re.findall(r"^\| `([^`]+)` \|", tool_section, re.MULTILINE))

    assert documented == {tool.name for tool in run_tool_definitions()}


def test_manual_smoke_agent_tool_evidence_ledger_tracks_run_tool_surface() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Agent Tool Evidence Ledger"),
        expected_items={tool.name for tool in run_tool_definitions()},
    )


def test_manual_smoke_research_mode_evidence_ledger_tracks_runtime_modes() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Research Mode Evidence Ledger"),
        expected_items=RESEARCH_MODE_EVIDENCE_ITEMS,
    )


def test_manual_smoke_ai_judgment_evidence_ledger_tracks_schema_constraint() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### AI Judgment Evidence Ledger"),
        expected_items=_ai_judgment_types(),
    )


def test_manual_smoke_ai_judgment_current_host_evidence_requires_live_audit_rows() -> None:
    for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
        "### AI Judgment Evidence Ledger"
    ):
        if current_host_state in {"passed", "partial"}:
            normalized = evidence.lower()
            assert "ai_judgments" in normalized and "row" in normalized, item


def test_manual_smoke_agent_loop_rail_evidence_ledger_tracks_doc_contract() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Agent Loop Rail Evidence Ledger"),
        expected_items=AGENT_LOOP_RAILS,
    )


def test_maps_surface_stays_read_only_without_write_or_proactive_caps() -> None:
    assert MAPS_CAPABILITY_IDS == {"cap.maps.directions", "cap.maps.search_places"}
    assert {
        capability_id
        for capability_id in internal_callable_capability_ids()
        if capability_id.startswith("cap.maps.")
    } == MAPS_CAPABILITY_IDS
    assert not any("maps" in task_type for task_type in _background_task_types())
    for capability_id in MAPS_CAPABILITY_IDS:
        capability = get_capability(capability_id)
        assert capability is not None, capability_id
        assert capability.impact_level == "read", capability_id
        assert capability.policy_decision == "allow_inline", capability_id


def test_manual_smoke_discord_blocked_ui_rows_name_ui_evidence_gap() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### Discord User Action Evidence Ledger"
        )
    }
    for item in (
        "Owner DM",
        "Home-guild ambient message",
        "Owner DM attachment-only no-instruction message",
        "Home-guild attachment-only no-instruction message",
        "Owner DM attachment read request",
        "Home-guild attachment read request",
        "Origin reply routing",
    ):
        current_host_state, evidence = rows[item]
        if current_host_state == "blocked":
            assert "Discord" in evidence and ("UI" in evidence or "ui" in evidence)


def test_manual_smoke_discord_slash_rows_cover_owner_dm_contexts() -> None:
    rows = {
        item: evidence
        for item, _contract_state, _current_host_state, evidence in _four_column_evidence_rows(
            "### Discord User Action Evidence Ledger"
        )
    }

    assert "test_slash_status_allows_configured_user_dm" in rows["/status"]
    assert "test_slash_jobs_allows_configured_user_dm" in rows["/jobs"]
    assert "test_slash_capture_allows_configured_user_dm" in rows["/capture"]


def test_manual_smoke_discord_attachment_and_approval_rows_split_transport_from_execution() -> None:
    rows = {
        item: evidence
        for item, _contract_state, _current_host_state, evidence in _four_column_evidence_rows(
            "### Discord User Action Evidence Ledger"
        )
    }

    assert "model-independent" in rows["Owner DM attachment-only no-instruction message"]
    assert "model-independent" in rows["Home-guild attachment-only no-instruction message"]
    assert "main-model-backed tool selection" in rows["Owner DM attachment read request"]
    assert "main-model-backed tool selection" in rows["Home-guild attachment read request"]
    assert (
        "test_on_message_answers_owner_dm_attachment_read_request"
        in rows["Owner DM attachment read request"]
    )
    assert "not by Discord transport" in rows["Approval approve button"]
    assert "not by Discord transport" in rows["Approval deny button"]


def test_manual_smoke_artifact_route_passed_requires_controlled_artifact() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### HTTP Route Evidence Ledger"
        )
    }
    current_host_state, evidence = rows["GET /v1/artifacts/{artifact_id}"]

    if current_host_state == "passed":
        assert "controlled artifact" in evidence.lower()
        assert "latest" not in evidence.lower()


def test_manual_smoke_detail_routes_passed_require_controlled_rows() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### HTTP Route Evidence Ledger"
        )
    }

    email_state, email_evidence = rows["GET /v1/email/actions/{email_action_id}"]
    if email_state == "passed":
        normalized = email_evidence.lower()
        assert "controlled email action" in normalized
        assert "existing" not in normalized
        assert "latest" not in normalized

    discord_state, discord_evidence = rows["GET /v1/discord-messages/{discord_message_id}/events"]
    if discord_state == "passed":
        normalized = discord_evidence.lower()
        assert "controlled discord" in normalized
        assert "existing" not in normalized
        assert "latest" not in normalized


def test_manual_smoke_detail_route_smoke_uses_controlled_rollback_rows() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### HTTP Route Evidence Ledger"
        )
    }
    expected_markers = {
        "GET /v1/email/actions/{email_action_id}": "controlled email action",
        "GET /v1/discord-messages/{discord_message_id}/events": "controlled discord",
        "GET /v1/jobs/{job_id}": "controlled job",
        "GET /v1/jobs/{job_id}/events": "controlled job",
        "GET /v1/artifacts/{artifact_id}": "controlled artifact",
    }
    for item, marker in expected_markers.items():
        current_host_state, evidence = rows[item]
        assert current_host_state == "passed", item
        normalized = evidence.lower()
        assert marker in normalized, item
        assert "existing" not in normalized, item
        assert "latest" not in normalized, item

    smoke_sequence = _section("## Smoke Sequence")
    assert "controlled route detail smoke" in smoke_sequence
    assert "ProviderWriteReceiptRecord(" in smoke_sequence
    assert "DiscordMessageRecord(" in smoke_sequence
    assert "DiscordMessageEventRecord(" in smoke_sequence
    assert "JobRecord(" in smoke_sequence
    assert "JobEventRecord(" in smoke_sequence
    assert "ArtifactRecord(" in smoke_sequence
    assert "finally:" in smoke_sequence
    assert 'route_evidence:"partial_existing_row"' not in smoke_sequence
    assert ".email_actions[0].id" not in smoke_sequence
    assert ".discord_messages[0].id" not in smoke_sequence
    assert "order by created_at desc limit 1" not in smoke_sequence


def test_manual_smoke_runtime_syscall_inventory_lists_non_capability_syscalls() -> None:
    syscall_section = _section("## Model Runtime Syscall Inventory")
    documented = set(re.findall(r"\| `((?:agent|scratch)\.[^`]+)` \|", syscall_section))

    assert documented == NON_CAPABILITY_SYSCALLS


def test_manual_smoke_runtime_syscall_evidence_ledger_lists_non_capability_syscalls() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Model Runtime Syscall Evidence Ledger"),
        expected_items=NON_CAPABILITY_SYSCALLS,
    )


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


def test_manual_smoke_background_work_evidence_ledger_covers_inventory() -> None:
    background_section = _section("## Background Work Inventory").split(
        "### Background Work Evidence Ledger", maxsplit=1
    )[0]
    documented = {
        item.strip("`").strip()
        for item in re.findall(
            r"^\| (`?[^|`]+`?) \| [^|]+ \| [^|]+ \|$",
            background_section,
            re.MULTILINE,
        )
        if item not in {"Worker path", "---"}
    }

    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Background Work Evidence Ledger"),
        expected_items=documented,
    )


def test_manual_smoke_background_work_inventory_includes_worker_feature_rows() -> None:
    background_section = _section("## Background Work Inventory").split(
        "### Background Work Evidence Ledger", maxsplit=1
    )[0]
    documented = {
        item.strip("`").strip()
        for item in re.findall(
            r"^\| (`?[^|`]+`?) \| [^|]+ \| [^|]+ \|$",
            background_section,
            re.MULTILINE,
        )
        if item not in {"Worker path", "---"}
    }

    assert BACKGROUND_WORK_FEATURE_ITEMS.issubset(documented)


def test_manual_smoke_agency_event_evidence_ledger_tracks_worker_event_classes() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Agency Event Behavior Evidence Ledger"),
        expected_items=AGENCY_EVENT_BEHAVIOR_ITEMS,
    )


def test_manual_smoke_terminal_agency_wake_passed_requires_wake_evidence() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### Agency Event Behavior Evidence Ledger"
        )
    }
    current_host_state, evidence = rows["terminal-state wake"]

    if current_host_state == "passed":
        normalized = evidence.lower()
        assert "agent_wake" in normalized or "turn" in normalized


def test_manual_smoke_agency_signed_event_uses_controlled_job_route_reads() -> None:
    smoke_sequence = _section("## Smoke Sequence")

    assert "jobs?limit=1" not in smoke_sequence
    assert "agency job routes" in smoke_sequence
    assert 'f"http://127.0.0.1:8000/v1/jobs/{job_id}"' in smoke_sequence
    assert 'f"http://127.0.0.1:8000/v1/jobs/{job_id}/events"' in smoke_sequence
    assert "agent_wake_count" in smoke_sequence
    assert "wake_note_pattern" in smoke_sequence


def test_manual_smoke_job_detail_routes_passed_require_controlled_rollback_rows() -> None:
    rows = {
        item: (current_host_state, evidence)
        for item, _contract_state, current_host_state, evidence in _four_column_evidence_rows(
            "### HTTP Route Evidence Ledger"
        )
    }

    for item in ("GET /v1/jobs/{job_id}", "GET /v1/jobs/{job_id}/events"):
        current_host_state, evidence = rows[item]
        if current_host_state == "passed":
            normalized = evidence.lower()
            assert "controlled job" in normalized
            assert "latest" not in normalized
            assert "existing" not in normalized
            assert "historical" not in normalized


def test_manual_smoke_agency_event_aggregate_rows_wait_for_all_event_classes() -> None:
    behavior_states = {
        item: current_host_state
        for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
            "### Agency Event Behavior Evidence Ledger"
        )
    }
    aggregate_states = {
        "Agency event ingest": {
            item: current_host_state
            for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
                "### Background Work Evidence Ledger"
            )
        }["Agency event ingest"],
        "agency_event_received": {
            item: current_host_state
            for item, _contract_state, current_host_state, _evidence in _four_column_evidence_rows(
                "### Background Task Type Evidence Ledger"
            )
        }["agency_event_received"],
    }

    if any(state != "passed" for state in behavior_states.values()):
        assert all(state != "passed" for state in aggregate_states.values())


def test_manual_smoke_background_task_inventory_tracks_schema_constraint() -> None:
    task_section = _section("## Background Task Type Inventory").split(
        "### Background Task Type Evidence Ledger", maxsplit=1
    )[0]
    documented = set(re.findall(r"\| `([a-z_]+)` \|", task_section))

    assert documented == _background_task_types()


def test_manual_smoke_background_task_evidence_ledger_tracks_schema_constraint() -> None:
    _assert_four_column_evidence_rows(
        rows=_four_column_evidence_rows("### Background Task Type Evidence Ledger"),
        expected_items=_background_task_types(),
    )


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
    assert "provider_reconcile_schedule" in smoke_sequence
    assert "recurrence_seconds" in smoke_sequence


def test_manual_smoke_google_connector_smoke_reports_granted_scopes() -> None:
    route_section = _section("## HTTP And User Action Inventory")
    smoke_sequence = _section("## Smoke Sequence")

    assert "granted scopes" in route_section
    assert ".connector.granted_scopes" in smoke_sequence
    assert ".connector.scopes" not in smoke_sequence
