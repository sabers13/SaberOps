"""H53: a corrected candidate can be accepted (ADR-0111).

A candidate produced by a continuation (``resume_job from=last_candidate``,
or the reject-with-a-note flow) starts its first attempt at the saved
candidate of the run it continues, not at the successor run's own base
commit.  Acceptance must be able to prove that lineage from durable
evidence instead of refusing every continuation candidate.

The tests use a real durable store, a real toy repository and the real
service/CLI seams; only the detached execution launch is stubbed.  The
post-integration gate is the real (trivial) ``true`` command, so a
successful case really fast-forwards the target repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from saberops.cli import main
from saberops.config import resolve_run_config
from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import JobState
from saberops.jobs.service import submit_job
from saberops.jobs.store import (
    get_job,
    mark_dispatched,
    pending_steering_notes_for_run,
    reconcile_transition,
)
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    Run,
    RunStatus,
    Tier,
)
from saberops.service import OrchestratorService
from saberops.steering import record_steering_delivery
from saberops.supervisor import RunSupervisor

PROBE_TASK = "H53 accept lineage probe"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "h53@example.com")
    _git(path, "config", "user.name", "H53")
    (path / "README.md").write_text("# h53\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _candidate_commit(
    repo: Path, *, base: str, branch: str, name: str
) -> str:
    """Create a real candidate commit descending from ``base`` off to the side."""
    _git(repo, "checkout", "-b", branch, base)
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-m", name)
    sha = _head(repo)
    _git(repo, "checkout", "main")
    return sha


def _frozen_contracts() -> tuple[str, str, str]:
    from saberops.control_plane import (
        freeze_control_plane_policy,
        load_control_plane_policy,
    )
    from saberops.routing_config import load_effective_routing, snapshot_to_payload

    created = current_iso_timestamp()
    routing_json = json.dumps(snapshot_to_payload(load_effective_routing(), created))
    frozen_control = freeze_control_plane_policy(load_control_plane_policy())
    return routing_json, frozen_control.as_json(), frozen_control.digest


def _seed_parent_run(
    db: Database,
    repo: Path,
    run_id: str,
    status: RunStatus,
    *,
    candidate: str,
    base: str,
    gate_command: str = "true",
) -> Run:
    config = resolve_run_config(
        task=PROBE_TASK,
        target_repo=repo,
        manual_tier=Tier.T1,
        routing_mode="manual",
        gate_command=gate_command,
        review_disabled=True,
        publish_candidate=False,
    )
    routing_json, control_json, control_digest = _frozen_contracts()
    run = Run(
        id=run_id,
        task=PROBE_TASK,
        target_repo=str(repo),
        base_commit=base,
        tier=Tier.T1,
        training_allowed=True,
        gate_command=gate_command,
        status=status,
        created_at=current_iso_timestamp(),
        config_json=json.dumps(config.as_dict()),
        routing_snapshot_json=routing_json,
        control_plane_snapshot_json=control_json,
        control_plane_snapshot_digest=control_digest,
    )
    db.create_run(run)
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="h53-model",
            worktree_path=str(repo.parent / f"{run_id}-a1-wt"),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            base_sha=base,
            created_at=current_iso_timestamp(),
        )
    )
    db.create_check(
        CheckResult(
            id=f"{run_id}_a1-check",
            attempt_id=f"{run_id}_a1",
            gate_command=gate_command,
            exit_code=0,
            stdout="gate ok",
            stderr="",
            passed=True,
            created_at=current_iso_timestamp(),
        )
    )
    return run


def _job_for_run(
    db: Database, repo: Path, run_id: str, *, state: JobState
) -> str:
    submitted = submit_job(db, repo=repo, goal=PROBE_TASK)
    job_id = submitted.job.job_id
    mark_dispatched(db, job_id, run_id)
    reconcile_transition(
        db,
        job_id,
        to=state,
        failure_reason="RUN_FAILED" if state is JobState.FAILED else None,
    )
    return job_id


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    launches: list[str] = []
    monkeypatch.setattr(
        RunSupervisor, "_launch", lambda self, run_id, config: launches.append(run_id)
    )
    return launches


def _finish_continuation(
    db: Database,
    repo: Path,
    *,
    run_id: str,
    source_candidate: str,
    candidate: str,
    branch: str,
) -> str:
    """Record the successor's gate-passed attempt, as the worker path would."""
    attempt_id = f"{run_id}_a1"
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="h53-model",
            worktree_path=str(repo),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            base_sha=source_candidate,
            created_at=current_iso_timestamp(),
        )
    )
    notes = pending_steering_notes_for_run(db, run_id)
    record_steering_delivery(db, notes, run_id=run_id, attempt_id=attempt_id)
    db.update_run(
        run_id,
        RunStatus.COMPLETED,
        completed_at=current_iso_timestamp(),
        final_attempt_id=attempt_id,
        result_summary="continuation candidate ready",
    )
    return attempt_id


# ---------------------------------------------------------------------------
# resume_job from=last_candidate
# ---------------------------------------------------------------------------


