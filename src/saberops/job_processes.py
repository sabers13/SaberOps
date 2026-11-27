"""Per-job process and listening-port inventory (ADR-0100).

A job's worker runs in its own session and process group, and the engine
already records that fenced identity (host, boot id, pid, start ticks, session
and process group).  This module adds the missing observation: while the worker
runs it samples the group's members and their listening sockets, and at attempt
end it records a leftovers verdict naming anything from the job that is still
running or listening.

Two rules are absolute:

* **Report, never kill.** This module only reads ``/proc`` and writes evidence.
  Ending a process stays with the fenced termination path in
  :mod:`saberops.monitor` / :mod:`saberops.ownership`; a leftover is evidence
  for the owner, never a termination target.
* **Unknown is not zero.** Unreadable evidence, a worker with no engine-owned
  process group (host-run native workers) and processes that left the group are
  recorded as ``unknown`` / ``not_observable``, never as "nothing found".

Only reduced, scoped ``/proc`` reads are used: ``/proc/<pid>/stat``,
``/proc/<pid>/fd``, and ``/proc/net/tcp`` / ``/proc/net/tcp6``.  No ``lsof``,
``netstat`` or ``ss`` dependency exists or is added.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.db import Database, current_iso_timestamp
from saberops.ownership import ProcessIdentity

__all__ = [
    "ENGINE_SERVICE_PORTS",
    "SAMPLE_INTERVAL_SECONDS",
    "JobProcessRecorder",
    "ListeningSocket",
    "ListeningSockets",
    "read_listening_sockets",
    "record_native_worker_not_observable",
]

#: Sampling cadence while a worker runs.  Reading the group's fds and the two
#: ``/proc/net`` tables is cheap; two seconds keeps a short-lived listener from
#: vanishing between samples.  Tests patch this module constant.
SAMPLE_INTERVAL_SECONDS: float = 2.0

#: Ports the engine itself listens on, excluded from a job's port inventory by
#: service name.  The MCP server speaks stdio and owns no listening socket.
ENGINE_SERVICE_PORTS: dict[int, str] = {
    8765: "saberops-web",
}

_TCP_LISTEN = "0A"
_ZOMBIE_STATES = frozenset({"Z", "X", "x"})


def _sample_interval_from_env() -> float:
    """Return the sampling cadence, allowing an explicit override for rigs."""
    raw = os.environ.get("SABEROPS_JOB_PROCESS_SAMPLE_SECONDS", "").strip()
    if not raw:
        return SAMPLE_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return SAMPLE_INTERVAL_SECONDS


@dataclass(frozen=True)
class ListeningSocket:
    """One listening socket joined from a process fd to ``/proc/net``."""

    protocol: str
    port: int
    inode: int
    address: str


@dataclass(frozen=True)
class ListeningSockets:
    """Listening sockets from both tables, plus whether the read was complete."""

    sockets: tuple[ListeningSocket, ...]
    #: False when a table could not be read (unknown evidence, never zero).
    complete: bool


def _decode_address(raw: str, protocol: str) -> str:
    """Decode a ``/proc/net`` local address; return the raw text when unparsable."""
    chunks = raw.upper().split(":")
    if len(chunks) != 2:
        return raw
    host_hex, _port_hex = chunks
    try:
        if protocol == "tcp" and len(host_hex) == 8:
            packed = struct.pack("<L", int(host_hex, 16))
            return socket.inet_ntoa(packed)
        if protocol == "tcp6" and len(host_hex) == 32:
            words = [int(host_hex[index : index + 8], 16) for index in range(0, 32, 8)]
            packed = b"".join(struct.pack("<L", word) for word in words)
            return socket.inet_ntop(socket.AF_INET6, packed)
    except (OSError, ValueError):
        return raw
    return raw


def _parse_net_table(text: str, protocol: str) -> list[ListeningSocket]:
    """Parse one ``/proc/net/tcp*`` table and return its LISTEN sockets."""
    sockets: list[ListeningSocket] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or not fields[0].endswith(":"):
            # Header or malformed row.
            continue
        if fields[3] != _TCP_LISTEN:
            continue
        try:
            port = int(fields[1].split(":")[1], 16)
            inode = int(fields[9])
        except (IndexError, ValueError):
            continue
        sockets.append(
            ListeningSocket(
                protocol=protocol,
                port=port,
                inode=inode,
                address=_decode_address(fields[1], protocol),
            )
        )
    return sockets


def read_listening_sockets(net_root: Path = Path("/proc/net")) -> ListeningSockets:
    """Read every listening TCP socket from ``/proc/net/tcp`` and ``tcp6``.

    A missing IPv6 table does not make the evidence incomplete (many hosts do
    not have one).  Any other unreadable table, or an unreadable directory,
    marks the read incomplete so callers record ``unknown`` rather than zero.
    """
    sockets: list[ListeningSocket] = []
    complete = True
    saw_table = False
    for protocol in ("tcp", "tcp6"):
        path = net_root / protocol
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            if protocol == "tcp":
                complete = False
            continue
        except OSError:
            complete = False
            continue
        saw_table = True
        sockets.extend(_parse_net_table(text, protocol))
    if not saw_table:
        complete = False
    return ListeningSockets(tuple(sockets), complete)


@dataclass
class _Member:
    pid: int
    start_ticks: int
    comm: str
    is_leader: bool


def _read_member(pid: int, leader_pid: int, proc_root: Path) -> _Member | None:
    """Read one group member's stat.

    ``None`` means the process vanished between the snapshot and this read (a
    benign race, not missing evidence); unreadable or malformed content raises
    so the caller can record ``unknown`` instead of a false zero.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    close = raw.rfind(")")
    if close < 0:
        raise ValueError(f"unparsable /proc/{pid}/stat")
    comm = raw[raw.find("(") + 1 : close]
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        raise ValueError(f"truncated /proc/{pid}/stat")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ValueError(f"unparsable start ticks in /proc/{pid}/stat") from exc
    return _Member(
        pid=pid,
        start_ticks=start_ticks,
        comm=comm[:64],
        is_leader=pid == leader_pid,
    )


