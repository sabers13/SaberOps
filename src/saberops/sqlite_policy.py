"""Shared bounded SQLite connection/transaction policy (ADR-0003 D7, GS-1).

D7 requires the engine's SQLite writes to be robust to contention:

* WAL journaling;
* ``busy_timeout`` of 30 s;
* ``BEGIN IMMEDIATE`` with bounded ``SQLITE_BUSY`` retry/backoff whose **total**
  wall time is at most 30 s -- one shared deadline covering the connection
  acquisition, the statements and the commit, never ``N x 30 s``;
* a lock timeout that still escapes is a **typed storage INFRA** event for the
  owning run/scheduler boundary, never a process crash and never a duplicate
  launch;
* reads never take a write lock.

This module is the single place that policy is implemented so the main
``Database`` helper, ``capacity.store`` and every new storage store share one
definition instead of drifting into several weak variants.

Design rules kept deliberately narrow:

* :func:`immediate_transaction` retries only the ``BEGIN IMMEDIATE`` acquisition.
  The caller's read-check-write body is yielded exactly once: a body with
  external side effects is never silently replayed.
* The busy timeout for each phase is narrowed to the *remaining* shared
  deadline before the statement runs, so a connection acquisition that already
  consumed most of the budget cannot be followed by a fresh 30 s wait.
* Read-only connections open with ``mode=ro`` and never issue ``BEGIN
  IMMEDIATE``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

__all__ = [
    "BUSY_BACKOFF_START_SECONDS",
    "BUSY_BACKOFF_MAX_SECONDS",
    "BUSY_TIMEOUT_MS",
    "READ_CONNECT_TIMEOUT_SECONDS",
    "WRITE_BUDGET_SECONDS",
    "StorageInfraError",
    "SqliteBusyError",
    "SqliteInfraError",
    "apply_write_pragmas",
    "free_page_ratio",
    "immediate_transaction",
    "is_busy_error",
    "open_read_connection",
    "read_connection",
    "vacuum_database",
    "write_connection",
]

#: ADR-0003 D7: ``busy_timeout`` of 30 s.  Also the total shared write budget.
BUSY_TIMEOUT_MS: Final[int] = 30_000

#: Total wall-clock budget for one bounded write attempt, in seconds.
WRITE_BUDGET_SECONDS: Final[float] = 30.0

#: TCP-style bounded backoff between ``BEGIN IMMEDIATE`` retries.
BUSY_BACKOFF_START_SECONDS: Final[float] = 0.05
BUSY_BACKOFF_MAX_SECONDS: Final[float] = 1.0

#: Connect-level timeout for a write connection (SQLite's own accept wait).
READ_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0

_BUSY_MARKERS: Final[tuple[str, ...]] = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


class StorageInfraError(RuntimeError):
    """A storage/SQLite infrastructure failure, never a code/test failure.

    ``reason_code`` is a stable machine-readable token (for example
    ``SQLITE_BUSY``) that a scheduler/run boundary maps to a typed INFRA park.
    """

    reason_code: str = "STORAGE_INFRA"

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class SqliteBusyError(StorageInfraError):
    """``SQLITE_BUSY``/``database is locked`` persisted past the bounded budget."""

    reason_code = "SQLITE_BUSY"


class SqliteInfraError(StorageInfraError):
    """Any other unusable SQLite state (corruption, missing file, I/O error)."""

    reason_code = "SQLITE_INFRA"


def is_busy_error(exc: BaseException) -> bool:
    """Return True when *exc* is a lock-contention ``OperationalError``."""
    if not isinstance(exc, sqlite3.OperationalError) or isinstance(exc, sqlite3.IntegrityError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def apply_write_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the D7 write-side pragmas to an already-open connection."""
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys = ON;")


def _set_busy_timeout_ms(conn: sqlite3.Connection, milliseconds: float) -> None:
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(milliseconds))};")


