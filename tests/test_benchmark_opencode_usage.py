"""B2/so-gofake: the fake OpenCode worker reports real 2.3.2 usage.

The fake-mode benchmark prints per-subscription usage.  Its OpenCode Go rows
were ``unknown`` because the fake worker emitted a legacy ``step_finish`` shape
(``input``/``output``/``total``) that no longer matches the installed CLI, and
the fake server exposed no ``step-finish`` message part at all.

These tests pin the fake to the checked-in real 2.3.2 ``run --format json``
capture (``opencode-v2.3.2/run_json.jsonl``): exact counters, no
``tokens.total`` and no model identity.  No vendor CLI, model or network call is
used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from benchmark_core import fake_mode_environment

from saberops.telemetry import extract_opencode_identity, parse_opencode_jsonl

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLI = FIXTURES / "fake_worker_cli.py"
REAL_232_STREAM = (
    FIXTURES / "vendor_cli" / "opencode" / "opencode-v2.3.2" / "run_json.jsonl"
)

#: Small deterministic counters the fake reports for one OpenCode turn.
FAKE_TOKENS = {
    "input": 12,
    "output": 8,
    "reasoning": 0,
    "cache": {"read": 4, "write": 2},
}


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "gofake@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "GoFake"], cwd=path, check=True)
    (path / "README.md").write_text("# gofake\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def _child_env(home: Path) -> dict[str, str]:
    """A clean fake-worker environment: no inherited worker or reviewer control."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ORCH_FAKE_")
    }
    env["HOME"] = str(home)
    env["TMPDIR"] = "/var/tmp"
    return env


def _fixture_token_shape() -> tuple[set[str], set[str]]:
    """Return the real 2.3.2 ``step_finish`` token and cache keys."""
    events = [
        json.loads(line)
        for line in REAL_232_STREAM.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tokens = next(
        event["part"]["tokens"] for event in events if event.get("type") == "step_finish"
    )
    return set(tokens), set(tokens["cache"])


def test_fake_opencode_run_stream_matches_the_232_fixture(tmp_path: Path) -> None:
    """The fake ``run --format json`` stream has the real 2.3.2 token shape."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _git_repo(tmp_path / "workspace")
    completed = subprocess.run(
        [
            sys.executable,
            str(FAKE_CLI),
            "--fake-harness",
            "opencode",
            "run",
            "Reply with the single word OK.",
            "--model",
            "opencode-go/deepseek-v4-flash",
            "--format",
            "json",
        ],
        cwd=repo,
        env=_child_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    events = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip()
    ]
    assert [event["type"] for event in events] == ["step_start", "text", "step_finish"]
    assert events[0]["part"] == {"type": "step-start"}

    finish = events[-1]["part"]
    assert finish["type"] == "step-finish"
    token_keys, cache_keys = _fixture_token_shape()
    assert set(finish["tokens"]) == token_keys
    assert set(finish["tokens"]["cache"]) == cache_keys
    assert finish["tokens"] == FAKE_TOKENS
    # 2.3.2 does not emit tokens.total, so the fake must not invent one.
    assert "total" not in finish["tokens"]

    # Identity stays unreported exactly as in the real capture.
    assert extract_opencode_identity(completed.stdout) == (None, None)

    _text, usage = parse_opencode_jsonl(completed.stdout)
    assert len(usage) == 1
    assert usage[0].usage_source == "opencode_step_finish_json"
    assert usage[0].input_tokens == 12
    assert usage[0].output_tokens == 8
    assert usage[0].reasoning_tokens == 0
    assert usage[0].cached_tokens == 4
    assert usage[0].cache_read_tokens == 4
    assert usage[0].cache_write_tokens == 2
    assert usage[0].total_tokens is None


def test_fake_mode_environment_never_probes_vendor_clis() -> None:
    """Running the benchmark script keeps the F5 guard off the fake CLIs."""
    env = fake_mode_environment()
    assert env["SABEROPS_DISABLE_AUTO_SCHEDULER"] == "1"
    assert env["SABEROPS_VENDOR_CHECKS"] == "0"
    assert env["TMPDIR"] == "/var/tmp"
