"""In-sandbox guest worker for the Ariel run-program sandbox runtime.

This script runs INSIDE the gVisor sandbox as a fresh process per program. It
cannot import any ``ariel`` module: it is a standalone script the host copies
into the sandbox rootfs and launches with ``runsc exec``.

It speaks line-delimited JSON over stdin/stdout with the host:

  host -> guest   {"type": "run-program", "source", "syscall_names", "limits"}
  guest -> host   {"type": "syscall", "name", "input"}
  host -> guest   {"type": "syscall-result", "ok", "value" | "error"}
  guest -> host   {"type": "program-result", "ok", "error"?}

The model-authored program is untrusted. gVisor, the read-only rootfs, and the
absent network are the enforced boundary; the restrictions here are defense in
depth: a narrow builtin set, an import allowlist over the safe compute surface,
and self-imposed CPU-time and address-space rlimits on this process.
"""

from __future__ import annotations

import builtins
import json
import resource
import sys
from types import SimpleNamespace
from typing import Any, Callable

# Standard-library modules the program may import: the safe compute surface
# named in the run-program cutover. No os, sys, socket, subprocess, importlib.
_ALLOWED_IMPORTS = frozenset(
    {
        "json",
        "re",
        "datetime",
        "math",
        "statistics",
        "decimal",
        "fractions",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "textwrap",
        "random",
        "base64",
        "hashlib",
        "hmac",
        "uuid",
        "calendar",
        "zoneinfo",
        "unicodedata",
    }
)

# Builtins the program may call. Everything that can import, open files, read
# the environment, or reach the interpreter internals is withheld.
_SAFE_BUILTIN_NAMES = frozenset(
    {
        "abs",
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytearray",
        "bytes",
        "callable",
        "chr",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hash",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "True",
        "False",
        "None",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "Exception",
        "ImportError",
        "IndexError",
        "KeyError",
        "LookupError",
        "NameError",
        "NotImplementedError",
        "OSError",
        "OverflowError",
        "RecursionError",
        "RuntimeError",
        "StopIteration",
        "TypeError",
        "UnicodeError",
        "ValueError",
        "ZeroDivisionError",
    }
)


class _SyscallError(Exception):
    """Raised inside the program when a host syscall returns a typed error."""


class _ProgramAbort(Exception):
    """Raised to abort the program without a Python traceback (limit/protocol)."""


def _read_message(reason: str) -> dict[str, Any]:
    line = sys.stdin.readline()
    if line == "":
        raise _ProgramAbort(f"host channel closed while waiting for {reason}")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise _ProgramAbort(f"host sent invalid JSON for {reason}: {exc}") from exc
    if not isinstance(message, dict):
        raise _ProgramAbort(f"host message for {reason} was not an object")
    return message


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _restricted_import(
    name: str,
    globals_: Any = None,
    locals_: Any = None,
    fromlist: Any = (),
    level: int = 0,
) -> Any:
    del globals_, locals_, fromlist
    if level != 0:
        raise ImportError("relative imports are not allowed in a run program")
    root = name.split(".", 1)[0]
    if root not in _ALLOWED_IMPORTS:
        raise ImportError(f"import of {name!r} is not allowed in a run program")
    return __import__(name, level=0)


def _build_safe_builtins() -> dict[str, Any]:
    safe: dict[str, Any] = {
        attr: getattr(builtins, attr) for attr in _SAFE_BUILTIN_NAMES if hasattr(builtins, attr)
    }
    safe["__import__"] = _restricted_import
    return safe


