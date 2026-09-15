"""Cross-process completion wake for one run's durable event log.

Orchestration progress is already durable in SQLite.  The only thing missing
for a *detached* execution owner is a way to tell interested processes -- the
UI event stream, ``orch wait``, a following CLI -- that a new event exists,
without anyone polling the database.

The mechanism is deliberately the smallest Unix primitive that works across
independent processes: one FIFO per subscriber inside the project-local state
directory.  A subscriber creates its FIFO and holds it open read/write (so it
never sees a spurious EOF); the writer opens every FIFO non-blockingly and
writes a single byte.  A FIFO whose subscriber is gone reports ``ENXIO`` and is
unlinked on the spot, so nothing accumulates.

This is not a message bus: the byte carries no content.  It only says "look at
the durable log again", which keeps the database the single source of truth.
"""

from __future__ import annotations

import errno
import os
import select
import uuid
from pathlib import Path
from types import TracebackType

__all__ = ["RunWakeSubscription", "notify_run", "subscribe_run", "wake_dir_for"]


def _safe(component: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in component)


def wake_dir_for(db_path: Path | str, run_id: str) -> Path:
    """Return the FIFO directory for a run, alongside its own project state."""
    return Path(db_path).resolve().parent / "wake" / _safe(run_id)


def notify_run(db_path: Path | str, run_id: str) -> int:
    """Wake every subscriber of ``run_id``; return how many were signalled.

    Never raises: a failure to wake degrades to the subscriber's coarse
    fallback timeout, and must never affect orchestration.
    """
    directory = wake_dir_for(db_path, run_id)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    signalled = 0
    for entry in entries:
        if entry.suffix != ".fifo":
            continue
        fd = -1
        try:
            fd = os.open(str(entry), os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"\x01")
            signalled += 1
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                # No subscriber is holding the read end any more.
                try:
                    entry.unlink()
                except OSError:
                    pass
            # EAGAIN means the pipe is already full, i.e. already woken.
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return signalled


class RunWakeSubscription:
    """A single process's wake channel for one run."""

    def __init__(self, db_path: Path | str, run_id: str) -> None:
        self.run_id = run_id
        self.path = wake_dir_for(db_path, run_id) / f"{uuid.uuid4().hex}.fifo"
        self._fd: int | None = None

    def open(self) -> RunWakeSubscription:
        """Create the FIFO and hold both ends so it never reports EOF."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkfifo(str(self.path), 0o600)
        except FileExistsError:
            pass
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_NONBLOCK)
        return self

    def wait(self, timeout: float) -> bool:
        """Block until woken or ``timeout`` elapses; True if woken."""
        fd = self._fd
        if fd is None:
            return False
        try:
            readable, _, _ = select.select([fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return False
        if not readable:
            return False
        self._drain(fd)
        return True

    @staticmethod
    def _drain(fd: int) -> None:
        while True:
            try:
                if not os.read(fd, 4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def close(self) -> None:
        """Release the FIFO; the writer unlinks any leftover on next notify."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> RunWakeSubscription:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def subscribe_run(db_path: Path | str, run_id: str) -> RunWakeSubscription:
    """Return an unopened subscription; use it as a context manager."""
    return RunWakeSubscription(db_path, run_id)
