"""Phase 1 C4 tests: concurrent multi-provider jobs and two-sibling stale-base drill."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.capacity.scheduler import DispatchDecision
from saberops.config import RunConfig
from saberops.jobs.models import Job, JobRequirements
from saberops.jobs.service import job_dispatch_spec, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.paths import owner_state_dir
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def test_scheduler_dispatches_two_jobs_concurrently_on_two_providers(
    rig: RigEnvironment,
) -> None:
    """Two admitted jobs routed to codex and opencode run concurrently via scheduler."""
    db = rig.db()
    sub1 = submit_job(
        db,
        repo=rig.repo,
        goal="Create codex artifact",
        idempotency_key="c4-concurrent-codex",
        requirements=JobRequirements(model_family_exclude=("deepseek",)),
    )
    sub2 = submit_job(
        db,
        repo=rig.repo,
        goal="Create opencode artifact",
        idempotency_key="c4-concurrent-opencode",
        requirements=JobRequirements(model_family_include=("deepseek",)),
    )
    assert sub1.created and sub2.created

    launched: list[tuple[str, str, str]] = []
    supervisor = RunSupervisor(db=db)

    def _supervised_launcher(job: Job, decision: DispatchDecision, _run_id: str) -> str:
        spec = job_dispatch_spec(job)
        config: RunConfig = resolve_effective_run_config(
            RunRequest(
                task=spec.request.task,
                repo_path=rig.repo,
                provider_override=decision.candidate.provider,
                model_override=decision.candidate.model,
                max_attempts=1,
                # Generous test-only budgets: the full suite runs these two
                # fake-worker runs while other tests and lanes load the host.
                worker_timeout=60.0,
                gate_timeout=60.0,
                review_enabled=True,
                review_timeout=60.0,
            )
        )
        started = supervisor.start_supervised_run(config)
        # Verify any previously launched run is still active while the second starts.
        for prev_run_id, _, _ in launched:
            prev_run = db.get_run(prev_run_id)
            assert prev_run is not None
            assert prev_run.status in (RunStatus.PENDING, RunStatus.RUNNING)
        launched.append((started.run_id, decision.candidate.provider, decision.candidate.model))
        return started.run_id

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(
        db,
        once=True,
        readiness_service=readiness,
        run_launcher=_supervised_launcher,
    )
    assert tick is not None and tick.ran is True
    assert tick.dispatched == 2
    assert len(launched) == 2
    assert {provider for _, provider, _ in launched} == {"codex", "opencode"}

    runs = [rig.wait_for_run(run_id, timeout=120.0) for run_id, _, _ in launched]
    assert all(run.status == RunStatus.COMPLETED for run in runs)

    providers_executed = set()
    for run in runs:
        attempts = db.get_attempts_for_run(run.id)
        assert attempts
        providers_executed.add(attempts[-1].provider)
    assert providers_executed == {"codex", "opencode"}


def test_two_sibling_jobs_from_same_base_stale_second_preserves_work(
    rig: RigEnvironment,
) -> None:
    """Accepting sibling 1 makes sibling 2 report stale base while preserving work & evidence."""
    db = rig.db()

    def _run_sibling(goal: str, target_file: str, provider: str, model: str) -> str:
        rig.set_worker_mode("commit", target_file=target_file, content=f"{goal}\n")
        config: RunConfig = resolve_effective_run_config(
            RunRequest(
                task=goal,
                repo_path=rig.repo,
                provider_override=provider,
                model_override=model,
                max_attempts=1,
                # Test-only stall bounds (H29): a sibling job is awaited at
                # 90 s below; 15 s per phase was sized for an idle host.
                worker_timeout=60.0,
                gate_timeout=60.0,
                review_enabled=True,
                review_timeout=60.0,
            )
        )
        started = RunSupervisor(db=db).start_supervised_run(config)
        run = rig.wait_for_run(started.run_id, timeout=90.0)
        assert run.status == RunStatus.COMPLETED, run.result_summary
        return run.id

    run1_id = _run_sibling(
        "Sibling one change",
        "sibling_one.txt",
        "codex",
        "gpt-5.6-terra",
    )
    run2_id = _run_sibling(
        "Sibling two change",
        "sibling_two.txt",
        "opencode",
        "opencode-go/deepseek-v4-flash",
    )

    accept_engine = AcceptEngine(db=db)
    accepted1 = accept_engine.accept_run(run1_id)
    assert accepted1.accepted is True
    new_head = _git(rig.repo, "rev-parse", "HEAD")
    assert new_head == accepted1.final_sha
    assert new_head != rig.base_sha
    assert (rig.repo / "sibling_one.txt").is_file()

    # Sibling 2 now has a stale base: eligibility and accept both refuse cleanly.
    eligibility2 = accept_engine.check_accept_eligibility(run2_id)
    assert eligibility2.eligible is False
    assert any(
        check.key == "target_head" and not check.passed() for check in eligibility2.checks
    ), eligibility2.checks

    with pytest.raises(RuntimeError, match="has moved from recorded base commit"):
        accept_engine.accept_run(run2_id)

    # Target repo remains at sibling 1's accepted commit; sibling 2's work & evidence stay intact.
    assert _git(rig.repo, "rev-parse", "HEAD") == new_head
    assert not (rig.repo / "sibling_two.txt").exists()
    attempts2 = db.get_attempts_for_run(run2_id)
    assert attempts2 and attempts2[-1].commit_sha is not None
    assert Path(attempts2[-1].worktree_path).is_dir()
    assert db.get_checks_for_run(run2_id)
    assert db.get_reviews_for_run(run2_id)
