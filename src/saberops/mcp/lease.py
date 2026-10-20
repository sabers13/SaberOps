"""Single-writer Orchestrator lease with read-only second clients (M3).

The finished product has exactly one Orchestrator **writer** at a time
(REHEARSAL §2: ``state/orch.lock``; ADR-0004 D11 rotation).  Over stdio every
client is its own process, so the lease is a small JSON record guarded by an
exclusive :func:`fcntl.flock` during each read-modify-write:

* the first live client becomes the **writer**;
* a second live client is recognised as **read-only** and every mutating tool
  call is refused with :class:`LeaseReadOnlyError`; its read tools keep
  working;
* a record whose ``pid`` is dead is **stale** and is self-healed: the next
  client steals it (``os.kill(pid, 0)`` is the liveness probe), so a crashed
  client can never lock out future writers;
* a non-empty but unreadable/malformed record is treated as stale for the same
  reason -- a corrupt lock file must not permanently disable the writer role.

Dead-PID detection cannot distinguish a recycled PID owned by an unrelated
process; liveness is deliberately optimistic (a live PID is respected) because
the failure mode of a wrongly-respected PID is a read-only session, which is
safe, while the failure mode of stealing a live lease is two writers.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from dataclasses import dataclass
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
]

#: Schema version of the durable lease record.
LEASE_SCHEMA_VERSION = 1

#: Test/diagnostics override for the lease file path.
LEASE_ENV = "SABEROPS_MCP_LEASE"

#: File name inside the owner state directory.
LEASE_FILENAME = "mcp-orchestrator.lock"


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
    schema_version: int = LEASE_SCHEMA_VERSION

    def to_mapping(self) -> dict[str, Any]:
        mapping: dict[str, Any] = {
            "schema_version": self.schema_version,
            "holder": self.holder,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "host": self.host,
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
        return self._writer

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
        return LeaseRecord.from_mapping(payload)

    def is_stale(self, record: LeaseRecord | None) -> bool:
        """True when ``record`` is absent or belongs to a dead process."""
        if record is None:
            return True
        if record.pid == self._pid:
            return False
        return not pid_is_alive(record.pid)

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
                        if existing is not None and existing.pid == self._pid:
                            self._writer = True
                            return True
                        self._writer = False
                        return False
                    self._stale_cleared = existing is not None
                    record = LeaseRecord(
                        holder=self._holder,
                        pid=self._pid,
                        acquired_at=_now_iso(),
                        host=socket.gethostname(),
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
                    record = LeaseRecord(
                        holder=existing.holder,
                        pid=existing.pid,
                        acquired_at=existing.acquired_at,
                        host=existing.host,
                        client=label,
                    )
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
                    if existing is not None and existing.pid != self._pid:
                        self._writer = False
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

    def status(self) -> dict[str, Any]:
        """Render a redaction-safe status view for the ``saberops://lease`` resource."""
        record = self.read()
        return {
            "writer": self._writer,
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
        return LeaseRecord.from_mapping(payload)
