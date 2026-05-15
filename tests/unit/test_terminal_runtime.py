from __future__ import annotations

from pathlib import Path
import time

import pytest
from ariel.capability_registry import get_capability
from ariel.executor import execute_capability
from ariel.policy_engine import evaluate_proposal
from ariel.terminal_runtime import (
    execute_terminal_cancel,
    execute_terminal_read_output,
    execute_terminal_run,
    execute_terminal_run_background,
    execute_terminal_status,
)


@pytest.fixture(autouse=True)
def _isolated_terminal_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))


def test_terminal_run_records_exit_code_output_and_truncation(tmp_path: Path) -> None:
    output = execute_terminal_run(
        {"cwd": str(tmp_path), "command": "printf hello", "purpose": "test output"}
    )

    assert output["status"] == "completed"
    assert output["cwd"] == str(tmp_path)
    assert output["exit_code"] == 0
    assert output["stdout"] == "hello"
    assert output["stderr"] == ""
    assert output["stdout_ref"].endswith("/stdout.txt")
    assert output["stderr_ref"].endswith("/stderr.txt")
    assert output["truncated"] is False
    assert isinstance(output["duration_ms"], int)


def test_terminal_background_status_and_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))
    output = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "printf hello; printf err >&2",
            "purpose": "test background",
            "_action_attempt_id": "act_test",
            "_session_id": "ses_test",
        }
    )
    command_id = output["command_id"]
    assert command_id == "act_test"
    assert output["action_attempt_id"] == "act_test"
    assert output["process_group_id"] == output["pid"]
    assert output["stdout_ref"].endswith("/stdout.txt")
    assert output["stderr_ref"].endswith("/stderr.txt")
    assert output["exit_code_ref"].endswith("/exit_code.txt")

    for _ in range(50):
        status = execute_terminal_status({"command_id": command_id, "_session_id": "ses_test"})
        if status["status"] == "completed":
            break
        time.sleep(0.02)

    assert status["status"] == "completed"
    assert status["exit_code"] == 0
    assert status["action_attempt_id"] == "act_test"
    stdout = execute_terminal_read_output(
        {
            "command_id": command_id,
            "stream": "stdout",
            "offset": 0,
            "limit": 100,
            "_session_id": "ses_test",
        }
    )
    stderr = execute_terminal_read_output(
        {
            "command_id": command_id,
            "stream": "stderr",
            "offset": 0,
            "limit": 100,
            "_session_id": "ses_test",
        }
    )
    assert stdout["text"] == "hello"
    assert stdout["action_attempt_id"] == "act_test"
    assert stderr["text"] == "err"


def test_terminal_background_reuses_action_attempt_command_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))
    first = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "printf first",
            "purpose": "first try",
            "_action_attempt_id": "act_retry",
            "_session_id": "ses_owner",
        }
    )
    second = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "printf second",
            "purpose": "retry should not rerun",
            "_action_attempt_id": "act_retry",
            "_session_id": "ses_owner",
        }
    )

    assert first["command_id"] == "act_retry"
    assert second["command_id"] == "act_retry"
    assert second["command"] == "printf first"


def test_terminal_run_times_out_with_process_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_RUN_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("ARIEL_TERMINAL_TIMEOUT_KILL_AFTER_SECONDS", "0.1")
    output = execute_terminal_run(
        {"cwd": str(tmp_path), "command": "sleep 60", "purpose": "test timeout"}
    )

    assert output["status"] == "timeout"
    assert output["exit_code"] == 124
    assert "terminated" in output["stderr"]


def test_terminal_run_bounds_output_while_process_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_RUN_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("ARIEL_TERMINAL_TIMEOUT_KILL_AFTER_SECONDS", "0.1")
    output = execute_terminal_run(
        {"cwd": str(tmp_path), "command": "yes", "purpose": "test bounded output"}
    )

    assert output["status"] == "timeout"
    assert len(output["stdout"]) == 12000
    assert output["truncated"] is True