class _SyscallChannel:
    """The program's view of the host: each call is one round-trip syscall."""

    def __init__(self, *, max_syscalls: int, max_output_bytes: int) -> None:
        self._max_syscalls = max_syscalls
        self._max_output_bytes = max_output_bytes
        self.count = 0

    def call(self, name: str, payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            raise TypeError(f"syscall {name} input must be a dict")
        self.count += 1
        if self.count > self._max_syscalls:
            raise _ProgramAbort(f"program exceeded max syscalls of {self._max_syscalls}")
        request = {"type": "syscall", "name": name, "input": payload}
        encoded = json.dumps(request, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._max_output_bytes:
            raise _ProgramAbort(f"syscall {name} input exceeded max output bytes")
        _write_message(request)
        result = _read_message(f"result of syscall {name}")
        if result.get("type") != "syscall-result":
            raise _ProgramAbort(f"host did not send a syscall-result for {name}")
        if result.get("ok") is True:
            return result.get("value")
        if result.get("ok") is False:
            raise _SyscallError(str(result.get("error") or f"syscall {name} failed"))
        raise _ProgramAbort(f"host sent a malformed syscall-result for {name}")


def _build_syscall_namespace(
    syscall_names: list[str],
    channel: _SyscallChannel,
) -> dict[str, Any]:
    """Expose ``email.search`` style callables as namespace objects in globals.

    Syscall names are dotted (``email.search``, ``email.labels.modify``);
    each intermediate segment is a nested ``SimpleNamespace`` and the final
    segment is the syscall function. ``SimpleNamespace`` holds plain functions as
    attributes without binding them as methods.
    """

    bindings: dict[str, SimpleNamespace] = {}
    for full_name in syscall_names:
        segments = full_name.split(".")
        if len(segments) < 2 or not all(segment for segment in segments):
            raise _ProgramAbort(f"syscall name {full_name!r} must be a dotted path")

        def _make(bound_name: str) -> Callable[..., Any]:
            def _syscall(**kwargs: Any) -> Any:
                return channel.call(bound_name, dict(kwargs))

            _syscall.__name__ = bound_name.replace(".", "_")
            return _syscall

        namespace = bindings.setdefault(segments[0], SimpleNamespace())
        for segment in segments[1:-1]:
            child = getattr(namespace, segment, None)
            if not isinstance(child, SimpleNamespace):
                child = SimpleNamespace()
                setattr(namespace, segment, child)
            namespace = child
        setattr(namespace, segments[-1], _make(full_name))
    return dict(bindings)


def _apply_process_limits(limits: dict[str, Any]) -> None:
    """Self-impose CPU-time and address-space limits on this guest process.

    Rootless gVisor runs with ``--ignore-cgroups``, so the program process is
    bounded with RLIMIT_CPU and RLIMIT_AS instead of cgroup controllers. Both
    are enforced by the gVisor kernel; the host keeps a wall-clock backstop.
    """

    cpu_seconds = limits.get("cpu_seconds")
    if isinstance(cpu_seconds, int) and cpu_seconds > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    memory_bytes = limits.get("memory_bytes")
    if isinstance(memory_bytes, int) and memory_bytes > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _run() -> dict[str, Any]:
    start = _read_message("run-program")
    if start.get("type") != "run-program":
        raise _ProgramAbort("first host message must be run-program")

    source = start.get("source")
    if not isinstance(source, str) or not source:
        raise _ProgramAbort("run-program source must be a non-empty string")

    limits = start.get("limits")
    if not isinstance(limits, dict):
        raise _ProgramAbort("run-program limits must be an object")
    max_source_bytes = limits.get("source_bytes")
    if isinstance(max_source_bytes, int) and len(source.encode("utf-8")) > max_source_bytes:
        raise _ProgramAbort(f"program source exceeded max source bytes of {max_source_bytes}")

    raw_syscall_names = start.get("syscall_names")
    if not isinstance(raw_syscall_names, list) or not all(
        isinstance(name, str) and name for name in raw_syscall_names
    ):
        raise _ProgramAbort("run-program syscall_names must be a list of strings")
    syscall_names: list[str] = list(raw_syscall_names)

    _apply_process_limits(limits)

    channel = _SyscallChannel(
        max_syscalls=int(limits.get("max_syscalls", 0)) or 1,
        max_output_bytes=int(limits.get("max_output_bytes", 0)) or 1,
    )

    try:
        compiled = compile(source, "<run-program>", "exec")
    except SyntaxError as exc:
        return {"type": "program-result", "ok": False, "error": f"program_syntax_error: {exc}"}

    program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
    program_globals.update(_build_syscall_namespace(syscall_names, channel))

    try:
        exec(compiled, program_globals)  # noqa: S102 - sandboxed, gVisor-isolated
    except _ProgramAbort:
        raise
    except _SyscallError as exc:
        return {"type": "program-result", "ok": False, "error": f"syscall_error: {exc}"}
    except Exception as exc:  # noqa: BLE001 - report any program failure to the host
        return {
            "type": "program-result",
            "ok": False,
            "error": f"program_error: {type(exc).__name__}: {exc}",
        }
    return {"type": "program-result", "ok": True, "error": None}


def main() -> int:
    try:
        result = _run()
    except _ProgramAbort as exc:
        result = {"type": "program-result", "ok": False, "error": f"program_aborted: {exc}"}
    except Exception as exc:  # noqa: BLE001 - never crash without a final message
        result = {
            "type": "program-result",
            "ok": False,
            "error": f"worker_error: {type(exc).__name__}: {exc}",
        }
    _write_message(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
