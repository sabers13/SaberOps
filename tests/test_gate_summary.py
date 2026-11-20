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
