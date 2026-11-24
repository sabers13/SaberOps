"""Always-on real-process lifecycle rig tests (Phase 1 A6).

Exercises the end-to-end SaberOps path from a fresh empty store and toy Git
repository using the fake worker/reviewer CLI shims on ``PATH``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

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
from saberops.jobs.models import Difficulty, JobRequirements
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.tools import _catch_up
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
from saberops.preflight import Finding
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
    # A fresh setup needs one build route and one reviewer: no second model
    # role and no required strong-start setting (ADR-0005 §3).
    assert tuple(role for role, _ in snap.role_chains) == ("BUILD",)
    assert snap.strong_start == ()
    worker_routes = [
        f"{c.provider}/{c.model}" for group in snap.groups_for("BUILD") for c in group
    ]
    assert WORKER_CANDIDATE_CODEX in worker_routes
    assert WORKER_CANDIDATE_OPENCODE in worker_routes

    rev_status = rig.service().get_reviewer_status()
    assert rev_status["selected_binding_id"] == REVIEWER_BINDING_ID
    assert rev_status["status"] == "RESOLVED"

    settings = load_project_settings(rig.repo)
    assert settings.gate_command == rig.gate_command


def test_discovered_reviewer_quota_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.preflight import collect_preflight

    rig = build_lifecycle_rig(
        tmp_path, monkeypatch, reviewer_pool="discovered-pool:claude-code-subscription"
    )
    findings = collect_preflight(rig.repo)
    assert any(
        finding.severity == "OK"
        and "anthropic/discovered-pool:claude-code-subscription quota: available"
        in finding.message
        for finding in findings
    )
    _run_supervised_and_accept(rig, task="Exercise discovered reviewer quota")


@pytest.mark.parametrize("remaining", [None, 0])
def test_discovered_reviewer_quota_refuses_missing_or_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining: int | None
) -> None:
    rig = build_lifecycle_rig(
        tmp_path, monkeypatch, reviewer_pool="discovered-pool:claude-code-subscription",
        reviewer_remaining=remaining,
    )
    with pytest.raises(AssertionError, match="Required independent review"):
        _run_supervised_and_accept(rig, task="Exercise refused reviewer quota")


def test_discovered_reviewer_respects_existing_shared_pool_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    rig.db().set_quota("anthropic", "claude-pro", 0.0)
    with pytest.raises(AssertionError, match="Required independent review"):
        _run_supervised_and_accept(rig, task="Exercise shared reviewer quota")


_CODEX_REVIEWER_BINDING_ID = (
    "discovered-binding:reviewer:codex-chatgpt-account:gpt-5.6-terra"
)
_CODEX_REVIEWER_POOL = "discovered-pool:codex-chatgpt-account"


def _codex_reviewer_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    """Build the rig with a codex reviewer on its discovered binding pool."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    rig.service().set_reviewer_binding(_CODEX_REVIEWER_BINDING_ID)
    return rig


def _quota_line(findings: list[Finding], pool: str) -> str:
    prefix = f"codex/{pool} quota:"
    return next(finding.message for finding in findings if finding.message.startswith(prefix))


def _reviewer_admission(rig: RigEnvironment) -> Any:
    """Ask the dispatch admission path about the codex reviewer binding."""
    from datetime import UTC, datetime

    from saberops.capacity.admission import admit
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.models import WorkerCandidate

    moment = datetime.now(UTC)
    return admit(
        WorkerCandidate("codex", "gpt-5.6-terra"),
        now=moment,
        policy=load_control_plane_policy(),
        quota_states=effective_quota_states(rig.db(), run_probes=False, now=moment),
        binding_pool_id=_CODEX_REVIEWER_POOL,
        explicit_override=True,
        activate_overage=True,
    )