def test_terminal_policy_classifies_shell_commands(tmp_path: Path) -> None:
    run = get_capability("cap.terminal.run")
    assert run is not None
    worktree = Path.cwd()
    read_command = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(worktree), "command": "git status --short", "purpose": "inspect"},
        pending_approval_exists=False,
    )
    cross_repo_read = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(tmp_path), "command": "pwd", "purpose": "inspect elsewhere"},
        pending_approval_exists=False,
    )
    mutating = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(worktree), "command": "git reset --hard", "purpose": "mutate"},
        pending_approval_exists=False,
    )
    python = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": 'python -c \'open("x", "w").write("x")\'',
            "purpose": "mutate",
        },
        pending_approval_exists=False,
    )
    find_delete = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": "find . -name x -delete",
            "purpose": "mutate",
        },
        pending_approval_exists=False,
    )
    sed_in_place = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": "sed -i 's/a/b/' file.txt",
            "purpose": "mutate",
        },
        pending_approval_exists=False,
    )
    secret_read = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(worktree), "command": "cat .env", "purpose": "read secret"},
        pending_approval_exists=False,
    )
    home_secret_read = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": 'sed -n 1p "$HOME/.ssh/id_rsa"',
            "purpose": "read secret",
        },
        pending_approval_exists=False,
    )
    curl = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": "curl https://example.com",
            "purpose": "network",
        },
        pending_approval_exists=False,
    )
    kubectl = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(worktree), "command": "kubectl get pods", "purpose": "prod"},
        pending_approval_exists=False,
    )
    hidden_network = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(worktree),
            "command": "awk 'BEGIN{system(\"curl https://example.com\")}'",
            "purpose": "network",
        },
        pending_approval_exists=False,
    )

    assert read_command.decision == "allow_inline"
    assert cross_repo_read.decision == "requires_approval"
    assert mutating.decision == "requires_approval"
    assert python.decision == "requires_approval"
    assert find_delete.decision == "requires_approval"
    assert sed_in_place.decision == "requires_approval"
    assert find_delete.impact_level == "write_reversible"
    assert secret_read.decision == "deny"
    assert secret_read.reason == "terminal_command_denied_secret_path"
    assert home_secret_read.decision == "deny"
    assert home_secret_read.reason == "terminal_command_denied_secret_path"
    assert curl.impact_level == "external_send"
    assert curl.reason == "terminal_network_command_requires_approval"
    assert kubectl.impact_level == "write_irreversible"
    assert kubectl.reason == "terminal_production_command_requires_approval"
    assert hidden_network.decision == "requires_approval"
    assert hidden_network.impact_level == "external_send"


@pytest.mark.parametrize(
    "command",
    [
        "head <.env",
        "cat ./.env",
        'cat "$PWD/.env"',
        'head ".env"',
        "sed -n '1p' '.env'",
        "head .env.local",
        "head .[e]nv",
        "git show HEAD:.env",
        "head ~/.npmrc",
        "head ~/.git-credentials",
        "head ~/.config/gh/hosts.yml",
        "head /proc/self/environ",
        "rg --hidden API_KEY .",
        "rg -uu API_KEY .",
        "head .*",
    ],
)
def test_terminal_policy_denies_secret_read_variants(command: str, tmp_path: Path) -> None:
    result = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={"cwd": str(tmp_path), "command": command, "purpose": "read secret"},
        pending_approval_exists=False,
    )

    assert result.decision == "deny"
    assert result.reason == "terminal_command_denied_secret_path"


def test_terminal_policy_denies_absolute_env_path(tmp_path: Path) -> None:
    result = evaluate_proposal(
        capability_id="cap.terminal.run",
        input_payload={
            "cwd": str(tmp_path),
            "command": f"cat {tmp_path / '.env'}",
            "purpose": "read secret",
        },
        pending_approval_exists=False,
    )

    assert result.decision == "deny"
    assert result.reason == "terminal_command_denied_secret_path"


