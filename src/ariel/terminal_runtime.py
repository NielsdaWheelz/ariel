from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import selectors
import signal
import shlex
import subprocess
import time
import uuid
from typing import Any

from .config import AppSettings
from .terminal_safety import denylisted_terminal_command


_TIMEOUT_EXIT_CODE = 124
_CANCELLED_EXIT_CODE = 130


def execute_terminal_run(input_payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    settings = AppSettings()
    output_limit = settings.terminal_output_limit_bytes
    kill_after_seconds = settings.terminal_timeout_kill_after_seconds
    action_attempt_id = input_payload.get("_action_attempt_id")
    session_id = input_payload.get("_session_id")
    command_id = _new_command_id(action_attempt_id)
    command_dir = _command_dir(command_id, input_payload)
    existing_result = _existing_command_result(command_id, input_payload)
    if existing_result is not None:
        return existing_result
    if command_dir.exists():
        return _orphaned_command_result(command_id, input_payload)
    command_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    stdout_path = command_dir / "stdout.txt"
    stderr_path = command_dir / "stderr.txt"
    exit_path = command_dir / "exit_code.txt"
    metadata_path = command_dir / "metadata.json"
    started_at = datetime.now(UTC).isoformat()
    denied_reason = denylisted_terminal_command(input_payload["cwd"], input_payload["command"])
    if denied_reason is not None:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(denied_reason + "\n", encoding="utf-8")
        exit_path.write_text("126", encoding="utf-8")
        metadata = {
            "command_id": command_id,
            "pid": 0,
            "process_group_id": 0,
            "process_start_token": None,
            "kind": "foreground",
            "cwd": input_payload["cwd"],
            "command": input_payload["command"],
            "purpose": input_payload["purpose"],
            "started_at": started_at,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "exit_path": str(exit_path),
            "terminal_dir": str(_base_dir(input_payload)),
            "output_limit_bytes": output_limit,
        }
        if isinstance(action_attempt_id, str):
            metadata["action_attempt_id"] = action_attempt_id
        if isinstance(session_id, str):
            metadata["session_id"] = session_id
        metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        return {
            "command_id": command_id,
            "status": "denied",
            "cwd": input_payload["cwd"],
            "command": input_payload["command"],
            "purpose": input_payload["purpose"],
            "pid": 0,
            "process_group_id": 0,
            "process_start_token": None,
            "started_at": started_at,
            "exit_code": 126,
            "stdout": "",
            "stderr": denied_reason + "\n",
            "stdout_ref": str(stdout_path),
            "stderr_ref": str(stderr_path),
            "exit_code_ref": str(exit_path),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "truncated": False,
            "output_limit_bytes": output_limit,
        }
    process = subprocess.Popen(
        ["/bin/bash", "-c", input_payload["command"]],
        cwd=input_payload["cwd"],
        env=_terminal_env(input_payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    process_group_id = os.getpgid(process.pid)
    process_start_token = _process_start_token(process.pid)
    metadata = {
        "command_id": command_id,
        "pid": process.pid,
        "process_group_id": process_group_id,
        "process_start_token": process_start_token,
        "kind": "foreground",
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "started_at": started_at,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "exit_path": str(exit_path),
        "terminal_dir": str(_base_dir(input_payload)),
        "output_limit_bytes": output_limit,
    }
    if isinstance(action_attempt_id, str):
        metadata["action_attempt_id"] = action_attempt_id
    if isinstance(session_id, str):
        metadata["session_id"] = session_id
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    timed_out = False
    truncated = False
    stdout_size = 0
    stderr_size = 0
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + settings.terminal_run_timeout_seconds
    with stdout_path.open("wb") as stdout_file:
        with stderr_path.open("wb") as stderr_file:
            while selector.get_map():
                if not timed_out and time.monotonic() > deadline:
                    timed_out = True
                    _terminate_process_group(
                        process.pid,
                        process_group_id=process_group_id,
                        process_start_token=process_start_token,
                        kill_after_seconds=kill_after_seconds,
                    )
                    for registered in list(selector.get_map().values()):
                        selector.unregister(registered.fileobj)
                    process.stdout.close()
                    process.stderr.close()
                    break
                for key, _ in selector.select(timeout=0.05):
                    file_descriptor = key.fd
                    chunk = os.read(file_descriptor, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        remaining = output_limit - stdout_size
                        if remaining > 0:
                            stdout_file.write(chunk[:remaining])
                            stdout_size += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            truncated = True
                    else:
                        remaining = output_limit - stderr_size
                        if remaining > 0:
                            stderr_file.write(chunk[:remaining])
                            stderr_size += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            truncated = True
    selector.close()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    if timed_out:
        timeout_message = (
            f"\nterminal.run exceeded {settings.terminal_run_timeout_seconds:g} "
            "seconds and was terminated\n"
        ).encode()
        with stderr_path.open("ab") as stderr_file:
            remaining = output_limit - stderr_size
            if remaining > 0:
                stderr_file.write(timeout_message[:remaining])
                stderr_size += min(len(timeout_message), remaining)
            if len(timeout_message) > remaining:
                truncated = True
        exit_code = _TIMEOUT_EXIT_CODE
        status = "timeout"
    else:
        exit_code = int(process.returncode)
        status = "completed"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    exit_path.write_text(str(exit_code), encoding="utf-8")
    metadata = {
        "command_id": command_id,
        "pid": process.pid,
        "process_group_id": process_group_id,
        "process_start_token": process_start_token,
        "kind": "foreground",
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "started_at": started_at,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "exit_path": str(exit_path),
        "terminal_dir": str(_base_dir(input_payload)),
        "output_limit_bytes": output_limit,
    }
    if isinstance(action_attempt_id, str):
        metadata["action_attempt_id"] = action_attempt_id
    if isinstance(session_id, str):
        metadata["session_id"] = session_id
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    return {
        "command_id": command_id,
        "status": status,
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "pid": process.pid,
        "process_group_id": process_group_id,
        "process_start_token": process_start_token,
        "started_at": started_at,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_ref": str(stdout_path),
        "stderr_ref": str(stderr_path),
        "exit_code_ref": str(exit_path),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "truncated": truncated,
        "output_limit_bytes": output_limit,
    }


def execute_terminal_run_background(input_payload: dict[str, Any]) -> dict[str, Any]:
    settings = AppSettings()
    action_attempt_id = input_payload.get("_action_attempt_id")
    session_id = input_payload.get("_session_id")
    command_id = _new_command_id(action_attempt_id)
    command_dir = _command_dir(command_id, input_payload)
    existing_result = _existing_command_result(command_id, input_payload)
    if existing_result is not None:
        return existing_result
    if command_dir.exists():
        return _orphaned_command_result(command_id, input_payload)
    command_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    stdout_path = command_dir / "stdout.txt"
    stderr_path = command_dir / "stderr.txt"
    exit_path = command_dir / "exit_code.txt"
    script_path = command_dir / "run.sh"
    started_at = datetime.now(UTC).isoformat()
    denied_reason = denylisted_terminal_command(input_payload["cwd"], input_payload["command"])
    if denied_reason is not None:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(denied_reason + "\n", encoding="utf-8")
        exit_path.write_text("126", encoding="utf-8")
        metadata = {
            "command_id": command_id,
            "pid": 0,
            "process_group_id": 0,
            "process_start_token": None,
            "kind": "background",
            "cwd": input_payload["cwd"],
            "command": input_payload["command"],
            "purpose": input_payload["purpose"],
            "started_at": started_at,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "exit_path": str(exit_path),
            "terminal_dir": str(_base_dir(input_payload)),
            "output_limit_bytes": settings.terminal_output_limit_bytes,
        }
        if isinstance(action_attempt_id, str):
            metadata["action_attempt_id"] = action_attempt_id
        if isinstance(session_id, str):
            metadata["session_id"] = session_id
        (command_dir / "metadata.json").write_text(
            json.dumps(metadata, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "status": "denied",
            "command_id": command_id,
            "pid": 0,
            "process_group_id": 0,
            "process_start_token": None,
            "cwd": input_payload["cwd"],
            "command": input_payload["command"],
            "purpose": input_payload["purpose"],
            "started_at": started_at,
            "stdout_ref": str(stdout_path),
            "stderr_ref": str(stderr_path),
            "exit_code_ref": str(exit_path),
            "output_limit_bytes": settings.terminal_output_limit_bytes,
            "action_attempt_id": action_attempt_id if isinstance(action_attempt_id, str) else None,
            "session_id": session_id if isinstance(session_id, str) else None,
        }
    output_limit_blocks = max(1, (settings.terminal_output_limit_bytes + 511) // 512)
    metadata = {
        "command_id": command_id,
        "pid": 0,
        "process_group_id": 0,
        "process_start_token": None,
        "kind": "background",
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "started_at": started_at,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "exit_path": str(exit_path),
        "terminal_dir": str(_base_dir(input_payload)),
        "output_limit_bytes": settings.terminal_output_limit_bytes,
    }
    if isinstance(action_attempt_id, str):
        metadata["action_attempt_id"] = action_attempt_id
    if isinstance(session_id, str):
        metadata["session_id"] = session_id
    metadata_path = command_dir / "metadata.json"
    metadata_tmp_path = command_dir / "metadata.json.tmp"
    metadata_tmp_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    os.replace(metadata_tmp_path, metadata_path)
    script_path.write_text(
        "#!/bin/bash\n"
        "set +e\n"
        f"ulimit -f {output_limit_blocks}\n"
        f"export ARIEL_EXIT_PATH={str(exit_path)!r}\n"
        'trap \'code=$?; printf "%s" "$code" > "$ARIEL_EXIT_PATH"\' EXIT\n'
        "exec </dev/null\n"
        f"timeout --kill-after={settings.terminal_timeout_kill_after_seconds:g}s "
        f"{settings.terminal_background_timeout_seconds}s "
        f"/bin/bash -c {shlex.quote(input_payload['command'])}\n",
        encoding="utf-8",
    )
    os.chmod(script_path, 0o700)
    with stdout_path.open("w", encoding="utf-8") as stdout_file:
        with stderr_path.open("w", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                ["/bin/bash", str(script_path)],
                cwd=input_payload["cwd"],
                env=_terminal_env(input_payload),
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
    process_group_id = os.getpgid(process.pid)
    process_start_token = _process_start_token(process.pid)
    metadata = {
        "command_id": command_id,
        "pid": process.pid,
        "process_group_id": process_group_id,
        "process_start_token": process_start_token,
        "kind": "background",
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "started_at": started_at,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "exit_path": str(exit_path),
        "terminal_dir": str(_base_dir(input_payload)),
        "output_limit_bytes": settings.terminal_output_limit_bytes,
    }
    if isinstance(action_attempt_id, str):
        metadata["action_attempt_id"] = action_attempt_id
    if isinstance(session_id, str):
        metadata["session_id"] = session_id
    metadata_tmp_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    os.replace(metadata_tmp_path, metadata_path)
    return {
        "status": "running",
        "command_id": command_id,
        "pid": process.pid,
        "process_group_id": process_group_id,
        "process_start_token": process_start_token,
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "started_at": started_at,
        "stdout_ref": str(stdout_path),
        "stderr_ref": str(stderr_path),
        "exit_code_ref": str(exit_path),
        "output_limit_bytes": settings.terminal_output_limit_bytes,
        "action_attempt_id": action_attempt_id if isinstance(action_attempt_id, str) else None,
        "session_id": session_id if isinstance(session_id, str) else None,
    }


def execute_terminal_status(input_payload: dict[str, Any]) -> dict[str, Any]:
    metadata, error = _read_metadata(input_payload["command_id"], input_payload)
    if error is not None:
        return {"status": "not_found", "command_id": input_payload["command_id"], "error": error}
    session_id = input_payload.get("_session_id")
    if not isinstance(session_id, str) or metadata.get("session_id") != session_id:
        return {
            "status": "not_found",
            "command_id": input_payload["command_id"],
            "error": "terminal_command_not_found",
        }
    exit_code = _read_exit_code(Path(metadata["exit_path"]))
    alive = (
        _pid_alive(
            int(metadata["pid"]),
            process_start_token=(
                metadata.get("process_start_token")
                if isinstance(metadata.get("process_start_token"), str)
                else None
            ),
        )
        if exit_code is None
        else False
    )
    return {
        "status": (
            _command_status_from_exit_code(exit_code)
            if exit_code is not None
            else "running"
            if alive
            else "unknown"
        ),
        "command_id": metadata["command_id"],
        "pid": metadata["pid"],
        "process_group_id": metadata["process_group_id"],
        "process_start_token": metadata.get("process_start_token"),
        "cwd": metadata["cwd"],
        "command": metadata["command"],
        "purpose": metadata["purpose"],
        "started_at": metadata["started_at"],
        "exit_code": exit_code,
        "alive": alive,
        "action_attempt_id": metadata.get("action_attempt_id"),
        "session_id": metadata.get("session_id"),
    }


def execute_terminal_read_output(input_payload: dict[str, Any]) -> dict[str, Any]:
    metadata, error = _read_metadata(input_payload["command_id"], input_payload)
    if error is not None:
        return {"status": "not_found", "command_id": input_payload["command_id"], "error": error}
    session_id = input_payload.get("_session_id")
    if not isinstance(session_id, str) or metadata.get("session_id") != session_id:
        return {
            "status": "not_found",
            "command_id": input_payload["command_id"],
            "error": "terminal_command_not_found",
        }
    stream = input_payload["stream"]
    path = Path(metadata["stdout_path"] if stream == "stdout" else metadata["stderr_path"])
    offset = input_payload["offset"]
    limit = input_payload["limit"]
    if path.exists():
        bytes_available = path.stat().st_size
        with path.open("rb") as output_file:
            output_file.seek(offset)
            raw = output_file.read(limit)
    else:
        bytes_available = 0
        raw = b""
    chunk = raw.decode("utf-8", errors="replace")
    return {
        "status": "read",
        "command_id": metadata["command_id"],
        "stream": stream,
        "offset": offset,
        "next_offset": offset + len(raw),
        "bytes_available": bytes_available,
        "text": chunk,
        "truncated": offset + len(raw) < bytes_available,
        "action_attempt_id": metadata.get("action_attempt_id"),
        "session_id": metadata.get("session_id"),
    }


def execute_terminal_cancel(input_payload: dict[str, Any]) -> dict[str, Any]:
    metadata, error = _read_metadata(input_payload["command_id"], input_payload)
    if error is not None:
        return {"status": "not_found", "command_id": input_payload["command_id"], "error": error}
    session_id = input_payload.get("_session_id")
    if not isinstance(session_id, str) or metadata.get("session_id") != session_id:
        return {
            "status": "not_found",
            "command_id": input_payload["command_id"],
            "error": "terminal_command_not_found",
        }
    exit_path = Path(metadata["exit_path"])
    exit_code = _read_exit_code(exit_path)
    if exit_code is not None:
        return {
            "status": "already_completed",
            "command_id": metadata["command_id"],
            "exit_code": exit_code,
        }
    process_start_token = (
        metadata.get("process_start_token")
        if isinstance(metadata.get("process_start_token"), str)
        else None
    )
    pid = int(metadata["pid"])
    was_alive = _pid_alive(pid, process_start_token=process_start_token)
    _terminate_process_group(
        pid,
        process_group_id=(
            int(metadata["process_group_id"])
            if isinstance(metadata.get("process_group_id"), int)
            else None
        ),
        process_start_token=process_start_token,
    )
    exit_code = _read_exit_code(exit_path) if not was_alive else None
    if exit_code is not None:
        return {
            "status": "already_completed",
            "command_id": metadata["command_id"],
            "exit_code": exit_code,
        }
    exit_path.write_text(str(_CANCELLED_EXIT_CODE), encoding="utf-8")
    return {
        "status": "cancelled",
        "command_id": metadata["command_id"],
        "exit_code": _CANCELLED_EXIT_CODE,
    }


def _terminal_env(input_payload: dict[str, Any]) -> dict[str, str]:
    home = _base_dir(input_payload) / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def _new_command_id(action_attempt_id: Any) -> str:
    if isinstance(action_attempt_id, str) and _valid_command_id(action_attempt_id.strip()):
        return action_attempt_id.strip()
    return uuid.uuid4().hex


def _existing_command_result(
    command_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any] | None:
    metadata, error = _read_metadata(command_id, input_payload)
    if error is not None:
        return None
    exit_code = _read_exit_code(Path(metadata["exit_path"]))
    process_start_token = (
        metadata.get("process_start_token")
        if isinstance(metadata.get("process_start_token"), str)
        else None
    )
    alive = (
        _pid_alive(int(metadata["pid"]), process_start_token=process_start_token)
        if exit_code is None
        else False
    )
    if exit_code is None:
        status = "running" if alive else "unknown"
    else:
        status = _command_status_from_exit_code(exit_code)
    stdout_path = Path(metadata["stdout_path"])
    stderr_path = Path(metadata["stderr_path"])
    stdout = (
        stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    )
    stderr = (
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    )
    return {
        "command_id": metadata["command_id"],
        "status": status,
        "cwd": metadata["cwd"],
        "command": metadata["command"],
        "purpose": metadata["purpose"],
        "pid": metadata["pid"],
        "process_group_id": metadata["process_group_id"],
        "process_start_token": process_start_token,
        "started_at": metadata["started_at"],
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_ref": metadata["stdout_path"],
        "stderr_ref": metadata["stderr_path"],
        "exit_code_ref": metadata["exit_path"],
        "duration_ms": None,
        "truncated": False,
        "output_limit_bytes": (
            metadata["output_limit_bytes"]
            if isinstance(metadata.get("output_limit_bytes"), int)
            and metadata["output_limit_bytes"] > 0
            else AppSettings().terminal_output_limit_bytes
        ),
        "action_attempt_id": metadata.get("action_attempt_id"),
        "session_id": metadata.get("session_id"),
    }


def _orphaned_command_result(command_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    command_dir = _command_dir(command_id, input_payload)
    return {
        "command_id": command_id,
        "status": "unknown",
        "cwd": input_payload["cwd"],
        "command": input_payload["command"],
        "purpose": input_payload["purpose"],
        "pid": 0,
        "process_group_id": 0,
        "process_start_token": None,
        "started_at": datetime.now(UTC).isoformat(),
        "exit_code": None,
        "stdout": "",
        "stderr": "terminal_command_metadata_missing\n",
        "stdout_ref": str(command_dir / "stdout.txt"),
        "stderr_ref": str(command_dir / "stderr.txt"),
        "exit_code_ref": str(command_dir / "exit_code.txt"),
        "duration_ms": None,
        "truncated": False,
        "output_limit_bytes": AppSettings().terminal_output_limit_bytes,
        "action_attempt_id": input_payload.get("_action_attempt_id"),
        "session_id": input_payload.get("_session_id"),
    }


def _command_status_from_exit_code(exit_code: int) -> str:
    if exit_code == _TIMEOUT_EXIT_CODE:
        return "timeout"
    if exit_code == _CANCELLED_EXIT_CODE:
        return "cancelled"
    if exit_code == 126:
        return "denied"
    return "completed"


def _base_dir(input_payload: dict[str, Any] | None = None) -> Path:
    if input_payload is not None:
        configured = input_payload.get("_terminal_dir")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
    return Path(AppSettings().terminal_dir).expanduser()


def _command_dir(command_id: str, input_payload: dict[str, Any] | None = None) -> Path:
    return _base_dir(input_payload) / command_id


def _read_metadata(
    command_id: str,
    input_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    if not _valid_command_id(command_id):
        return {}, "terminal_command_not_found"
    command_dir = _command_dir(command_id, input_payload)
    metadata_path = command_dir / "metadata.json"
    if not metadata_path.exists():
        return {}, "terminal_command_not_found"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except ValueError:
        return {}, "terminal_command_metadata_invalid"
    if not isinstance(metadata, dict):
        return {}, "terminal_command_metadata_invalid"
    for key in {
        "command_id",
        "pid",
        "process_group_id",
        "cwd",
        "command",
        "purpose",
        "started_at",
        "stdout_path",
        "stderr_path",
        "exit_path",
    }:
        if key not in metadata:
            return {}, "terminal_command_metadata_invalid"
    try:
        resolved_command_dir = command_dir.resolve()
        for key in {"stdout_path", "stderr_path", "exit_path"}:
            path = Path(metadata[key]).resolve()
            if not path.is_relative_to(resolved_command_dir):
                return {}, "terminal_command_metadata_invalid"
    except (OSError, TypeError):
        return {}, "terminal_command_metadata_invalid"
    return metadata, None


def _valid_command_id(command_id: str) -> bool:
    command_id_parts = command_id.split(".")
    return (
        bool(command_id)
        and len(command_id) <= 80
        and all(part and all(ch.isalnum() or ch == "_" for ch in part) for part in command_id_parts)
    )


def _read_exit_code(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _process_start_token(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if ") " not in stat_text:
        return None
    fields_after_name = stat_text.rsplit(") ", maxsplit=1)[1].split()
    if len(fields_after_name) <= 19:
        return None
    return fields_after_name[19]


def _pid_alive(pid: int, *, process_start_token: str | None = None) -> bool:
    if pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if ") " in stat_text:
            fields_after_name = stat_text.rsplit(") ", maxsplit=1)[1].split()
            if fields_after_name and fields_after_name[0] == "Z":
                return False
            if (
                process_start_token is not None
                and len(fields_after_name) > 19
                and fields_after_name[19] != process_start_token
            ):
                return False
        else:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(
    pid: int,
    *,
    process_group_id: int | None = None,
    process_start_token: str | None = None,
    kill_after_seconds: float | None = None,
) -> None:
    if not _pid_alive(pid, process_start_token=process_start_token):
        return
    if process_group_id is None:
        try:
            process_group_id = os.getpgid(pid)
        except ProcessLookupError:
            return
    for sig, wait_seconds in (
        (signal.SIGTERM, kill_after_seconds or AppSettings().terminal_timeout_kill_after_seconds),
        (signal.SIGKILL, 0),
    ):
        try:
            os.killpg(process_group_id, sig)
        except ProcessLookupError:
            return
        if wait_seconds == 0:
            return
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid, process_start_token=process_start_token):
                return
            time.sleep(0.05)
