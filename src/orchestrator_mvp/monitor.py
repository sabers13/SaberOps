"""WorkerMonitor: deterministic lifecycle monitor for a provider process tree.

The monitor lives alongside the streaming/wait loop in run_process.  Every
stdout/stderr line touches the monitor; the main wait-loop samples it at a
configurable interval.  State transitions drive lifecycle events; no SQLite
row is written on every sample.

States:
    ACTIVE       — recent output (< soft_inactivity threshold).  Continue.
    QUIET        — no output and no runtime activity for soft_inactivity but
                   < stall_confirmation.  Continue; attention only.
    AMBIGUOUS_BUSY — no recent output but descendant CPU/I/O activity found
                   (or evidence unreadable).  Continue; attention only.
                   Never kill.  A productive AMBIGUOUS_BUSY period resets the
                   stall-confirmation anchor.
    STALLED      — whole-tree inactivity (no output, no CPU/I/O/topology
                   activity) sustained from the LAST meaningful activity for
                   the full stall_confirmation window, with evidence complete
                   enough for a destructive decision.  Graceful tree
                   termination is authorised.
    TERMINATING  — process tree is being stopped.
    TERMINAL     — process tree is confirmed extinct.

The stall clock is anchored on the most recent *meaningful* activity
(stdout, stderr, aggregate tree CPU delta, aggregate tree I/O delta,
meaningful topology change) — never on when output merely went quiet.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class MonitorState(StrEnum):
    ACTIVE = "ACTIVE"
    QUIET = "QUIET"
    AMBIGUOUS_BUSY = "AMBIGUOUS_BUSY"
    STALLED = "STALLED"
    TERMINATING = "TERMINATING"
    TERMINAL = "TERMINAL"


# ── defaults ────────────────────────────────────────────────────────────────
SOFT_INACTIVITY_SECONDS: float = 300.0
STALL_CONFIRMATION_SECONDS: float = 1800.0
MONITOR_SAMPLE_INTERVAL_SECONDS: float = 5.0


# ── canonical whole-tree /proc primitives ────────────────────────────────────

@dataclass
class _ProcEntry:
    """Raw /proc data for one live, non-zombie process in the PGID."""
    pid: int
    utime: int
    stime: int
    state: str  # single character from /proc/<pid>/stat field 3
    # I/O counters (bytes read+written); the enclosing snapshot is marked
    # evidence_incomplete when they could not be read (never silently zero)
    io_read_bytes: int = 0
    io_write_bytes: int = 0


def _read_stat_fields(stat_text: str) -> list[str] | None:
    """Extract post-comm fields from raw /proc/*/stat text."""
    close = stat_text.rfind(")")
    if close < 0:
        return None
    parts = stat_text[close + 1:].split()
    # Fields after ')': index 0 = state (field 3), 1=ppid, 2=pgrp, ...
    # utime = field 14 (index 11 post-comm), stime = field 15 (index 12)
    # pgrp  = field  5 (index  2 post-comm)
    return parts


def _read_io_counters(pid: int) -> tuple[int, int] | None:
    """Return (read_bytes, write_bytes) from /proc/<pid>/io, or None.

    ``None`` is deliberately distinct from ``(0, 0)``: for a process that is
    known to belong to the worker PGID, unreadable I/O evidence must be
    represented as *unknown*, never silently as zero activity.  Callers mark
    the evidence incomplete and fail conservatively (AMBIGUOUS_BUSY, not
    STALLED).
    """
    try:
        text = (Path("/proc") / str(pid) / "io").read_text(
            encoding="utf-8", errors="replace"
        )
        rb = wb = None
        for line in text.splitlines():
            if line.startswith("read_bytes:"):
                try:
                    rb = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("write_bytes:"):
                try:
                    wb = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if rb is None or wb is None:
            # Counters absent or unparsable: unknown, not zero.
            return None
        return rb, wb
    except (OSError, PermissionError, FileNotFoundError):
        return None


@dataclass
class ProcessGroupSnapshot:
    """Whole-tree snapshot for one worker PGID."""

    pgid: int
    members: list[_ProcEntry] = field(default_factory=list)
    # True iff at least one member stat was unreadable (fail-conservative)
    evidence_incomplete: bool = False

    @property
    def live_count(self) -> int:
        """Number of live (non-zombie) members."""
        return sum(1 for m in self.members if m.state not in ("Z", "X", "x"))

    @property
    def zombie_count(self) -> int:
        return sum(1 for m in self.members if m.state in ("Z", "X", "x"))

    @property
    def has_live_members(self) -> bool:
        return self.live_count > 0

    @property
    def total_cpu_ticks(self) -> int:
        return sum(m.utime + m.stime for m in self.members)

    @property
    def total_io_bytes(self) -> int:
        return sum(m.io_read_bytes + m.io_write_bytes for m in self.members)


def snapshot_process_group(pgid: int) -> ProcessGroupSnapshot:
    """Build a whole-tree snapshot for all /proc entries with the given PGID.

    - Zombies are included in the listing (for topology counting) but not
      counted as live workers.
    - Unreadable stat entries set ``evidence_incomplete`` so callers fail
      conservatively (AMBIGUOUS_BUSY, not STALLED).
    - Never uses ``os.kill``, ``ps``, or shell parsing.
    """
    snap = ProcessGroupSnapshot(pgid=pgid)
    try:
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid_str = entry.name
            stat_path = entry / "stat"
            try:
                stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, ProcessLookupError):
                # Process vanished between listing and read — benign.
                continue
            except (PermissionError, OSError):
                # Unreadable → conservative
                snap.evidence_incomplete = True
                continue

            fields = _read_stat_fields(stat_text)
            if fields is None or len(fields) < 13:
                snap.evidence_incomplete = True
                continue

            try:
                proc_pgid = int(fields[2])
            except (ValueError, IndexError):
                snap.evidence_incomplete = True
                continue

            if proc_pgid != pgid:
                continue

            # This PID belongs to the target process group.
            try:
                pid = int(pid_str)
                state = fields[0]
                utime = int(fields[11])
                stime = int(fields[12])
            except (ValueError, IndexError):
                snap.evidence_incomplete = True
                continue

            io_read_bytes = io_write_bytes = 0
            if state not in ("Z", "X", "x"):
                # Zombies have no address space: /proc/<pid>/io is denied by
                # the kernel for them by design.  Their I/O is irrelevant —
                # extinction checks ignore zombies — so never count a
                # zombie's unreadable io as incomplete evidence.
                io_counters = _read_io_counters(pid)
                if io_counters is None:
                    # The member was alive when its stat was read.
                    # Distinguish a benign race (it exited before its io file
                    # was read) from evidence we genuinely cannot read; the
                    # latter is *unknown*, never silently zero.
                    try:
                        stat_path.read_text(encoding="utf-8", errors="replace")
                    except (FileNotFoundError, ProcessLookupError):
                        # Vanished between the stat and io reads — benign.
                        continue
                    except OSError:
                        snap.evidence_incomplete = True
                        continue
                    snap.evidence_incomplete = True
                else:
                    io_read_bytes, io_write_bytes = io_counters
            snap.members.append(
                _ProcEntry(
                    pid=pid,
                    utime=utime,
                    stime=stime,
                    state=state,
                    io_read_bytes=io_read_bytes,
                    io_write_bytes=io_write_bytes,
                )
            )
    except PermissionError:
        snap.evidence_incomplete = True
    except OSError:
        snap.evidence_incomplete = True
    return snap


def group_has_live_members(pgid: int) -> bool:
    """Return True if the PGID has at least one live (non-zombie) member.

    Reads /proc directly.  On any unreadable evidence fails conservatively
    (returns True so callers do not falsely declare extinction).
    """
    snap = snapshot_process_group(pgid)
    if snap.evidence_incomplete:
        return True  # fail closed
    return snap.has_live_members


# Re-export canonical name used by process.py and ownership.py
_group_has_members = group_has_live_members


# ── evidence dataclass ───────────────────────────────────────────────────────

@dataclass
class MonitorEvidence:
    """Snapshot of deterministic runtime evidence for one sample."""

    timestamp: float = 0.0
    process_tree_live: bool = False
    stdout_at: float = 0.0
    stderr_at: float = 0.0
    cpu_user_ticks: int = 0
    cpu_kernel_ticks: int = 0
    io_bytes: int = 0
    member_count: int = 0
    evidence_incomplete: bool = False


# ── WorkerMonitor ────────────────────────────────────────────────────────────

class WorkerMonitor:
    """Monitor a single worker process tree via deterministic evidence.

    Wire-in:
      - Call ``touch_stdout()`` / ``touch_stderr()`` from stream drain callbacks.
      - Call ``sample()`` periodically at *sample_interval* from the wait loop.
      - When ``state`` is STALLED, call ``should_terminate()`` to confirm.
      - Callers then call ``mark_terminating()`` / ``mark_terminal()``.

    No model calls, no Git polling, no per-byte SQLite writes.
    """

    def __init__(
        self,
        pgid: int,
        *,
        soft_inactivity: float = SOFT_INACTIVITY_SECONDS,
        stall_confirmation: float = STALL_CONFIRMATION_SECONDS,
        sample_interval: float = MONITOR_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._pgid = pgid
        self._soft_inactivity = soft_inactivity
        self._stall_confirmation = stall_confirmation
        self._sample_interval = sample_interval

        now = time.monotonic()
        self._last_stdout: float = now
        self._last_stderr: float = now
        self._last_evidence: MonitorEvidence = MonitorEvidence()
        self._last_sample: float = now
        self._state: MonitorState = MonitorState.ACTIVE
        self._state_reason: str = "monitor_started"
        # The ONE stall-confirmation anchor: the most recent *meaningful*
        # execution activity (stdout, stderr, tree CPU delta, tree I/O delta,
        # or meaningful topology change).  STALLED requires sustained
        # whole-tree inactivity measured from this instant — never from the
        # first time output merely went quiet.
        self._last_activity: float = now
        self._prev_snap: ProcessGroupSnapshot | None = None

    # Backwards-compatible alias: historically the anchor was called
    # ``_quiet_since`` (first-quiet timestamp).  It is now the
    # last-meaningful-activity timestamp; the alias keeps external pokes
    # (tests, tooling) working against the same anchor.
    @property
    def _quiet_since(self) -> float | None:
        return self._last_activity

    @_quiet_since.setter
    def _quiet_since(self, value: float | None) -> None:
        if value is not None:
            self._last_activity = value

    @property
    def state(self) -> MonitorState:
        """Current monitor state."""
        return self._state

    @property
    def pgid(self) -> int:
        return self._pgid

    @property
    def evidence(self) -> MonitorEvidence:
        """Most recent evidence snapshot."""
        return self._last_evidence

    @property
    def reason(self) -> str:
        """Reason/evidence summary for the most recent state decision."""
        return self._state_reason

    def touch_stdout(self) -> None:
        """Record that stdout was emitted (called from streaming callbacks)."""
        now = time.monotonic()
        self._last_stdout = now
        self._last_activity = now

    def touch_stderr(self) -> None:
        """Record that stderr was emitted (called from streaming callbacks)."""
        now = time.monotonic()
        self._last_stderr = now
        self._last_activity = now

    def _last_output(self) -> float:
        return max(self._last_stdout, self._last_stderr)

    def sample(self) -> MonitorState:
        """Take one deterministic evidence sample and return the new state.

        Call this periodically at *sample_interval* (default 5 seconds)
        from the process wait loop.  The call is cheap: if the interval
        has not elapsed it returns the cached state immediately.
        """
        now = time.monotonic()
        if now - self._last_sample < self._sample_interval:
            return self._state
        self._last_sample = now

        snap = snapshot_process_group(self._pgid)
        prev_snap = self._prev_snap
        self._prev_snap = snap

        # 1. Extinction check
        if not snap.has_live_members and not snap.evidence_incomplete:
            self._state = MonitorState.TERMINAL
            self._state_reason = "process_tree_extinct"
            self._last_evidence = MonitorEvidence(
                timestamp=now,
                process_tree_live=False,
                stdout_at=self._last_stdout,
                stderr_at=self._last_stderr,
                evidence_incomplete=snap.evidence_incomplete,
            )
            return self._state

        # 2. Compute activity signals
        output_quiet = now - self._last_output()
        output_active = output_quiet < self._soft_inactivity

        # CPU delta across the whole tree
        cpu_delta = 0
        if prev_snap is not None:
            prev_cpu = {m.pid: (m.utime + m.stime) for m in prev_snap.members}
            for m in snap.members:
                if m.state in ("Z", "X", "x"):
                    continue
                prev = prev_cpu.get(m.pid, 0)
                cpu_delta += max(0, (m.utime + m.stime) - prev)

        # I/O delta
        io_delta = 0
        if prev_snap is not None:
            prev_io = {m.pid: (m.io_read_bytes + m.io_write_bytes) for m in prev_snap.members}
            for m in snap.members:
                if m.state in ("Z", "X", "x"):
                    continue
                prev = prev_io.get(m.pid, 0)
                io_delta += max(0, (m.io_read_bytes + m.io_write_bytes) - prev)

        # Topology change (member count change) is also evidence of activity
        topology_changed = prev_snap is not None and (
            len(snap.members) != len(prev_snap.members)
        )

        tree_activity = cpu_delta > 0 or io_delta > 0 or topology_changed

        # 3. State machine.  Every meaningful-activity signal (output, tree
        # CPU, tree I/O, topology) resets the ONE stall-confirmation anchor,
        # so a productive AMBIGUOUS_BUSY period always postpones — never
        # preserves — a pending stall verdict.
        if output_active:
            self._state = MonitorState.ACTIVE
            self._state_reason = "recent_output"
            self._last_activity = now
        elif snap.evidence_incomplete:
            # Unreadable runtime evidence cannot authorise a destructive
            # decision and may conceal live activity: fail conservatively and
            # postpone the stall clock.
            self._state = MonitorState.AMBIGUOUS_BUSY
            self._state_reason = "evidence_incomplete"
            self._last_activity = now
        elif tree_activity:
            # Whole-tree CPU/I/O/topology activity without recent output.
            self._state = MonitorState.AMBIGUOUS_BUSY
            self._state_reason = "tree_activity_without_output"
            self._last_activity = now
        else:
            # No output and no runtime activity.  The inactivity duration is
            # measured from the LAST meaningful activity, so a long quiet
            # output period that overlapped productive AMBIGUOUS_BUSY work
            # does not short-circuit stall confirmation.
            inactivity = now - self._last_activity
            if inactivity < self._soft_inactivity:
                self._state = MonitorState.ACTIVE
                self._state_reason = f"whole_tree_inactive_{inactivity:.0f}s_below_soft"
            elif inactivity < self._stall_confirmation:
                self._state = MonitorState.QUIET
                self._state_reason = f"whole_tree_quiet_{inactivity:.0f}s"
            else:
                self._state = MonitorState.STALLED
                self._state_reason = f"whole_tree_inactive_{inactivity:.0f}s"

        self._last_evidence = MonitorEvidence(
            timestamp=now,
            process_tree_live=snap.has_live_members,
            stdout_at=self._last_stdout,
            stderr_at=self._last_stderr,
            cpu_user_ticks=snap.total_cpu_ticks,
            cpu_kernel_ticks=0,  # folded into total_cpu_ticks above
            io_bytes=snap.total_io_bytes,
            member_count=snap.live_count,
            evidence_incomplete=snap.evidence_incomplete,
        )
        return self._state

    def should_terminate(self) -> bool:
        """Return True when the monitor authorises graceful termination.

        ALL conditions must hold:
        - state is STALLED (not AMBIGUOUS_BUSY);
        - evidence is not marked incomplete.
        """
        return (
            self._state is MonitorState.STALLED
            and not self._last_evidence.evidence_incomplete
        )

    def mark_terminating(self) -> None:
        """Transition to TERMINATING (external decision reached)."""
        self._state = MonitorState.TERMINATING
        self._state_reason = "termination_in_progress"

    def mark_terminal(self) -> None:
        """Transition to TERMINAL (process tree confirmed dead)."""
        self._state = MonitorState.TERMINAL
        self._state_reason = "process_tree_extinct"


# ── extinction helper (shared with process.py) ──────────────────────────────

def terminate_group_confirmed(
    pgid: int,
    *,
    grace_seconds: float = 5.0,
    kill_wait_seconds: float = 5.0,
    poll_interval: float = 0.05,
) -> tuple[bool, str]:
    """SIGTERM the PGID, wait for all live non-zombie members to disappear.

    SIGKILL follows only if live members remain after the grace period.
    Returns (success, reason).

    ``success`` is True ONLY when extinction is confirmed.  If members
    remain or evidence is unreadable, returns False / incomplete.

    This is the one canonical group extinction primitive used by both
    the monitor-driven stall path and the explicit cancel path.
    """
    # Initial liveness check — if already dead, nothing to do.
    snap = snapshot_process_group(pgid)
    if not snap.has_live_members and not snap.evidence_incomplete:
        return True, "already_extinct"

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        # Group may already be gone.
        snap2 = snapshot_process_group(pgid)
        if not snap2.has_live_members and not snap2.evidence_incomplete:
            return True, "already_extinct"
        return False, "signal_failed"

    # Wait for graceful exit
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        s = snapshot_process_group(pgid)
        if s.evidence_incomplete:
            time.sleep(poll_interval)
            continue
        if not s.has_live_members:
            return True, "terminated_group"
        time.sleep(poll_interval)

    # SIGKILL remaining
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    post_kill = time.monotonic() + max(0.0, kill_wait_seconds)
    while time.monotonic() < post_kill:
        s = snapshot_process_group(pgid)
        if s.evidence_incomplete:
            time.sleep(poll_interval)
            continue
        if not s.has_live_members:
            return True, "killed_group"
        time.sleep(poll_interval)

    # Check final state
    final = snapshot_process_group(pgid)
    if final.evidence_incomplete:
        return False, "extinction_evidence_incomplete"
    if final.has_live_members:
        return False, "members_remain_after_kill"
    return True, "killed_group"


__all__ = [
    "MonitorEvidence",
    "MonitorState",
    "ProcessGroupSnapshot",
    "SOFT_INACTIVITY_SECONDS",
    "STALL_CONFIRMATION_SECONDS",
    "MONITOR_SAMPLE_INTERVAL_SECONDS",
    "WorkerMonitor",
    "_group_has_members",
    "group_has_live_members",
    "snapshot_process_group",
    "terminate_group_confirmed",
]
