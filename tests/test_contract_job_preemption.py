"""Cross-module crash-recovery contract: jobs + capacity + preemption + supervisor.

ADR-0002 D8 R1 (post-requeue capacity settlement) and R2 (interrupted
preemption preparation).  Hermetic by construction: every test uses an
isolated file-backed :class:`Database`, a real temporary local Git repository
(local ``git`` only, no remotes/network), synthetic durable quota evidence and
a frozen clock.  No provider, model, credential, billing, or owner-config
state is touched, and no paid path is reachable.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops import preemption
from saberops.capacity.ledger import PoolDraw, reserve
from saberops.capacity.loop import acquire_lease, scheduler_tick
from saberops.capacity.store import owner_connection
from saberops.db import Database
from saberops.git import GitManager
from saberops.jobs import service, store
from saberops.jobs.models import (
    PREEMPTION_LIMIT,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    ResumeFrom,
    WaitingReason,
)
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
from saberops.ownership import ProcessIdentity

_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
_QUOTA = "QUOTA_EXHAUSTED"


@pytest.fixture(autouse=True)
def _isolate_owner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate owner XDG so no machine config/routing leaks into the tests."""
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        directory = tmp_path / var.lower()
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(directory))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers (real Git, real SQLite, real durable receipts)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "crash@example.com"], path)
    _git(["config", "user.name", "crash"], path)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path)


def _db(tmp_path: Path, name: str = "crash.db") -> Database:
    return Database(tmp_path / name)


def _job(job_id: str, repo: str, base: str, *, preemptions: int = 0) -> Job:
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="owner",
        repo=repo,
        base_ref="HEAD",
        base_sha=base,
        goal="do the thing",
        acceptance=None,
        requirements=JobRequirements(role=JobRole.WORKER),
        priority=Priority.NORMAL,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        preemptions=preemptions,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
        request_digest="digest-" + job_id,
    )


def _dispatched(db: Database, repo: str, base: str, job_id: str, run_id: str) -> None:
    store.insert_job(db, _job(job_id, repo, base), now=_NOW)
    store.mark_dispatched(db, job_id, run_id, now=_NOW)
    db.create_run(
        Run(
            id=run_id,
            task="do the thing",
            target_repo=repo,
            base_commit=base,
            tier=Tier.T2,
            training_allowed=True,
            gate_command="make gate",
            status=RunStatus.RUNNING,
            created_at=_NOW.isoformat(),
            config_json="{}",
        )
    )


def _quota_evidence(db: Database, run_id: str) -> None:
    db.record_event(
        run_id,
        "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )
    db.record_event(
        run_id, "no_eligible_candidate", payload={"outcome": "NO_ELIGIBLE_CANDIDATE"}
    )


def _quota_attempt(db: Database, run_id: str, repo: str, status: AttemptStatus) -> None:
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="p",
            model="m",
            worktree_path=repo,
            branch_name=f"orch/{run_id}/a1",
            status=status,
            created_at=_NOW.isoformat(),
            completed_at=_NOW.isoformat(),
            interruption_reason=_QUOTA,
        )
    )


def _dead_identity() -> ProcessIdentity:
    """A provably DEAD incarnation (recorded boot id cannot be this boot)."""
    return ProcessIdentity(
        host=ProcessIdentity.for_self().host,
        boot_id="00000000-0000-0000-0000-000000000000",
        pid=2147483646,
        start_ticks=None,
        session_id=None,
        process_group_id=None,
    )


def _claim_dead_owner(db: Database, run_id: str) -> tuple[str, int]:
    """Claim the run for a provably dead generation; return its token."""
    owner = db.claim_execution_owner(run_id, identity=_dead_identity())
    return owner.owner_id, int(owner.generation)


def _seed_bound_decision(
    db: Database,
    run_id: str,
    job_id: str,
    *,
    owner_id: str,
    owner_generation: int,
    decision_ref: str = "test:bound",
) -> None:
    """Record an authentic current-protocol D8 decision receipt (setup-only)."""
    job = _job_for(db, job_id)
    decision = preemption.build_preemption_decision(
        db, run_id=run_id, job=job,
        owner_id=owner_id, owner_generation=owner_generation,
        decision_ref=decision_ref,
    )
    assert preemption.record_preparation_decision(db, decision)


