"""Exercise lease ownership through real stdio server processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast


def test_stdio_eof_releases_writer_for_replacement(tmp_path: Path) -> None:
    lease = tmp_path / "lease.json"
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
    command = [
        sys.executable,
        "-m",
        "saberops.cli",
        "mcp",
        "serve",
        "--active",
        "--lease",
        str(lease),
    ]
    processes: list[subprocess.Popen[str]] = []

    def start() -> tuple[subprocess.Popen[str], str]:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        processes.append(process)
        assert process.stderr is not None
        return process, process.stderr.readline()

    try:
        first, message = start()
        assert "writer lease acquired" in message
        second, message = start()
        assert "read-only session" in message
        assert second.stdin is not None
        second.stdin.close()
        assert second.wait(timeout=10) == 0
        assert first.stdin is not None
        first.stdin.close()
        assert first.wait(timeout=10) == 0
        third, message = start()
        assert "writer lease acquired" in message
        assert third.stdin is not None
        third.stdin.close()
        assert third.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def test_stdio_server_crash_allows_replacement(tmp_path: Path) -> None:
    lease = tmp_path / "lease.json"
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
    command = [
        sys.executable,
        "-m",
        "saberops.cli",
        "mcp",
        "serve",
        "--active",
        "--lease",
        str(lease),
    ]
    processes: list[subprocess.Popen[str]] = []
    try:
        for index in range(2):
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            processes.append(process)
            assert process.stderr is not None
            assert "writer lease acquired" in process.stderr.readline()
            if index == 0:
                process.kill()
                process.wait(timeout=10)
            else:
                assert process.stdin is not None
                process.stdin.close()
                assert process.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def test_retained_pipe_expires_and_old_writer_is_fenced(tmp_path: Path) -> None:
    import json
    import time

    lease = tmp_path / "lease.json"
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"), SABEROPS_MCP_IDLE_SECONDS="900")
    command = [
        sys.executable,
        "-m",
        "saberops.cli",
        "mcp",
        "serve",
        "--active",
        "--lease",
        str(lease),
    ]
    processes: list[subprocess.Popen[str]] = []
    try:

        def start() -> subprocess.Popen[str]:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            processes.append(process)
            assert process.stderr is not None
            assert "writer lease acquired" in process.stderr.readline()
            return process

        old = start()
        assert old.stdin is not None and old.stdout is not None

        def send(data: dict[str, object]) -> dict[str, Any]:
            assert old.stdin is not None and old.stdout is not None
            old.stdin.write(json.dumps(data) + "\n")
            old.stdin.flush()
            return cast(dict[str, Any], json.loads(old.stdout.readline()))

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        old.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        old.stdin.flush()
        record = json.loads(lease.read_text())
        old_epoch = record["epoch"]
        record["last_activity"] = time.time() - 901
        lease.write_text(json.dumps(record))
        new = start()
        assert json.loads(lease.read_text())["epoch"] > old_epoch
        result = send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "answer_question",
                    "arguments": {
                        "question_id_or_key": "x",
                        "answer_text": "x",
                        "authority": "OWNER",
                    },
                },
            }
        )
        assert result["error"]["code"] == -32020
        assert new.stdin is not None
        new.stdin.close()
        assert new.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def test_active_job_blocks_expiry_and_targeted_release(tmp_path: Path, monkeypatch: object) -> None:
    import json
    import sqlite3
    import time

    from saberops.mcp.lease import OrchestratorLease

    lease_path = tmp_path / "lease"
    db_path = tmp_path / "owner.db"
    owner = OrchestratorLease(lease_path)
    owner.configure_database(db_path)
    assert owner.acquire()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL,"
            " waiting_reason TEXT)"
        )
        conn.execute("CREATE TABLE mcp_job_owners (job_id TEXT, session_id TEXT)")
        conn.execute("INSERT INTO jobs (job_id, state) VALUES ('job', 'RUNNING')")
        conn.execute("INSERT INTO mcp_job_owners VALUES ('job', ?)", (owner.session_id,))
    record = json.loads(lease_path.read_text())
    record["last_activity"] = time.time() - 3600
    lease_path.write_text(json.dumps(record))
    other = OrchestratorLease(lease_path, pid=1)
    assert not other.acquire()
    released, message = other.release_target(owner.session_id)
    assert not released and "active job" in message
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert other.release_target(owner.session_id)[0]
    assert other.acquire()
    with owner.guarded_call(mutating=True) as owned:
        assert not owned


def test_dead_lease_with_only_never_started_queued_jobs_is_recoverable(
    tmp_path: Path,
) -> None:
    """A dead planner's queued-but-never-started jobs must not block recovery.

    ``active_work`` means *executing* work.  A QUEUED job has not started, so a
    dead session that owns only queued jobs leaves nothing to protect and the
    next session must be able to take the lease and release the dead holder
    without deleting the lease file.
    """
    import json
    import sqlite3
    import time

    from saberops.mcp.lease import OrchestratorLease

    lease_path = tmp_path / "lease"
    db_path = tmp_path / "owner.db"
    owner = OrchestratorLease(lease_path)
    owner.configure_database(db_path)
    assert owner.acquire()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL,"
            " waiting_reason TEXT)"
        )
        conn.execute("CREATE TABLE mcp_job_owners (job_id TEXT, session_id TEXT)")
        conn.execute("INSERT INTO jobs (job_id, state) VALUES ('job', 'QUEUED')")
        conn.execute("INSERT INTO mcp_job_owners VALUES ('job', ?)", (owner.session_id,))
    record = json.loads(lease_path.read_text())
    record["pid"] = 999_999
    record["holder"] = "orch-pid-999999"
    record["last_activity"] = time.time() - 3600
    lease_path.write_text(json.dumps(record))

    next_session = OrchestratorLease(lease_path, pid=1)
    assert next_session.is_stale(next_session.read()) is True
    released, message = next_session.release_target(owner.session_id)
    assert released, message
    assert next_session.acquire()


def test_client_killed_closes_stdin_and_releases(tmp_path: Path) -> None:
    import json
    import signal
    import time

    lease = tmp_path / "lease.json"
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
    script = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-m','saberops.cli','mcp','serve',"
        "'--active','--lease',sys.argv[1]],stdin=subprocess.PIPE,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "time.sleep(60)"
    )
    client = subprocess.Popen([sys.executable, "-c", script, str(lease)], env=env)
    holder_pid = None
    try:
        for _ in range(100):
            if lease.exists() and lease.read_text().strip():
                holder_pid = json.loads(lease.read_text())["pid"]
                break
            time.sleep(0.05)
        assert holder_pid is not None
        client.kill()
        client.wait(timeout=10)
        for _ in range(100):
            if not lease.read_text().strip():
                break
            time.sleep(0.05)
        assert not lease.read_text().strip()
    finally:
        if client.poll() is None:
            client.kill()
            client.wait(timeout=10)
        if holder_pid is not None and lease.exists() and lease.read_text().strip():
            os.kill(holder_pid, signal.SIGKILL)


def test_cli_status_and_targeted_handoff(tmp_path: Path, capsys: Any) -> None:
    from saberops.cli import main
    from saberops.mcp.lease import OrchestratorLease

    lease_path = tmp_path / "lease"
    owner = OrchestratorLease(lease_path)
    assert owner.acquire()
    assert main(["mcp", "status", "--lease", str(lease_path)]) == 0
    status = capsys.readouterr().out
    assert "This chat activation" in status
    assert owner.session_id in status
    assert "Active work" in status and "Takeover eligible" in status
    assert main(["mcp", "release", "--lease", str(lease_path), "--holder", "wrong"]) == 1
    assert owner.writer
    assert main(["mcp", "release", "--lease", str(lease_path), "--holder", owner.session_id]) == 0
    assert not owner.writer
