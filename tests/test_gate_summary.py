"""The ``orch run`` summary must report the durable gate result, not the run status.

Regression for the A7 real run: the gate passed, required review then failed the
run closed, and the summary still printed ``Gate Passed : NO`` because it was
derived from the terminal run status.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.cli import _follow_run
from saberops.models import Run, RunStatus
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _run_supervised(rig: RigEnvironment, task: str) -> Run:
    config = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            max_attempts=1,
            worker_timeout=20.0,
            gate_timeout=20.0,
            review_enabled=True,
            review_timeout=20.0,
        )
    )
    started = RunSupervisor(db=rig.db()).start_supervised_run(config)
    return rig.wait_for_run(started.run_id, timeout=60.0)


def _summary(rig: RigEnvironment, run: Run, capsys: pytest.CaptureFixture[str]) -> str:
    _follow_run(rig.db(), run.id)
    return capsys.readouterr().out


def test_summary_reports_gate_passed_when_review_later_fails(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate passed, required review failed closed -> ``Gate Passed : YES``."""
    # The reviewer crashes, reproducing A7's REVIEW_EXECUTION_FAILED after a
    # passing gate.  The worker still commits and the gate still runs.
    rig.set_worker_mode("commit", per_harness={"claude": "crash"})
    run = _run_supervised(rig, "Add a subtract helper")

    assert run.status == RunStatus.FAILED
    checks = rig.db().get_checks_for_run(run.id)
    assert checks and checks[-1].passed is True

    out = _summary(rig, run, capsys)
    assert "Status      : FAILED" in out
    assert "Gate Passed : YES" in out


def test_summary_reports_gate_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing gate prints ``NO`` even though the run also failed."""
    failing_gate = f"{sys.executable} -c 'raise SystemExit(1)'"
    rig = build_lifecycle_rig(
        tmp_path,
        monkeypatch,
        gate_command=failing_gate,
    )
    run = _run_supervised(rig, "Add a subtract helper")

    assert run.status == RunStatus.FAILED
    checks = rig.db().get_checks_for_run(run.id)
    assert checks and checks[-1].passed is False

    out = _summary(rig, run, capsys)
    assert "Gate Passed : NO" in out


def test_summary_reports_gate_not_run_when_worker_fails_first(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that never reached the gate prints the not-run wording."""
    rig.set_worker_mode("crash")
    run = _run_supervised(rig, "Add a subtract helper")

    assert run.status == RunStatus.FAILED
    assert rig.db().get_checks_for_run(run.id) == []

    out = _summary(rig, run, capsys)
    assert "Gate Passed : NOT RUN" in out


def test_successful_attempt_records_candidate_changed_paths(
    rig: RigEnvironment,
) -> None:
    """A7: the durable attempt event names the files the candidate changed.

    The candidate commit lands before the gate runs, so reading
    ``git status`` afterwards always produced an empty list.  The durable
    event and the report must diff the committed candidate against its base.
    """
    from saberops.observability.run_report import build_run_report

    rig.set_worker_mode("commit")
    run = _run_supervised(rig, "Add a subtract helper")
    assert run.status == RunStatus.COMPLETED

    succeeded = [
        event
        for event in rig.db().get_events(run.id, limit=10000)
        if event.event_type == "attempt_succeeded"
    ]
    assert succeeded
    payload = succeeded[-1].payload
    assert payload is not None
    assert payload["changed_paths"] == ["fake_worker_output.txt"]

    report = build_run_report(rig.db(), run.id)
    assert report["workspace"]["changed_paths"] == ["fake_worker_output.txt"]
    assert report["workspace"]["cleanup_state"] == "NOT_CLEANED"


def test_recover_summary_reports_durable_gate_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A7: ``orch recover`` must not re-derive the gate line from run status.

    445f9b5 fixed the run/follow summaries but left this status-derived print,
    so a failed run with a passing gate still printed ``Gate Passed : NO``.
    """
    from types import SimpleNamespace

    from saberops import cli
    from saberops.db import Database
    from saberops.models import (
        Attempt,
        AttemptStatus,
        CheckResult,
        Run,
        RunResult,
        Tier,
    )
    from saberops.service import OrchestratorService

    db_path = tmp_path / "recover.db"
    db = Database(db_path)
    db.create_run(
        Run(
            id="run_recover",
            task="task",
            target_repo=str(tmp_path),
            base_commit="base",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.FAILED,
            created_at="2026-11-21T01:00:00+00:00",
            completed_at="2026-11-21T01:01:00+00:00",
            final_attempt_id="run_recover_a1",
            config_json="{}",
        )
    )
    db.create_attempt(
        Attempt(
            id="run_recover_a1",
            run_id="run_recover",
            attempt_number=1,
            provider="codex",
            model="gpt-6-luna",
            worktree_path=str(tmp_path / "wt"),
            branch_name="attempt-1",
            status=AttemptStatus.SUCCESS,
            commit_sha="candidate-sha",
            created_at="2026-11-21T01:00:00+00:00",
        )
    )
    db.create_check(
        CheckResult(
            id="check_recover",
            attempt_id="run_recover_a1",
            gate_command="true",
            exit_code=0,
            stdout="",
            stderr="",
            passed=True,
            skipped=False,
            created_at="2026-11-21T01:00:30+00:00",
        )
    )
    monkeypatch.setattr(
        cli,
        "parse_owner_reservation",
        lambda: SimpleNamespace(owner_id="owner", generation=1),
    )
    monkeypatch.setattr(
        Database,
        "claim_execution_owner",
        lambda self, *args, **kwargs: SimpleNamespace(owner_id="owner", generation=1),
    )
    monkeypatch.setattr(
        OrchestratorService,
        "recover_sync",
        lambda self, run_id, config=None: RunResult(
            run_id=run_id,
            passed=False,
            status=RunStatus.FAILED,
            summary="required review failed after a passing gate",
            target_repo=str(tmp_path),
            base_commit="base",
        ),
    )

    assert cli.main(["recover", "run_recover", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "Status      : FAILED" in out
    assert "Gate Passed : YES" in out
