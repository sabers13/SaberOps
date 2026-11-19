"""F25: the plain-language first-run walkthrough.

Hermetic: every store, home and CLI shim is temporary; no vendor prompt and no
network call is made.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from saberops.walkthrough import (
    GLOSSARY,
    NEVER_DO,
    SAMPLE_FIRST_JOB,
    build_walkthrough,
    intro_text,
    readability_problems,
    render_text,
    store_has_job,
    store_needs_introduction,
)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from lifecycle_rig import build_lifecycle_rig

    return build_lifecycle_rig(tmp_path, monkeypatch)


def _fresh_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    """A fresh owner store with fake codex/claude CLIs and a toy repository."""
    from lifecycle_rig import _install_shim, create_toy_repo

    for variable, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state" / "saberops"
    state.mkdir()
    for cli in ("codex", "claude"):
        shim = _install_shim(bindir, cli, state)
        contents = shim.read_text()
        prefix = f'if [ "$1" = "--version" ]; then echo "{cli} fake"; exit 0; fi\n'
        shim.write_text(contents.replace("#!/bin/sh\n", "#!/bin/sh\n" + prefix))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    repo, _ = create_toy_repo(tmp_path)
    return repo, bindir


def _owner_args(command: str) -> list[list[str]]:
    """Split one walkthrough command into ``orch`` argv lists."""
    argv: list[list[str]] = []
    for part in command.split(" && "):
        words = shlex.split(part)
        assert words.pop(0) == "orch"
        argv.append(words)
    return argv


def _with_answer(command: str) -> str:
    """Fill the placeholders with the values an owner would give."""
    return (
        command.replace("<check command>", "true")
        .replace("<percent>", "80")
        .replace("<owner-confirmed-class>", "A")
        .replace("<policy-url>", "https://example.com/policy")
    )


def _start_scheduler(command: str, log_path: Path) -> subprocess.Popen[bytes]:
    words = shlex.split(command)
    assert words.pop(0) == "orch"
    log = open(log_path, "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "saberops.cli", *words],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


# --------------------------------------------------------------------------- #
# The copy itself
# --------------------------------------------------------------------------- #


def test_introduction_and_step_text_are_short_and_plain() -> None:
    assert readability_problems() == []


def test_introduction_says_what_it_does_and_what_it_never_does() -> None:
    text = intro_text()
    assert "subscriptions you already pay for" in text
    assert "checks their work" in text
    for never in NEVER_DO:
        assert never.rstrip(".").lower() in text.lower()
    assert "ten minutes" in text
    assert {"job", "review", "accept"} <= set(GLOSSARY)


# --------------------------------------------------------------------------- #
# A fresh store
# --------------------------------------------------------------------------- #


def test_a_fresh_store_needs_the_introduction(tmp_path: Path) -> None:
    assert store_needs_introduction(tmp_path / "absent.db") is True
    assert store_has_job(tmp_path / "absent.db") is False


def test_fresh_walkthrough_is_plain_and_starts_with_the_first_missing_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lifecycle_rig import create_toy_repo

    for variable, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(variable, str(tmp_path / name))
    repo, _ = create_toy_repo(tmp_path / "repo-root")

    walk = build_walkthrough(repo)
    assert walk.needed is True and walk.ready is False
    assert walk.next_step is not None
    assert walk.next_step.command
    payload = walk.as_dict()
    assert payload["title"] == "Welcome to SaberOps"
    assert payload["intro"] and payload["never_do"] and payload["glossary"]
    assert payload["sample_first_job"] == SAMPLE_FIRST_JOB
    assert payload["what_next"][0]["say"] == "Start a task"
    text = render_text(walk)
    assert "[ ]" in text and "You type:" in text


@pytest.mark.packaged_routing_default
def test_interrupted_walkthrough_resumes_at_the_next_missing_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lifecycle_rig import create_toy_repo

    for variable, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(variable, str(tmp_path / name))
    repo, _ = create_toy_repo(tmp_path / "repo-root")

    first = build_walkthrough(repo)
    assert first.next_step is not None
    assert "Say how to test this project" not in first.done_titles

    from saberops.cli import main

    assert main(["project", "set-gate", "--repo", str(repo), "true"]) == 0

    # The conversation resumes where it stopped: the finished item is a check
    # mark and the next missing item is a different one.
    resumed = build_walkthrough(repo)
    assert "Say how to test this project" in resumed.done_titles
    assert resumed.next_step is None or resumed.next_step.key != "check_command"
    text = render_text(resumed)
    assert "[x] Say how to test this project" in text


def test_owner_only_steps_are_marked_and_never_runnable() -> None:
    from saberops.mcp.guide import GUIDE
    from saberops.walkthrough import WalkthroughStep, _classify

    for message, key in (
        ("anthropic/* billing: not confirmed", "billing_anthropic"),
        ("codex sign-in: NOT_AUTHENTICATED (no credentials)", "sign_in_codex"),
        ("codex provider is blocked", "provider_blocked_codex"),
        (
            "target/gate: /p (a filesystem): 1 bytes available; 2 bytes required",
            "disk",
        ),
        (
            "codex CLI version unsupported: --json missing from --help",
            "cli_update_codex",
        ),
    ):
        spec = _classify(message)
        assert spec is not None and spec.key == key and spec.kind == "owner", message
    quota = _classify("codex/chatgpt-plus quota: missing, stale or blocked (age 2d)")
    assert quota is not None and quota.kind == "answer"
    step = WalkthroughStep(
        key=key,
        title="t",
        explain="e",
        kind="owner",
        command="orch posture verify anthropic --class '<c>' --url '<u>'",
        detail="",
        done=False,
    )
    assert step.needs_value is True
    assert "Never run an owner step" in GUIDE


# --------------------------------------------------------------------------- #
# The scripted conversation, start to green preflight
# --------------------------------------------------------------------------- #


@pytest.mark.packaged_routing_default
def test_scripted_conversation_reaches_a_green_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the walkthrough's own prompts are needed to reach a green preflight."""
    from saberops.cli import main
    from saberops.preflight import collect_preflight

    repo, _ = _fresh_store(monkeypatch, tmp_path)
    process: subprocess.Popen[bytes] | None = None
    owner_steps: list[str] = []
    scheduler_log = tmp_path / "scheduler.log"
    try:
        for _ in range(40):
            walk = build_walkthrough(repo)
            step = walk.next_step
            if step is None:
                break
            assert step.command, step.key
            if step.key == "scheduler":
                process = _start_scheduler(step.command, scheduler_log)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not [f for f in collect_preflight(repo) if f.severity == "FAIL"]:
                        break
                    assert process.poll() is None, scheduler_log.read_text()
                    time.sleep(0.2)
                else:
                    pytest.fail(
                        "preflight never turned green with the walkthrough: "
                        + scheduler_log.read_text()
                    )
                continue
            words = _with_answer(step.command)
            if step.kind == "owner":
                owner_steps.append(step.key)
            for argv in _owner_args(words):
                assert main(argv) == 0, (step.key, argv)

        final = build_walkthrough(repo)
        assert final.ready is True, [s for s in final.steps if not s.done]
        assert final.next_step is None
        assert owner_steps, "the fresh store has owner-only steps to hand over"
        assert "sign_in_codex" in owner_steps or "sign_in_anthropic" in owner_steps
        # Green preflight but no job yet: the walkthrough is still offered.
        assert final.first_job_done is False and final.needed is True
        text = render_text(final)
        assert "Setup is done" in text and SAMPLE_FIRST_JOB["goal"] in text
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Surfaces: the MCP tool, the CLI and the turn-on message
# --------------------------------------------------------------------------- #


