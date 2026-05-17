"""Real-gVisor tests for the run-program sandbox runtime.

These run actual model-style Python programs inside a real ``runsc`` sandbox:
the second test layer the run-program cutover requires. They are skipped when
``runsc`` is unavailable so the unit suite still runs on any host; CI provides
``runsc`` and the Systrap platform needs no special host capability.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ariel.sandbox_runtime import ProgramResult, SandboxRuntime


def _runsc_available() -> bool:
    if shutil.which("runsc") is not None:
        return True
    return (Path.home() / ".local" / "bin" / "runsc").exists()


pytestmark = pytest.mark.skipif(
    not _runsc_available(),
    reason="runsc is not installed; the real-gVisor sandbox layer cannot run",
)


def _deny_callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
    del name, payload
    return False, "no_syscalls_configured"


@pytest.fixture
def sandbox() -> Iterator[SandboxRuntime]:
    """One persistent gVisor sandbox, started once and torn down after the test."""

    runtime = SandboxRuntime(container_id="ariel-run-sandbox-test")
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.close()


def test_trivial_program_runs_as_a_fresh_process_in_the_sandbox(
    sandbox: SandboxRuntime,
) -> None:
    result = sandbox.run_program(
        source="total = sum(range(10))\nassert total == 45\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result == ProgramResult(ok=True, error=None, syscall_count=0)


def test_program_round_trips_a_stub_syscall(sandbox: SandboxRuntime) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        calls.append((name, payload))
        if name == "stub.echo":
            return True, {"echoed": payload["text"]}
        return False, "unknown_syscall"

    source = (
        "reply = stub.echo(text='hello sandbox')\n"
        "assert reply['echoed'] == 'hello sandbox', reply\n"
    )
    result = sandbox.run_program(
        source=source,
        syscall_names=("stub.echo",),
        syscall_callback=callback,
    )
    assert result.ok is True, result.error
    assert result.syscall_count == 1
    assert calls == [("stub.echo", {"text": "hello sandbox"})]


def test_control_flow_and_data_flow_between_syscalls(sandbox: SandboxRuntime) -> None:
    def callback(name: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        del name
        return True, {"doubled": payload["n"] * 2}

    # A loop binds each syscall's result and feeds it forward.
    source = (
        "acc = 1\n"
        "for _ in range(4):\n"
        "    acc = stub.grow(n=acc)['doubled']\n"
        "assert acc == 16, acc\n"
    )
    result = sandbox.run_program(
        source=source,
        syscall_names=("stub.grow",),
        syscall_callback=callback,
    )
    assert result.ok is True, result.error
    assert result.syscall_count == 4


def test_persistent_sandbox_runs_several_programs_in_sequence(
    sandbox: SandboxRuntime,
) -> None:
    # The sandbox is created once; each program is a fresh process inside it.
    first = sandbox.run_program(
        source="a = 11 * 11\nassert a == 121\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    second = sandbox.run_program(
        source="b = 12 * 12\nassert b == 144\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert first.ok is True and second.ok is True
    # Fresh interpreter state: a name bound in the first program is gone.
    third = sandbox.run_program(
        source="value = a\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert third.ok is False
    assert third.error is not None
    assert "NameError" in third.error


def test_program_cannot_import_socket(sandbox: SandboxRuntime) -> None:
    # Defense in depth: the program's import allowlist excludes socket.
    source = (
        "blocked = False\n"
        "try:\n"
        "    import socket\n"
        "except ImportError:\n"
        "    blocked = True\n"
        "assert blocked, 'socket import was not blocked'\n"
    )
    result = sandbox.run_program(
        source=source,
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result.ok is True, result.error


def test_sandbox_has_no_network(sandbox: SandboxRuntime) -> None:
    # Checks the gVisor layer directly: even with raw socket access inside the
    # running sandbox, the program's builtins withhold open()/import, so this
    # probes the kernel boundary itself — there is no reachable network.
    probe = (
        "import socket\n"
        "blocked = False\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.settimeout(3)\n"
        "try:\n"
        "    sock.connect(('1.1.1.1', 53))\n"
        "except OSError:\n"
        "    blocked = True\n"
        "finally:\n"
        "    sock.close()\n"
        "assert blocked, 'network was reachable from the sandbox'\n"
        "print('network-blocked-ok')\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sandbox.runsc_path,
            "--rootless",
            "--ignore-cgroups",
            "--network=none",
            "exec",
            sandbox.container_id,
            "/usr/bin/python3",
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "network-blocked-ok" in completed.stdout


def test_sandbox_root_filesystem_is_read_only(sandbox: SandboxRuntime) -> None:
    # The program's builtins withhold open(), so this checks the gVisor layer
    # directly: a write to the root filesystem inside the running sandbox fails
    # with EROFS, while the tmpfs scratch at /tmp accepts a write.
    probe = (
        "import errno\n"
        "rootfs_readonly = False\n"
        "try:\n"
        "    open('/program-probe.txt', 'w').write('x')\n"
        "except OSError as exc:\n"
        "    rootfs_readonly = exc.errno == errno.EROFS\n"
        "open('/tmp/scratch-probe.txt', 'w').write('ok')\n"
        "assert rootfs_readonly, 'root filesystem was writable'\n"
        "print('rootfs-readonly-ok')\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sandbox.runsc_path,
            "--rootless",
            "--ignore-cgroups",
            "--network=none",
            "exec",
            sandbox.container_id,
            "/usr/bin/python3",
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "rootfs-readonly-ok" in completed.stdout


def test_cpu_limit_is_enforced(sandbox: SandboxRuntime) -> None:
    # An unbounded busy loop must be killed by the guest's RLIMIT_CPU.
    result = sandbox.run_program(
        source="x = 0\nwhile True:\n    x += 1\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result.ok is False
    assert result.error is not None
    assert "CPU-time or memory limit" in result.error


def test_memory_limit_is_enforced(sandbox: SandboxRuntime) -> None:
    # Allocating past RLIMIT_AS raises MemoryError inside the program.
    source = "chunks = []\nfor _ in range(100000):\n    chunks.append(bytearray(1024 * 1024))\n"
    result = sandbox.run_program(
        source=source,
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result.ok is False
    assert result.error is not None
    assert "MemoryError" in result.error or "CPU-time or memory limit" in result.error


def test_program_raising_an_exception_is_reported(sandbox: SandboxRuntime) -> None:
    result = sandbox.run_program(
        source="raise ValueError('program failed deliberately')\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result.ok is False
    assert result.error is not None
    assert "ValueError" in result.error


def test_restricted_imports_block_os_access(sandbox: SandboxRuntime) -> None:
    result = sandbox.run_program(
        source="import os\nos.listdir('/')\n",
        syscall_names=(),
        syscall_callback=_deny_callback,
    )
    assert result.ok is False
    assert result.error is not None
    assert "ImportError" in result.error
