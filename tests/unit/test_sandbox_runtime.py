"""Host-side tests for the run-program sandbox runtime.

These exercise the host module with the gVisor boundary replaced by a fake
transport that speaks the same line-delimited JSON protocol. The real-runsc
layer lives in tests/integration/test_sandbox_runtime_runsc.py.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from ariel.sandbox_runtime import (
    SANDBOX_MAX_MESSAGE_BYTES,
    ProgramResult,
    SandboxRuntimeError,
    _build_oci_config,
    _drive_program,
    _validate_guest_message,
)


class _FakeGuestPipe:
    """A blocking in-memory line channel between the host and the fake guest."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._closed = False
        self._condition = threading.Condition()

    def write(self, data: str) -> None:
        with self._condition:
            self._lines.append(data)
            self._condition.notify_all()

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        with self._condition:
            while not self._lines and not self._closed:
                self._condition.wait()
            if self._lines:
                return self._lines.pop(0)
            return ""

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class _FakeGuestProcess:
    """A fake ``runsc exec`` guest: drives a scripted protocol over fake pipes.

    ``script`` is the ordered list of guest->host messages — each a dict, or a
    ``{"_raw": "..."}`` entry to emit a verbatim (possibly malformed) line. Each
    ``syscall`` blocks for the host's ``syscall-result``; a final message ends
    the run. When ``keep_open`` is set the channel is never closed, which lets
    a test exercise the host's wall-clock backstop against a wedged guest.
    """

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        exit_code: int = 0,
        keep_open: bool = False,
    ) -> None:
        self._script = script
        self._exit_code = exit_code
        self._keep_open = keep_open
        self._host_to_guest = _FakeGuestPipe()
        self._guest_to_host = _FakeGuestPipe()
        self.stdin = self._host_to_guest
        self.stdout = self._guest_to_host
        self.stderr = None
        self.received_run_program: dict[str, Any] | None = None
        self.received_syscall_results: list[dict[str, Any]] = []
        self._killed = False
        self._thread = threading.Thread(target=self._run_guest, daemon=True)
        self._thread.start()

    def _run_guest(self) -> None:
        first = self._host_to_guest.readline()
        if first:
            self.received_run_program = json.loads(first)
        for message in self._script:
            if "_raw" in message:
                self._guest_to_host.write(message["_raw"] + "\n")
                continue
            self._guest_to_host.write(json.dumps(message) + "\n")
            if message.get("type") == "syscall":
                reply = self._host_to_guest.readline()
                if reply:
                    self.received_syscall_results.append(json.loads(reply))
        if not self._keep_open:
            self._guest_to_host.close()

    def kill(self) -> None:
        self._killed = True
        self._guest_to_host.close()
        self._host_to_guest.close()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._thread.join(timeout=1.0)
        return 137 if self._killed else self._exit_code


def _drive(
    process: _FakeGuestProcess,
    *,
    source: str = "pass\n",
    syscall_names: tuple[str, ...] = (),
    syscall_callback: Any = None,
    wall_clock_seconds: float = 5.0,
) -> ProgramResult:
    if syscall_callback is None:
        syscall_callback = lambda _name, _input: (False, "no syscalls")  # noqa: E731
    return _drive_program(
        process=process,  # type: ignore[arg-type]
        source=source,
        syscall_names=syscall_names,
        syscall_callback=syscall_callback,
        wall_clock_seconds=wall_clock_seconds,
    )


def test_clean_program_result_is_reported() -> None:
    process = _FakeGuestProcess([{"type": "program-result", "ok": True, "error": None}])
    result = _drive(process)
    assert result == ProgramResult(ok=True, error=None, syscall_count=0)