def _process_state(pid: int, proc_root: Path) -> str | None:
    """Return the process state character, or ``None`` when it is gone."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return "?"
    close = raw.rfind(")")
    if close < 0:
        return "?"
    fields = raw[close + 1 :].split()
    return fields[0] if fields else "?"


def _socket_inodes(pid: int, proc_root: Path) -> set[int] | None:
    """Return the socket inodes a live process holds, or ``None`` if unreadable.

    ``/proc/<pid>/fd`` is denied for a process that has exited but is not yet
    reaped (a benign race with the snapshot), so a denial for a dead process is
    an empty set.  A denial for a still-live process is genuinely unreadable
    evidence and returns ``None``.
    """
    fd_dir = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, ProcessLookupError):
        return set()
    except OSError:
        state = _process_state(pid, proc_root)
        if state is None or state in _ZOMBIE_STATES:
            return set()
        return None
    inodes: set[int] = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                inodes.add(int(target[len("socket:[") : -1]))
            except ValueError:
                continue
    return inodes


class JobProcessRecorder:
    """Sample and persist what one attempt's process group started.

    Wire it into a dispatch: attach the fenced identity from the process-start
    callback, pass :meth:`sample` as the process wait-loop sample hook, and call
    :meth:`finish` once the worker returns.  Every method is non-fatal: a
    recording failure never aborts the worker, it only makes the verdict
    ``unknown``.
    """

    def __init__(
        self,
        db: Database,
        *,
        run_id: str,
        attempt_id: str,
        sample_interval: float | None = None,
        proc_root: Path = Path("/proc"),
        net_root: Path = Path("/proc/net"),
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._sample_interval = (
            _sample_interval_from_env()
            if sample_interval is None
            else max(0.0, sample_interval)
        )
        self._proc_root = proc_root
        self._net_root = net_root
        self._identity: ProcessIdentity | None = None
        self._pgid: int | None = None
        self._last_sample = 0.0
        #: Current-state evidence gaps.  They are recomputed every sample so a
        #: transient read race that later succeeds does not poison the verdict.
        self._socket_table_unreadable = False
        self._group_snapshot_incomplete = False
        #: Group pids whose socket fds were never read successfully.  Kept even
        #: after the pid exits: a port we never attributed cannot be ruled out.
        self._fd_unreadable: set[int] = set()
        #: Pids whose fds were read successfully at least once.  A failure while
        #: the process is tearing down is benign when an earlier read succeeded.
        self._fd_read_ok: set[int] = set()
        #: Pids whose stat could not be read (process-row detail only).
        self._stat_unreadable: set[int] = set()
        self._last_gaps: set[str] = set()
        self._persist_failed = False
        self._excluded_services: set[str] = set()
        #: Listening sockets ever attributed to this attempt, by socket inode.
        self._ports: dict[int, ListeningSocket] = {}

    # -- lifecycle -----------------------------------------------------

    def attach_identity(self, identity: ProcessIdentity) -> None:
        """Attach the fenced worker identity and take the first sample."""
        if identity.process_group_id is not None and identity.process_group_id > 0:
            pgid = identity.process_group_id
        else:
            pgid = identity.pid
        self._identity = identity
        self._pgid = pgid
        self.sample(force=True)

    def sample(self, *, force: bool = False) -> None:
        """Take one throttled sample of the group's processes and ports."""
        if self._pgid is None:
            return
        now = time.monotonic()
        if not force and now - self._last_sample < self._sample_interval:
            return
        self._last_sample = now
        try:
            self._sample_once()
        except Exception:
            # Observation is advisory evidence; never abort the worker.
            self._last_gaps.add("sample_failed")

    def finish(self) -> str:
        """Take the final sample, persist the leftovers verdict and return it."""
        try:
            self.sample(force=True)
            verdict, detail = self._verdict()
        except Exception:
            # The verdict is evidence only; it must never mask a worker result.
            verdict, detail = "unknown", {"reason": "verdict_failed"}
        try:
            self._db.record_job_leftovers(
                run_id=self._run_id,
                attempt_id=self._attempt_id,
                verdict=verdict,
                detail=detail,
            )
        except Exception:
            self._persist_failed = True
        return verdict

    # -- sampling ------------------------------------------------------

    def _sample_once(self) -> None:
        assert self._pgid is not None
        gaps: set[str] = set()
        identity = self._identity
        if identity is None:
            return
        listening = read_listening_sockets(self._net_root)
        self._socket_table_unreadable = not listening.complete
        if not listening.complete:
            gaps.add("socket_table_unreadable")
        by_inode = {entry.inode: entry for entry in listening.sockets}
        live_pids, group_complete = self._live_group()
        self._group_snapshot_incomplete = not group_complete
        if not group_complete:
            gaps.add("group_snapshot_incomplete")
        seen_at = current_iso_timestamp()
        for pid in live_pids:
            try:
                member = _read_member(pid, self._pgid, self._proc_root)
            except (OSError, ValueError):
                self._stat_unreadable.add(pid)
                gaps.add("member_stat_unreadable")
                member = None
            else:
                if member is None:
                    # Vanished between the snapshot and the read: benign.
                    continue
                self._stat_unreadable.discard(pid)
                self._write_process(member, identity, seen_at)
            inodes = _socket_inodes(pid, self._proc_root)
            if inodes is None:
                self._fd_unreadable.add(pid)
                gaps.add("process_fds_unreadable")
                continue
            self._fd_unreadable.discard(pid)
            for inode in inodes:
                observed = by_inode.get(inode)
                if observed is None:
                    continue
                service = ENGINE_SERVICE_PORTS.get(observed.port)
                if service is not None:
                    self._excluded_services.add(service)
                    continue
                self._write_port(observed, identity, seen_at)
        self._last_gaps = gaps

    def _live_group(self) -> tuple[list[int], bool]:
        """Return live group pids and whether the snapshot evidence was complete."""
        from saberops.monitor import snapshot_process_group

        assert self._pgid is not None
        snapshot = snapshot_process_group(self._pgid)
        pids = [
            entry.pid
            for entry in snapshot.members
            if entry.state not in _ZOMBIE_STATES
        ]
        return pids, not snapshot.evidence_incomplete

    def _write_process(
        self, member: _Member, identity: ProcessIdentity, seen_at: str
    ) -> None:
        try:
            self._db.upsert_job_process(
                run_id=self._run_id,
                attempt_id=self._attempt_id,
                host=identity.host,
                boot_id=identity.boot_id,
                pgid=identity.process_group_id or identity.pid,
                pid=member.pid,
                start_ticks=member.start_ticks,
                comm=member.comm,
                is_leader=member.is_leader,
                seen_at=seen_at,
            )
        except Exception:
            self._persist_failed = True

    def _write_port(
        self, observed: ListeningSocket, identity: ProcessIdentity, seen_at: str
    ) -> None:
        self._ports[observed.inode] = observed
        try:
            self._db.upsert_job_port(
                run_id=self._run_id,
                attempt_id=self._attempt_id,
                host=identity.host,
                boot_id=identity.boot_id,
                pgid=identity.process_group_id or identity.pid,
                protocol=observed.protocol,
                port=observed.port,
                inode=observed.inode,
                address=observed.address,
                seen_at=seen_at,
            )
        except Exception:
            self._persist_failed = True

    # -- verdict -------------------------------------------------------

    def _verdict(self) -> tuple[str, dict[str, Any]]:
        detail: dict[str, Any] = {
            "pgid": self._pgid,
            "excluded_engine_services": sorted(self._excluded_services),
            # Honest limits: never reported as zero.
            "limits": [
                "out_of_group_processes_unknown",
                "shared_service_sockets_unknown",
            ],
        }
        if self._pgid is None:
            detail["reason"] = "no_engine_owned_process_group"
            return "unknown", detail
        if self._persist_failed:
            detail["reason"] = "process_record_write_failed"
            return "unknown", detail
        live_pids, group_complete = self._live_group()
        listening = read_listening_sockets(self._net_root)
        gaps = set(self._last_gaps)
        if not listening.complete:
            gaps.add("socket_table_unreadable")
        if not group_complete:
            gaps.add("group_snapshot_incomplete")
        # A member whose fds were never read could own an unattributed
        # listening socket, so the leftover answer is unknown, never "none".
        blocking_fds = self._fd_unreadable - self._fd_read_ok
        if blocking_fds:
            gaps.add("process_fds_unreadable")
            detail["unreadable_fds_pids"] = sorted(blocking_fds)
        if (
            not listening.complete
            or not group_complete
            or blocking_fds
        ):
            detail["reason"] = "proc_evidence_incomplete"
            detail["evidence_gaps"] = sorted(gaps)
            return "unknown", detail
        active_inodes = {entry.inode for entry in listening.sockets}
        leftover_ports = [
            {
                "protocol": entry.protocol,
                "port": entry.port,
                "address": entry.address,
                "inode": entry.inode,
            }
            for inode, entry in sorted(self._ports.items())
            if inode in active_inodes
        ]
        leftover_processes: list[dict[str, Any]] = []
        for pid in live_pids:
            try:
                member = _read_member(pid, self._pgid, self._proc_root)
            except (OSError, ValueError):
                member = None
            leftover_processes.append(
                {
                    "pid": pid,
                    "start_ticks": member.start_ticks if member is not None else None,
                    "comm": member.comm if member is not None else None,
                }
            )
        if self._stat_unreadable:
            detail["processes_without_stat"] = sorted(self._stat_unreadable)
        detail["live_processes"] = leftover_processes
        detail["listening_ports"] = leftover_ports
        if leftover_processes or leftover_ports:
            return "leftovers", detail
        return "none", detail


def record_native_worker_not_observable(
    db: Database, *, run_id: str, attempt_id: str
) -> None:
    """Record that a host-run worker has no engine-owned process group.

    Native workers call the engine's MCP tools from the orchestrating CLI's own
    subagent; the engine never launches or observes that process
    (``native_workers.py``).  The honest record is ``not_observable``, never a
    clean result.
    """
    db.record_job_leftovers(
        run_id=run_id,
        attempt_id=attempt_id,
        verdict="not_observable",
        detail={
            "reason": "host_run_worker_has_no_engine_owned_process_group",
            "limits": [
                "native_worker_processes_not_observable",
                "native_worker_ports_not_observable",
            ],
        },
    )