@contextmanager
def write_connection(
    path: Path | str,
    *,
    connect_timeout: float = READ_CONNECT_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Open a writable connection configured for explicit transactions.

    ``isolation_level=None`` puts SQLite in autocommit mode so the caller
    controls every transaction boundary explicitly; it never holds an implicit
    write transaction open behind a bare read.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=connect_timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        apply_write_pragmas(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def read_connection(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a genuinely read-only connection (``mode=ro``)."""
    target = Path(path)
    if not target.exists():
        raise SqliteInfraError(f"cannot open missing database for reading: {target}")
    conn = sqlite3.connect(
        f"{target.as_uri()}?mode=ro", uri=True, timeout=READ_CONNECT_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def open_read_connection(path: Path | str) -> sqlite3.Connection:
    """Return an open read-only connection; the caller closes it.

    For long-lived readers that manage their own lifetime.
    """
    target = Path(path)
    conn = sqlite3.connect(
        f"{target.as_uri()}?mode=ro", uri=True, timeout=READ_CONNECT_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def immediate_transaction(
    conn: sqlite3.Connection,
    *,
    total_seconds: float = WRITE_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[sqlite3.Connection]:
    """Run one bounded ``BEGIN IMMEDIATE`` read-check-write transaction.

    ``conn`` must be in autocommit mode (see :func:`write_connection`).  The
    connection acquisition is retried with bounded backoff inside a single
    shared wall-clock deadline of ``total_seconds`` (default 30 s); each phase
    narrows ``busy_timeout`` to the time still remaining, so the total is never
    a multiple of the per-statement timeout.  When the budget is exhausted a
    typed :class:`SqliteBusyError` is raised --- the owner boundary turns that
    into an INFRA park rather than crashing or relaunching.

    The yielded body runs exactly once.  On any exception the transaction is
    rolled back before the error propagates; the body is never retried.
    """
    if total_seconds <= 0:
        raise ValueError("total_seconds must be positive")
    deadline = clock() + total_seconds
    attempt = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SqliteBusyError("sqlite write budget exhausted before BEGIN IMMEDIATE")
        _set_busy_timeout_ms(conn, remaining * 1000.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise SqliteInfraError(f"cannot begin transaction: {exc}") from exc
            attempt += 1
            delay = min(BUSY_BACKOFF_MAX_SECONDS, BUSY_BACKOFF_START_SECONDS * (2**(attempt - 1)))
            remaining = deadline - clock()
            if remaining <= delay:
                raise SqliteBusyError(
                    "database is locked; bounded write budget exhausted acquiring BEGIN IMMEDIATE"
                ) from exc
            sleep(delay)
    try:
        yield conn
        remaining = deadline - clock()
        if remaining <= 0:
            raise SqliteBusyError("sqlite write budget exhausted before commit")
        _set_busy_timeout_ms(conn, remaining * 1000.0)
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            if is_busy_error(exc):
                raise SqliteBusyError(
                    "database is locked; bounded write budget exhausted at commit"
                ) from exc
            raise SqliteInfraError(f"cannot commit transaction: {exc}") from exc
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise


def free_page_ratio(path: Path | str) -> float:
    """Return the free-page ratio (``freelist_count / page_count``) of a DB.

    Returns ``0.0`` for an empty database (``page_count == 0``).
    """
    with read_connection(path) as conn:
        page_count_row = conn.execute("PRAGMA page_count").fetchone()
        freelist_row = conn.execute("PRAGMA freelist_count").fetchone()
    page_count = int(page_count_row[0]) if page_count_row is not None else 0
    freelist = int(freelist_row[0]) if freelist_row is not None else 0
    if page_count <= 0:
        return 0.0
    return freelist / page_count


def vacuum_database(
    path: Path | str,
    *,
    total_seconds: float = WRITE_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run a bounded ``VACUUM`` on *path*.

    ``VACUUM`` cannot run inside a transaction, so this opens its own
    autocommit write connection and narrows ``busy_timeout`` to the shared
    deadline.  A lock that escapes the budget raises :class:`SqliteBusyError`.
    """
    deadline = clock() + total_seconds
    with write_connection(path) as conn:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SqliteBusyError("sqlite vacuum budget exhausted before start")
        _set_busy_timeout_ms(conn, remaining * 1000.0)
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            if is_busy_error(exc):
                raise SqliteBusyError("database is locked during VACUUM") from exc
            raise SqliteInfraError(f"VACUUM failed: {exc}") from exc