def test_mcp_start_walkthrough_returns_the_same_walkthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lifecycle_rig import create_toy_repo

    from saberops.mcp.tools import TOOL_NAMES, ServerContext, build_default_registry

    for variable, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(variable, str(tmp_path / name))
    repo, _ = create_toy_repo(tmp_path / "repo-root")
    assert "start_walkthrough" in TOOL_NAMES
    registry = build_default_registry(ServerContext(db_path=tmp_path / "owner.db"))
    payload = registry.call("start_walkthrough", {"repo": str(repo)})
    assert payload["v"] == 1 and payload["needed"] is True
    walked = build_walkthrough(repo)
    assert walked.next_step is not None
    assert [(step["key"], step["kind"], step["done"]) for step in payload["steps"]] == [
        (step.key, step.kind, step.done) for step in walked.steps
    ]
    assert payload["next_step"]["key"] == walked.next_step.key
    assert payload["intro"] and payload["sample_first_job"]


@pytest.mark.packaged_routing_default
def test_orch_welcome_prints_the_walkthrough(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from saberops.cli import main

    repo, _ = _fresh_store(monkeypatch, tmp_path)
    assert main(["welcome", "--repo", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "Welcome to SaberOps"
    assert payload["next_step"]["kind"] in {"agent", "answer", "owner"}
    assert main(["welcome", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Welcome to SaberOps")
    assert "SaberOps will never" in text and "What you can say next" in text


def test_turn_on_opens_with_the_introduction_only_on_a_fresh_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from saberops.mcp.evidence import EvidenceRoots
    from saberops.mcp.ledger import EventLedger
    from saberops.mcp.protocol import McpServer
    from saberops.mcp.tools import ServerContext

    context = ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path / ".orch",)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
    )
    server = McpServer(context, start_active=False)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    turned_on = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "saberops_on"}}
    )
    assert turned_on is not None
    on = turned_on["result"]["content"][0]["text"]
    assert on.startswith("Orchestrator: SaberOps is on.")
    assert "subscriptions you already pay for" in on
    assert "start_walkthrough" in on
    assert "preflight" in on

    server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "saberops_off"}}
    )
    import saberops.walkthrough as walkthrough_module

    monkeypatch.setattr(walkthrough_module, "store_has_job", lambda db_path=None: True)
    server2 = McpServer(context, start_active=False)
    server2.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server2.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    turned_on_again = server2.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "saberops_on"}}
    )
    assert turned_on_again is not None
    on_again = turned_on_again["result"]["content"][0]["text"]
    assert "subscriptions you already pay for" not in on_again
    assert "preflight" in on_again