def test_discovered_codex_reviewer_pool_agrees_with_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-member packaged plan still governs its provider's binding pool.

    A7 regression: ``chatgpt-plus`` lists ``codex`` plus an opencode route, so
    the binding pool kept no policy; preflight called a 41%-remaining pool
    "missing, stale or blocked" while admission admitted it.  Both now resolve
    the packaging plan that names the provider.
    """
    from datetime import UTC, datetime, timedelta

    from saberops.cli import main as cli_main
    from saberops.preflight import collect_preflight

    rig = _codex_reviewer_rig(tmp_path, monkeypatch)
    reset_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    assert (
        cli_main(
            [
                "quota",
                "set",
                "codex",
                _CODEX_REVIEWER_POOL,
                "--remaining",
                "41",
                "--reset-at",
                reset_at,
            ]
        )
        == 0
    )
    findings = collect_preflight(rig.repo)
    line = _quota_line(findings, _CODEX_REVIEWER_POOL)
    assert line.startswith(f"codex/{_CODEX_REVIEWER_POOL} quota: available"), line
    decision = _reviewer_admission(rig)
    assert decision.admitted is True, decision.reason


def test_discovered_codex_reviewer_pool_below_red_line_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from saberops.cli import main as cli_main
    from saberops.preflight import collect_preflight

    rig = _codex_reviewer_rig(tmp_path, monkeypatch)
    reset_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    assert (
        cli_main(
            [
                "quota",
                "set",
                "codex",
                _CODEX_REVIEWER_POOL,
                "--remaining",
                "5",
                "--reset-at",
                reset_at,
            ]
        )
        == 0
    )
    findings = collect_preflight(rig.repo)
    line = _quota_line(findings, _CODEX_REVIEWER_POOL)
    assert "below red line (5% < 15%)" in line, line
    decision = _reviewer_admission(rig)
    assert decision.admitted is False
    assert decision.reason is not None and decision.reason.startswith("RED_LINE")


def test_discovered_codex_reviewer_pool_stale_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from saberops.preflight import collect_preflight

    rig = _codex_reviewer_rig(tmp_path, monkeypatch)
    observed_at = datetime.now(UTC) - timedelta(days=2)
    reset_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    rig.db().set_quota(
        "codex",
        _CODEX_REVIEWER_POOL,
        41.0,
        reset_at=reset_at,
        now=observed_at,
    )
    findings = collect_preflight(rig.repo)
    line = _quota_line(findings, _CODEX_REVIEWER_POOL)
    assert line.startswith(f"codex/{_CODEX_REVIEWER_POOL} quota: stale (age 2d)"), line
    decision = _reviewer_admission(rig)
    assert decision.admitted is False
    assert decision.reason == "QUOTA_EVIDENCE_REQUIRED"


def test_discovered_opencode_reviewer_pool_agrees_with_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-route glob in a packaged plan still governs its discovered pool.

    A7 regression: OpenCode's packaged plans express membership as
    ``opencode/opencode-go/*`` (not the bare provider), so a discovered
    OpenCode reviewer pool resolved no policy and preflight called its fresh
    41% evidence "stale (age 0s)" while admission refused it.  One plan matches
    the candidate route, so it governs the discovered pool exactly as it
    governs the packaged one.
    """
    from datetime import UTC, datetime, timedelta

    from saberops.capacity.admission import admit
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.cli import main as cli_main
    from saberops.control_plane.binding_store import load_owner_bindings
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.models import WorkerCandidate
    from saberops.preflight import collect_preflight

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    owner = load_owner_bindings()
    binding = next(
        entry
        for entry in owner.registry.bindings
        if entry.provider == "opencode" and entry.binding_role.value == "CHECK"
    )
    rig.service().set_reviewer_binding(binding.binding_id)
    reset_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    assert (
        cli_main(
            [
                "quota",
                "set",
                "opencode",
                binding.quota_pool_id,
                "--remaining",
                "41",
                "--reset-at",
                reset_at,
            ]
        )
        == 0
    )

    findings = collect_preflight(rig.repo)
    prefix = f"opencode/{binding.quota_pool_id} quota:"
    line = next(finding.message for finding in findings if finding.message.startswith(prefix))
    assert line.startswith(f"{prefix} available"), line
    assert line.count("(age ") == 1, line

    moment = datetime.now(UTC)
    decision = admit(
        WorkerCandidate(binding.provider, binding.model),
        now=moment,
        policy=load_control_plane_policy(),
        quota_states=effective_quota_states(rig.db(), run_probes=False, now=moment),
        binding_pool_id=binding.quota_pool_id,
        explicit_override=True,
        activate_overage=True,
    )
    assert decision.admitted is True, decision.reason


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

    run = rig.wait_for_run(job.current_run_id, timeout=90.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True
    assert _git(rig.repo, "rev-parse", "HEAD") == accepted.final_sha
    assert accepted.final_sha != rig.base_sha
    assert (rig.repo / "fake_worker_output.txt").is_file()


#: The rig's build chain is codex/gpt-5.6-terra plus opencode; this
#: strong-start-only candidate is not on it.
STRONG_START_ONLY = "codex/gpt-5.6-sol"


def _configure_strong_start_only_chain() -> None:
    """Add a strong-start setting distinct from the build chain (public writer)."""
    from saberops.routing_config import load_effective_routing_payload, save_user_routing

    payload = load_effective_routing_payload()
    payload["strong_start"] = [{"routes": [STRONG_START_ONLY]}]
    save_user_routing(payload)


def _dispatch_one_job(rig: RigEnvironment, requirements: JobRequirements) -> list[Any]:
    db = rig.db()
    submitted = submit_job(
        db,
        repo=rig.repo,
        goal="Rig difficulty dispatch",
        requirements=requirements,
    )
    assert submitted.created is True
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=90.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    return db.get_attempts_for_run(run.id)


def test_rig_hard_difficulty_dispatches_the_strong_start_chain(
    rig: RigEnvironment,
) -> None:
    """Covers: H1 (a planner-recorded hard task starts on strong_start)."""
    _configure_strong_start_only_chain()
    attempts = _dispatch_one_job(rig, JobRequirements(difficulty=Difficulty.HARD))
    assert attempts and (attempts[-1].provider, attempts[-1].model) == (
        "codex",
        "gpt-5.6-sol",
    )


def test_rig_routine_and_missing_difficulty_stay_on_the_build_chain(
    rig: RigEnvironment,
) -> None:
    """Covers: H1 (routine and missing difficulty never take the strong start)."""
    _configure_strong_start_only_chain()
    for requirements in (
        JobRequirements(difficulty=Difficulty.ROUTINE),
        JobRequirements(),
    ):
        attempts = _dispatch_one_job(rig, requirements)
        assert attempts
        assert (attempts[-1].provider, attempts[-1].model) != ("codex", "gpt-5.6-sol")


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
            # Test-only stall bounds, not product timeouts: the run is awaited
            # below at 90 s, and 10 s per phase was sized for an idle host
            # (H29, so-testprof §3.1).
            worker_timeout=60.0,
            gate_timeout=60.0,
            review_enabled=True,
            review_timeout=60.0,
        )
    )
    supervisor = RunSupervisor(db=db)
    started = supervisor.start_supervised_run(config)
    run = rig.wait_for_run(started.run_id, timeout=90.0)
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


