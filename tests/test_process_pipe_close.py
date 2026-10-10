"""run_process closes each pipe descriptor exactly once."""

from __future__ import annotations

import gc
import sys
import threading
from pathlib import Path
from typing import Any

from saberops.process import run_process


def test_run_process_does_not_double_close_pipe_descriptors(tmp_path: Path) -> None:
    """Closing the raw fd and leaving the wrapper to the GC raised EBADF at finalization.

    In a threaded server the same fd number can be reused before that second close,
    so the stale close would hit an unrelated descriptor.  The trigger is a control
    interrupt while a descendant still holds the pipe: the group is killed, then the
    owned pipe handles are closed.
    """
    child = tmp_path / "child.pid"
    ready = threading.Event()
    code = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); print('ready',flush=True);time.sleep(60)"
    )
    unraisable: list[Any] = []
    previous = sys.unraisablehook
    sys.unraisablehook = unraisable.append
    try:
        process = run_process(
            [sys.executable, "-c", code, str(child)],
            timeout=8,
            on_stdout=lambda _: ready.set(),
            control_interrupt=ready.is_set,
            termination_grace_seconds=0.1,
        )
        assert process.control_interrupted and not process.termination_incomplete
        gc.collect()
    finally:
        sys.unraisablehook = previous
    assert unraisable == []
