"""H47: a resumed job finishes and the scheduler keeps running.

The lane's contract (ADR-0105):

* every resume path stamps the dispatching job id on the successor run;
* completion reconciliation advances a job whose current run is terminal, even
  when the successor run holds no capacity reservation;
* one stuck RUNNING job never holds the whole store, so other jobs still
  dispatch;
* a PENDING run that never produced an attempt (its owner already released) can
  still be cancelled by the owner.

These tests use a real durable store, a real toy repository and the real
service/CLI seams; only the detached execution launch is stubbed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_loop import _make_candidate, _make_policy, _make_routes

from saberops.capacity.ledger import record_owner_quota
from saberops.capacity.loop import acquire_lease, scheduler_tick
from saberops.config import resolve_run_config
from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import JobState
from saberops.jobs.service import cancel_job, submit_job
from saberops.jobs.store import get_job, mark_dispatched, reconcile_transition
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
from saberops.ownership import ProcessIdentity
from saberops.service import OrchestratorService
from saberops.supervisor import RunSupervisor

PROBE_TASK = "H47 resume probe"
T0 = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "h47@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "H47"], cwd=path, check=True)
    (path / "README.md").write_text("# h47\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_run(
    db: Database,
    repo: Path,
    run_id: str,
    status: RunStatus,
    *,
    with_contracts: bool = False,
) -> Run:
    config = resolve_run_config(
        task=PROBE_TASK,
        target_repo=repo,
        manual_tier=Tier.T1,
        routing_mode="manual",
        gate_command="true",
    )
    routing_json: str | None = None
    control_json: str | None = None
    control_digest: str | None = None
    if with_contracts:
        from saberops.control_plane import (
            freeze_control_plane_policy,
            load_control_plane_policy,
        )
        from saberops.routing_config import load_effective_routing, snapshot_to_payload

        created = current_iso_timestamp()
        routing_json = json.dumps(snapshot_to_payload(load_effective_routing(), created))
        frozen_control = freeze_control_plane_policy(load_control_plane_policy())
        control_json = frozen_control.as_json()
        control_digest = frozen_control.digest
    run = Run(
        id=run_id,
        task=PROBE_TASK,
        target_repo=str(repo),
        base_commit=_head(repo),
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=status,
        created_at=current_iso_timestamp(),
        config_json=json.dumps(config.as_dict()),
        routing_snapshot_json=routing_json,
        control_plane_snapshot_json=control_json,
        control_plane_snapshot_digest=control_digest,
    )
    db.create_run(run)
    return run


def _failed_job(db: Database, repo: Path, run_id: str) -> str:
    """A FAILED job whose one run is ``run_id`` (the ordinary dispatch chain)."""
    submitted = submit_job(db, repo=repo, goal=PROBE_TASK)
    job_id = submitted.job.job_id
    mark_dispatched(db, job_id, run_id)
    reconcile_transition(db, job_id, to=JobState.FAILED, failure_reason="RUN_FAILED")
    return job_id


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    launches: list[str] = []
    monkeypatch.setattr(
        RunSupervisor, "_launch", lambda self, run_id, config: launches.append(run_id)
    )
    return launches


def _commit_candidate(db: Database, repo: Path, run_id: str) -> str:
    candidate = _head(repo)
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="h47-model",
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=current_iso_timestamp(),
        )
    )
    return candidate


@pytest.mark.parametrize("from_", ["scratch", "last_candidate"])
def test_resume_stamps_the_dispatching_job_on_the_successor_run(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, from_: str
) -> None:
    repo = _git_repo(isolated / f"link-{from_}")
    db = Database(isolated / f"link-{from_}.db")
    _seed_run(db, repo, "run_link_src", RunStatus.FAILED, with_contracts=True)
    if from_ == "last_candidate":
        _commit_candidate(db, repo, "run_link_src")
    job_id = _failed_job(db, repo, "run_link_src")
    _stub_launch(monkeypatch)

    payload = OrchestratorService(db=db).resume_job(job_id, from_=from_)

    successor = db.get_run(payload["run_id"])
    assert successor is not None
    assert successor.job_id == job_id
    job = get_job(db, job_id)
    assert job is not None and job.current_run_id == payload["run_id"]


def _scheduler_inputs(db: Database) -> tuple[Any, Any]:
    record_owner_quota(
        db, "codex", "chatgpt-plus", 100.0, None, source="OWNER", now=T0
    )
    routes = _make_routes(((_make_candidate("codex", "gpt-5"),),))
    policy = _make_policy(posture_now=T0)
    return routes, policy


@pytest.mark.parametrize("from_", ["scratch", "last_candidate"])
def test_completed_resumed_run_advances_its_job_and_the_next_tick_dispatches(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, from_: str
) -> None:
    repo = _git_repo(isolated / f"finish-{from_}")
    db = Database(isolated / f"finish-{from_}.db")
    _seed_run(db, repo, "run_finish_src", RunStatus.FAILED, with_contracts=True)
    if from_ == "last_candidate":
        _commit_candidate(db, repo, "run_finish_src")
    job_id = _failed_job(db, repo, "run_finish_src")
    _stub_launch(monkeypatch)

    payload = OrchestratorService(db=db).resume_job(job_id, from_=from_)
    successor = payload["run_id"]
    # The resumed run ends without ever holding a capacity reservation.
    db.update_run(
        successor,
        RunStatus.COMPLETED,
        completed_at=current_iso_timestamp(),
        result_summary="resumed candidate completed",
    )
    queued = submit_job(db, repo=repo, goal="independent queued work").job.job_id

    routes, policy = _scheduler_inputs(db)
    lease = acquire_lease(db, "h47_finish", ttl_seconds=300.0, now=T0)
    assert lease is not None
    launched: list[str] = []

    result = scheduler_tick(
        db,
        lease,
        now=T0,
        routes=routes,
        policy=policy,
        run_launcher=lambda job, decision, run_id: launched.append(job.job_id),
        probes={},
        clock_fn=lambda: T0,
    )

    assert result.reason != "legacy_running_without_reservation"
    assert result.legacy_running_jobs == ()
    finished = get_job(db, job_id)
    assert finished is not None and finished.state is JobState.SUCCEEDED
    assert queued in launched


def test_one_stuck_legacy_job_does_not_stop_other_dispatch(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "legacy-repo")
    db = Database(isolated / "legacy.db")
    _seed_run(db, repo, "run_legacy_src", RunStatus.FAILED)
    job_id = _failed_job(db, repo, "run_legacy_src")
    _stub_launch(monkeypatch)
    successor = OrchestratorService(db=db).resume_job(job_id, from_="scratch")["run_id"]
    # The exact stuck shape from H37: the job stayed RUNNING while its current
    # run is terminal and nothing reserved capacity for it.
    db.update_run(successor, RunStatus.FAILED, completed_at=current_iso_timestamp())
    queued = submit_job(db, repo=repo, goal="independent queued work").job.job_id

    routes, policy = _scheduler_inputs(db)
    lease = acquire_lease(db, "h47_legacy", ttl_seconds=300.0, now=T0)
    assert lease is not None
    launched: list[str] = []

    result = scheduler_tick(
        db,
        lease,
        now=T0,
        routes=routes,
        policy=policy,
        run_launcher=lambda job, decision, run_id: launched.append(job.job_id),
        probes={},
        clock_fn=lambda: T0,
    )

    assert result.legacy_running_jobs == ()
    assert result.dispatched >= 1
    assert queued in launched


def test_pending_run_without_attempt_can_be_cancelled(isolated: Path) -> None:
    repo = _git_repo(isolated / "cancel-repo")
    db = Database(isolated / "cancel.db")
    _seed_run(db, repo, "run_pending_shape", RunStatus.PENDING)
    job_id = submit_job(db, repo=repo, goal=PROBE_TASK).job.job_id
    mark_dispatched(db, job_id, "run_pending_shape")
    # The launch never produced an attempt, and its owner is already released.
    owner = db.claim_execution_owner(
        "run_pending_shape", identity=ProcessIdentity.for_self()
    )
    assert db.release_execution_owner(
        "run_pending_shape",
        owner_id=owner.owner_id,
        generation=owner.generation,
    )
    assert db.get_attempts_for_run("run_pending_shape") == []

    result = cancel_job(
        db, job_id, run_canceller=RunSupervisor(db=db).cancel
    )

    assert result.cancelled is True
    assert result.job.state is JobState.CANCELLED
    run = db.get_run("run_pending_shape")
    assert run is not None and run.status is RunStatus.FAILED
