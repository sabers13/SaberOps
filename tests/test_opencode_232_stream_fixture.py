"""Replay the real OpenCode 2.3.2 ``run --format json`` capture through the adapter.

The checked-in ``opencode/opencode-v2.3.2/run_json.jsonl`` stream was captured
by lane so-gosmoke with the owner-approved one-prompt smoke (see the manifest
provenance).  These tests pin what SaberOps can and cannot read from that CLI
version:

* ``step_finish.part.tokens`` is present and still yields exact input/output/
  cache/reasoning counts, with no invented total (2.3.2 omits ``tokens.total``).
* the stream carries no provider/model identity fields, so the adapter keeps
  its explicit ``unreported`` evidence instead of guessing the requested model.

The adapter is fed by a fake ``opencode`` binary that emits the capture, and
the identity fallback for older streams that did carry ``providerID``/
``modelID`` stays covered so 2.0.29 parsing is not regressed.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from saberops.models import AttemptStatus, WorkerRequest
from saberops.telemetry import extract_opencode_identity
from saberops.vendor_cli_fixtures import FIXTURES_ROOT, load_manifest
from saberops.workers.opencode import OpenCodeAdapter

STREAM_RELATIVE = "opencode/opencode-v2.3.2/run_json.jsonl"
STREAM = FIXTURES_ROOT / STREAM_RELATIVE


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "gosmoke@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "GoSmoke"], cwd=path, check=True)
    (path / "README.md").write_text("# gosmoke\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        directory = tmp_path / sub
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(directory))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _fake_opencode(directory: Path) -> Path:
    """Write a fake CLI that returns the committed 2.3.2 capture."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "opencode"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('opencode v2.3.2')\n"
        "    raise SystemExit(0)\n"
        f"sys.stdout.write(Path({str(STREAM)!r}).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def test_manifest_declares_the_232_stream_as_a_real_usage_fixture() -> None:
    """Covers: OpenCode 2.3.2 ``usage_json`` is no longer a fake-only gap."""
    manifest = load_manifest()
    fixture = next(fx for fx in manifest.fixtures if fx.file == STREAM_RELATIVE)
    assert fixture.vendor == "opencode"
    assert fixture.cli_version == "opencode v2.3.2"
    assert fixture.kind == "usage_json"
    assert fixture.source == "real-capture"
    assert fixture.consumer == "saberops.telemetry:parse_opencode_jsonl"
    assert STREAM.is_file()


def test_232_stream_reports_exact_tokens_and_no_identity() -> None:
    """Covers: the real 2.3.2 stream shape the adapter parses."""
    text = STREAM.read_text(encoding="utf-8")
    # 2.3.2's JSON writer emits only snapshot/reason/cost/tokens for step
    # events, so model identity never reaches --format json consumers.
    assert extract_opencode_identity(text) == (None, None)
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    finishes = [event for event in events if event.get("type") == "step_finish"]
    assert len(finishes) == 1
    assert finishes[0]["part"]["tokens"] == {
        "input": 3521,
        "output": 3,
        "reasoning": 0,
        "cache": {"read": 1664, "write": 0},
    }


def test_adapter_parses_the_232_stream_through_its_run_path(isolated_env: Path) -> None:
    """Covers: exact usage evidence and explicit unknowns from 2.3.2 output."""
    fake_bin = _fake_opencode(isolated_env / "bin")
    worktree = _git_repo(isolated_env / "wt")
    adapter = OpenCodeAdapter(opencode_bin=str(fake_bin))

    result = adapter.run(
        WorkerRequest(
            task="Reply with the single word OK.",
            worktree_path=worktree,
            model="opencode-go/deepseek-v4.1-flash",
        )
    )

    assert result.status is AttemptStatus.SUCCESS
    assert result.stdout == "OK"
    assert result.cli_version == "opencode v2.3.2"
    assert len(result.usage) == 1
    usage = result.usage[0]
    assert usage.input_tokens == 3521
    assert usage.output_tokens == 3
    assert usage.cached_tokens == 1664
    assert usage.cache_read_tokens == 1664
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0
    # 2.3.2 omits tokens.total; it must stay an explicit unknown, not a sum.
    assert usage.total_tokens is None
    assert usage.usage_source == "opencode_step_finish_json"
    # No identity in the stream: report "unreported" rather than the request.
    assert result.actual_model == "unreported"
    assert result.requested_variant == "unreported"
    assert result.reported_variant == "unreported"


def test_legacy_identity_events_still_parse() -> None:
    """Covers: streams that do carry identity keep 2.0.29-era compatibility."""
    stdout = json.dumps(
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4.1-flash",
                "variant": "fast",
            },
        }
    )
    assert extract_opencode_identity(stdout) == (
        "opencode-go/deepseek-v4.1-flash",
        "fast",
    )
