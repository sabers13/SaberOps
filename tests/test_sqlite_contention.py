"""D7 SQLite robustness contracts for the GS-1 storage lifecycle service.

These tests exercise the real cross-process behaviour of
:mod:`saberops.sqlite_policy` with actual child processes and real SQLite locks,
not mocked ``OperationalError`` values.

Covers: ADR-0003 D7 (WAL, 30 s busy timeout, bounded ``BEGIN IMMEDIATE`` total
<= 30 s, typed infrastructure on lock exhaustion, reads never take a write
lock).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from saberops.sqlite_policy import (
    BUSY_TIMEOUT_MS,
    WRITE_BUDGET_SECONDS,
    SqliteBusyError,
    free_page_ratio,
    immediate_transaction,
    is_busy_error,
    read_connection,
    vacuum_database,
    write_connection,
)

_CHILD_HOLD = textwrap.dedent(
    """
    import sqlite3, sys, time
    path, hold = sys.argv[1], float(sys.argv[2])
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t (v) VALUES (1)")
    print("LOCKED", flush=True)
    time.sleep(hold)
    conn.commit()
    conn.close()
    """
)

_CHILD_INCR = textwrap.dedent(
    """
    import sqlite3, sys
    path, n = sys.argv[1], int(sys.argv[2])
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY, n INTEGER)")
    for _ in range(n):
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT n FROM counter WHERE id = 1").fetchone()
        current = 0 if row is None else int(row[0])
        conn.execute(
            "INSERT INTO counter (id, n) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET n = excluded.n",
            (current + 1,),
        )
        conn.commit()
    conn.close()
    """
)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "contention.db"
    with write_connection(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY, n INTEGER)")
        conn.execute("INSERT OR IGNORE INTO counter (id, n) VALUES (1, 0)")
    return path


def test_write_connection_sets_wal_and_d7_busy_timeout(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- WAL and a 30 s busy timeout on write connections."""
    with write_connection(tmp_path / "a.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert BUSY_TIMEOUT_MS == 30_000
        assert WRITE_BUDGET_SECONDS == 30.0


def test_read_connection_is_read_only(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- reads never take a write lock or mutate the file."""
    path = _db(tmp_path)
    with read_connection(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO counter (id, n) VALUES (2, 0)")


def test_immediate_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- explicit commit / rollback with no partial write."""
    path = _db(tmp_path)
    with write_connection(path) as conn:
        with immediate_transaction(conn):
            conn.execute("UPDATE counter SET n = 5 WHERE id = 1")
    with read_connection(path) as conn:
        assert conn.execute("SELECT n FROM counter WHERE id = 1").fetchone()[0] == 5

    with pytest.raises(RuntimeError):
        with write_connection(path) as conn:
            with immediate_transaction(conn):
                conn.execute("UPDATE counter SET n = 99 WHERE id = 1")
                raise RuntimeError("boom")
    with read_connection(path) as conn:
        assert conn.execute("SELECT n FROM counter WHERE id = 1").fetchone()[0] == 5


def test_lock_exhaustion_is_a_typed_infra_error(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- a held lock past the budget is typed INFRA."""
    path = _db(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(path), "3.0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        started = time.monotonic()
        with (
            write_connection(str(path)) as conn,
            pytest.raises(SqliteBusyError) as excinfo,
        ):
            with immediate_transaction(conn, total_seconds=0.4):
                conn.execute("UPDATE counter SET n = 1 WHERE id = 1")
        assert excinfo.value.reason_code == "SQLITE_BUSY"
        # Bounded: the failure must arrive close to the budget, never 30 s per phase.
        assert time.monotonic() - started < 5.0
    finally:
        child.wait(timeout=10)


def test_release_before_deadline_succeeds_exactly_once(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- a lock released inside the budget is taken once."""
    path = _db(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(path), "0.3"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with write_connection(str(path)) as conn:
            with immediate_transaction(conn, total_seconds=20.0):
                conn.execute("UPDATE counter SET n = 7 WHERE id = 1")
        with read_connection(path) as conn:
            assert conn.execute("SELECT n FROM counter WHERE id = 1").fetchone()[0] == 7
    finally:
        child.wait(timeout=20)


def test_two_writer_processes_never_lose_a_transaction(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- exclusive read-check-write across processes."""
    path = _db(tmp_path)
    procs = [
        subprocess.Popen([sys.executable, "-c", _CHILD_INCR, str(path), "15"]) for _ in range(2)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    with read_connection(path) as conn:
        assert conn.execute("SELECT n FROM counter WHERE id = 1").fetchone()[0] == 30


def test_read_only_connection_never_blocks_a_writer(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- no write lock is taken on a read path."""
    path = _db(tmp_path)
    with read_connection(path) as reader:
        assert reader.execute("SELECT n FROM counter WHERE id = 1").fetchone()[0] == 0
        with write_connection(str(path)) as writer:
            with immediate_transaction(writer, total_seconds=5.0):
                writer.execute("UPDATE counter SET n = 3 WHERE id = 1")


def test_free_page_ratio_and_vacuum(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- owner-DB VACUUM uses the free-page ratio."""
    path = tmp_path / "vacuum.db"
    with write_connection(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, blob BLOB)")
        with immediate_transaction(conn):
            conn.executemany(
                "INSERT INTO t (blob) VALUES (?)", [(b"x" * 512,) for _ in range(200)]
            )
        with immediate_transaction(conn):
            conn.execute("DELETE FROM t")
    ratio = free_page_ratio(path)
    assert 0.0 <= ratio <= 1.0
    before = path.stat().st_size
    vacuum_database(path)
    assert path.stat().st_size <= before
    # VACUUM leaves no free pages behind for a trivially small file.
    assert free_page_ratio(path) <= ratio


def test_is_busy_error_classification() -> None:
    """Covers: ADR-0003 D7 -- only real lock contention maps to BUSY."""
    assert is_busy_error(sqlite3.OperationalError("database is locked"))
    assert is_busy_error(sqlite3.OperationalError("database table is locked"))
    assert not is_busy_error(sqlite3.OperationalError("no such table: nope"))
    assert not is_busy_error(RuntimeError("database is locked"))


_DB_HOLD = textwrap.dedent(
    """
    import sqlite3, sys, time
    path, hold = sys.argv[1], float(sys.argv[2])
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE runs SET task = task WHERE 0")
    print("LOCKED", flush=True)
    time.sleep(hold)
    conn.commit()
    conn.close()
    """
)


def _seed_run_db(tmp_path: Path) -> Path:
    from saberops.db import Database
    from saberops.models import Run, RunStatus, Tier

    path = tmp_path / "orch.db"
    db = Database(path)
    db.create_run(
        Run(
            id="run1",
            task="t",
            target_repo=str(tmp_path),
            base_commit="0" * 40,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at="2026-09-24T10:00:00+00:00",
            completed_at="2026-09-24T10:00:00+00:00",
        )
    )
    return path


def test_real_database_write_is_a_typed_infra_error_under_contention(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- an actual Database write raises typed INFRA, not a crash."""
    from saberops.db import Database
    from saberops.models import RunStatus

    path = _seed_run_db(tmp_path)
    db = Database(path)
    child = subprocess.Popen(
        [sys.executable, "-c", _DB_HOLD, str(path), "3.0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        started = time.monotonic()
        with pytest.raises(SqliteBusyError) as excinfo:
            with db.write_transaction(total_seconds=0.5) as conn:
                conn.execute("UPDATE runs SET status = ? WHERE id = 'run1'", ("FAILED",))
        assert excinfo.value.reason_code == "SQLITE_BUSY"
        assert time.monotonic() - started < 5.0
    finally:
        child.wait(timeout=10)
    # No partial write and reads stay available.
    run_after = db.get_run("run1")
    assert run_after is not None and run_after.status is RunStatus.COMPLETED


def test_real_database_write_succeeds_once_after_release(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- a lock released inside the budget commits exactly once."""
    from saberops.db import Database
    from saberops.models import RunStatus

    path = _seed_run_db(tmp_path)
    db = Database(path)
    child = subprocess.Popen(
        [sys.executable, "-c", _DB_HOLD, str(path), "0.3"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with db.write_transaction(total_seconds=20.0) as conn:
            conn.execute("UPDATE runs SET status = 'FAILED' WHERE id = 'run1'")
    finally:
        child.wait(timeout=20)
    run_after = db.get_run("run1")
    assert run_after is not None and run_after.status is RunStatus.FAILED


def test_capacity_store_write_is_a_typed_infra_error_under_contention(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- capacity.store writes share the bounded write path."""
    from saberops.capacity.store import ensure_tables, owner_write_connection
    from saberops.db import Database

    path = _seed_run_db(tmp_path)
    db = Database(path)
    ensure_tables(db)
    child = subprocess.Popen(
        [sys.executable, "-c", _DB_HOLD, str(path), "3.0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(SqliteBusyError):
            with owner_write_connection(db, total_seconds=0.5) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS probe_marker (id INTEGER PRIMARY KEY)"
                )
    finally:
        child.wait(timeout=10)


def test_read_paths_are_not_blocked_by_a_writer(tmp_path: Path) -> None:
    """Covers: ADR-0003 D7 -- reads take no write lock and stay available."""
    from saberops.capacity.store import owner_connection
    from saberops.db import Database

    path = _seed_run_db(tmp_path)
    db = Database(path)
    child = subprocess.Popen(
        [sys.executable, "-c", _DB_HOLD, str(path), "2.0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        assert db.get_run("run1") is not None
        with owner_connection(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        child.wait(timeout=10)
