"""N9: a busy tree with no output is cut before the hard 1200 s timeout.

The monitor distinguishes a whole-tree stall (STALLED) from a tree that keeps
doing CPU/I/O work without emitting any output (AMBIGUOUS_BUSY /
``tree_activity_without_output``).  The latter used to run until the hard
timeout, burning the whole worker budget.  It is now cut after a conservative
sustained period so the repair/retry path runs; any output resets the clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import saberops.monitor as monitor_module
from saberops.monitor import (
    MonitorState,
    ProcessGroupSnapshot,
    WorkerMonitor,
    _ProcEntry,
)


def _snapshot(pgid: int, *, tick: int, incomplete: bool = False) -> Any:
    return ProcessGroupSnapshot(
        pgid=pgid,
        members=[
            _ProcEntry(
                pid=pgid,
                utime=tick,
                stime=0,
                state="R",
                io_read_bytes=0,
                io_write_bytes=0,
                rss_bytes=0,
            )
        ],
        evidence_incomplete=incomplete,
    )


def _monitor(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> WorkerMonitor:
    monkeypatch.setattr(
        monitor_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    tick = [0]

    def snapshot(pgid: int) -> Any:
        tick[0] += 1
        return _snapshot(pgid, tick=tick[0])

    monkeypatch.setattr(monitor_module, "snapshot_process_group", snapshot)
    return WorkerMonitor(
        424242,
        soft_inactivity=300.0,
        stall_confirmation=1800.0,
        sample_interval=0.0,
        tree_activity_without_output_cutoff=600.0,
    )


def test_busy_tree_without_output_is_cut_after_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monitor = _monitor(monkeypatch, clock)

    assert monitor.sample() is MonitorState.ACTIVE
    clock[0] = 301.0
    assert monitor.sample() is MonitorState.AMBIGUOUS_BUSY
    assert monitor.reason == "tree_activity_without_output"
    assert monitor.should_terminate() is False
    assert monitor.should_terminate_for_idle_tree() is False

    clock[0] = 900.0
    assert monitor.sample() is MonitorState.AMBIGUOUS_BUSY
    assert monitor.should_terminate_for_idle_tree() is True


def test_any_output_resets_the_idle_tree_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monitor = _monitor(monkeypatch, clock)

    assert monitor.sample() is MonitorState.ACTIVE
    clock[0] = 301.0
    assert monitor.sample() is MonitorState.AMBIGUOUS_BUSY
    clock[0] = 901.0
    monitor.sample()
    assert monitor.should_terminate_for_idle_tree() is True

    monitor.touch_stdout()
    assert monitor.should_terminate_for_idle_tree() is False
    clock[0] = 902.0
    assert monitor.sample() is MonitorState.ACTIVE
    assert monitor.should_terminate_for_idle_tree() is False


def test_incomplete_evidence_never_cuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        monitor_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(
        monitor_module,
        "snapshot_process_group",
        lambda pgid: _snapshot(pgid, tick=1, incomplete=True),
    )
    monitor = WorkerMonitor(
        424242,
        soft_inactivity=300.0,
        stall_confirmation=1800.0,
        sample_interval=0.0,
        tree_activity_without_output_cutoff=600.0,
    )
    clock[0] = 9000.0
    assert monitor.sample() is MonitorState.AMBIGUOUS_BUSY
    assert monitor.reason == "evidence_incomplete"
    assert monitor.should_terminate_for_idle_tree() is False
