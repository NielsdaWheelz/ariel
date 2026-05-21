"""Unit tests for the sandbox guest worker's program-authoring contract.

The guest worker runs untrusted model-authored Python inside the gVisor
sandbox.  These tests pin down the import allow-list it exposes to the
program — the set of standard-library modules a ``run`` program may import.
They run the worker's own restricted compile/exec pipeline directly so they
do not require ``runsc``; gVisor isolation is covered separately in
``tests/integration/test_sandbox_runtime_runsc.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ariel.sandbox_guest_worker import _ALLOWED_IMPORTS, _build_safe_builtins


def _exec_program(source: str) -> dict[str, Any]:
    program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
    exec(compile(source, "<test-program>", "exec"), program_globals)  # noqa: S102
    return program_globals


def test_time_is_in_the_program_import_allowlist() -> None:
    # The model frequently reaches for ``time.time`` and ``time.strftime`` to
    # compute "today" boundaries.  Both are benign reads; CPU and wall-clock
    # limits already bound the program even against ``time.sleep``.
    assert "time" in _ALLOWED_IMPORTS


def test_program_can_import_time_and_call_a_safe_function() -> None:
    # A program that asks for "emails received today" naturally builds an
    # epoch boundary from ``time``.  This is the exact failure mode that
    # produced misleading "the Gmail connector failed" replies in production.
    program_globals = _exec_program(
        "import time\nnow_seconds = time.time()\nstamp = time.strftime('%Y', time.gmtime(0))\n"
    )
    assert isinstance(program_globals["now_seconds"], float)
    assert program_globals["stamp"] == "1970"


def test_program_cannot_import_os_even_through_the_import_builtin() -> None:
    # Defense-in-depth: the dangerous modules stay rejected regardless of
    # how the program reaches for them.
    program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
    with pytest.raises(ImportError, match="not allowed"):
        exec(compile("import os\n", "<t>", "exec"), program_globals)  # noqa: S102
    with pytest.raises(ImportError, match="not allowed"):
        exec(  # noqa: S102
            compile("__import__('socket')\n", "<t>", "exec"), program_globals
        )