# --------------------------------------------------------------------------- #
# The tiny first job, to owner acceptance (lifecycle rig)
# --------------------------------------------------------------------------- #


@pytest.mark.packaged_routing_default
def test_first_job_from_the_walkthrough_reaches_owner_accept(
    rig: Any, tmp_path: Path
) -> None:
    """Green preflight, one tiny job, the owner accepts with the printed command."""
    from saberops.cli import main
    from saberops.jobs.service import get_job, submit_job
    from saberops.models import RunStatus
    from saberops.preflight import collect_preflight

    # Finish setup by following the walkthrough's own steps, exactly as the
    # conversation would: answer steps take the owner's value, agent steps run.
    process: subprocess.Popen[bytes] | None = None
    log = tmp_path / "scheduler.log"
    try:
        for _ in range(20):
            walk = build_walkthrough(rig.repo)
            step = walk.next_step
            if step is None:
                break
            assert step.command, step.key
            if step.key == "scheduler":
                process = _start_scheduler(step.command, log)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not [f for f in collect_preflight(rig.repo) if f.severity == "FAIL"]:
                        break
                    assert process.poll() is None, log.read_text()
                    time.sleep(0.2)
                else:
                    pytest.fail("preflight never turned green: " + log.read_text())
                continue
            for argv in _owner_args(_with_answer(step.command)):
                assert main(argv) == 0, (step.key, argv)

        walk = build_walkthrough(rig.repo)
        assert walk.ready is True, [s for s in walk.steps if not s.done]
        assert walk.needed is True and walk.first_job_done is False

        job = walk.as_dict()["sample_first_job"]
        db = rig.db()
        submitted = submit_job(
            db,
            repo=rig.repo,
            goal=job["goal"],
            acceptance=job["acceptance"],
            idempotency_key="f25-first-job",
        )
        assert submitted.created is True
        deadline = time.monotonic() + 30
        row = None
        while time.monotonic() < deadline:
            row = get_job(db, submitted.job.job_id)
            if row is not None and row.current_run_id is not None:
                break
            time.sleep(0.2)
        assert row is not None and row.current_run_id is not None, log.read_text()
        run = rig.wait_for_run(row.current_run_id, timeout=30.0)
        assert run.status is RunStatus.COMPLETED, run.result_summary

        # Exactly the command the walkthrough tells the owner to use.
        assert main(["accept", run.id]) == 0
        assert build_walkthrough(rig.repo).needed is False
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
