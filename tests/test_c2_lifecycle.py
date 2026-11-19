"""Real process recovery exercises using the isolated lifecycle rig."""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from lifecycle_rig import build_lifecycle_rig

from saberops.capacity.loop import run_scheduler_loop
from saberops.jobs import service
from saberops.jobs.models import Job
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.tools import ServerContext, _get_job, _list_jobs
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import HealthState, ManagerState
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.paths import owner_state_dir
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default


# Setup steps are not the product bound: dispatch can be deferred while the
# host-wide memory ledger is spent by concurrent test runs.  Only the
# post-kill final-state waits keep the lane's 60-second acceptance limit.
_SETUP_TIMEOUT = 180


def _wait(predicate: Callable[[], bool], timeout: float = 55) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    pytest.fail(
        f"condition {predicate.__name__} did not become true within {timeout:g}s"
    )


@pytest.mark.parametrize("case", ["worker", "owner", "retry"])
def test_killed_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    rig.set_worker_mode("hang", hang_seconds=120)
    if case != "retry":
        original = service.job_dispatch_spec

        def single_attempt(job: Job) -> service.JobDispatchSpec:
            spec = original(job)
            return replace(spec, request=replace(spec.request, max_attempts=1))

        monkeypatch.setattr(service, "job_dispatch_spec", single_attempt)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="test process death").job
    readiness = ReadinessService.for_owner_state(owner_state_dir())

    def tick() -> None:
        result = run_scheduler_loop(db, once=True, readiness_service=readiness)
        assert result is not None and result.ran

    def dispatched() -> bool:
        tick()
        current = get_job(db, submitted.job_id)
        return bool(current and current.current_run_id)

    _wait(dispatched, _SETUP_TIMEOUT)
    job = get_job(db, submitted.job_id)
    assert job is not None and job.current_run_id
    run_id = job.current_run_id
    owner_identity = worker_identity = None
    try:
        def started() -> bool:
            owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
            sup = db.get_supervision(run_id)
            return bool(owner and sup and sup.worker_pid and sup.worker_identity
                        and sup.current_attempt_id)

        _wait(started, _SETUP_TIMEOUT)
        owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
        sup = db.get_supervision(run_id)
        assert owner and sup and sup.worker_pid and sup.worker_identity
        owner_identity = owner.identity
        worker_identity = ProcessIdentity.from_json(sup.worker_identity)
        assert owner_identity is not None and worker_identity is not None
        first_attempt = db.get_attempts_for_run(run_id)[-1]
        worktree = Path(first_attempt.worktree_path)
        assert worktree.exists()
        marker = worktree / "preserved-partial-work.txt"
        marker.write_text("unfinished work\n", encoding="utf-8")
        if case == "worker":
            assert db.update_supervision_health(
                run_id, health_state=HealthState.STALLED, health_reason="no output",
                manager_state=ManagerState.ATTENTION_REQUIRED,
            )
            stalled = service.job_status_view(db, job)
            assert stalled["activity"] == "needs_attention"
            assert stalled["reason"] == "no output"
            assert get_job(db, job.job_id).state.value == "RUNNING"  # type: ignore[union-attr]
            assert db.update_supervision_health(
                run_id, health_state=HealthState.HEALTHY, health_reason="active",
            )
            assert service.job_status_view(db, job)["activity"] == "running"
        os.kill(owner_identity.pid if case == "owner" else worker_identity.pid, signal.SIGKILL)

        if case == "owner":
            def attention() -> bool:
                tick()  # a fresh scheduler simulates restart on every poll
                current = get_job(db, job.job_id)
                assert current is not None
                return bool(service.job_status_view(db, current)["activity"] == "needs_attention")

            _wait(attention)
            context = ServerContext(db_path=Path(db.db_path))
            assert _get_job(context, {"job_id": job.job_id})["job"]["activity"] == (
                "needs_attention"
            )
            assert _list_jobs(context, {})["jobs"][0]["activity"] == "needs_attention"
            assert probe_liveness(worker_identity) is Liveness.LIVE
            os.kill(worker_identity.pid, signal.SIGKILL)
        if case == "retry":
            def retrying() -> bool:
                tick()
                current = get_job(db, job.job_id)
                assert current is not None
                view = service.job_status_view(db, current)
                return (view["activity"] == "retrying" and len(db.get_attempts_for_run(run_id)) >= 2
                        and bool(view["last_failed_attempt"]))

            _wait(retrying, _SETUP_TIMEOUT)
        else:
            def failed() -> bool:
                tick()
                current = get_job(db, job.job_id)
                return current is not None and current.state.value == "FAILED"

            _wait(failed)
            current = get_job(db, job.job_id)
            assert current is not None
            view = service.job_status_view(db, current)
            assert view["work_location"] == str(worktree)
            assert view["reason"]
        assert marker.read_text(encoding="utf-8") == "unfinished work\n"
    finally:
        sup = db.get_supervision(run_id)
        identities = [owner_identity, worker_identity]
        if sup and sup.worker_identity:
            current_identity = ProcessIdentity.from_json(sup.worker_identity)
            if current_identity is not None:
                identities.append(current_identity)
        for identity in identities:
            if identity and probe_liveness(identity) is Liveness.LIVE:
                try:
                    os.kill(identity.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
