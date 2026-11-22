"""D3: streaming pipe handles are closed once, never by a later finalizer."""

from __future__ import annotations

import gc
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from saberops.process import run_process

_SRC = str(Path(__file__).resolve().parents[1] / "src")

#: Python 3.14 reports a close of an already-closed descriptor as an ignored
#: finalizer exception; 3.12 silently tolerates it.  The confirming real run
#: used 3.14, so the regression is replayed on that interpreter when present.
_PROBE = """\
import gc, sys
from saberops.process import run_process

seen = []
sys.unraisablehook = lambda arg: seen.append(f"{arg.exc_type.__name__}: {arg.exc_value}")
result = run_process(
    [sys.executable, "-c", "print('pipe-ok')"],
    on_stdout=lambda _line: None,
    timeout=30.0,
)
gc.collect()
print("exit", result.exit_code)
print("stdout", result.stdout.strip())
print("unraisable", len(seen))
for message in seen:
    print("detail", message)
"""


def test_streaming_pipe_cleanup_leaves_no_finalizer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed streaming run must not double-close its pipe wrappers."""
    unraisable: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    result = run_process(
        [sys.executable, "-c", "print('pipe-ok')"],
        on_stdout=lambda _line: None,
        timeout=30.0,
    )
    gc.collect()

    assert result.exit_code == 0
    assert "pipe-ok" in result.stdout
    assert unraisable == [], [repr(item) for item in unraisable]


def test_python_314_finalizer_sees_no_bad_file_descriptor() -> None:
    """D3 replay on the interpreter that reported the 12 tracebacks."""
    python314 = shutil.which("python3.14")
    if python314 is None:
        pytest.skip("python3.14 is not installed on this host")
    completed = subprocess.run(
        [python314, "-c", _PROBE],
        cwd=Path(_SRC).parent,
        env={"PYTHONPATH": _SRC, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "exit 0" in completed.stdout
    assert "stdout pipe-ok" in completed.stdout
    assert "unraisable 0" in completed.stdout, completed.stdout
