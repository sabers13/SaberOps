"""H36 wave 0: a fresh install reaches a first accepted job from setup alone.

The test starts from an empty owner store and a clean toy repository, then
follows only the first-run walkthrough the setup skill serves
(``start_walkthrough``, generated live from ``preflight``) plus the ``preflight``
tool itself.  Every command it runs is copied from that output; the only values
it supplies are the two the walkthrough marks as the owner's own answers: the
project check command and a quota reading.

It then submits the first job through the MCP ``submit_job`` tool the skill
names, lets the scheduler the walkthrough started run it in fake mode, and
accepts the reviewed candidate.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from lifecycle_rig import _install_shim, create_toy_repo

from saberops.accept import AcceptEngine
from saberops.cli import main as cli_main
from saberops.db import Database
from saberops.jobs.service import get_job
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.preflight import collect_preflight
from saberops.walkthrough import build_walkthrough

pytestmark = [
    pytest.mark.packaged_routing_default,
    pytest.mark.slow,
]

#: The two answers the walkthrough asks the owner for in this test.  The check
#: command only proves the worker's file exists, so no vendor is involved.
CHECK_COMMAND = (
    f"{sys.executable} -c 'import pathlib; assert pathlib.Path(\"app.py\").is_file()'"
)
QUOTA_PERCENT = "80"
#: A leftover placeholder the walkthrough itself supplies rather than the owner.
CHECK_COMMAND_PLACEHOLDER = "__CHECK_COMMAND__"
ANSWER_PLACEHOLDERS = {
    "<number from 0 to 100>": QUOTA_PERCENT,
    "<percent>": QUOTA_PERCENT,
}

JOB_GOAL = "Add a short greeting to the toy repository"
JOB_ACCEPTANCE = "the check command passes and fake_worker_output.txt exists"

SETUP_TIMEOUT_SECONDS = 120.0


@dataclass
class FreshInstallStore:
    """An empty owner store, a clean toy repository, and fake CLI shims."""

    root: Path
    home_dir: Path
    state_home: Path
    config_home: Path
    cache_home: Path
    data_home: Path
    bin_dir: Path
    repo: Path
    base_sha: str

    @property
    def state_dir(self) -> Path:
        return self.state_home / "saberops"

    def db(self) -> Database:
        return Database()


def _build_fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FreshInstallStore:
    """Create the environment a clean checkout installs into: nothing else."""
    home_dir = tmp_path / "home"
    state_home = tmp_path / "state"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    data_home = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    for directory in (home_dir, state_home, config_home, cache_home, data_home, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    # The test host is not the owner's machine; host memory pressure from other
    # lanes must not park this lane's first job, and the host disk floor belongs
    # to the host rather than to the setup flow under test.
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    monkeypatch.setenv("SABEROPS_GATE_MIN_FREE_GB", "0")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in (
        "SABEROPS_PROFILE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "ORCH_READINESS_STORE",
        "ORCH_EXECUTABLE",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ORCH_FAKE_WORKER_MODE",
        "ORCH_FAKE_WORKER_HANG_SECONDS",
        "ORCH_FAKE_WORKER_EXIT_CODE",
        "ORCH_FAKE_WORKER_FILE",
        "ORCH_FAKE_WORKER_CONTENT",
        "ORCH_FAKE_REVIEWER_VERDICT",
    ):
        monkeypatch.delenv(var, raising=False)

    state_dir = state_home / "saberops"
    state_dir.mkdir(parents=True, exist_ok=True)
    for cli_name in ("codex", "opencode", "claude"):
        _install_shim(bin_dir, cli_name, state_dir)

    repo, base_sha = create_toy_repo(tmp_path)
    # The deterministic fake worker commits and the fake reviewer passes.
    (state_dir / "fake-worker-mode.json").write_text(
        json.dumps({"mode": "commit"}), encoding="utf-8"
    )
    (state_dir / "fake-reviewer-mode.json").write_text(
        json.dumps({"verdict": "PASS"}), encoding="utf-8"
    )
    return FreshInstallStore(
        root=tmp_path,
        home_dir=home_dir,
        state_home=state_home,
        config_home=config_home,
        cache_home=cache_home,
        data_home=data_home,
        bin_dir=bin_dir,
        repo=repo,
        base_sha=base_sha,
    )


def _walkthrough_argv(command: str) -> list[list[str]]:
    """Turn one walkthrough command into the argv list(s) it names.

    The walkthrough renders exact shell commands, including one ``&&`` chain.
    A placeholder that is the owner's answer is filled with this test's answer;
    nothing else is rewritten.
    """
    prepared = command.replace("'<check command>'", CHECK_COMMAND_PLACEHOLDER)
    prepared = prepared.replace("<check command>", CHECK_COMMAND_PLACEHOLDER)
    for placeholder, answer in ANSWER_PLACEHOLDERS.items():
        prepared = prepared.replace(placeholder, answer)
    segments: list[list[str]] = []
    for segment in prepared.split(" && "):
        argv = shlex.split(segment)
        argv = [CHECK_COMMAND if part == CHECK_COMMAND_PLACEHOLDER else part for part in argv]
        assert argv[:1] == ["orch"], f"the walkthrough printed a non-orch command: {command}"
        segments.append(argv[1:])
    return segments


def _run_walkthrough_until_ready(store: FreshInstallStore) -> tuple[list[str], list[str]]:
    """Follow the live walkthrough one step at a time until preflight is green."""
    executed: list[str] = []
    keys: list[str] = []
    walkthrough = build_walkthrough(store.repo)
    assert not walkthrough.ready, "a fresh store must not start ready"
    assert walkthrough.next_step is not None, walkthrough
    for _ in range(30):
        step = walkthrough.next_step
        if step is None:
            break
        assert step.command, f"step {step.key} has no command to follow"
        for argv in _walkthrough_argv(step.command):
            assert argv, step.command
            if argv[0] == "scheduler":
                # The walkthrough names ``orch scheduler start``; run exactly
                # that.  Most steps are short commands; this one stays running.
                assert argv == ["scheduler", "start"], argv
                assert cli_main(argv) == 0
            else:
                assert cli_main(argv) == 0, (step.key, step.command, argv)
            executed.append(" ".join(argv))
        keys.append(step.key)
        walkthrough = build_walkthrough(store.repo)
    assert walkthrough.ready, [
        (step.key, step.command) for step in walkthrough.steps if not step.done
    ]
    return executed, keys


def _stop_background_scheduler() -> None:
    from saberops.capacity.loop import stop_scheduler

    stop_scheduler(Database())


def _wait_for_terminal(db: Database, job_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(db, job_id)
        assert job is not None
        if job.state.value in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return
        time.sleep(0.05)
    job = get_job(db, job_id)
    raise AssertionError(f"job did not reach a final state: {job.state if job else None}")


def test_fresh_install_reaches_a_green_preflight_and_first_accepted_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _build_fresh_store(tmp_path, monkeypatch)
    try:
        assert not (store.state_dir / "orchestrator.db").exists()
        executed, keys = _run_walkthrough_until_ready(store)
        assert executed, "the walkthrough must have taken at least one step"
        assert {"build_route", "reviewer_route", "check_command", "scheduler"} <= set(keys), (
            keys
        )

        # The planner-visible preflight agrees with the walkthrough.
        db = store.db()
        lease = OrchestratorLease(store.state_dir / "mcp-orchestrator.lock", holder="fresh-install")
        context = ServerContext(
            db_path=db.db_path,
            questions_store=store.state_dir / "questions.json",
            config_ledger_path=store.state_dir / "config-ledger.jsonl",
            evidence=EvidenceRoots(roots=(store.root,)),
            ledger=EventLedger(store.state_dir / "events.jsonl"),
            lease=lease,
        )
        registry = build_default_registry(context)
        reported = registry.call("preflight", {"repo": str(store.repo)})
        assert reported["ready"] is True, reported
        assert [f for f in reported["findings"] if f["severity"] == "FAIL"] == []
        assert [f for f in collect_preflight(store.repo) if f.severity == "FAIL"] == []

        # The first job is submitted through the MCP tool the skill names.
        assert lease.acquire(), "the fresh store must accept the planner's lease"
        submitted = registry.call(
            "submit_job",
            {
                "repo": str(store.repo),
                "goal": JOB_GOAL,
                "acceptance": JOB_ACCEPTANCE,
                "idempotency_key": "h36-fresh-install-first-job",
            },
        )
        assert submitted["created"] is True, submitted

        _wait_for_terminal(db, submitted["job_id"], SETUP_TIMEOUT_SECONDS)
        job = get_job(db, submitted["job_id"])
        assert job is not None and job.state.value == "SUCCEEDED", (
            None if job is None else (job.state, job.failure_reason)
        )
        assert job.current_run_id is not None

        accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
        assert accepted.accepted, accepted
        assert accepted.final_sha
        assert (store.repo / "fake_worker_output.txt").is_file()
    finally:
        _stop_background_scheduler()


def test_walkthrough_commands_only_name_the_setup_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard the parser above: the walkthrough never prints a shell escape."""
    store = _build_fresh_store(tmp_path, monkeypatch)
    walkthrough = build_walkthrough(store.repo)
    allowed = {"access", "posture", "routing", "reviewer", "project", "quota", "scheduler"}
    seen: set[str] = set()
    for step in walkthrough.steps:
        if not step.command:
            continue
        for argv in _walkthrough_argv(step.command):
            seen.add(argv[0])
    assert seen <= allowed, seen - allowed