def _job_for(db: Database, job_id: str) -> Job:
    job = store.get_job(db, job_id)
    assert job is not None
    return job


def _park_then_reopen_release_window(
    db: Database, repo: Path, base: str, *,
    job_id: str = "job1", run_id: str = "run_old",
) -> None:
    """Produce the exact R1 crash window deterministically.

    The coordinator is run for real under the authentic executor token, so the
    durable evidence (bound decision, verified intent, capsule, anchor ref, one
    count) is genuine; then, setup-only, the old run's settlement rows are put
    back to RESERVED to re-create the precise state "committed requeue, release
    not yet committed" that the crash leaves behind.  Nothing else about the
    transaction is synthesized.
    """
    _dispatched(db, str(repo), base, job_id, run_id)
    _quota_attempt(db, run_id, str(repo), AttemptStatus.FAILED)
    _quota_evidence(db, run_id)
    owner_id, owner_generation = _claim_dead_owner(db, run_id)
    reserve(
        db,
        run_id=run_id,
        job_id=job_id,
        route="p/m",
        draws=[PoolDraw("pool1", "5h")],
        size_bucket="M",
        now=_NOW,
    )
    out = preemption.complete_preemption(
        db, run_id=run_id, repo_path=repo, base_sha=base,
        owner_id=owner_id, owner_generation=owner_generation,
        decision_ref="test:r1-window", now=_NOW,
    )
    assert out.owned and out.detail == "preempted"
    job = _job_for(db, job_id)
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    with owner_connection(db) as conn:
        conn.execute(
            "UPDATE capacity_reservations SET state = 'RESERVED', settled_at = NULL"
            " WHERE run_id = ?",
            (run_id,),
        )
        conn.commit()
    assert _reservation_states(db, run_id) == ["RESERVED"]



def _reservation_states(db: Database, run_id: str) -> list[str]:
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [str(r["state"]) for r in rows]


def _assert_released(db: Database, run_id: str) -> None:
    states = _reservation_states(db, run_id)
    assert states and all(s == "RELEASED" for s in states), states


# ---------------------------------------------------------------------------
# R1 -- post-requeue settlement is durable and independent of job state
# ---------------------------------------------------------------------------