def test_fake_cli_version_probe_has_no_side_effects(rig: RigEnvironment) -> None:
    """A ``<cli> --version`` probe prints a version and never commits work.

    SaberOps reads the installed CLI version before dispatch.  A real CLI
    answers and exits; the fake must not treat the probe as a work request
    or tests would commit into whatever directory pytest ran from.
    """
    before = _git(rig.repo, "rev-parse", "HEAD")
    for name in ("codex", "opencode", "claude"):
        result = subprocess.run(
            [str(rig.bin_dir / name), "--version"],
            cwd=rig.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (name, result.stderr)
        assert name in result.stdout
    assert _git(rig.repo, "rev-parse", "HEAD") == before
    assert not (rig.repo / "fake_worker_output.txt").exists()


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
            stage=DispatchStage.REVIEW, role=DispatchRole.CHECK, tier=Tier.T1,
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


def _run_to_terminal(rig: RigEnvironment, *, task: str, max_attempts: int = 3) -> Run:
    """Start one supervised run from the rig's project settings and await it."""
    db = rig.db()
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            max_attempts=max_attempts,
            worker_timeout=60.0,
            gate_timeout=60.0,
            review_enabled=True,
            review_timeout=60.0,
        )
    )
    started = RunSupervisor(db=db).start_supervised_run(config)
    return rig.wait_for_run(started.run_id, timeout=90.0)


def _catch_up_context(rig: RigEnvironment) -> Any:
    """MCP context over the rig's isolated store (read-only catch_up lookup)."""
    from saberops.mcp.evidence import EvidenceRoots
    from saberops.mcp.lease import OrchestratorLease
    from saberops.mcp.ledger import EventLedger
    from saberops.mcp.tools import ServerContext

    state_dir = rig.state_home / "saberops"
    return ServerContext(
        db_path=rig.db().db_path,
        questions_store=state_dir / "questions.json",
        config_ledger_path=state_dir / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root,)),
        ledger=EventLedger(state_dir / "events.jsonl"),
        lease=OrchestratorLease(state_dir / "lease.json"),
    )


def _dispatch_submitted_job(rig: RigEnvironment) -> tuple[str, str]:
    """Submit a job and dispatch it the way the scheduler does; return (job_id, run_id)."""
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Continue on another subscription")
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1, tick
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    return submitted.job.job_id, job.current_run_id