def test_accepts_a_candidate_from_resume_job_last_candidate(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(isolated / "last-candidate")
    db = Database(isolated / "last-candidate.db")
    parent_candidate = _candidate_commit(
        repo, base=base, branch="orch/run_parent/a1", name="parent"
    )
    _seed_parent_run(
        db,
        repo,
        "run_parent",
        RunStatus.FAILED,
        candidate=parent_candidate,
        base=base,
    )
    job_id = _job_for_run(db, repo, "run_parent", state=JobState.FAILED)
    _stub_launch(monkeypatch)
    service = OrchestratorService(db=db)

    resumed = service.resume_job(job_id, from_="last_candidate")
    child_id = resumed["run_id"]
    child = db.get_run(child_id)
    assert child is not None
    assert child.base_commit == base
    child_candidate = _candidate_commit(
        repo, base=parent_candidate, branch=f"orch/{child_id}/a1", name="child"
    )
    _finish_continuation(
        db,
        repo,
        run_id=child_id,
        source_candidate=parent_candidate,
        candidate=child_candidate,
        branch=f"orch/{child_id}/a1",
    )

    result = service.accept_run(child_id, gate_timeout=60.0)

    assert result.accepted is True
    assert result.final_sha == child_candidate
    assert _head(repo) == child_candidate
    job = get_job(db, job_id)
    assert job is not None and job.current_run_id == child_id


# ---------------------------------------------------------------------------
# resume_job from=scratch (fresh attempt keeps working)
# ---------------------------------------------------------------------------


def test_accepts_a_candidate_from_resume_job_scratch(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(isolated / "from-scratch")
    db = Database(isolated / "from-scratch.db")
    parent_candidate = _candidate_commit(
        repo, base=base, branch="orch/run_scratch_parent/a1", name="parent"
    )
    _seed_parent_run(
        db,
        repo,
        "run_scratch_parent",
        RunStatus.FAILED,
        candidate=parent_candidate,
        base=base,
    )
    job_id = _job_for_run(db, repo, "run_scratch_parent", state=JobState.FAILED)
    _stub_launch(monkeypatch)
    service = OrchestratorService(db=db)

    resumed = service.resume_job(job_id, from_="scratch")
    child_id = resumed["run_id"]
    child = db.get_run(child_id)
    assert child is not None and child.base_commit == base
    child_candidate = _candidate_commit(
        repo, base=base, branch=f"orch/{child_id}/a1", name="fresh"
    )
    _finish_continuation(
        db,
        repo,
        run_id=child_id,
        source_candidate=base,
        candidate=child_candidate,
        branch=f"orch/{child_id}/a1",
    )

    result = service.accept_run(child_id, gate_timeout=60.0)

    assert result.accepted is True
    assert _head(repo) == child_candidate


# ---------------------------------------------------------------------------
# reject the candidate with a note, then accept the corrected candidate
# ---------------------------------------------------------------------------


def test_accepts_a_candidate_after_reject_with_a_note(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(isolated / "reject-note")
    db = Database(isolated / "reject-note.db")
    parent_candidate = _candidate_commit(
        repo, base=base, branch="orch/run_reject/a1", name="parent"
    )
    _seed_parent_run(
        db,
        repo,
        "run_reject",
        RunStatus.COMPLETED,
        candidate=parent_candidate,
        base=base,
    )
    job_id = _job_for_run(db, repo, "run_reject", state=JobState.SUCCEEDED)
    _stub_launch(monkeypatch)
    service = OrchestratorService(db=db)

    rejected = service.reject_job(job_id, note="Keep the public API unchanged.")
    child_id = rejected["run_id"]
    child = db.get_run(child_id)
    assert child is not None and child.base_commit == base
    child_candidate = _candidate_commit(
        repo, base=parent_candidate, branch=f"orch/{child_id}/a1", name="corrected"
    )
    _finish_continuation(
        db,
        repo,
        run_id=child_id,
        source_candidate=parent_candidate,
        candidate=child_candidate,
        branch=f"orch/{child_id}/a1",
    )

    result = service.accept_run(child_id, gate_timeout=60.0)

    assert result.accepted is True
    assert _head(repo) == child_candidate


# ---------------------------------------------------------------------------
# Unrelated bases are still refused
# ---------------------------------------------------------------------------


def test_refuses_a_continuation_candidate_with_an_unrelated_base(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(isolated / "unrelated")
    db = Database(isolated / "unrelated.db")
    parent_candidate = _candidate_commit(
        repo, base=base, branch="orch/run_unrelated/a1", name="parent"
    )
    _seed_parent_run(
        db,
        repo,
        "run_unrelated",
        RunStatus.FAILED,
        candidate=parent_candidate,
        base=base,
    )
    job_id = _job_for_run(db, repo, "run_unrelated", state=JobState.FAILED)
    _stub_launch(monkeypatch)
    service = OrchestratorService(db=db)

    resumed = service.resume_job(job_id, from_="last_candidate")
    child_id = resumed["run_id"]
    # A real commit that is in no way part of this job's lineage.
    unrelated = _candidate_commit(
        repo, base=base, branch="orch/stranger/a1", name="stranger"
    )
    unrelated_head = _candidate_commit(
        repo, base=unrelated, branch="orch/stranger/a2", name="stranger2"
    )
    _finish_continuation(
        db,
        repo,
        run_id=child_id,
        source_candidate=unrelated_head,
        candidate=unrelated_head,
        branch="orch/stranger/a2",
    )

    with pytest.raises(RuntimeError, match="not the authorized base"):
        service.accept_run(child_id, gate_timeout=60.0)
    assert _head(repo) == base


# ---------------------------------------------------------------------------
# orch accept exits non-zero when integration fails (N9)
# ---------------------------------------------------------------------------


def test_cli_accept_exits_non_zero_when_the_gate_fails(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base = _make_repo(isolated / "gate-failure")
    db = Database(isolated / "gate-failure.db")
    candidate = _candidate_commit(
        repo, base=base, branch="orch/run_gate_fail/a1", name="candidate"
    )
    _seed_parent_run(
        db,
        repo,
        "run_gate_fail",
        RunStatus.COMPLETED,
        candidate=candidate,
        base=base,
        gate_command="false",
    )

    code = main(["accept", "run_gate_fail", "--db", str(db.db_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "Integration failed" in captured.err
    assert "INTEGRATION SUCCESSFUL" not in captured.out
    assert _head(repo) == base