def test_run_program_message_carries_source_limits_and_syscalls() -> None:
    process = _FakeGuestProcess([{"type": "program-result", "ok": True, "error": None}])
    _drive(process, source="x = 1\n", syscall_names=("email.search", "memory.search"))
    assert process.received_run_program is not None
    assert process.received_run_program["type"] == "run-program"
    assert process.received_run_program["source"] == "x = 1\n"
    assert process.received_run_program["syscall_names"] == ["email.search", "memory.search"]
    limits = process.received_run_program["limits"]
    assert limits["source_bytes"] > 0
    assert limits["max_syscalls"] > 0
    assert limits["cpu_seconds"] > 0
    assert limits["memory_bytes"] > 0


def test_syscall_is_dispatched_to_the_callback_and_result_returned() -> None:
    process = _FakeGuestProcess(
        [
            {"type": "syscall", "name": "stub.echo", "input": {"text": "hi"}},
            {"type": "program-result", "ok": True, "error": None},
        ]
    )
    seen: list[tuple[str, dict[str, Any]]] = []

    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        seen.append((name, payload))
        return True, {"echoed": payload["text"]}

    result = _drive(process, syscall_names=("stub.echo",), syscall_callback=callback)
    assert result.ok is True
    assert result.syscall_count == 1
    assert seen == [("stub.echo", {"text": "hi"})]
    assert process.received_syscall_results == [
        {"type": "syscall-result", "ok": True, "value": {"echoed": "hi"}}
    ]


def test_multiple_syscalls_are_dispatched_in_order() -> None:
    process = _FakeGuestProcess(
        [
            {"type": "syscall", "name": "stub.one", "input": {"n": 1}},
            {"type": "syscall", "name": "stub.two", "input": {"n": 2}},
            {"type": "program-result", "ok": True, "error": None},
        ]
    )
    order: list[str] = []

    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        del payload
        order.append(name)
        return True, None

    result = _drive(process, syscall_names=("stub.one", "stub.two"), syscall_callback=callback)
    assert result.syscall_count == 2
    assert order == ["stub.one", "stub.two"]


def test_callback_failure_is_marshalled_back_as_a_typed_error() -> None:
    process = _FakeGuestProcess(
        [
            {"type": "syscall", "name": "stub.deny", "input": {}},
            {"type": "program-result", "ok": True, "error": None},
        ]
    )

    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        del name, payload
        return False, "policy_denied"

    _drive(process, syscall_names=("stub.deny",), syscall_callback=callback)
    assert process.received_syscall_results == [
        {"type": "syscall-result", "ok": False, "error": "policy_denied"}
    ]


def test_callback_that_raises_is_surfaced_to_the_guest_not_crashing_the_host() -> None:
    process = _FakeGuestProcess(
        [
            {"type": "syscall", "name": "stub.boom", "input": {}},
            {"type": "program-result", "ok": True, "error": None},
        ]
    )

    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        del name, payload
        raise RuntimeError("host fault")

    result = _drive(process, syscall_names=("stub.boom",), syscall_callback=callback)
    assert result.ok is True
    assert process.received_syscall_results[0]["ok"] is False
    assert "syscall_host_error" in process.received_syscall_results[0]["error"]


def test_program_failure_result_is_propagated() -> None:
    process = _FakeGuestProcess(
        [{"type": "program-result", "ok": False, "error": "program_error: boom"}]
    )
    result = _drive(process)
    assert result.ok is False
    assert result.error == "program_error: boom"


def test_guest_closing_channel_without_result_is_a_failure() -> None:
    process = _FakeGuestProcess([])
    result = _drive(process)
    assert result.ok is False
    assert result.error is not None
    assert "exited with code" in result.error


def test_guest_sigkill_exit_is_reported_as_a_resource_limit() -> None:
    process = _FakeGuestProcess([], exit_code=137)
    result = _drive(process)
    assert result.ok is False
    assert result.error == "program exceeded its CPU-time or memory limit"


def test_clean_result_with_nonzero_exit_is_a_failure() -> None:
    process = _FakeGuestProcess(
        [{"type": "program-result", "ok": True, "error": None}], exit_code=3
    )
    result = _drive(process)
    assert result.ok is False
    assert result.error is not None
    assert "exited with code 3" in result.error


