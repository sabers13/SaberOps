"""Exercise lease ownership through real stdio server processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
