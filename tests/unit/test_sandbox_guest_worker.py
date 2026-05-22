"""Unit tests for the sandbox guest worker's program-authoring contract.

The guest worker runs untrusted model-authored Python inside the gVisor
sandbox.  These tests pin down the import allow-list it exposes to the
program — the set of standard-library modules a ``run`` program may import.
They run the worker's own restricted compile/exec pipeline directly so they
do not require ``runsc``; gVisor isolation is covered separately in
``tests/integration/test_sandbox_runtime_runsc.py``.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from ariel.sandbox_guest_worker import _build_safe_builtins, _run


def _exec_program(source: str) -> dict[str, Any]:
    program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
    exec(compile(source, "<test-program>", "exec"), program_globals)  # noqa: S102
    return program_globals


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


def test_program_can_import_urllib_parse_for_url_text_parsing() -> None:
    # ``urllib.parse`` is pure-text URL parsing — no network, no I/O. The
    # model uses it to inspect source URLs; pure-text utilities should not block
    # normal use.
    program_globals = _exec_program(
        "from urllib.parse import urlparse\n"
        "parsed = urlparse('https://example.com/path?q=1')\n"
        "host = parsed.netloc\n"
    )
    assert program_globals["host"] == "example.com"


def test_program_can_import_email_utils_for_rfc2822_dates() -> None:
    # ``email.utils`` is pure-text RFC2822 date and address parsing. The
    # model uses ``parsedate_to_datetime`` when reasoning over message metadata.
    program_globals = _exec_program(
        "from email.utils import parsedate_to_datetime\n"
        "dt = parsedate_to_datetime('Tue, 20 May 2026 12:00:00 +0000')\n"
        "year = dt.year\n"
    )
    assert program_globals["year"] == 2026


def test_program_cannot_import_urllib_request_or_other_io_submodules() -> None:
    # The allowlist is per-exact-dotted-path: ``urllib.parse`` is allowed
    # but ``urllib.request`` (network-capable), ``urllib.robotparser`` (HTTP
    # client), and the bare ``urllib`` package are not.
    program_globals: dict[str, Any] = {"__builtins__": _build_safe_builtins()}
    for blocked in (
        "import urllib.request\n",
        "import urllib.robotparser\n",
        "import urllib\n",
        "from urllib.request import urlopen\n",
        "import email.message\n",
        "import email\n",
    ):
        with pytest.raises(ImportError, match="not allowed"):
            exec(compile(blocked, "<t>", "exec"), program_globals)  # noqa: S102


def _drive_run(source: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Drive ``_run`` end-to-end so the exec try/except in the production code
    # path is the thing under test. Feed a single ``run-program`` start message
    # on stdin; discard whatever the worker writes to stdout; return the dict
    # ``_run`` produces. No syscalls means the program cannot block on the host
    # channel, which keeps the test single-shot.
    start = {
        "type": "run-program",
        "source": source,
        "syscall_names": [],
        "limits": {"max_syscalls": 1, "max_output_bytes": 1024},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(start) + "\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    return _run()


def test_program_clean_systemexit(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``raise SystemExit()`` is the natural Python idiom for "end here, all
    # good"; it must produce a clean program result, not ``NameError``.
    result = _drive_run("raise SystemExit()\n", monkeypatch)
    assert result == {"type": "program-result", "ok": True, "error": None}


def test_program_systemexit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``raise SystemExit(0)`` is also a clean exit, mirroring CPython.
    result = _drive_run("raise SystemExit(0)\n", monkeypatch)
    assert result == {"type": "program-result", "ok": True, "error": None}


def test_program_systemexit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-zero int code is a failure exit; the code is rendered into the
    # error string so the host can see what the program reported.
    result = _drive_run("raise SystemExit(2)\n", monkeypatch)
    assert result["type"] == "program-result"
    assert result["ok"] is False
    assert "SystemExit: 2" in result["error"]


def test_program_systemexit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # A string code is the "die with a message" idiom; treat it as failure
    # and surface the message in the error string.
    result = _drive_run("raise SystemExit('no results found')\n", monkeypatch)
    assert result["type"] == "program-result"
    assert result["ok"] is False
    assert "no results found" in result["error"]