def test_oversized_guest_message_is_rejected_before_dispatch() -> None:
    oversized = json.dumps({"type": "program-result", "ok": True, "error": "x" * (256 * 1024)})
    process = _FakeGuestProcess([{"_raw": oversized}])
    result = _drive(process)
    assert result.ok is False
    assert result.error is not None
    assert "channel protocol" in result.error


def test_validate_guest_message_accepts_a_well_formed_syscall() -> None:
    message = _validate_guest_message(
        json.dumps({"type": "syscall", "name": "email.search", "input": {"q": "x"}}) + "\n"
    )
    assert message == {"type": "syscall", "name": "email.search", "input": {"q": "x"}}


def test_validate_guest_message_accepts_a_program_result() -> None:
    message = _validate_guest_message(
        json.dumps({"type": "program-result", "ok": False, "error": "boom"})
    )
    assert message == {"type": "program-result", "ok": False, "error": "boom"}


def test_validate_guest_message_rejects_oversized_lines() -> None:
    oversized = "x" * (SANDBOX_MAX_MESSAGE_BYTES + 1)
    with pytest.raises(SandboxRuntimeError, match="maximum message size"):
        _validate_guest_message(oversized)


def test_validate_guest_message_rejects_invalid_json() -> None:
    with pytest.raises(SandboxRuntimeError, match="invalid JSON"):
        _validate_guest_message("{not json}\n")


def test_validate_guest_message_rejects_non_object_payloads() -> None:
    with pytest.raises(SandboxRuntimeError, match="not a JSON object"):
        _validate_guest_message("[1, 2, 3]\n")


def test_validate_guest_message_rejects_unknown_message_types() -> None:
    with pytest.raises(SandboxRuntimeError, match="unknown message type"):
        _validate_guest_message(json.dumps({"type": "exfiltrate", "data": "x"}))


def test_validate_guest_message_rejects_syscall_with_bad_name() -> None:
    with pytest.raises(SandboxRuntimeError, match="invalid name"):
        _validate_guest_message(json.dumps({"type": "syscall", "name": "", "input": {}}))


def test_validate_guest_message_rejects_syscall_with_non_object_input() -> None:
    with pytest.raises(SandboxRuntimeError, match="non-object input"):
        _validate_guest_message(
            json.dumps({"type": "syscall", "name": "email.search", "input": "nope"})
        )


def test_validate_guest_message_rejects_program_result_with_bad_ok() -> None:
    with pytest.raises(SandboxRuntimeError, match="non-boolean ok"):
        _validate_guest_message(json.dumps({"type": "program-result", "ok": "yes"}))


def test_oci_config_has_no_network_and_a_readonly_root() -> None:
    from pathlib import Path

    config = _build_oci_config(Path("rootfs"))
    assert config["root"]["readonly"] is True
    namespace_types = {ns["type"] for ns in config["linux"]["namespaces"]}
    assert "network" in namespace_types
    # The program process is given no Linux capabilities.
    assert config["process"]["capabilities"]["effective"] == []
    # Only /proc and a bounded tmpfs scratch are writable; binds are read-only.
    writable = [m for m in config["mounts"] if "ro" not in m.get("options", [])]
    assert {m["destination"] for m in writable} <= {"/proc", "/tmp"}
    tmp_mount = next(m for m in config["mounts"] if m["destination"] == "/tmp")
    assert tmp_mount["type"] == "tmpfs"


def test_wall_clock_limit_terminates_a_wedged_guest() -> None:
    # A guest that never sends anything must not block the host forever.
    process = _FakeGuestProcess([], keep_open=True)
    result = _drive(process, wall_clock_seconds=0.5)
    assert result.ok is False
    assert result.error is not None
    assert "wall-clock" in result.error