def test_terminal_runtime_denies_secret_path_even_when_called_directly(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FAKE_SECRET=1\n", encoding="utf-8")
    output = execute_terminal_run(
        {
            "cwd": str(tmp_path),
            "command": "head <.env",
            "purpose": "read secret",
        }
    )

    assert output["status"] == "denied"
    assert output["exit_code"] == 126
    assert "terminal_command_denied_secret_path" in output["stderr"]
    assert "FAKE_SECRET" not in output["stdout"]
    assert "FAKE_SECRET" not in output["stderr"]


def test_terminal_run_uses_sandbox_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    terminal_dir = tmp_path / "terminal"
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(terminal_dir))
    output = execute_terminal_run(
        {
            "cwd": str(tmp_path),
            "command": 'printf "$HOME"',
            "purpose": "inspect home",
        }
    )

    assert output["status"] == "completed"
    assert output["stdout"] == str(terminal_dir / "home")


def test_terminal_output_reads_are_session_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))
    output = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "printf hello",
            "purpose": "test session scope",
            "_action_attempt_id": "act_scope",
            "_session_id": "ses_owner",
        }
    )

    assert execute_terminal_status(
        {"command_id": output["command_id"], "_session_id": "ses_other"}
    ) == {
        "status": "not_found",
        "command_id": output["command_id"],
        "error": "terminal_command_not_found",
    }
    assert execute_terminal_status({"command_id": output["command_id"]}) == {
        "status": "not_found",
        "command_id": output["command_id"],
        "error": "terminal_command_not_found",
    }
    assert execute_terminal_read_output(
        {
            "command_id": output["command_id"],
            "stream": "stdout",
            "offset": 0,
            "limit": 100,
        }
    ) == {
        "status": "not_found",
        "command_id": output["command_id"],
        "error": "terminal_command_not_found",
    }


def test_terminal_status_inspection_succeeds_for_failed_background_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))
    output = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "exit 7",
            "purpose": "test failed command status",
            "_session_id": "ses_owner",
        }
    )
    command_id = output["command_id"]
    for _ in range(50):
        status = execute_terminal_status({"command_id": command_id, "_session_id": "ses_owner"})
        if status["status"] == "completed":
            break
        time.sleep(0.02)

    capability = get_capability("cap.terminal.status")
    assert capability is not None
    result = execute_capability(
        capability=capability,
        normalized_input={"command_id": command_id, "_session_id": "ses_owner"},
    )
    assert result.status == "succeeded"
    assert result.output is not None
    assert result.output["exit_code"] == 7


def test_terminal_cancel_is_session_scoped_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))
    output = execute_terminal_run_background(
        {
            "cwd": str(tmp_path),
            "command": "sleep 60",
            "purpose": "test cancel",
            "_session_id": "ses_owner",
        }
    )
    command_id = output["command_id"]

    assert execute_terminal_cancel({"command_id": command_id, "_session_id": "ses_other"}) == {
        "status": "not_found",
        "command_id": command_id,
        "error": "terminal_command_not_found",
    }
    cancelled = execute_terminal_cancel({"command_id": command_id, "_session_id": "ses_owner"})
    repeated = execute_terminal_cancel({"command_id": command_id, "_session_id": "ses_owner"})
    status_after_cancel = execute_terminal_status(
        {"command_id": command_id, "_session_id": "ses_owner"}
    )

    assert cancelled == {"status": "cancelled", "command_id": command_id, "exit_code": 130}
    assert repeated == {
        "status": "already_completed",
        "command_id": command_id,
        "exit_code": 130,
    }
    assert status_after_cancel["status"] == "cancelled"
    assert status_after_cancel["exit_code"] == 130


def test_terminal_command_id_rejects_path_traversal() -> None:
    status = get_capability("cap.terminal.status")
    read_output = get_capability("cap.terminal.read_output")
    assert status is not None
    assert read_output is not None

    assert status.validate_input({"command_id": ".."}) == (None, "schema_invalid")
    assert status.validate_input({"command_id": "safe..id"}) == (None, "schema_invalid")
    assert read_output.validate_input(
        {"command_id": "../x", "stream": "stdout", "offset": 0, "limit": 1}
    ) == (None, "schema_invalid")
