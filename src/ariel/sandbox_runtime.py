"""Host side of the Ariel run-program gVisor sandbox runtime.

This module owns one persistent gVisor (``runsc``) sandbox for the life of the
service and runs each model-authored ``run`` program as a fresh process inside
it. It owns the host side of the line-delimited JSON syscall channel and is the
ingress trust boundary for everything the in-sandbox guest worker sends back.

The runtime is a clean seam: it receives a parsed ``{name, input}`` syscall and
calls a host-provided ``syscall_callback`` to actually run it. Phase 4/5 inject
the real capability-execution callback; this module never imports the
capability registry, the action runtime, or the run runtime.

Design — persistent sandbox, fresh process per program:

  * ``start()`` builds a minimal OCI bundle (a near-empty rootfs with the host
    Python bind-mounted read-only and the guest worker copied in) and launches
    one detached ``runsc`` container whose entrypoint just sleeps. Starting the
    sandbox pays the gVisor start cost once.
  * ``run_program()`` spawns a fresh ``runsc exec python3 guest_worker.py``
    process inside that running container. Each program therefore gets clean
    interpreter state and tmpfs scratch discarded with the process; the only
    per-program cost is a process spawn, not a container start.
  * ``close()`` deletes the container.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

# The guest worker self-imposes RLIMIT_CPU / RLIMIT_AS from these; the host
# enforces the wall-clock backstop and the channel size limits.
SANDBOX_MAX_SOURCE_BYTES = 65_536
SANDBOX_MAX_SYSCALLS = 64
SANDBOX_CPU_SECONDS = 5
SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024
SANDBOX_WALL_CLOCK_SECONDS = 30.0
# The host-side channel reader rejects any single guest line larger than this
# before parsing it. Guest messages are untrusted input.
SANDBOX_MAX_MESSAGE_BYTES = 256 * 1024
SANDBOX_SCRATCH_BYTES = 64 * 1024 * 1024

_GUEST_WORKER_PATH = Path(__file__).resolve().with_name("sandbox_guest_worker.py")
# Host paths bind-mounted read-only so the sandbox CPython can run without
# assembling a full rootfs. /usr carries the interpreter and its stdlib.
_GUEST_BIND_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/etc/python3.12")
_GUEST_PYTHON = "/usr/bin/python3"
_GUEST_WORKER_DEST = "/opt/ariel/sandbox_guest_worker.py"


class SandboxRuntimeError(Exception):
    """Raised when the sandbox cannot be managed or a program cannot be run."""


@dataclass(frozen=True, slots=True)
class ProgramResult:
    """Outcome of one run-program execution inside the sandbox."""

    ok: bool
    error: str | None
    syscall_count: int


@dataclass(frozen=True, slots=True)
class _Syscall:
    """A parsed, size-checked, schema-validated guest syscall request."""

    name: str
    input: dict[str, Any]


# A syscall callback returns (ok, value_or_error): ok True carries a JSON value,
# ok False carries a typed error string. Phase 4/5 supply the real one.
SyscallCallback = Callable[[str, dict[str, Any]], "tuple[bool, Any]"]


class RunSandbox(Protocol):
    """The run-program sandbox interface the host path depends on.

    ``SandboxRuntime`` is the production gVisor implementation; the test suite
    injects an in-process double. Both satisfy this protocol so the host path
    (``create_app`` and ``execute_run_program``) is written against one type.
    """

    def start(self) -> None: ...

    def close(self) -> None: ...

    def run_program(
        self,
        *,
        source: str,
        syscall_names: tuple[str, ...],
        syscall_callback: SyscallCallback,
    ) -> ProgramResult: ...


def _runsc_path() -> str:
    found = shutil.which("runsc")
    if found is not None:
        return found
    local = Path.home() / ".local" / "bin" / "runsc"
    if local.exists():
        return str(local)
    raise SandboxRuntimeError("runsc executable was not found on PATH or in ~/.local/bin")


def _build_oci_config(rootfs: Path) -> dict[str, Any]:
    """Build an OCI config.json: read-only rootfs, no network, tmpfs scratch."""

    def _ro_bind(source: str) -> dict[str, Any]:
        return {
            "destination": source,
            "type": "bind",
            "source": source,
            "options": ["ro", "rbind"],
        }

    mounts: list[dict[str, Any]] = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {
            "destination": "/tmp",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "nodev", "size=" + str(SANDBOX_SCRATCH_BYTES)],
        },
    ]
    mounts.extend(_ro_bind(path) for path in _GUEST_BIND_PATHS if Path(path).exists())
    return {
        "ociVersion": "1.0.0",
        "process": {
            "user": {"uid": 0, "gid": 0},
            "args": ["/usr/bin/sleep", "infinity"],
            "env": ["PATH=/usr/bin:/bin", "HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"],
            "cwd": "/",
            "capabilities": {
                "bounding": [],
                "effective": [],
                "inheritable": [],
                "permitted": [],
            },
        },
        "root": {"path": str(rootfs), "readonly": True},
        "mounts": mounts,
        "linux": {
            "namespaces": [
                {"type": "pid"},
                {"type": "network"},
                {"type": "ipc"},
                {"type": "uts"},
                {"type": "mount"},
            ]
        },
    }


@dataclass
class SandboxRuntime:
    """Owns one persistent gVisor sandbox and runs programs as fresh processes.

    ``start()`` once at service start, ``run_program()`` per program, ``close()``
    at shutdown. Not safe for concurrent ``run_program()`` calls — the design is
    single-user and runs one program at a time.
    """

    container_id: str = "ariel-run-sandbox"
    runsc_path: str = field(default_factory=_runsc_path)
    wall_clock_seconds: float = SANDBOX_WALL_CLOCK_SECONDS
    _bundle_dir: Path | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _base_command(self, *args: str) -> list[str]:
        # Rootless runsc requires an explicit network mode; "none" is also the
        # sandbox's no-network guarantee. --ignore-cgroups is required rootless.
        return [
            self.runsc_path,
            "--rootless",
            "--ignore-cgroups",
            "--network=none",
            *args,
        ]

    def start(self) -> None:
        """Build the OCI bundle and launch the persistent detached sandbox."""

        with self._lock:
            if self._started:
                return
            if not _GUEST_WORKER_PATH.exists():
                raise SandboxRuntimeError("guest worker script is missing")

            bundle = Path(tempfile.mkdtemp(prefix="ariel-sandbox-"))
            rootfs = bundle / "rootfs"
            (rootfs / "opt" / "ariel").mkdir(parents=True, exist_ok=True)
            (rootfs / "tmp").mkdir(parents=True, exist_ok=True)
            (rootfs / "proc").mkdir(parents=True, exist_ok=True)
            for path in _GUEST_BIND_PATHS:
                if Path(path).exists():
                    (rootfs / path.lstrip("/")).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                _GUEST_WORKER_PATH,
                rootfs / _GUEST_WORKER_DEST.lstrip("/"),
            )
            config = _build_oci_config(Path("rootfs"))
            (bundle / "config.json").write_text(json.dumps(config), encoding="utf-8")

            # The detached sandbox process inherits and holds open any stdio
            # pipe, so capturing via pipes wedges subprocess.communicate. Send
            # the detached process's stdio to DEVNULL and capture launch errors
            # to a file, which does not keep the channel open.
            stderr_path = bundle / "runsc-start.err"
            try:
                with stderr_path.open("wb") as stderr_file:
                    launcher = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                        self._base_command(
                            "run", "--detach", "--bundle", str(bundle), self.container_id
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_file,
                    )
                    return_code = launcher.wait(timeout=self.wall_clock_seconds)
            except subprocess.TimeoutExpired as exc:
                launcher.kill()
                shutil.rmtree(bundle, ignore_errors=True)
                raise SandboxRuntimeError("sandbox container did not start in time") from exc
            if return_code != 0:
                detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                shutil.rmtree(bundle, ignore_errors=True)
                raise SandboxRuntimeError(f"sandbox container failed to start: {detail}")
            self._bundle_dir = bundle
            self._started = True

    def close(self) -> None:
        """Delete the persistent sandbox and remove its bundle directory."""

        with self._lock:
            if self._started:
                subprocess.run(
                    self._base_command("delete", "--force", self.container_id),
                    capture_output=True,
                    text=True,
                    timeout=self.wall_clock_seconds,
                    check=False,
                )
            self._started = False
            if self._bundle_dir is not None:
                shutil.rmtree(self._bundle_dir, ignore_errors=True)
                self._bundle_dir = None

    def run_program(
        self,
        *,
        source: str,
        syscall_names: tuple[str, ...],
        syscall_callback: SyscallCallback,
    ) -> ProgramResult:
        """Run one program as a fresh process inside the persistent sandbox.

        ``syscall_names`` are the eligible syscalls exposed to the program as
        namespaced callables. ``syscall_callback`` is invoked for each guest
        syscall: it receives the validated ``(name, input)`` and returns
        ``(ok, value_or_error)``. This is the seam Phase 4/5 use to inject the
        real capability-derived syscall surface and capability execution.
        """

        with self._lock:
            if not self._started:
                raise SandboxRuntimeError("sandbox is not started")
            command = self._base_command(
                "exec", self.container_id, _GUEST_PYTHON, _GUEST_WORKER_DEST
            )
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return _drive_program(
                process=process,
                source=source,
                syscall_names=syscall_names,
                syscall_callback=syscall_callback,
                wall_clock_seconds=self.wall_clock_seconds,
            )


def _validate_guest_message(line: str) -> dict[str, Any]:
    """Ingress trust boundary: size-limit and schema-check one guest line.

    The guest worker runs untrusted, model-authored code, so every line it
    sends is untrusted input. This rejects anything that is not a known,
    well-formed message before the runtime acts on it.
    """

    if len(line.encode("utf-8")) > SANDBOX_MAX_MESSAGE_BYTES:
        raise SandboxRuntimeError("guest message exceeded the maximum message size")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise SandboxRuntimeError("guest emitted invalid JSON") from exc
    if not isinstance(message, dict):
        raise SandboxRuntimeError("guest message was not a JSON object")

    message_type = message.get("type")
    if message_type == "syscall":
        name = message.get("name")
        syscall_input = message.get("input")
        if not isinstance(name, str) or not name:
            raise SandboxRuntimeError("guest syscall message had an invalid name")
        if not isinstance(syscall_input, dict):
            raise SandboxRuntimeError("guest syscall message had a non-object input")
        return {"type": "syscall", "name": name, "input": syscall_input}
    if message_type == "program-result":
        ok = message.get("ok")
        error = message.get("error")
        if not isinstance(ok, bool):
            raise SandboxRuntimeError("guest program-result had a non-boolean ok")
        if error is not None and not isinstance(error, str):
            raise SandboxRuntimeError("guest program-result had a non-string error")
        return {"type": "program-result", "ok": ok, "error": error}
    raise SandboxRuntimeError("guest sent an unknown message type")


def _encode_host_message(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":")) + "\n"


def _invoke_syscall_callback(
    syscall: _Syscall,
    syscall_callback: SyscallCallback,
) -> dict[str, Any]:
    """Run the host syscall callback and shape its outcome as a result message.

    A callback that raises is a host-side defect, not a guest error; it is
    surfaced to the guest as a typed failure so the program can observe it,
    while the runtime keeps a clean lifecycle.
    """

    try:
        ok, value_or_error = syscall_callback(syscall.name, syscall.input)
    except Exception as exc:  # noqa: BLE001 - host callback faults must not crash the runtime
        return {
            "type": "syscall-result",
            "ok": False,
            "error": f"syscall_host_error: {type(exc).__name__}: {exc}",
        }
    if ok:
        return {"type": "syscall-result", "ok": True, "value": value_or_error}
    return {"type": "syscall-result", "ok": False, "error": str(value_or_error)}


def _drive_program(
    *,
    process: "subprocess.Popen[str]",
    source: str,
    syscall_names: tuple[str, ...],
    syscall_callback: SyscallCallback,
    wall_clock_seconds: float,
) -> ProgramResult:
    """Own the host side of the channel for one program: send, dispatch, finish.

    The channel conversation — and therefore ``syscall_callback`` — runs on the
    CALLER's thread. Phase 4 syscalls do DB work on the caller's SQLAlchemy
    session, which is not safe to touch off-thread, so the callback must never
    be driven from a worker thread. The wall-clock backstop is a
    ``threading.Timer`` watchdog that only calls ``process.kill()`` on overrun;
    killing the process is thread-safe and unblocks the blocking ``readline``.
    """

    outcome: dict[str, Any] = {}
    timed_out = threading.Event()

    def _watchdog() -> None:
        timed_out.set()
        process.kill()

    watchdog = threading.Timer(wall_clock_seconds, _watchdog)
    watchdog.daemon = True

    def _conversation() -> None:
        assert process.stdin is not None
        assert process.stdout is not None
        run_program = {
            "type": "run-program",
            "source": source,
            "syscall_names": sorted(set(syscall_names)),
            "limits": {
                "source_bytes": SANDBOX_MAX_SOURCE_BYTES,
                "max_syscalls": SANDBOX_MAX_SYSCALLS,
                "max_output_bytes": SANDBOX_MAX_MESSAGE_BYTES,
                "cpu_seconds": SANDBOX_CPU_SECONDS,
                "memory_bytes": SANDBOX_MEMORY_BYTES,
            },
        }
        process.stdin.write(_encode_host_message(run_program))
        process.stdin.flush()

        syscall_count = 0
        while True:
            line = process.stdout.readline()
            if line == "":
                outcome.update(closed_early=True, syscall_count=syscall_count)
                return
            message = _validate_guest_message(line)
            if message["type"] == "syscall":
                syscall_count += 1
                syscall = _Syscall(name=message["name"], input=message["input"])
                result = _invoke_syscall_callback(syscall, syscall_callback)
                process.stdin.write(_encode_host_message(result))
                process.stdin.flush()
                continue
            outcome.update(
                ok=bool(message["ok"]),
                error=message["error"],
                syscall_count=syscall_count,
            )
            return

    conversation_error: list[str] = []
    watchdog.start()
    try:
        _conversation()
    except SandboxRuntimeError as exc:
        conversation_error.append(str(exc))
    except (OSError, ValueError) as exc:
        # A broken pipe or decode fault means the guest died mid-channel — which
        # is also how a watchdog kill surfaces while a read or write is blocked.
        conversation_error.append(f"channel failed: {exc}")
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        process.wait()
        return ProgramResult(
            ok=False,
            error="program exceeded the wall-clock limit",
            syscall_count=int(outcome.get("syscall_count", 0)),
        )

    try:
        return_code = process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait()
    syscall_count = int(outcome.get("syscall_count", 0))

    if conversation_error:
        return ProgramResult(
            ok=False,
            error=f"guest violated the channel protocol: {conversation_error[0]}",
            syscall_count=syscall_count,
        )
    if outcome.get("closed_early"):
        # The guest exited before sending a program-result. A SIGKILL exit
        # (128 + signal) is how the guest's RLIMIT_CPU / RLIMIT_AS appears, so
        # report the resource limit precisely.
        if return_code == 137:
            detail = "program exceeded its CPU-time or memory limit"
        else:
            detail = f"guest process exited with code {return_code}"
        return ProgramResult(ok=False, error=detail, syscall_count=syscall_count)
    if outcome["ok"] and return_code != 0:
        return ProgramResult(
            ok=False,
            error=f"guest process exited with code {return_code}",
            syscall_count=syscall_count,
        )
    return ProgramResult(
        ok=bool(outcome["ok"]),
        error=outcome["error"],
        syscall_count=syscall_count,
    )


if __name__ == "__main__":  # pragma: no cover - manual smoke entry point
    runtime = SandboxRuntime()
    runtime.start()
    try:
        outcome = runtime.run_program(
            source="x = 1 + 1\n",
            syscall_names=(),
            syscall_callback=lambda _name, _input: (False, "no syscalls in phase 3"),
        )
        sys.stdout.write(f"{outcome}\n")
    finally:
        runtime.close()