def test_restart_settles_after_committed_requeue_before_release(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R1 -- crash after requeue commit, before release.

    The old run is conclusively terminal and the job already left RUNNING, so
    no job/run scan can find the debt: only durable owner-storage debt can.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _park_then_reopen_release_window(db, repo, base)

    settlement = service.settle_preemption_debts(db, now=_NOW)
    assert settlement.released == 1
    _assert_released(db, "run_old")

    # Idempotent: a second restart changes nothing and never double-counts.
    assert service.settle_preemption_debts(db, now=_NOW).released == 0
    job = _job_for(db, "job1")
    assert job.preemptions == 1
    assert job.state == JobState.QUEUED
    requeued = [
        e for e in store.list_events(db, "job1") if e["kind"] == "requeued"
    ]
    assert len(requeued) == 1


def test_settlement_survives_relabel_terminal_job_and_newer_run(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R1 -- discovery ignores waiting label and job state."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _park_then_reopen_release_window(db, repo, base)

    # The waiting label is not a recovery key.
    store.set_waiting(db, "job1", WaitingReason.NO_CAPACITY, now=_NOW)
    assert _reservation_states(db, "run_old") == ["RESERVED"]
    assert service.settle_preemption_debts(db, now=_NOW).released == 1
    _assert_released(db, "run_old")

    # A newer run of the same job must be settled independently and untouched.
    _park_then_reopen_release_window(
        db, repo, base, job_id="job2", run_id="run_two"
    )
    store.mark_dispatched(db, "job2", "run_newer", now=_NOW)
    db.create_run(
        Run(
            id="run_newer",
            task="t",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T2,
            training_allowed=True,
            gate_command="make gate",
            status=RunStatus.RUNNING,
            created_at=_NOW.isoformat(),
            config_json="{}",
        )
    )
    reserve(
        db,
        run_id="run_newer",
        job_id="job2",
        route="p/m",
        draws=[PoolDraw("pool1", "5h")],
        size_bucket="M",
        now=_NOW,
    )
    assert service.settle_preemption_debts(db, now=_NOW).released == 1
    _assert_released(db, "run_two")
    # The live newer run keeps its reservation and ownership.
    assert _reservation_states(db, "run_newer") == ["RESERVED"]
    newer = _job_for(db, "job2")
    assert newer.state == JobState.RUNNING
    assert newer.current_run_id == "run_newer"
    assert newer.preemptions == 1


def test_settlement_is_fail_closed_on_unproven_or_live_runs(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R1 -- never free capacity without terminal proof."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)

    # A live RUNNING run with RESERVED rows is never settled.
    _dispatched(db, str(repo), base, "job_live", "run_live")
    reserve(
        db, run_id="run_live", job_id="job_live", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    settlement = service.settle_preemption_debts(db, now=_NOW)
    assert settlement.released == 0
    assert _reservation_states(db, "run_live") == ["RESERVED"]

    # A future crash-window row for the *same live generation* (RUNNING job on
    # that run) is retained: releasing it early would over-admit the pool.
    _park_then_reopen_release_window(db, repo, base, job_id="job1", run_id="run_old")
    store.mark_dispatched(db, "job1", "run_old2", now=_NOW)
    db.create_run(
        Run(
            id="run_old2", task="t", target_repo=str(repo), base_commit=base,
            tier=Tier.T2, training_allowed=True, gate_command="make gate",
            status=RunStatus.RUNNING, created_at=_NOW.isoformat(), config_json="{}",
        )
    )
    # The old generation already left RUNNING, so it settles; the new one holds.
    assert service.settle_preemption_debts(db, now=_NOW).released == 1
    _assert_released(db, "run_old")


def test_settlement_requires_a_durable_d8_receipt(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R1 -- an ordinary terminal run is never D8-settled."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    # Ordinary failure: terminal, but no preemption receipt at all.
    db.update_run(
        run_id="run_old", status=RunStatus.FAILED,
        completed_at=_NOW.isoformat(), result_summary="gate failed",
    )
    assert service.settle_preemption_debts(db, now=_NOW).released == 0
    assert _reservation_states(db, "run_old") == ["RESERVED"]


def test_settlement_lease_guard_blocks_a_stale_scheduler(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R1 -- settlement is fenced by the scheduler lease."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _park_then_reopen_release_window(db, repo, base)

    denied = service.settle_preemption_debts(db, lease_guard=lambda _conn: False, now=_NOW)
    assert denied.released == 0
    assert _reservation_states(db, "run_old") == ["RESERVED"]
    # The rightful holder completes it.
    settled = service.settle_preemption_debts(db, lease_guard=lambda _conn: True, now=_NOW)
    assert settled.released == 1
    _assert_released(db, "run_old")


def test_scheduler_tick_settles_old_debt_on_restart(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R1 -- the real restart tick settles stranded debt."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _park_then_reopen_release_window(db, repo, base)
    lease = acquire_lease(db, "instance_A", ttl_seconds=300.0, now=_NOW)
    assert lease is not None

    res = scheduler_tick(
        db,
        lease,
        now=_NOW,
        run_status=lambda rid: db.get_run(rid).status,  # type: ignore[union-attr]
        run_launcher=lambda *_a, **_k: None,
        probes={},
        clock_fn=lambda: _NOW,
    )
    assert res.ran is True
    assert res.preemption_settled == 1
    _assert_released(db, "run_old")
    assert _job_for(db, "job1").preemptions == 1


def test_limit_restart_settles_once_and_never_counts_sixth(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 -- the preemption bound is not escapable by restart."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    store.insert_job(db, _job("job1", str(repo), base, preemptions=5), now=_NOW)
    store.mark_dispatched(db, "job1", "run_old", now=_NOW)
    db.create_run(
        Run(
            id="run_old", task="t", target_repo=str(repo), base_commit=base,
            tier=Tier.T2, training_allowed=True, gate_command="make gate",
            status=RunStatus.RUNNING, created_at=_NOW.isoformat(), config_json="{}",
        )
    )
    _quota_evidence(db, "run_old")
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert out.owned and out.limit_reached
    job = _job_for(db, "job1")
    assert job.state == JobState.FAILED
    assert job.failure_reason == PREEMPTION_LIMIT
    assert job.preemptions == 5
    _assert_released(db, "run_old")
    # A restart finds nothing left to settle.
    assert service.settle_preemption_debts(db, now=_NOW).released == 0
    assert _job_for(db, "job1").preemptions == 5


# ---------------------------------------------------------------------------
# R2 -- supervisor finishes an interrupted preemption before orphaning
# ---------------------------------------------------------------------------


def _seed_running_preparation_crash(
    db: Database, repo: Path, base: str, *,
    live_attempt: bool = False, job_id: str = "job1", run_id: str = "run_old",
) -> None:
    """Crash after the durable decision receipt, before the WIP anchor (R2)."""
    _dispatched(db, str(repo), base, job_id, run_id)
    _quota_attempt(
        db, run_id, str(repo),
        AttemptStatus.RUNNING if live_attempt else AttemptStatus.FAILED,
    )
    _quota_evidence(db, run_id)
    owner_id, owner_generation = _claim_dead_owner(db, run_id)

    # Scoped patch: kill the protocol right after the decision receipt, before
    # any checkpoint/ref side effect.  A context manager keeps the autouse
    # env-isolation fixture's monkeypatches intact.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("saberops.preemption._resolve_anchor_sha", lambda *_a, **_k: None)
        out = preemption.complete_preemption(
            db, run_id=run_id, repo_path=repo, base_sha=base,
            owner_id=owner_id, owner_generation=owner_generation,
            decision_ref="test:prep-crash", now=_NOW,
        )
    assert not out.owned and out.detail == "no_anchor"
    # Durable, *authentic* D8 decision exists; the run is still RUNNING.
    decision = authenticate_bound_transaction(db, run_id, job_id, repo)
    assert decision == "preparing", decision
    assert db.get_run(run_id).status == RunStatus.RUNNING  # type: ignore[union-attr]
    assert _job_for(db, job_id).state == JobState.RUNNING


def authenticate_bound_transaction(db: Database, run_id: str, job_id: str, repo: Path) -> str:
    """Classify the durable transaction state via the production classifier."""
    job = _job_for(db, job_id)
    return preemption.classify_preemption_transaction(
        db, run_id=run_id, job=job, repo_path=repo
    ).state


def test_supervisor_recovers_interrupted_preparation_before_orphaning(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- dead-owner restart finishes the transaction."""
    from saberops.supervisor import RunSupervisor

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _seed_running_preparation_crash(db, repo, base)

    result = RunSupervisor(db=db).reconcile("run_old")
    assert result["action"] == "preempt_recovered", result
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.PREEMPTED
    job = _job_for(db, "job1")
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    assert job.resume_from is not None and job.resume_from.run_id == "run_old"
    # A real, verified direct anchor was created for the capsule's SHA.
    refs = GitManager.list_checkpoint_refs(repo, "job1")
    assert refs and job.resume_from.checkpoint_sha in set(refs.values())
    assert db.get_handoff(job.resume_from.handoff_id) is not None
    # Restarting again is a no-op: exactly one preemption.
    again = RunSupervisor(db=db).reconcile("run_old")
    assert again["reason"] == "terminal_run_immutable"
    assert _job_for(db, "job1").preemptions == 1
    assert len(db.get_events_by_type("run_old", "preemption_recovered")) == 1


def test_reconcile_preemptions_discovers_and_delegates_pending_transaction(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- the scheduler-facing discovery pass delegates."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _seed_running_preparation_crash(db, repo, base)

    seen: list[str] = []
    recon = service.reconcile_preemptions(
        db,
        run_status=lambda rid: db.get_run(rid).status,  # type: ignore[union-attr]
        recover=lambda rid: seen.append(rid),
        now=_NOW,
    )
    assert recon.pending == ("run_old",)
    assert seen == ["run_old"]


def test_supervisor_refuses_live_attempt_without_orphaning(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- a live attempt is not a recoverable crash."""
    from saberops.supervisor import RunSupervisor

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _seed_running_preparation_crash(db, repo, base, live_attempt=True)

    result = RunSupervisor(db=db).reconcile("run_old")
    assert result["action"] == "none", result
    assert str(result["reason"]).startswith("preemption_recovery_pending")
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    assert _job_for(db, "job1").preemptions == 0


def test_supervisor_does_not_infer_d8_from_quota_history_alone(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- no receipt means no invented preemption."""
    from saberops.supervisor import RunSupervisor

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    # Quota attempt + exhausted history, but NO durable D8 decision/intent.
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_attempt(db, "run_old", str(repo), AttemptStatus.FAILED)
    _quota_evidence(db, "run_old")
    db.claim_execution_owner("run_old", identity=_dead_identity())

    result = RunSupervisor(db=db).reconcile("run_old")
    assert result["action"] != "preempt_recovered"
    run = db.get_run("run_old")
    assert run is not None and run.status != RunStatus.PREEMPTED
    assert _job_for(db, "job1").preemptions == 0
    assert db.get_events_by_type("run_old", preemption.PREPARATION_EVENT) == []


def test_supervisor_blocks_legacy_intent_with_valid_artifacts(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- an unbound legacy intent is NOT D8 authority.

    The intent's SHA, direct anchor ref, capsule and quota-attempt evidence all
    genuinely exist, but there is no authentic bound ``preemption_preparing``
    decision: a valid SHA + capsule is not proof that D8 took this job's
    terminal outcome, so restart must retain WIP/job/charge and NOT preempt or
    orphan.
    """
    from saberops.handoff import create_handoffs
    from saberops.supervisor import RunSupervisor

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_attempt(db, "run_old", str(repo), AttemptStatus.FAILED)
    _quota_evidence(db, "run_old")
    db.claim_execution_owner("run_old", identity=_dead_identity())
    # Fully genuine artifacts, deliberately WITHOUT a bound decision receipt.
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    _, capsule = create_handoffs(db, "run_old")
    db.record_event(
        "run_old",
        "preemption_committed",
        payload={
            "job_id": "job1",
            "checkpoint_sha": base,
            "checkpoint_ref": "refs/saberops/checkpoints/job1/0",
            "handoff_id": capsule.id,
            "not_before": None,
            "preemptions_next": 1,
        },
    )
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    assert authenticate_bound_transaction(db, "run_old", "job1", repo) == "blocked"

    result = RunSupervisor(db=db).reconcile("run_old")

    assert result["action"] == "none", result
    assert str(result["reason"]).startswith("preemption_blocked")
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    job = _job_for(db, "job1")
    assert job.state == JobState.RUNNING
    assert job.preemptions == 0
    assert job.resume_from is None
    # A bounded, owner-facing diagnostic is recorded; nothing was mutated.
    blocked = db.get_events_by_type("run_old", "preemption_recovery_blocked")
    assert len(blocked) == 1
    # The charged reservation is retained.
    assert _reservation_states(db, "run_old") == ["RESERVED"]


def test_settlement_retains_charge_for_a_legacy_parked_run(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R1 -- no release on an unauthenticated receipt."""
    from saberops.handoff import create_handoffs

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_attempt(db, "run_old", str(repo), AttemptStatus.FAILED)
    _quota_evidence(db, "run_old")
    db.claim_execution_owner("run_old", identity=_dead_identity())
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    _, capsule = create_handoffs(db, "run_old")
    # Legacy parked state: PREEMPTED run + legacy intent, job requeued, with
    # NO authentic bound decision.
    db.record_event(
        "run_old",
        "preemption_committed",
        payload={
            "job_id": "job1",
            "checkpoint_sha": base,
            "checkpoint_ref": "refs/saberops/checkpoints/job1/0",
            "handoff_id": capsule.id,
            "not_before": None,
            "preemptions_next": 1,
        },
    )
    db.update_run(
        run_id="run_old", status=RunStatus.PREEMPTED,
        completed_at=_NOW.isoformat(), result_summary="legacy park",
    )
    store.requeue_exact(
        db, "job1", WaitingReason.PREEMPTED,
        expected_run_id="run_old", expected_preemptions=0,
        resume_from=ResumeFrom(
            run_id="run_old", checkpoint_sha=base, handoff_id=capsule.id
        ),
        now=_NOW,
    )
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )

    settlement = service.settle_preemption_debts(db, now=_NOW)
    assert settlement.released == 0
    assert _reservation_states(db, "run_old") == ["RESERVED"]
    assert _job_for(db, "job1").preemptions == 1


def test_supervisor_blocks_stale_owner_generation_decision(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R2 -- a decision bound to another generation is not authority."""
    from saberops.supervisor import RunSupervisor

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _seed_running_preparation_crash(db, repo, base)
    # A newer execution generation claimed the run after the crash: the old
    # decision can no longer be authenticated and must not be promoted.
    db.claim_execution_owner("run_old", identity=_dead_identity())
    assert authenticate_bound_transaction(db, "run_old", "job1", repo) == "blocked"

    result = RunSupervisor(db=db).reconcile("run_old")

    assert result["action"] == "none", result
    assert str(result["reason"]).startswith("preemption_blocked")
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    assert _job_for(db, "job1").preemptions == 0


def test_recovery_refuses_unfenced_terminalization(
    tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 R2 -- crash recovery never takes the unfenced path."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _seed_running_preparation_crash(db, repo, base)
    job = _job_for(db, "job1")
    # Authentic decision, but the recovery caller supplies no owner token.
    outcome = preemption.finish_interrupted_preemption(
        db, run_id="run_old", job=job, repo_path=repo,
        owner_id=None, owner_generation=None, now=_NOW,
    )
    assert not outcome.owned
    assert outcome.detail in ("fencing_required", "fenced_terminalize_refused"), outcome.detail
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    assert _job_for(db, "job1").preemptions == 0


def test_limit_tail_is_fenced_against_a_newer_generation(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 -- the limit tail never rewrites another generation."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    store.insert_job(db, _job("job1", str(repo), base, preemptions=5), now=_NOW)
    store.mark_dispatched(db, "job1", "run_old", now=_NOW)
    db.create_run(
        Run(
            id="run_old", task="t", target_repo=str(repo), base_commit=base,
            tier=Tier.T2, training_allowed=True, gate_command="make gate",
            status=RunStatus.RUNNING, created_at=_NOW.isoformat(), config_json="{}",
        )
    )
    _quota_evidence(db, "run_old")
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    # The durable owner row belongs to a DIFFERENT (newer) generation.
    owner = db.claim_execution_owner("run_old", identity=_dead_identity())

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base,
        owner_id="stale-owner", owner_generation=owner.generation + 7,
        decision_ref="test:stale", now=_NOW,
    )

    assert not out.owned and out.detail == "limit_fence_refused", out.detail
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    job = _job_for(db, "job1")
    assert job.state == JobState.RUNNING
    assert job.preemptions == 5
    assert _reservation_states(db, "run_old") == ["RESERVED"]


@pytest.mark.parametrize("second_reason", ["relabel", "requeued_and_redispatched"])
def test_requeue_exact_rejects_aba_on_the_job_row(
    tmp_path: Path, second_reason: str
) -> None:
    """Covers: ADR-0002 D8 R2 -- stale recovery cannot requeue a newer run."""
    from saberops.jobs.store import JobConflictError

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    resume = ResumeFrom(run_id="run_old", checkpoint_sha=base, handoff_id="h1")

    if second_reason == "relabel":
        # A completed requeue, then a pure waiting-label change: the stale tail
        # must still refuse (its expected generation is gone).
        store.requeue_exact(
            db, "job1", WaitingReason.PREEMPTED,
            expected_run_id="run_old", expected_preemptions=0,
            resume_from=resume, now=_NOW,
        )
        store.set_waiting(db, "job1", WaitingReason.NO_CAPACITY, now=_NOW)
    else:
        store.requeue_exact(
            db, "job1", WaitingReason.PREEMPTED,
            expected_run_id="run_old", expected_preemptions=0,
            resume_from=resume, now=_NOW,
        )
        store.mark_dispatched(db, "job1", "run_new", now=_NOW)

    from saberops.jobs.store import JobTransitionError

    with pytest.raises((JobConflictError, JobTransitionError)):
        store.requeue_exact(
            db, "job1", WaitingReason.PREEMPTED,
            expected_run_id="run_old", expected_preemptions=0,
            resume_from=resume, now=_NOW,
        )
    job = _job_for(db, "job1")
    # Never a second count, never the newer run's ownership cleared.
    assert job.preemptions == 1
    if second_reason == "requeued_and_redispatched":
        assert job.state == JobState.RUNNING
        assert job.current_run_id == "run_new"


# ---------------------------------------------------------------------------
# R1 across a SPLIT store: job/ledger in the owner DB, run evidence in the
# project-local execution DB.  No mocked run lookup and no owner-DB run rows.
# ---------------------------------------------------------------------------


def _split_stores(tmp_path: Path, repo: Path) -> tuple[Database, Database, str]:
    """Build a real owner DB plus a properly registered project execution DB."""
    from saberops.db import Database as _Database
    from saberops.project import compute_project_identity, new_run_id
    from saberops.runtime import project_db_path_for_repo

    owner_db = _db(tmp_path, "owner.db")
    exec_path = project_db_path_for_repo(repo)  # binds the project state dir
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    exec_db = _Database(exec_path, owner_db=owner_db)
    assert exec_db.db_path != owner_db.db_path
    assert project_id_of(run_id) == identity.project_id
    return owner_db, exec_db, run_id


def project_id_of(run_id: str) -> str | None:
    from saberops.project import project_id_from_run_id

    return project_id_from_run_id(run_id)


def _split_park(
    owner_db: Database, exec_db: Database, repo: Path, base: str, run_id: str,
    *, job_id: str = "job1",
) -> str:
    """Real cross-store D8 park, then re-open the pre-release crash window."""
    store.insert_job(owner_db, _job(job_id, str(repo), base), now=_NOW)
    store.mark_dispatched(owner_db, job_id, run_id, now=_NOW)
    reserve(
        owner_db, run_id=run_id, job_id=job_id, route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    exec_db.create_run(
        Run(
            id=run_id, task="t", target_repo=str(repo), base_commit=base,
            tier=Tier.T2, training_allowed=True, gate_command="make gate",
            status=RunStatus.RUNNING, created_at=_NOW.isoformat(), config_json="{}",
        )
    )
    _quota_attempt(exec_db, run_id, str(repo), AttemptStatus.FAILED)
    _quota_evidence(exec_db, run_id)
    owner = exec_db.claim_execution_owner(run_id, identity=_dead_identity())

    # The job/ledger live in the owner DB; the run evidence lives elsewhere.
    assert owner_db.get_run(run_id) is None
    assert exec_db.get_run(run_id) is not None

    out = preemption.complete_preemption(
        exec_db, run_id=run_id, repo_path=repo, base_sha=base,
        owner_id=owner.owner_id, owner_generation=owner.generation,
        decision_ref="test:split", now=_NOW,
    )
    assert out.owned and out.detail == "preempted", out.detail
    with owner_connection(owner_db) as conn:
        conn.execute(
            "UPDATE capacity_reservations SET state = 'RESERVED', settled_at = NULL"
            " WHERE run_id = ?",
            (run_id,),
        )
        conn.commit()
    assert _reservation_states(owner_db, run_id) == ["RESERVED"]
    return owner.owner_id


def test_split_db_settlement_releases_stranded_old_run_exactly_once(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R1 -- split-DB stranded debt settles exactly once.

    Real two-file layout: the job, its requeue event and the RESERVED rows live
    in the owner DB; the run, attempt, execution-owner row, decision, intent,
    capsule and handoff live in the project execution DB.  Settlement must
    authenticate the receipt against the database that actually owns that
    evidence, or a legitimate preemption looks like ``decision_missing`` and
    the charge leaks forever.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    owner_db, exec_db, run_id = _split_stores(tmp_path, repo)
    _split_park(owner_db, exec_db, repo, base, run_id)

    # A newer live run of the same job keeps its own reservation untouched.
    store.mark_dispatched(owner_db, "job1", "run_newer", now=_NOW)
    reserve(
        owner_db, run_id="run_newer", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )

    settlement = service.settle_preemption_debts(owner_db, now=_NOW)

    assert settlement.released == 1
    _assert_released(owner_db, run_id)
    assert _reservation_states(owner_db, "run_newer") == ["RESERVED"]
    job = _job_for(owner_db, "job1")
    assert job.preemptions == 1
    assert job.state == JobState.RUNNING
    assert job.current_run_id == "run_newer"
    assert len([e for e in store.list_events(owner_db, "job1") if e["kind"] == "requeued"]) == 1
    # Idempotent across restarts.
    assert service.settle_preemption_debts(owner_db, now=_NOW).released == 0
    assert _job_for(owner_db, "job1").preemptions == 1


def test_split_db_settlement_retains_unbound_legacy_charge(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R1 -- split-DB legacy receipt is not authenticated."""
    from saberops.handoff import create_handoffs

    repo = tmp_path / "repo"
    base = _repo(repo)
    owner_db, exec_db, run_id = _split_stores(tmp_path, repo)
    store.insert_job(owner_db, _job("job1", str(repo), base), now=_NOW)
    store.mark_dispatched(owner_db, "job1", run_id, now=_NOW)
    reserve(
        owner_db, run_id=run_id, job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )
    exec_db.create_run(
        Run(
            id=run_id, task="t", target_repo=str(repo), base_commit=base,
            tier=Tier.T2, training_allowed=True, gate_command="make gate",
            status=RunStatus.RUNNING, created_at=_NOW.isoformat(), config_json="{}",
        )
    )
    _quota_attempt(exec_db, run_id, str(repo), AttemptStatus.FAILED)
    _quota_evidence(exec_db, run_id)
    exec_db.claim_execution_owner(run_id, identity=_dead_identity())
    # Genuine artifacts in the run's own DB, but NO bound decision receipt.
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    _, capsule = create_handoffs(exec_db, run_id)
    exec_db.record_event(
        run_id,
        "preemption_committed",
        payload={
            "job_id": "job1",
            "checkpoint_sha": base,
            "checkpoint_ref": "refs/saberops/checkpoints/job1/0",
            "handoff_id": capsule.id,
            "not_before": None,
            "preemptions_next": 1,
        },
    )
    exec_db.update_run(
        run_id=run_id, status=RunStatus.PREEMPTED,
        completed_at=_NOW.isoformat(), result_summary="legacy park",
    )
    store.requeue_exact(
        owner_db, "job1", WaitingReason.PREEMPTED,
        expected_run_id=run_id, expected_preemptions=0,
        resume_from=ResumeFrom(run_id=run_id, checkpoint_sha=base, handoff_id=capsule.id),
        now=_NOW,
    )

    assert service.settle_preemption_debts(owner_db, now=_NOW).released == 0
    assert _reservation_states(owner_db, run_id) == ["RESERVED"]
    assert _job_for(owner_db, "job1").preemptions == 1


def test_split_db_scheduler_tick_settles_stranded_debt(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R1 -- the real tick settles split-DB debt.

    The scheduler is handed the owner DB (its production shape) and its
    default status lookup has no evidence for a project-local run; the
    resolved run row must therefore decide.
    """
    from saberops.capacity.loop import acquire_lease, scheduler_tick

    repo = tmp_path / "repo"
    base = _repo(repo)
    owner_db, exec_db, run_id = _split_stores(tmp_path, repo)
    _split_park(owner_db, exec_db, repo, base, run_id)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=300.0, now=_NOW)
    assert lease is not None

    def owner_only_status(rid: str) -> RunStatus | None:
        run = owner_db.get_run(rid)
        return run.status if run is not None else None

    res = scheduler_tick(
        owner_db,
        lease,
        now=_NOW,
        run_status=owner_only_status,
        run_launcher=lambda *_a, **_k: None,
        probes={},
        clock_fn=lambda: _NOW,
    )

    assert res.ran is True
    assert res.preemption_settled == 1
    _assert_released(owner_db, run_id)
    assert _job_for(owner_db, "job1").preemptions == 1
