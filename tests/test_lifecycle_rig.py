"""Always-on real-process lifecycle rig tests (Phase 1 A6).

Exercises the end-to-end SaberOps path from a fresh empty store and toy Git
repository using the fake worker/reviewer CLI shims on ``PATH``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import (
    REVIEWER_BINDING_ID,
    WORKER_CANDIDATE_CODEX,
    WORKER_CANDIDATE_OPENCODE,
    AcceptanceFixture,
    RigEnvironment,
    build_acceptance_fixture,
    build_lifecycle_rig,
)

from saberops.accept import AcceptEngine
from saberops.candidate_lifecycle import project_candidate_state
from saberops.capacity.loop import run_scheduler_loop
from saberops.config import RunConfig
from saberops.control_plane import (
    freeze_control_plane_policy,
    load_control_plane_policy,
    load_owner_bindings,
    reviewer_candidate_for_dispatch,
)
from saberops.db import current_iso_timestamp
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    CandidateState,
    DispatchEvidence,
    DispatchOutcome,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerInterruptionReason,
    WorkerRequest,
)
from saberops.paths import owner_state_dir
from saberops.project_settings import load_project_settings
from saberops.provenance import mint_run_provenance
from saberops.review import ReviewEngine
from saberops.routing_config import load_effective_routing
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor
from saberops.workers.codex import CodexAdapter
from saberops.workers.opencode import OpenCodeAdapter
from saberops.workers.registry import AdapterRegistry

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


@pytest.fixture()
def acceptance_rig(rig: RigEnvironment) -> AcceptanceFixture:
    return build_acceptance_fixture(rig)


def test_rig_fresh_store_registers_two_providers_and_reviewer(rig: RigEnvironment) -> None:
    """Fresh-store setup registers two routable worker providers, reviewer, and gate."""
    snap = load_effective_routing()
    worker_routes = [
        f"{c.provider}/{c.model}" for group in snap.groups_for("WORKER") for c in group
    ]
    assert WORKER_CANDIDATE_CODEX in worker_routes
    assert WORKER_CANDIDATE_OPENCODE in worker_routes

    rev_status = rig.service().get_reviewer_status()
    assert rev_status["selected_binding_id"] == REVIEWER_BINDING_ID
    assert rev_status["status"] == "RESOLVED"

    settings = load_project_settings(rig.repo)
    assert settings.gate_command == rig.gate_command


@pytest.mark.xfail(
    reason=(
        "Scheduler _default_run_launcher creates pending Run without frozen "
        "control_plane_snapshot_json/routing_snapshot_json/provenance, causing "
        "adopted Orchestrator run to fail with posture_unverified (C1)."
    ),
    strict=True,
)
def test_lifecycle_happy_path_via_scheduler(rig: RigEnvironment) -> None:
    """Job submitted -> scheduler dispatches -> worker commits -> review -> owner accepts."""
    db = rig.db()
    submitted = submit_job(
        db,
        repo=rig.repo,
        goal="Add scheduler-driven feature line",
    )
    assert submitted.created is True

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None

    run = rig.wait_for_run(job.current_run_id, timeout=15.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True
    assert _git(rig.repo, "rev-parse", "HEAD") == accepted.final_sha
    assert accepted.final_sha != rig.base_sha
    assert (rig.repo / "fake_worker_output.txt").is_file()


def _run_supervised_and_accept(
    rig: RigEnvironment,
    *,
    task: str,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> tuple[Run, str]:
    db = rig.db()
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            provider_override=provider_override,
            model_override=model_override,
            max_attempts=1,
            worker_timeout=10.0,
            gate_timeout=10.0,
            review_enabled=True,
            review_timeout=10.0,
        )
    )
    supervisor = RunSupervisor(db=db)
    started = supervisor.start_supervised_run(config)
    run = rig.wait_for_run(started.run_id, timeout=30.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    reviews = db.get_reviews_for_run(run.id)
    assert reviews and reviews[-1].verdict == ReviewVerdict.PASS

    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True
    return run, accepted.final_sha


def test_lifecycle_rig_worker_gate_review_accept_end_to_end(rig: RigEnvironment) -> None:
    """Detached RunSupervisor -> fake codex -> gate -> fake claude -> accept."""
    run, final_sha = _run_supervised_and_accept(
        rig,
        task="Create feature artifact via codex",
    )
    assert _git(rig.repo, "rev-parse", "HEAD") == final_sha
    assert final_sha != rig.base_sha
    output_file = rig.repo / "fake_worker_output.txt"
    assert output_file.is_file()
    assert "codex:" in output_file.read_text(encoding="utf-8")
    attempts = rig.db().get_attempts_for_run(run.id)
    assert attempts and attempts[-1].provider == "codex"


def test_lifecycle_rig_second_provider_opencode_commits_and_accepts(rig: RigEnvironment) -> None:
    """Second routable provider (opencode) commits, passes review, and lands cleanly."""
    run, final_sha = _run_supervised_and_accept(
        rig,
        task="Create feature artifact via opencode",
        provider_override="opencode",
        model_override="opencode-go/deepseek-v4-flash",
    )
    assert _git(rig.repo, "rev-parse", "HEAD") == final_sha
    assert final_sha != rig.base_sha
    output_file = rig.repo / "fake_worker_output.txt"
    assert output_file.is_file()
    assert "opencode:" in output_file.read_text(encoding="utf-8")
    attempts = rig.db().get_attempts_for_run(run.id)
    assert attempts and attempts[-1].provider == "opencode"


def test_fake_reviewer_approves_and_rejects_on_instruction(rig: RigEnvironment) -> None:
    """Fake reviewer returns PASS or BLOCK according to owner/test instruction."""
    db = rig.db()
    task = "Evaluate fake reviewer switches"
    provenance = mint_run_provenance(rig.repo, task)
    run_id = provenance.run_id

    worktree_dir = rig.root / "wt_review"
    _git(rig.repo, "worktree", "add", "-b", f"orch/{run_id}/a1", str(worktree_dir), rig.base_sha)
    (worktree_dir / "reviewed.txt").write_text("reviewed change\n", encoding="utf-8")
    _git(worktree_dir, "add", "reviewed.txt")
    _git(worktree_dir, "commit", "-m", "candidate for review")
    cand_sha = _git(worktree_dir, "rev-parse", "HEAD")

    now = current_iso_timestamp()
    frozen_cp = freeze_control_plane_policy(load_control_plane_policy())
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
            control_plane_snapshot_json=frozen_cp.as_json(),
            control_plane_snapshot_digest=frozen_cp.digest,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(worktree_dir),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=cand_sha,
            created_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=rig.base_sha,
        )
    )

    reviewer_cand = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer_cand is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )

    rig.set_reviewer_verdict("PASS")
    pass_res = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=10.0)
    assert pass_res.verdict == ReviewVerdict.PASS
    assert pass_res.provider == "anthropic"

    rig.set_reviewer_verdict("BLOCK")
    block_res = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=10.0)
    assert block_res.verdict == ReviewVerdict.BLOCK
    assert block_res.provider == "anthropic"


@pytest.mark.parametrize(
    ("adapter_cls", "model"),
    [
        (CodexAdapter, "gpt-5.6-terra"),
        (OpenCodeAdapter, "opencode-go/deepseek-v4-flash"),
    ],
)
def test_fake_worker_switch_hang(
    rig: RigEnvironment,
    adapter_cls: type[CodexAdapter] | type[OpenCodeAdapter],
    model: str,
) -> None:
    """Hang switch sleeps without output until adapter timeout triggers."""
    rig.set_worker_mode("hang", hang_seconds=30.0)
    res = adapter_cls().run(
        WorkerRequest(
            task="hang switch check",
            worktree_path=rig.repo,
            model=model,
            timeout_seconds=0.3,
        )
    )
    assert res.status == AttemptStatus.TIMEOUT
    assert res.outcome == DispatchOutcome.TIMEOUT
    assert res.stdout == ""
    assert res.stderr == "" or "timed out" in res.stderr.lower()


@pytest.mark.parametrize(
    ("adapter_cls", "model"),
    [
        (CodexAdapter, "gpt-5.6-terra"),
        (OpenCodeAdapter, "opencode-go/deepseek-v4-flash"),
    ],
)
def test_fake_worker_switch_crash(
    rig: RigEnvironment,
    adapter_cls: type[CodexAdapter] | type[OpenCodeAdapter],
    model: str,
) -> None:
    """Crash switch exits non-zero mid-task without committing."""
    rig.set_worker_mode("crash", exit_code=7)
    res = adapter_cls().run(
        WorkerRequest(
            task="crash switch check",
            worktree_path=rig.repo,
            model=model,
            timeout_seconds=5.0,
        )
    )
    assert res.status == AttemptStatus.FAILED
    assert res.provider_exit_code == 7
    assert res.has_changes is False
    assert _git(rig.repo, "rev-parse", "HEAD") == rig.base_sha


@pytest.mark.parametrize(
    ("adapter_cls", "model"),
    [
        (CodexAdapter, "gpt-5.6-terra"),
        (OpenCodeAdapter, "opencode-go/deepseek-v4-flash"),
    ],
)
def test_fake_worker_switch_quota_exhausted(
    rig: RigEnvironment,
    adapter_cls: type[CodexAdapter] | type[OpenCodeAdapter],
    model: str,
) -> None:
    """Quota-exhaustion switch emits provider message recognized by exhaustion detector."""
    rig.set_worker_mode("quota_exhausted")
    res = adapter_cls().run(
        WorkerRequest(
            task="quota exhaustion switch check",
            worktree_path=rig.repo,
            model=model,
            timeout_seconds=5.0,
        )
    )
    assert res.status == AttemptStatus.FAILED
    assert res.interruption_reason is WorkerInterruptionReason.QUOTA_EXHAUSTED


def test_acceptance_fixture_refuses_unreviewed_replacement_b(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Replacement candidate B must be refused until B has its own passing review."""
    db = acceptance_rig.rig.db()
    assert acceptance_rig.candidate_a_sha != acceptance_rig.candidate_b_sha

    projection = project_candidate_state(db, acceptance_rig.run_id)
    assert projection.candidate_sha == acceptance_rig.candidate_b_sha
    assert projection.state is CandidateState.READY
    assert projection.verdict is None

    eligibility = AcceptEngine(db=db).check_accept_eligibility(acceptance_rig.run_id)
    assert eligibility.eligible is False
    assert any("historical only" in check.reason for check in eligibility.checks)
    with pytest.raises(RuntimeError, match="no candidate-linked review"):
        AcceptEngine(db=db).accept_run(acceptance_rig.run_id)
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha


