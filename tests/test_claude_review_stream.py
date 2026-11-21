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

import pytest

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


# Claude Code 2.1.330 exits 0 before flushing its async stdout writes, so the
# stream ends mid-frame with no assistant/result tail reaching the pipe while
# the CLI's own session transcript records the completed turn.  This fake CLI
# reproduces exactly that: bytes cut inside a JSON frame, exit 0, and (when the
# config dir is provided) a durable transcript of the finished turn.
_TRUNCATING_CLI = '''#!{python}
"""Truncate stream-json output like Claude Code 2.1.330; no provider call."""
import datetime
import json
import os
import re
import sys

SESSION_ID = "{session_id}"
STAMP = "{stamp}"
AUTH = '{{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}}'


def write_session(worktree: str) -> None:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config:
        return
    encoded = re.sub(r"[^A-Za-z0-9]", "-", worktree)
    folder = os.path.join(config, "projects", encoded)
    os.makedirs(folder, exist_ok=True)
    if STAMP:
        stamp = STAMP
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    final = {{
        "type": "assistant",
        "timestamp": stamp,
        "message": {{
            "stop_reason": "end_turn",
            "content": [{{"type": "text", "text": "VERDICT: PASS\\n\\nAll checks passed."}}],
            "usage": {{
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            }},
        }},
    }}
    with open(os.path.join(folder, SESSION_ID + ".jsonl"), "w", encoding="utf-8") as handle:
        prompt_entry = {{"type": "user", "message": {{"role": "user", "content": "prompt"}}}}
        handle.write(json.dumps(prompt_entry))
        handle.write("\\n")
        handle.write(json.dumps(final))
        handle.write("\\n")
        handle.write(json.dumps({{"type": "cost-state", "sessionId": SESSION_ID}}))
        handle.write("\\n")


def main() -> int:
    if "auth" in sys.argv[1:]:
        sys.stdout.write(AUTH)
        sys.stdout.flush()
        return 0
    sys.stdin.read()
    out = sys.stdout.buffer
    out.write((json.dumps({{"type": "system", "subtype": "init",
                            "model": "claude-native-id",
                            "session_id": SESSION_ID}}) + "\\n").encode())
    out.write((json.dumps({{"type": "assistant",
                            "message": {{"content": [{{"type": "text", "text": "working"}}]}},
                            "session_id": SESSION_ID}}) + "\\n").encode())
    out.flush()
    write_session(os.getcwd())
    # The lost tail: a frame cut mid-JSON with no newline, then exit 0.
    out.write(b'{{"type":"stream_event","event":{{"delta":{{"text":"No')
    out.flush()
    return 0


raise SystemExit(main())
'''

_SESSION_ID = "5df316e3-ab45-48f2-a803-815f95e6aac6"


def _install_truncating_cli(
    directory: Path, *, stamp: str | None = None
) -> Path:
    path = directory / "claude"
    path.write_text(
        _TRUNCATING_CLI.format(
            python=sys.executable,
            session_id=_SESSION_ID,
            stamp=stamp or "",
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_mid_frame_stream_is_reported_as_truncated_provider_output(
    tmp_path: Path,
) -> None:
    claude = _install_truncating_cli(tmp_path)

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions")

    assert result.status is AttemptStatus.FAILED
    assert "truncated" in (result.error or "")
    assert "did not report successful task completion" not in (result.error or "")
    assert result.provider_terminal_status == "FAILED"


def test_truncated_stream_recovers_completion_from_native_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    claude = _install_truncating_cli(tmp_path)

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions")

    assert result.status is AttemptStatus.SUCCESS, result.error
    assert result.error is None
    assert result.provider_terminal_status == "SUCCESS"
    assert "VERDICT: PASS" in result.stdout
    # The truncation stays visible in the evidence rather than being hidden.
    assert "truncated" in result.stderr
    assert "recovered" in result.stderr


def test_session_recovery_ignores_an_earlier_completed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed session's previous turn must never stand in for this one."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    claude = _install_truncating_cli(tmp_path, stamp="2020-01-01T00:00:00.000Z")

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions")

    assert result.status is AttemptStatus.FAILED
    assert "truncated" in (result.error or "")


# ---------------------------------------------------------------------------
# B3 D8: Claude token counters survive both the normal and the recovered path
# ---------------------------------------------------------------------------


def test_result_frame_records_claude_token_counters(tmp_path: Path) -> None:
    frames = FIXTURE.read_text(encoding="utf-8").splitlines()
    usage = json.loads(frames[-1])["usage"]
    claude = _install_fake_cli(tmp_path, frames)

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions")

    assert result.status is AttemptStatus.SUCCESS, result.error
    assert len(result.usage) == 1
    entry = result.usage[0]
    assert entry.usage_source == "claude_result_json"
    assert entry.input_tokens == usage["input_tokens"]
    assert entry.output_tokens == usage["output_tokens"]
    assert entry.cache_read_tokens == usage["cache_read_input_tokens"]
    assert entry.cache_write_tokens == usage["cache_creation_input_tokens"]
    assert entry.total_tokens == usage["input_tokens"] + usage["output_tokens"]


def test_recovered_completion_keeps_transcript_token_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript's assistant message carries the same usage as the frame.

    Before this fix every Claude batch row was ``unknown``: the recovery path
    returned text only, and the adapter never populated ``WorkerResult.usage``
    at all.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    claude = _install_truncating_cli(tmp_path)

    result = _review(_candidate_worktree(tmp_path), claude, tmp_path / "sessions")

    assert result.status is AttemptStatus.SUCCESS, result.error
    assert len(result.usage) == 1
    entry = result.usage[0]
    assert entry.usage_source == "claude_transcript_json"
    assert entry.input_tokens == 11
    assert entry.output_tokens == 7
    assert entry.cache_read_tokens == 3
    assert entry.cache_write_tokens == 4
    assert entry.total_tokens == 18
