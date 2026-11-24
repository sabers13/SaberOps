"""Attended terminal sessions: presence without an AI CLI planning session.

Presence is mandatory for every store: with no AI CLI planning session
connected, SaberOps starts nothing.  A plain ``orch`` command
in a terminal is not presence by itself.  Presence is granted only while an
*attended terminal session* stays open:

* ``orch watch`` (the read-only tmux view) while a terminal is attached;
* ``orch run --attended`` while that command runs in the foreground.

Each session writes one small record under the owner state directory and
refreshes its ``heartbeat_at`` every few seconds.  :func:`live_attendance`
answers whether at least one record is still alive: the pid must exist and the
heartbeat must be younger than the shared planning-heartbeat timeout.  A closed
terminal, Ctrl-C, a killed CLI or a crashed process therefore ends presence
within one bounded heartbeat, and the work gate pauses exactly as it does when
a planning CLI closes.

Nothing here starts, stops or steers a worker.  The records are advisory
evidence for the gate; :mod:`saberops.runtime_gate` is still the only place
that decides whether work may start.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

from saberops.mcp.lease import pid_is_alive
from saberops.paths import get_attendance_dir
from saberops.runtime_gate import heartbeat_timeout

__all__ = [
    "ATTENDANCE_DIR_ENV",
    "ATTENDANCE_HEARTBEAT_SECONDS_ENV",
    "AttendanceRecord",
    "AttendedSession",
    "attendance_dir",
    "attend",
    "live_attendance",
    "record_attendance",
    "start_attendance",
]

#: Test/diagnostics override for the attendance-record directory.
ATTENDANCE_DIR_ENV: Final[str] = "SABEROPS_ATTENDANCE_DIR"

#: Heartbeat cadence of an attended terminal session.
ATTENDANCE_HEARTBEAT_SECONDS_ENV: Final[str] = "SABEROPS_ATTENDANCE_HEARTBEAT_SECONDS"

DEFAULT_ATTENDANCE_HEARTBEAT_SECONDS: Final[float] = 5.0


def attendance_dir() -> Path:
    """The directory holding one record per attended terminal session."""
    override = os.environ.get(ATTENDANCE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return get_attendance_dir()


def _heartbeat_seconds() -> float:
    try:
        value = float(
            os.environ.get(ATTENDANCE_HEARTBEAT_SECONDS_ENV, "").strip() or "0"
        )
    except ValueError:
        return DEFAULT_ATTENDANCE_HEARTBEAT_SECONDS
    return value if value > 0 else DEFAULT_ATTENDANCE_HEARTBEAT_SECONDS


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AttendanceRecord:
    """One attended terminal session, as read from its durable record."""

    session_id: str
    kind: str
    pid: int
    started_at: str
    heartbeat_at: float
    label: str = ""
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.schema_version,
            "session_id": self.session_id,
            "kind": self.kind,
            "pid": self.pid,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "label": self.label,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> AttendanceRecord | None:
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("session_id")
        kind = payload.get("kind")
        pid = payload.get("pid")
        started_at = payload.get("started_at")
        heartbeat_at = payload.get("heartbeat_at")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(kind, str) or not kind:
            return None
        if type(pid) is not int or pid <= 0:
            return None
        if not isinstance(started_at, str) or not started_at:
            return None
        if isinstance(heartbeat_at, bool) or not isinstance(heartbeat_at, (int, float)):
            return None
        label = payload.get("label")
        return cls(
            session_id=session_id,
            kind=kind,
            pid=pid,
            started_at=started_at,
            heartbeat_at=float(heartbeat_at),
            label=label if isinstance(label, str) else "",
        )


def record_path(session_id: str, directory: Path | None = None) -> Path:
    """The durable record path for one attended session."""
    safe = "".join(
        ch for ch in session_id if ch.isalnum() or ch in ("-", "_", ".")
    ).strip(".")
    if not safe:
        safe = uuid.uuid4().hex
    return (directory or attendance_dir()) / f"{safe}.json"


# Kept as the internal spelling used throughout this module.
_record_path = record_path


def _atomic_write(target: Path, record: AttendanceRecord) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".attendance-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def record_attendance(
    kind: str,
    *,
    label: str = "",
    session_id: str | None = None,
    now: datetime | None = None,
    directory: Path | None = None,
) -> AttendanceRecord:
    """Write one attendance record without starting a heartbeat thread.

    This is the single beat used by tests and by a one-shot attended command.
    Long-lived sessions use :func:`start_attendance`, which refreshes it.
    """
    moment = now or _now()
    record = AttendanceRecord(
        session_id=session_id or f"{kind}-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        kind=kind,
        pid=os.getpid(),
        started_at=moment.isoformat(),
        heartbeat_at=moment.timestamp(),
        label=label,
    )
    _atomic_write(_record_path(record.session_id, directory), record)
    return record


def records(*, directory: Path | None = None) -> list[AttendanceRecord]:
    """Read every parseable attendance record; malformed files are ignored."""
    folder = directory or attendance_dir()
    if not folder.is_dir():
        return []
    found: list[AttendanceRecord] = []
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record = AttendanceRecord.from_mapping(payload)
        if record is not None:
            found.append(record)
    return found


def _is_live(record: AttendanceRecord, *, moment: datetime, timeout: float) -> bool:
    if not pid_is_alive(record.pid):
        return False
    age = max(0.0, moment.timestamp() - record.heartbeat_at)
    return age <= timeout


def live_attendance(
    *,
    now: datetime | None = None,
    directory: Path | None = None,
) -> AttendanceRecord | None:
    """The live attended terminal session, if one is open right now."""
    moment = now or _now()
    timeout = heartbeat_timeout()
    candidates = [
        record
        for record in records(directory=directory)
        if _is_live(record, moment=moment, timeout=timeout)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: record.heartbeat_at)


def _prune(*, moment: datetime, directory: Path | None = None) -> None:
    """Remove records that are provably dead and stale (best effort)."""
    folder = directory or attendance_dir()
    timeout = heartbeat_timeout()
    for record in records(directory=folder):
        if _is_live(record, moment=moment, timeout=timeout):
            continue
        if pid_is_alive(record.pid):
            # A live pid with a stale heartbeat may still be starting up; keep it.
            continue
        try:
            _record_path(record.session_id, folder).unlink(missing_ok=True)
        except OSError:
            pass


class AttendedSession:
    """One attended terminal session that refreshes its heartbeat until closed."""

    def __init__(
        self,
        record: AttendanceRecord,
        *,
        interval: float | None = None,
        directory: Path | None = None,
    ) -> None:
        self.record = record
        self._directory = directory
        self._every = interval if interval is not None else _heartbeat_seconds()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self.record.session_id

    def heartbeat(self) -> None:
        """Refresh the record's heartbeat once (never raises)."""
        with self._lock:
            try:
                self.record = AttendanceRecord(
                    session_id=self.record.session_id,
                    kind=self.record.kind,
                    pid=self.record.pid,
                    started_at=self.record.started_at,
                    heartbeat_at=_now().timestamp(),
                    label=self.record.label,
                )
                _atomic_write(
                    _record_path(self.record.session_id, self._directory),
                    self.record,
                )
            except OSError:
                # A heartbeat write failure must never break the attended command;
                # presence simply goes stale within the bounded timeout.
                pass

    def start(self) -> AttendedSession:
        self.heartbeat()
        every = max(0.1, self._every)

        def beat() -> None:
            while not self._stop.wait(every):
                self.heartbeat()

        self._thread = threading.Thread(
            target=beat, name="saberops-attendance-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._every))
        self._thread = None
        try:
            _record_path(self.record.session_id, self._directory).unlink(
                missing_ok=True
            )
        except OSError:
            pass

    def __enter__(self) -> AttendedSession:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def start_attendance(
    kind: str,
    *,
    label: str = "",
    session_id: str | None = None,
    interval: float | None = None,
    now: datetime | None = None,
    directory: Path | None = None,
) -> AttendedSession:
    """Start a heartbeating attended terminal session."""
    _prune(moment=now or _now(), directory=directory)
    record = record_attendance(
        kind,
        label=label,
        session_id=session_id,
        now=now,
        directory=directory,
    )
    return AttendedSession(record, interval=interval, directory=directory).start()


@contextmanager
def attend(
    kind: str,
    *,
    label: str = "",
    session_id: str | None = None,
    interval: float | None = None,
    directory: Path | None = None,
) -> Iterator[AttendedSession]:
    """Context manager form of :func:`start_attendance`."""
    session = start_attendance(
        kind,
        label=label,
        session_id=session_id,
        interval=interval,
        directory=directory,
    )
    try:
        yield session
    finally:
        session.stop()


def heartbeat_age(record: AttendanceRecord, *, now: datetime | None = None) -> float:
    """Seconds since the record's last heartbeat (0 when in the future)."""
    moment = now or _now()
    return max(0.0, moment.timestamp() - record.heartbeat_at)
