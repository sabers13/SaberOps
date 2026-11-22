"""D3: streaming pipe handles are closed once, never by a later finalizer."""

from __future__ import annotations

import gc
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from saberops import process
from saberops.process import run_process

_SRC = str(Path(__file__).resolve().parents[1] / "src")

#: Fake worker whose leader exits while a descendant in its own session keeps
#: the inherited stdout/stderr write ends open.  Group termination cannot reach
#: that descendant, so a drain thread is still blocked in readline when
#: run_process cleans up: the orphaned-pipe cleanup path.
_ORPHAN_WORKER = """\
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4.0)"],
                 start_new_session=True)
sys.stdout.write("partial")
sys.stdout.flush()
sys.exit(0)
"""


def _drain_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if getattr(thread, "_target", None) is process._drain_stream_marked
    ]

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


def test_orphaned_pipe_helper_closes_the_raw_file_not_only_its_fd() -> None:
    """The blocked-drain cleanup marks the FileIO closed, not just the fd.

    A bare ``os.close(fd)`` leaves the wrapper believing it still owns an open
    file, so a later finalizer closes the descriptor a second time.  Closing
    the raw FileIO through the wrapper is idempotent for every later close.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        raw = cast(Any, proc.stdout).buffer.raw
        process._close_pipe_stream_after_unconfirmed_drain(proc.stdout)
        assert raw.closed is True
        assert process._ORPHANED_PIPE_STREAMS == []
    finally:
        proc.kill()
        proc.wait()


def test_orphaned_pipe_cleanup_closes_the_raw_file_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orphaned-drain path must not leave a wrapper to double-close its fd.

    When a drain thread is still blocked in readline, the cleanup used to close
    only the numeric descriptor and retain the wrapper.  The retained wrapper
    then closed that descriptor a second time at interpreter shutdown
    (D3-remainder).  Closing the raw FileIO through the wrapper instead marks
    the layer closed, so every later finalizer is an idempotent no-op.
    """
    monkeypatch.setattr(process, "_DRAIN_COMPLETION_SECONDS", 0.1)
    monkeypatch.setattr(process, "_DRAIN_CLEANUP_SECONDS", 0.1)
    unraisable: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    result = run_process(
        [sys.executable, "-c", _ORPHAN_WORKER],
        on_stdout=lambda _line: None,
        timeout=30.0,
    )
    blocked = _drain_threads()
    assert result.exit_code == 0
    assert blocked, "the fake worker did not leave a blocked drain thread"

    # Let the orphan release the pipe and its drain thread end, then finalize
    # every remaining reference the way interpreter shutdown would.
    for thread in blocked:
        thread.join(timeout=10.0)
    process._ORPHANED_PIPE_STREAMS.clear()
    gc.collect()

    assert unraisable == [], [repr(item) for item in unraisable]


_PYTHON_314_ORPHAN_PROBE = """\
import gc, sys, threading
import saberops.process as P

P._DRAIN_COMPLETION_SECONDS = 0.1
P._DRAIN_CLEANUP_SECONDS = 0.1
worker = r'''
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4.0)"],
                 start_new_session=True)
sys.stdout.write("partial")
sys.stdout.flush()
sys.exit(0)
'''
result = P.run_process(
    [sys.executable, "-c", worker], on_stdout=lambda _line: None, timeout=30.0
)
threads = [
    thread
    for thread in threading.enumerate()
    if getattr(thread, "_target", None) is P._drain_stream_marked
]
for thread in threads:
    thread.join(timeout=10.0)
P._ORPHANED_PIPE_STREAMS.clear()
gc.collect()
print("worker dispatch ok", result.exit_code)
"""


def test_python_314_orphaned_worker_transcript_has_no_bad_fd(tmp_path: Path) -> None:
    """Replay the D3-remainder transcript on the interpreter that reported it."""
    python314 = shutil.which("python3.14")
    if python314 is None:
        pytest.skip("python3.14 is not installed on this host")
    transcript = tmp_path / "worker.ansi"
    with transcript.open("wb") as sink:
        completed = subprocess.run(
            [python314, "-c", _PYTHON_314_ORPHAN_PROBE],
            cwd=Path(_SRC).parent,
            env={"PYTHONPATH": _SRC, "PATH": "/usr/bin:/bin"},
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    text = transcript.read_text(encoding="utf-8", errors="replace")
    assert completed.returncode == 0, text
    assert "worker dispatch ok 0" in text, text
    assert "Bad file descriptor" not in text, text


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
