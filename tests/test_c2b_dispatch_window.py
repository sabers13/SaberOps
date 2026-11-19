"""Scheduler dispatch-window recovery (lane C2B, ADR-0011 §2.2).

A scheduler killed between the dispatch compare-and-swap (job RUNNING) and the
run row leaves a RUNNING job with a RESERVED capacity row and nothing running.
These tests pin the bounded recovery: a restart releases that reservation and
requeues the never-started job exactly once (then fails it), never guesses when
the dispatch cannot be proven abandoned, never holds a live run's job, and never
lets one such row hold the whole tick or stop the continuous loop.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from saberops.capacity.ledger import PoolDraw, record_owner_quota, reserve
from saberops.capacity.loop import (
    DISPATCH_WINDOW_GRACE_SECONDS,
    acquire_lease,
    renew_lease,
    run_scheduler_loop,
    scheduler_tick,
)
from saberops.capacity.store import ensure_tables as ensure_capacity_tables
from saberops.capacity.store import owner_connection
from saberops.control_plane.plans import (
    PlanSet,
    PoolMember,
    PoolPlan,
    PostureRecord,
    Window,
)
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicy,
    ProviderPolicyState,
)
from saberops.db import Database
from saberops.jobs import store
from saberops.jobs.models import (
    DISPATCH_WINDOW_LOST,
    Job,
    JobRequirements,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import Run, RunStatus, Tier, WorkerCandidate
from saberops.routing_config import RoutingSnapshot

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
LEASE_TTL = 30.0
DEAD_HOLDER = "process:{\"host\": \"test\", \"pid\": 4, \"start_ticks\": 1}"
_INSTANCES = count(1)


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


@pytest.fixture
def owner_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    db = Database(tmp_path / "test_owner.db")
    store.ensure_tables(db)
    ensure_capacity_tables(db)
    return db


def _make_job(
    job_id: str,
    repo: str,
    *,
    state: JobState = JobState.QUEUED,
    current_run_id: str | None = None,
    run_ids: tuple[str, ...] = (),
    created_at: str = T0.isoformat(),
) -> Job:
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="test",
        repo=repo,
        base_ref="main",
        base_sha="abc1234",
        goal=f"task for {job_id}",
        acceptance=None,
        requirements=JobRequirements(),
        priority=Priority.NORMAL,
        deadline=None,
        not_before=None,
        state=state,
        waiting_reason=WaitingReason.NEW if state is JobState.QUEUED else None,
        failure_reason=None,
        eta=None,
        eta_confidence=None,
        current_run_id=current_run_id,
        run_ids=run_ids,
        created_at=created_at,
        updated_at=created_at,
        request_digest=f"dg_{job_id}",
    )


def _make_candidate(provider: str, model: str) -> WorkerCandidate:
    return WorkerCandidate(provider=provider, model=model)


def _make_routes() -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash="test-hash",
        role_chains=(
            ("WORKER", ((_make_candidate("openai", "gpt-5"),),)),
            ("SURGEON", ((_make_candidate("codex", "gpt-5.6-sol"),),)),
            ("INTEGRATOR", ((_make_candidate("codex", "gpt-5.6-sol"),),)),
        ),
    )


def _make_policy() -> ControlPlanePolicy:
    verified_on = (T0.date() - timedelta(days=5)).isoformat()
    return ControlPlanePolicy(
        provider_policies={
            name: ProviderPolicy(name, ProviderPolicyState.ALLOWED)
            for name in ("codex", "openai", "anthropic", "opencode")
        },
        quota_pools={},
        content_hash="test-policy-hash",
        plans=PlanSet(
            accounts={},
            pool_plans={
                "chatgpt-plus": PoolPlan(
                    pool_id="chatgpt-plus",
                    members=(
                        PoolMember(route="openai", multiplier=1.0),
                        PoolMember(route="codex", multiplier=1.0),
                    ),
                    windows=(Window(id="5h", duration_min=300),),
                    mode="STRICT",
                    red_line_pct=10.0,
                    overage_possible=False,
                ),
            },
            postures={
                "openai": PostureRecord(
                    "openai", "A", ttl_days=30, verified_on=verified_on
                ),
            },
        ),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _insert_residue(
    db: Database,
    *,
    job_id: str,
    run_id: str,
    dispatched_at: datetime,
    holder: str = DEAD_HOLDER,
    token: int = 4,
    lease_expires_at: datetime | None = None,
    run_store: str | Path | None = None,
    with_context: bool = True,
    repo: Path | None = None,
) -> None:
    """Durably reproduce the residue a kill inside the dispatch window leaves."""
    repo_path = str(repo) if repo is not None else str(db.db_path.parent)
    store.insert_job(
        db,
        _make_job(
            job_id,
            repo_path,
            state=JobState.QUEUED,
            created_at=dispatched_at.isoformat(),
        ),
        now=dispatched_at,
    )
    context = None
    if with_context:
        context = {
            "holder_id": holder,
            "fencing_token": token,
            "lease_expires_at": (
                lease_expires_at
                if lease_expires_at is not None
                else dispatched_at + timedelta(seconds=LEASE_TTL)
            ).isoformat(),
            "run_store": str(run_store) if run_store is not None else str(db.db_path),
        }
    store.mark_dispatched(
        db, job_id, run_id, dispatch_context=context, now=dispatched_at
    )
    reserve(
        db,
        run_id=run_id,
        job_id=job_id,
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=dispatched_at,
    )


def _reservation_states(db: Database, run_id: str) -> list[str]:
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [str(row["state"]) for row in rows]


def _tick(db: Database, now: datetime, *, launched: list[str] | None = None) -> Any:
    # Each restarted scheduler is a fresh instance generation (as the real loop
    # is: its holder identity carries a per-run instance uuid).
    lease = acquire_lease(
        db, f"c2b_instance_{next(_INSTANCES)}", ttl_seconds=LEASE_TTL, now=now
    )
    assert lease is not None
    try:
        return scheduler_tick(
            db,
            lease,
            now=now,
            routes=_make_routes(),
            policy=_make_policy(),
            run_launcher=(
                (lambda job, decision, run_id: launched.append(job.job_id))
                if launched is not None
                else (lambda job, decision, run_id: None)
            ),
            clock_fn=lambda: now,
        )
    finally:
        from saberops.capacity.loop import release_lease

        release_lease(db, lease, now=now)


def _quota(db: Database, *, now: datetime = T0) -> None:
    record_owner_quota(db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=now)


# --------------------------------------------------------------------------
# The repair
# --------------------------------------------------------------------------


def test_dispatch_window_residue_is_requeued_once_then_failed(
    owner_db: Database, tmp_path: Path
) -> None:
    """A proven abandoned dispatch is requeued once; a repeat fails with a reason."""
    repo = _repo(tmp_path)
    _insert_residue(
        owner_db, job_id="residue", run_id="run_dead", dispatched_at=T0, repo=repo
    )
    _quota(owner_db)

    # Restarted scheduler, past the dead lease expiry plus the grace bound: the
    # job is requeued (never started) and dispatched again in the same tick.
    announced: list[str] = []
    first = _tick(owner_db, T0 + timedelta(seconds=60), launched=announced)
    assert first.ran is True
    assert first.dispatch_window_repaired == ("residue",)
    job = store.get_job(owner_db, "residue")
    assert job is not None
    assert job.state is JobState.RUNNING
    assert job.current_run_id not in (None, "run_dead")
    assert announced == ["residue"]
    assert _reservation_states(owner_db, "run_dead") == ["RELEASED"]
    requeued = [
        event
        for event in store.list_events(owner_db, "residue")
        if event["kind"] == "requeued"
    ]
    assert requeued
    assert (
        store.list_events(owner_db, "residue")[-1]["kind"] == "dispatched"
    ), "the requeue is durable, then the job is dispatched again"

    # The second loss (same job) is not requeued a third time: it fails loudly.
    second_run = job.current_run_id
    assert second_run is not None
    second = _tick(owner_db, T0 + timedelta(seconds=130))
    assert second.ran is True
    assert second.dispatch_window_repaired == ("residue",)
    failed = store.get_job(owner_db, "residue")
    assert failed is not None
    assert failed.state is JobState.FAILED
    assert failed.failure_reason == DISPATCH_WINDOW_LOST
    assert _reservation_states(owner_db, second_run) == ["RELEASED"]


def test_dispatch_window_grace_holds_young_dispatch(
    owner_db: Database, tmp_path: Path
) -> None:
    """A dispatch inside the grace window is not touched yet."""
    repo = _repo(tmp_path)
    _insert_residue(
        owner_db, job_id="young", run_id="run_young", dispatched_at=T0, repo=repo
    )
    just_inside = T0 + timedelta(seconds=LEASE_TTL + DISPATCH_WINDOW_GRACE_SECONDS - 5)
    result = _tick(owner_db, just_inside)
    assert result.ran is False
    assert result.reason == "running_run_status_unresolved"
    assert result.unresolved_running_jobs == ("young",)
    job = store.get_job(owner_db, "young")
    assert job is not None and job.state is JobState.RUNNING
    assert job.current_run_id == "run_young"
    assert _reservation_states(owner_db, "run_young") == ["RESERVED"]


def test_dispatch_without_a_lease_record_is_never_guessed(
    owner_db: Database, tmp_path: Path
) -> None:
    """A dispatch that recorded no scheduler identity is held, not reconciled."""
    repo = _repo(tmp_path)
    _insert_residue(
        owner_db,
        job_id="legacy",
        run_id="run_legacy",
        dispatched_at=T0,
        with_context=False,
        repo=repo,
    )
    result = _tick(owner_db, T0 + timedelta(hours=4))
    assert result.ran is False
    assert result.reason == "running_run_status_unresolved"
    assert result.unresolved_running_jobs == ("legacy",)
    job = store.get_job(owner_db, "legacy")
    assert job is not None and job.state is JobState.RUNNING
    assert _reservation_states(owner_db, "run_legacy") == ["RESERVED"]


def test_dispatch_from_the_current_generation_is_held(
    owner_db: Database, tmp_path: Path
) -> None:
    """The scheduler that took the job is still the lease holder: hold, do not repair."""
    repo = _repo(tmp_path)
    lease = acquire_lease(owner_db, "live_instance", ttl_seconds=LEASE_TTL, now=T0)
    assert lease is not None
    renewed = renew_lease(
        owner_db, lease, ttl_seconds=LEASE_TTL, now=T0 + timedelta(seconds=20)
    )
    assert renewed is not None
    # The dispatch recorded the pre-renewal expiry of the *same* generation.
    _insert_residue(
        owner_db,
        job_id="live",
        run_id="run_live",
        dispatched_at=T0,
        holder=lease.holder_id,
        token=lease.fencing_token,
        lease_expires_at=T0 + timedelta(seconds=LEASE_TTL),
        repo=repo,
    )
    result = scheduler_tick(
        owner_db,
        renewed,
        now=T0 + timedelta(seconds=45),
        routes=_make_routes(),
        policy=_make_policy(),
        clock_fn=lambda: T0 + timedelta(seconds=45),
    )
    assert result.ran is False
    assert result.reason == "running_run_status_unresolved"
    assert result.dispatch_window_repaired == ()
    job = store.get_job(owner_db, "live")
    assert job is not None and job.state is JobState.RUNNING
    assert _reservation_states(owner_db, "run_live") == ["RESERVED"]


def test_run_in_the_recorded_store_keeps_the_job_running(
    owner_db: Database, tmp_path: Path
) -> None:
    """A live project-local run is never terminalized, even past every bound."""
    repo = _repo(tmp_path)
    exec_db = Database(tmp_path / "project_local.db")
    run_id = "run_project_local"
    exec_db.create_run(
        Run(
            id=run_id,
            task="project-local run",
            target_repo=str(repo),
            base_commit="abc1234",
            tier=Tier.T2,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=T0.isoformat(),
            config_json="{}",
        )
    )
    _insert_residue(
        owner_db,
        job_id="split",
        run_id=run_id,
        dispatched_at=T0,
        run_store=exec_db.db_path,
        repo=repo,
    )
    assert owner_db.get_run(run_id) is None
    result = _tick(owner_db, T0 + timedelta(hours=2))
    assert result.ran is False
    assert result.reason == "running_run_status_unresolved"
    assert result.unresolved_running_jobs == ("split",)
    job = store.get_job(owner_db, "split")
    assert job is not None and job.current_run_id == run_id
    assert _reservation_states(owner_db, run_id) == ["RESERVED"]
    run_after = exec_db.get_run(run_id)
    assert run_after is not None and run_after.status == RunStatus.RUNNING


def test_one_repaired_row_does_not_hold_dispatch(
    owner_db: Database, tmp_path: Path
) -> None:
    """Repairing a residue and dispatching ordinary queued work happen in one tick."""
    repo = _repo(tmp_path)
    _insert_residue(
        owner_db, job_id="residue2", run_id="run_dead2", dispatched_at=T0, repo=repo
    )
    store.insert_job(
        owner_db,
        _make_job(
            "queued",
            str(repo),
            created_at=(T0 + timedelta(seconds=1)).isoformat(),
        ),
        now=T0,
    )
    _quota(owner_db)
    launched: list[str] = []
    result = _tick(owner_db, T0 + timedelta(seconds=60), launched=launched)
    assert result.ran is True
    assert result.dispatch_window_repaired == ("residue2",)
    assert "queued" in launched
    assert store.get_job(owner_db, "queued") is not None
    assert store.get_job(owner_db, "queued").state is JobState.RUNNING  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# The continuous loop
# --------------------------------------------------------------------------


def test_continuous_loop_keeps_running_until_the_window_clears(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop logs the hold and keeps polling instead of exiting silently."""
    repo = _repo(tmp_path)
    _insert_residue(
        owner_db, job_id="loop", run_id="run_loop", dispatched_at=T0, repo=repo
    )
    _quota(owner_db)
    monkeypatch.delenv("SABEROPS_DISABLE_AUTO_SCHEDULER", raising=False)

    stop = threading.Event()
    launched: list[str] = []
    clock_calls = {"n": 0}

    def clock() -> datetime:
        clock_calls["n"] += 1
        return T0 + timedelta(seconds=2 * clock_calls["n"])

    def launcher(job: Job, decision: object, run_id: str) -> None:
        launched.append(job.job_id)
        stop.set()

    result = run_scheduler_loop(
        owner_db,
        once=False,
        tick_interval=0.01,
        holder_id="loop_instance",
        stop_event=stop,
        run_launcher=launcher,
        routes=_make_routes(),
        policy=_make_policy(),
        now_fn=clock,
    )

    assert result is not None
    assert result.ran is True
    assert result.dispatch_window_repaired == ("loop",)
    assert launched == ["loop"]
    # It polled through several hold ticks before the bound passed.
    assert clock_calls["n"] > 4
    job = store.get_job(owner_db, "loop")
    assert job is not None and job.state is JobState.RUNNING
    assert job.current_run_id not in (None, "run_loop")
    assert _reservation_states(owner_db, "run_loop") == ["RELEASED"]