def test_acceptance_fixture_qualifies_b_after_own_review(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Once replacement B has its own candidate-linked PASS review, B becomes REVIEWED."""
    db = acceptance_rig.rig.db()
    acceptance_rig.record_passing_review_for_b()

    projection = project_candidate_state(db, acceptance_rig.run_id)
    assert projection.candidate_sha == acceptance_rig.candidate_b_sha
    assert projection.state is CandidateState.REVIEWED
    assert projection.verdict is ReviewVerdict.PASS

    eligibility = AcceptEngine(db=db).check_accept_eligibility(acceptance_rig.run_id)
    assert eligibility.eligible is True
    result = AcceptEngine(db=db).accept_run(acceptance_rig.run_id)
    assert result.accepted and result.final_sha == acceptance_rig.candidate_b_sha


@pytest.mark.parametrize("verdict", [ReviewVerdict.PASS, ReviewVerdict.BLOCK])
@pytest.mark.parametrize("review_b", [False, True])
def test_unrelated_review_does_not_change_replacement_acceptance(
    acceptance_rig: AcceptanceFixture, verdict: ReviewVerdict, review_b: bool
) -> None:
    db = acceptance_rig.rig.db()
    if review_b:
        acceptance_rig.record_passing_review_for_b()
    review_id = f"{acceptance_rig.run_id}-later-a"
    now = current_iso_timestamp()
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"dispatch-{review_id}", run_id=acceptance_rig.run_id,
            association_id=review_id, review_id=review_id,
            stage=DispatchStage.REVIEW, role=DispatchRole.REVIEW, tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="anthropic", requested_model="reviewer",
            candidate_sha=acceptance_rig.candidate_a_sha, started_at=now, completed_at=now,
        )
    )
    db.create_review(ReviewResult(
        id=review_id, run_id=acceptance_rig.run_id, provider="anthropic",
        model="reviewer", verdict=verdict, summary="Later review of A", created_at=now,
    ))
    engine = AcceptEngine(db=db)
    assert engine.check_accept_eligibility(acceptance_rig.run_id).eligible is review_b
    if review_b:
        assert engine.accept_run(acceptance_rig.run_id).final_sha == acceptance_rig.candidate_b_sha
    else:
        with pytest.raises(RuntimeError, match="no candidate-linked review"):
            engine.accept_run(acceptance_rig.run_id)
        assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha
