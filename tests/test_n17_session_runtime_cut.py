"""N17 replay: the idle cut must name itself on the OpenCode runtime path.

The confirming real run (``/var/tmp/so-confirm2/report.md`` §8) cut an
OpenCode worker at the 600 s idle-tree cutoff, but the durable attempt said
``worker_error: HTTP 503 {"code":"service_stopping"}`` (and, on the next run,
``<urlopen error [Errno 111] Connection refused>``) with
``termination_reason: null``.  The turn's HTTP call fails while the owned
server is being killed, which is *before* ``launch_worker_process`` returns and
publishes the typed cut.  These tests replay that race with the recorded
symptom and require the typed ``idle_tree_cutoff`` to reach the result.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from saberops.models import AttemptStatus, DispatchOutcome, WorkerRequest
from saberops.process import ProcessResult
from saberops.session_control import SessionControls
from saberops.session_journal import SessionJournal
from saberops.session_runtime import AgentSessionRuntime, SessionRuntimeError

_CUT_NOTE = (
    "\n[Execution error: worker process tree stayed active without producing "
    "output for 600s; stopped early so the repair/retry path can run]"
)


def _runtime(tmp_path: Path, **request_kwargs: Any) -> AgentSessionRuntime:
    controls = SessionControls(tmp_path / "controls")
    journal = SessionJournal(tmp_path / "journal")
    runtime = AgentSessionRuntime(
        tmp_path / "runtime", "sid", controls, journal, lambda _payload: None
    )
    runtime.request = WorkerRequest(
        "task",
        tmp_path,
        "opencode-go/deepseek-v4.1-flash",
        **request_kwargs,
    )
    return runtime


def test_idle_cut_reason_survives_the_turn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay ``HTTP 503 ... service_stopping`` racing the cut (report §8)."""
    runtime = _runtime(tmp_path)
    request = runtime.request
    assert request is not None
    launched = threading.Event()
    published = threading.Event()

    def serve() -> None:
        # Wait until the engine has decided to terminate, then publish the
        # result exactly like launch_worker_process does after reaping.
        assert published.wait(5.0)
        runtime.process_result = ProcessResult(
            ["opencode", "serve"],
            1,
            "",
            "",
            600.4,
            termination_reason="idle_tree_cutoff",
            termination_note=_CUT_NOTE,
        )

    thread = threading.Thread(target=serve, daemon=True)

    def ensure_started(_request: WorkerRequest, _binary: str) -> None:
        runtime.thread = thread
        thread.start()
        launched.set()

    def turn(_request: WorkerRequest, _started: float) -> Any:
        assert launched.wait(5.0)
        # The monitor begins terminating the owned server while the turn's
        # provider API call is in flight.
        runtime.monitor_transition(
            {"state": "TERMINATING", "reason": "termination_in_progress"}
        )
        published.set()
        raise SessionRuntimeError(
            'Provider API GET /api/session/ses_1/message failed: HTTP 503: '
            '{"code":"service_stopping"}'
        )

    monkeypatch.setattr(runtime, "ensure_started", ensure_started)
    monkeypatch.setattr(runtime, "_turn", turn)

    result = runtime.run_opencode(request, "opencode")

    assert result.status is AttemptStatus.FAILED
    assert result.outcome is DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
    assert result.termination_reason == "idle_tree_cutoff"
    assert result.termination_note == _CUT_NOTE
    assert result.error == _CUT_NOTE
    assert result.summary == _CUT_NOTE
    thread.join(5.0)


def test_monitor_transition_marks_termination_and_forwards(tmp_path: Path) -> None:
    """The runtime keeps its own cut signal and never swallows the hook."""
    seen: list[dict[str, Any]] = []
    runtime = _runtime(tmp_path, on_monitor_transition=seen.append)

    runtime.monitor_transition(
        {"state": "AMBIGUOUS_BUSY", "reason": "tree_activity_without_output"}
    )
    assert runtime.monitor_terminating is False
    runtime.monitor_transition({"state": "TERMINATING", "reason": "termination_in_progress"})
    runtime.monitor_transition({"state": "TERMINAL", "reason": "process_tree_extinct"})

    assert runtime.monitor_terminating is True
    assert [payload["state"] for payload in seen] == [
        "AMBIGUOUS_BUSY",
        "TERMINATING",
        "TERMINAL",
    ]
