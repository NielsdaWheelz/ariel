"""In-process test double for ``SandboxRuntime``.

This runs a model-authored ``run`` program in-process instead of in a real
gVisor sandbox. It implements the exact ``SandboxRuntime`` interface the host
path depends on — ``start``/``close``/``run_program`` — so it can be injected
into ``create_app`` and ``execute_run_program`` for fast tests.

Real gVisor isolation — no network, read-only rootfs, the resource limits, the
channel ingress boundary — stays covered by ``test_sandbox_runtime.py`` and
``test_sandbox_runtime_runsc.py``. This double exercises only the host path:
program control flow, syscall dispatch through the callback, and the
``ProgramResult`` contract. It is the spec's two-layer test decision.

It deliberately runs the source through a restricted interpreter mirroring the
guest worker so program-authoring assumptions stay honest, but it provides no
security boundary and must never run untrusted code outside tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from ariel.sandbox_guest_worker import _build_safe_builtins
from ariel.sandbox_runtime import ProgramResult, SyscallCallback


class _SyscallError(Exception):
    """Raised inside the program when a syscall callback returns ``ok=False``."""


def _build_namespaces(
    syscall_names: tuple[str, ...],
    syscall_callback: SyscallCallback,
) -> dict[str, Any]:
    """Build the dotted syscall callables wired to the callback.

    Mirrors the guest worker: each dotted syscall name (``email.search``,
    ``email.labels.modify``) becomes a plain function reached through
    nested ``SimpleNamespace`` objects; calling it round-trips through
    ``syscall_callback`` and either returns the value or raises ``_SyscallError``.
    """

    bindings: dict[str, SimpleNamespace] = {}
    for full_name in sorted(set(syscall_names)):
        segments = full_name.split(".")
        if len(segments) < 2 or not all(segment for segment in segments):
            raise AssertionError(f"syscall name {full_name!r} must be a dotted path")

        def _make(bound_name: str) -> Callable[..., Any]:
            def _syscall(**kwargs: Any) -> Any:
                ok, value_or_error = syscall_callback(bound_name, dict(kwargs))
                if ok:
                    return value_or_error
                raise _SyscallError(str(value_or_error))

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


class FakeSandboxRuntime:
    """In-process stand-in for ``SandboxRuntime`` with the same interface."""

    def __init__(self) -> None:
        self._started = False

    def start(self) -> None:
        self._started = True

    def close(self) -> None:
        self._started = False

    def run_program(
        self,
        *,
        source: str,
        syscall_names: tuple[str, ...],
        syscall_callback: SyscallCallback,
    ) -> ProgramResult:
        if not self._started:
            raise AssertionError("fake sandbox is not started")
        program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
        program_globals.update(_build_namespaces(syscall_names, syscall_callback))
        try:
            compiled = compile(source, "<run-program>", "exec")
        except SyntaxError as exc:
            return ProgramResult(
                ok=False,
                error=f"program_syntax_error: {exc}",
                syscall_count=0,
            )
        try:
            exec(compiled, program_globals)  # noqa: S102 - in-process test double
        except _SyscallError as exc:
            return ProgramResult(ok=False, error=f"syscall_error: {exc}", syscall_count=0)
        except Exception as exc:  # noqa: BLE001 - mirror the guest worker's catch-all
            return ProgramResult(
                ok=False,
                error=f"program_error: {type(exc).__name__}: {exc}",
                syscall_count=0,
            )
        return ProgramResult(ok=True, error=None, syscall_count=0)
