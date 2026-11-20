"""The real Claude review launch path must keep the terminal ``result`` frame.

Regression for run ``run_70ead49549d5_2bbf80c78d`` on 6c172ef: the Claude
reviewer exited 0 and answered ``VERDICT: PASS``, the durable session journal
recorded every frame of its stream-json transcript, yet SaberOps reported
``Reviewer execution failed (no usable verdict produced)``.

The launch path streams each provider line to the caller's callback, and the
production callback performs two durable session-journal writes plus a log
append per line.  That consumer is far slower than the CLI's writer, so the
drain thread is still delivering bytes it already read when the process leader
exits.  When the capture was snapshotted after a fixed cleanup join, the
trailing frames -- including the ``result`` frame that carries native
completion evidence -- were dropped from ``ProcessResult.stdout`` while the
daemon drain thread went on to write them into the transcript afterwards.
These tests drive the real launch path (real subprocess, real drain threads,
real adapter) with a replayed transcript, never a runner stub.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from saberops.agent_sessions import observe_invocation
from saberops.models import AttemptStatus, WorkerRequest, WorkerResult
from saberops.process import ProcessResult
from saberops.workers.claude import ClaudeCodeAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "claude_review_stream.jsonl"

# The capture used to be snapshotted after at most 1 s + 5 s of drain cleanup.
# One callback that blocks past that window is deterministic (it does not
# depend on machine speed) and mirrors a consumer slower than the producer,
# which is the production case.
_SLOW_CONSUMER_SECONDS = 6.5

_FAKE_CLI = '''#!{python}
"""Replay a captured Claude Code stream-json transcript; no provider call."""
import sys

FRAMES = {frames!r}
AUTH = '{{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}}'


def main() -> int:
    if "auth" in sys.argv[1:]:
        sys.stdout.write(AUTH)
        sys.stdout.flush()
        return 0
    sys.stdin.read()
    out = sys.stdout.buffer
    for index, line in enumerate(FRAMES):
        # Every frame is one write.  The real CLI's final frame may arrive
        # without a trailing newline, so the replay does the same.
        out.write(line.encode() + (b"" if index == len(FRAMES) - 1 else b"\\n"))
        out.flush()
    return 0


raise SystemExit(main())
'''


def _install_fake_cli(directory: Path, frames: list[str]) -> Path:
    path = directory / "claude"
    path.write_text(_FAKE_CLI.format(python=sys.executable, frames=frames))
    path.chmod(0o755)
    return path


def _candidate_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    return worktree


def _review(
    worktree: Path,
    claude: Path,
    sessions: Path,
    *,
    on_stdout: Callable[[str], None] | None = None,
) -> WorkerResult:
    return observe_invocation(
        ClaudeCodeAdapter(claude_bin=str(claude)).run,
        WorkerRequest(
            "Review the candidate.",
            worktree,
            "claude-haiku-4-5-20251001",
            timeout_seconds=120,
            read_only=True,
            on_stdout=on_stdout,
        ),
        root=sessions,
        owner="run_regression",
        role="reviewer",
        provider="anthropic",
        association="rev_regression",
    )


def test_reviewer_keeps_terminal_frame_behind_a_slow_consumer(tmp_path: Path) -> None:
    frames = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert json.loads(frames[-1])["type"] == "result"
    claude = _install_fake_cli(tmp_path, frames)
    observed: list[str] = []

    def slow_consumer(line: str) -> None:
        observed.append(line)
        if len(observed) == 1:
            # Block once, past the window the capture used to wait for.
            time.sleep(_SLOW_CONSUMER_SECONDS)

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions",
                     on_stdout=slow_consumer)

    assert result.status is AttemptStatus.SUCCESS, result.error
    assert result.provider_terminal_status == "SUCCESS"
    assert result.error is None
    # The captured ``result`` frame carries the reviewer's answer; seeing it
    # (rather than accumulated assistant text) proves native completion
    # evidence reached the adapter.
    assert result.stdout == json.loads(frames[-1])["result"]
    assert "VERDICT: PASS" in result.stdout
    assert len(observed) == len(frames)


def test_session_log_keeps_one_frame_per_line(tmp_path: Path) -> None:
    # Three representative frames (init, assistant text, terminal result):
    # the log layout, not stream length, is what this test pins down.
    all_frames = FIXTURE.read_text(encoding="utf-8").splitlines()
    frames = [all_frames[0], all_frames[-6], all_frames[-1]]
    assert [json.loads(line)["type"] for line in frames] == [
        "system", "assistant", "result"
    ]
    claude = _install_fake_cli(tmp_path, frames)
    sessions = tmp_path / "sessions"

    result = _review(_candidate_worktree(tmp_path), claude, sessions)

    assert result.status is AttemptStatus.SUCCESS, result.error
    logs = list(sessions.rglob("*.log"))
    assert len(logs) == 1
    written = logs[0].read_text(encoding="utf-8").splitlines()
    assert len(written) == len(frames)
    assert [json.loads(line)["type"] for line in written] == [
        json.loads(line)["type"] for line in frames
    ]


def test_incomplete_capture_is_reported_and_never_success(tmp_path: Path) -> None:
    """Fail closed with the real cause when the terminal frame is missing."""
    payload = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "model": "claude-native-id"}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "VERDICT: PASS"}]}}),
    ])

    def runner(argv: list[str], **kwargs: object) -> ProcessResult:
        if "auth" in argv:
            return ProcessResult(argv, 0, json.dumps(
                {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
            ), "", 0.1)
        return ProcessResult(argv, 0, payload, "", 0.1, output_incomplete=True)

    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest(
        "Review the candidate.", tmp_path, "claude-haiku-4-5-20251001", read_only=True))

    assert result.status is AttemptStatus.FAILED
    assert "output capture was incomplete" in (result.error or "")