def test_quota_exhausted_attempt_continues_on_another_binding_from_checkpoint(
    rig: RigEnvironment,
) -> None:
    """F4: a scheduler-dispatched job continues on another binding after quota exhaustion."""
    db = rig.db()
    rig.set_worker_mode("commit", per_harness={"codex": "partial_quota"})

    job_id, run_id = _dispatch_submitted_job(rig)
    run = rig.wait_for_run(run_id, timeout=60.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary

    attempts = db.get_attempts_for_run(run.id)
    assert [attempt.provider for attempt in attempts[:2]] == ["codex", "opencode"], (
        [(a.provider, a.status.value) for a in attempts]
    )
    first, second = attempts[0], attempts[1]
    assert first.status is AttemptStatus.FAILED
    assert first.interruption_reason == WorkerInterruptionReason.QUOTA_EXHAUSTED.value

    # The partial work survived as a checkpoint on the first attempt's own branch.
    checkpoint = db.get_worker_checkpoint_for_attempt(run.id, first.id)
    assert checkpoint is not None, "no checkpoint recorded for the quota-exhausted attempt"
    assert checkpoint.reason == WorkerInterruptionReason.QUOTA_EXHAUSTED.value
    assert checkpoint.checkpoint_sha != rig.base_sha
    assert "fake_worker_output.txt" in checkpoint.changed_paths
    assert _git(Path(first.worktree_path), "rev-parse", "HEAD") == checkpoint.checkpoint_sha

    # The replacement attempt inherited the checkpoint and did not lose the work.
    assert second.base_sha == checkpoint.checkpoint_sha
    inherited = (
        Path(second.worktree_path) / "fake_worker_output.txt"
    ).read_text(encoding="utf-8")
    assert "codex:" in inherited and "partial-work" in inherited
    assert "opencode:" in inherited

    # The switch is visible in catch_up from a read-only lookup.
    catch_up = _catch_up(_catch_up_context(rig), {"job_id": job_id})
    assert [row["provider"] for row in catch_up["usage"]["attempts"]] == [
        "codex",
        "opencode",
    ]
    switch = catch_up["switch"]
    assert switch is not None
    assert switch["reason"] == WorkerInterruptionReason.QUOTA_EXHAUSTED.value
    assert switch["from"]["provider"] == "codex"
    assert switch["to"]["provider"] == "opencode"
    assert switch["different_provider"] is True
    assert switch["checkpoint_sha"] == checkpoint.checkpoint_sha
    assert switch["continued_from_checkpoint"] is True
    assert switch["work_location"] == first.worktree_path
    assert switch["work_location_preserved"] is True

    # The continued candidate still reaches owner acceptance unchanged.
    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True
    assert (rig.repo / "fake_worker_output.txt").is_file()


def test_every_route_exhausted_keeps_the_job_and_its_work(rig: RigEnvironment) -> None:
    """F4: with no admitted fallback the run stops visibly and keeps the work; it never loops."""
    db = rig.db()
    rig.set_worker_mode("partial_quota")

    job_id, run_id = _dispatch_submitted_job(rig)
    run = rig.wait_for_run(run_id, timeout=60.0)
    assert run.status is not RunStatus.COMPLETED, run.result_summary
    assert len(db.get_attempts_for_run(run.id)) <= 2

    catch_up = _catch_up(_catch_up_context(rig), {"job_id": job_id})
    assert catch_up["failure"] is not None
    assert catch_up["failure"]["work_location"]
    assert Path(catch_up["failure"]["work_location"]).parent.is_dir()
    assert catch_up["next_action"]["action"] in {"wait", "inspect"}



def test_opencode_standalone_resume_dispatches_on_the_rig(rig: RigEnvironment) -> None:
    """``opencode run --standalone`` still resumes a session on the real rig.

    The fake CLI receives both ``--standalone`` and ``--session`` and completes
    the turn, so the private-server switch does not break session resume.
    """
    res = OpenCodeAdapter().run(
        WorkerRequest(
            task="resume under standalone server",
            worktree_path=rig.repo,
            model="opencode-go/deepseek-v4-flash",
            native_resume_id="ses_fake_resume_001",
            timeout_seconds=20.0,
        )
    )
    assert res.status == AttemptStatus.SUCCESS, res.error
    output_file = rig.repo / "fake_worker_output.txt"
    assert output_file.is_file()
    assert "opencode:" in output_file.read_text(encoding="utf-8")


def test_report_shows_full_gate_receipt_from_fake_run(rig: RigEnvironment) -> None:
    from saberops.observability.run_report import build_run_report, render_report_text

    run, _ = _run_supervised_and_accept(rig, task="Exercise report gate evidence")
    db = rig.db()
    events = db.get_events_by_type(run.id, "full_gate_receipt")
    assert events
    assert events[0].payload is not None
    report = build_run_report(db, run.id)
    receipts = report["gate"]["receipts"]
    full = next(row for row in receipts if row["event_id"] == events[0].id)
    assert full["verdict"] == "PASS"
    assert isinstance(full["duration_seconds"], (int, float))
    assert full["candidate_sha"] == events[0].payload["candidate_sha"]
    text = render_report_text(report)
    assert "[PASS] full_gate_receipt" in text
    assert "no durable gate_receipt event persisted" not in text
