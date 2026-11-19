"""Fenced, file-backed single-writer planning lease.

A lease is protected by an exclusive flock during acquisition, handoff, and
entire mutating tool calls. A session identity and monotonically increasing
epoch fence displaced owners. Idle holders can be replaced after a bounded
period only when their recorded jobs have no nonterminal work; dead holders
and malformed records self-heal subject to the same active-work protection.
Legacy live records cannot be fenced and must remain protected.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.mcp.errors import McpError

__all__ = [
    "LEASE_ENV",
    "LEASE_FILENAME",
    "LEASE_SCHEMA_VERSION",
    "LeaseRecord",
    "OrchestratorLease",
    "default_lease_path",
    "pid_is_alive",
    "planner_identity",
]

#: Schema version of the durable lease record.
LEASE_SCHEMA_VERSION = 1

#: Test/diagnostics override for the lease file path.
LEASE_ENV = "SABEROPS_MCP_LEASE"

#: File name inside the owner state directory.
LEASE_FILENAME = "mcp-orchestrator.lock"


def planner_identity() -> dict[str, Any]:
    """Return the live planning-session identity for run records (display only).

    The plan role is the external CLI session the owner sits in; the durable
    lease is its one recorded identity.  A store with no live planner reports
    ``UNKNOWN`` rather than guessing, so a run record never invents who
    planned it.
    """
    try:
        record = OrchestratorLease().read()
    except Exception:
        record = None
    if record is None or not pid_is_alive(record.pid):
        return {"identity": "UNKNOWN", "source": "no live planning lease"}
    identity = record.client or record.holder
    return {
        "identity": identity,
        "holder": record.holder,
        "session_id": record.session_id or "UNKNOWN",
        "client": record.client or "UNKNOWN",
        "source": "planning lease",
    }


def default_lease_path() -> Path:
    """Resolve the lease file path (``SABEROPS_MCP_LEASE`` overrides)."""
    override = os.environ.get(LEASE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.paths import saberops_state_dir

    return saberops_state_dir() / LEASE_FILENAME


def pid_is_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` currently exists.

    ``os.kill(pid, 0)`` performs the existence check without delivering a
    signal.  ``ProcessLookupError`` means dead; ``PermissionError`` means the
    process exists but belongs to another user (still alive).  Any other
    ``OSError`` fails *safe*: report dead so a stale lease is healed rather
    than blocking the writer role forever.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class LeaseRecord:
    """The durable single-writer lease record."""

    holder: str
    pid: int
    acquired_at: str
    host: str = ""
    #: The MCP client's self-reported name (``clientInfo.name``), display only.
    client: str = ""
    session_id: str = ""
    epoch: int = 0
    last_activity: float = 0.0
    db_path: str = ""
    #: F17: liveness heartbeat written by the connected stdio server.  It is
    #: deliberately separate from ``last_activity`` (which drives idle
    #: takeover) so an open-but-idle session keeps its idle handoff behaviour
    #: while a crashed or disconnected one is still detected.
    heartbeat_at: float = 0.0
    schema_version: int = LEASE_SCHEMA_VERSION

    def to_mapping(self) -> dict[str, Any]:
        mapping: dict[str, Any] = {
            "schema_version": self.schema_version,
            "holder": self.holder,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "host": self.host,
            "session_id": self.session_id,
            "epoch": self.epoch,
            "last_activity": self.last_activity,
            "db_path": self.db_path,
            "heartbeat_at": self.heartbeat_at,
        }
        if self.client:
            mapping["client"] = self.client
        return mapping

    @classmethod
    def from_mapping(cls, payload: object) -> LeaseRecord | None:
        """Rebuild a lease record, returning ``None`` for a malformed document.

        ``None`` is the self-healing signal: a corrupt record is never allowed
        to block the writer role permanently.  A record for a *different*
        schema version is also unreadable by this client and is treated the
        same way.
        """
        if not isinstance(payload, dict):
            return None
        version = payload.get("schema_version")
        if version != LEASE_SCHEMA_VERSION:
            return None
        holder = payload.get("holder")
        pid = payload.get("pid")
        acquired_at = payload.get("acquired_at")
        if not isinstance(holder, str) or not holder.strip():
            return None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(acquired_at, str) or not acquired_at.strip():
            return None
        host = payload.get("host")
        client = payload.get("client")
        return cls(
            holder=holder,
            pid=pid,
            acquired_at=acquired_at,
            host=host if isinstance(host, str) else "",
            client=client.strip()[:80] if isinstance(client, str) else "",
            session_id=str(payload.get("session_id") or ""),
            epoch=int(payload.get("epoch") or 0),
            last_activity=float(payload.get("last_activity") or 0),
            db_path=str(payload.get("db_path") or ""),
            heartbeat_at=float(payload.get("heartbeat_at") or 0),
        )


class OrchestratorLease:
    """A file-backed single-writer lease for the Orchestrator.

    ``acquire`` is the only mutation.  The instance remembers whether *this*
    process is the writer; :meth:`status` renders the record for the
    ``saberops://lease`` resource.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        holder: str | None = None,
        pid: int | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else default_lease_path()
        self._pid = os.getpid() if pid is None else int(pid)
        self._holder = holder or f"orch-pid-{self._pid}"
        self._writer = False
        self._stale_cleared = False
        self._session_id = uuid.uuid4().hex
        self._epoch = 0
        self._db_path = ""

    @property
    def path(self) -> Path:
        """The lease file this instance reads and writes."""
        return self._path

    @property
    def pid(self) -> int:
        """The process id this instance advertises."""
        return self._pid

    @property
    def holder(self) -> str:
        """The human-readable holder label this instance advertises."""
        return self._holder

    @property
    def writer(self) -> bool:
        """True once this instance acquired (or held) the writer lease."""
        record = self.read() if self._writer else None
        return bool(record and record.pid == self._pid and
                    (not record.session_id or record.session_id == self._session_id) and
                    record.epoch == self._epoch)

    @property
    def session_id(self) -> str:
        return self._session_id

    def configure_database(self, path: Path) -> None:
        self._db_path = str(path.expanduser().resolve())

    @staticmethod
    def active_work(record: LeaseRecord) -> int | None:
        """Jobs this session still has *executing* (not parked, not terminal).

        F17: a parked job (``PAUSED`` or quota ``PREEMPTED``) is durable work
        waiting for a decision, not work in flight.  Counting it here would
        keep the dead session's lease unreclaimable forever, so the returning
        planning session could never take over and confirm the resume.
        """
        if not record.session_id or not record.db_path:
            return 0
        try:
            path = Path(record.db_path)
            if not path.exists():
                return 0
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "mcp_job_owners" not in tables:
                    return 0
                row = conn.execute(
                    "SELECT count(*) FROM mcp_job_owners o JOIN jobs j ON j.job_id=o.job_id "
                    "WHERE o.session_id=? AND j.state NOT IN ('SUCCEEDED','FAILED','CANCELLED') "
                    "AND (j.state = 'RUNNING' OR j.waiting_reason IS NULL "
                    "     OR j.waiting_reason NOT IN ('PAUSED','PREEMPTED'))",
                    (record.session_id,),
                ).fetchone()
                return int(row[0])
        except (OSError, sqlite3.Error):
            return None

    @staticmethod
    def idle_seconds(record: LeaseRecord) -> float:
        return max(0.0, time.time() - record.last_activity) if record.last_activity else 0.0

    @staticmethod
    def idle_limit() -> float:
        try:
            value = float(os.environ.get("SABEROPS_MCP_IDLE_SECONDS", "900"))
            return value if value > 0 else 900.0
        except ValueError:
            return 900.0

    def read(self) -> LeaseRecord | None:
        """Read the current record; ``None`` when absent or malformed."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.strip():
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        try:
            return LeaseRecord.from_mapping(payload)
        except (TypeError, ValueError, OverflowError):
            return None

    def is_stale(self, record: LeaseRecord | None) -> bool:
        """True when ``record`` is absent or belongs to a dead process."""
        if record is None:
            return True
        if record.pid == self._pid and record.session_id == self._session_id:
            return False
        if not pid_is_alive(record.pid):
            return self.active_work(record) == 0
        return bool(
            record.session_id
            and record.last_activity
            and self.idle_seconds(record) >= self.idle_limit()
            and self.active_work(record) == 0
        )

    def acquire(self) -> bool:
        """Try to become the single writer.

        Returns True when this instance holds the writer lease, False when a
        live *other* process holds it (this instance is then read-only).
        Re-acquiring in the same process is idempotent.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    raw = handle.read()
                    existing = self._parse(raw)
                    if not self.is_stale(existing):
                        if (
                            existing is not None
                            and existing.pid == self._pid
                            and (not existing.session_id or existing.session_id == self._session_id)
                        ):
                            self._writer = True
                            self._epoch = existing.epoch
                            self._session_id = existing.session_id or self._session_id
                            return True
                        self._writer = False
                        return False
                    self._stale_cleared = existing is not None
                    counter = self._path.with_name(self._path.name + ".epoch")
                    try:
                        epochs = [
                            int(line) for line in counter.read_text(encoding="utf-8").splitlines()
                        ]
                    except FileNotFoundError:
                        epochs = []
                    epoch = max([existing.epoch if existing else 0, *epochs]) + 1
                    with counter.open("a", encoding="utf-8") as epoch_file:
                        epoch_file.write(f"{epoch}\n")
                        epoch_file.flush()
                        os.fsync(epoch_file.fileno())
                    self._epoch = epoch
                    record = LeaseRecord(
                        session_id=self._session_id,
                        epoch=epoch,
                        last_activity=time.time(),
                        db_path=self._db_path,
                        holder=self._holder,
                        pid=self._pid,
                        acquired_at=_now_iso(),
                        host=socket.gethostname(),
                        heartbeat_at=time.time(),
                    )
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json.dumps(record.to_mapping(), sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    self._writer = True
                    return True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            raise McpError(f"cannot acquire orchestrator lease {self._path}: {exc}") from exc

    def record_client(self, client: str) -> bool:
        """Label the held lease with the connected client's name (display only).

        Only the current writer relabels its own record; a read-only client,
        a missing record or a record owned by another process is left alone.
        The label never grants anything: it lets the owner's local pages say
        which agent plans the work.
        """
        label = " ".join(str(client).split())[:80]
        if not self._writer or not label:
            return False
        try:
            with self._path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    existing = self._parse(handle.read())
                    if existing is None or existing.pid != self._pid:
                        return False
                    record = replace(existing, client=label)
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json.dumps(record.to_mapping(), sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    return True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            return False

    def touch(self) -> bool:
        """Refresh this session's liveness heartbeat (F17 presence detection).

        Only the current writer touches its own record.  A read-only client, a
        missing record or a record owned by another process is left alone, so a
        heartbeat can never steal planning ownership.
        """
        if not self._writer:
            return False
        try:
            with self._path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    existing = self._parse(handle.read())
                    if (
                        existing is None
                        or existing.pid != self._pid
                        or (existing.session_id and existing.session_id != self._session_id)
                    ):
                        return False
                    record = replace(existing, heartbeat_at=time.time())
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json.dumps(record.to_mapping(), sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    return True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            return False

    def release(self) -> bool:
        """Release the lease when this instance holds it.

        Returns True when the lease was released (or already free); False when
        a different live process holds it (nothing is touched).
        """
        try:
            if not self._path.exists():
                self._writer = False
                return True
            with self._path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    existing = self._parse(handle.read())
                    if existing is not None and (
                        existing.pid != self._pid
                        or (existing.session_id and existing.session_id != self._session_id)
                    ):
                        self._writer = False
                        return False
                    if existing is not None and self.active_work(existing) != 0:
                        return False
                    handle.seek(0)
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                    self._writer = False
                    return True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            raise McpError(f"cannot release orchestrator lease {self._path}: {exc}") from exc

    def release_target(self, target: str) -> tuple[bool, str]:
        """Release exactly the named session or PID, unless work remains."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                record = self._parse(handle.read())
                if record is None:
                    return False, "No planning owner is recorded."
                if target not in (record.session_id, str(record.pid)):
                    return False, "The target does not match the current planning owner."
                if not record.session_id and pid_is_alive(record.pid):
                    return False, "Live legacy owner cannot be fenced; close it before release."
                active = self.active_work(record)
                if active is None:
                    return False, "Cannot verify active work; release refused."
                if active:
                    return False, f"Owner has {active} active job(s); release refused."
                handle.seek(0)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                return True, "Planning ownership released."
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def guarded_call(self, *, mutating: bool) -> Iterator[bool]:
        """Fence a call for its full duration against concurrent handoff."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                record = self._parse(handle.read())
                owned = bool(
                    self._writer
                    and record
                    and record.pid == self._pid
                    and (not record.session_id or record.session_id == self._session_id)
                    and record.epoch == self._epoch
                )
                if owned and record:
                    record = replace(record, last_activity=time.time())
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json.dumps(record.to_mapping(), sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                elif mutating:
                    self._writer = False
                try:
                    yield owned
                finally:
                    if owned and record:
                        record = replace(record, last_activity=time.time())
                        handle.seek(0)
                        handle.truncate()
                        handle.write(json.dumps(record.to_mapping(), sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self) -> dict[str, Any]:
        """Render a redaction-safe status view for the ``saberops://lease`` resource."""
        record = self.read()
        return {
            "writer": self.writer,
            "holder_client": None if record is None else record.client,
            "holder_live": bool(record and pid_is_alive(record.pid)),
            "session_id": None if record is None else record.session_id,
            "epoch": None if record is None else record.epoch,
            "last_activity": None if record is None else record.last_activity,
            "idle_seconds": None if record is None else self.idle_seconds(record),
            "active_work": None if record is None else self.active_work(record),
            "takeover_eligible": bool(record and self.is_stale(record)),
            "path": str(self._path),
            "self_pid": self._pid,
            "holder": None if record is None else record.holder,
            "holder_pid": None if record is None else record.pid,
            "acquired_at": None if record is None else record.acquired_at,
            "stale_cleared_on_acquire": self._stale_cleared,
            "held_by_live_other": (
                record is not None and record.pid != self._pid and pid_is_alive(record.pid)
            ),
        }

    @staticmethod
    def _parse(raw: str) -> LeaseRecord | None:
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        try:
            return LeaseRecord.from_mapping(payload)
        except (TypeError, ValueError, OverflowError):
            return None
