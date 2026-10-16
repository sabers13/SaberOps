"""Unit tests for scheduler loop, single-instance lease, and atomic dispatch (ADR-0002 D7)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.capacity.ledger import (
    PoolDraw,
    record_owner_quota,
    reserve,
)
from saberops.capacity.loop import (
    _default_run_launcher,
    acquire_lease,
    check_lease,
    release_lease,
    renew_lease,
    run_scheduler_loop,
    scheduler_tick,
)
from saberops.capacity.scheduler import WaitDecision
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
    Job,
    JobRequirements,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier, WorkerCandidate
from saberops.routing_config import RoutingSnapshot

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _make_candidate(provider: str, model: str, pool: str | None = None) -> WorkerCandidate:
    _ = pool
    return WorkerCandidate(
        provider=provider,
        model=model,
    )


def _make_routes(worker_groups: tuple[tuple[WorkerCandidate, ...], ...]) -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash="test-hash",
        role_chains=(
            ("WORKER", worker_groups),
            ("SURGEON", ((WorkerCandidate("codex", "gpt-5.6-sol"),),)),
            ("INTEGRATOR", ((WorkerCandidate("codex", "gpt-5.6-sol"),),)),
        ),
    )


def _make_policy(pool_plans: dict[str, PoolPlan] | None = None) -> ControlPlanePolicy:
    today_str = (_NOW.date() - timedelta(days=5)).isoformat()
    postures = {
        "codex": PostureRecord("codex", "A", ttl_days=30, verified_on=today_str),
        "openai": PostureRecord("openai", "A", ttl_days=30, verified_on=today_str),
    }
    providers = {
        name: ProviderPolicy(name, ProviderPolicyState.ALLOWED)
        for name in ("codex", "openai", "anthropic", "opencode", "cline")
    }
    base_plans: dict[str, PoolPlan] = {
        "chatgpt-plus": PoolPlan(
            pool_id="chatgpt-plus",
            members=(
                PoolMember(route="codex", multiplier=1.0),
                PoolMember(route="openai", multiplier=1.0),
            ),
            windows=(Window(id="5h", duration_min=300),),
            mode="STRICT",
            red_line_pct=10.0,
            overage_possible=False,
        ),
        "opencode-go": PoolPlan(
            pool_id="opencode-go",
            members=(),
            windows=(),
            mode="OFF",
            overage_possible=False,
        ),
    }
    if pool_plans:
        base_plans.update(pool_plans)
    return ControlPlanePolicy(
        provider_policies=providers,
        quota_pools={},
        content_hash="test-policy-hash",
        plans=PlanSet(accounts={}, pool_plans=base_plans, postures=postures),
    )


def _make_job(
    job_id: str,
    repo: str,
    *,
    priority: Priority = Priority.NORMAL,
    state: JobState = JobState.QUEUED,
    current_run_id: str | None = None,
    run_ids: tuple[str, ...] = (),
    waiting_reason: WaitingReason | None = None,
    created_at: str,
    updated_at: str,
    request_digest: str,
) -> Job:
    wr = waiting_reason
    if state == JobState.QUEUED and wr is None:
        wr = WaitingReason.NEW
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
        priority=priority,
        deadline=None,
        not_before=None,
        state=state,
        waiting_reason=wr,
        failure_reason=None,
        eta=None,
        eta_confidence=None,
        current_run_id=current_run_id,
        run_ids=run_ids,
        created_at=created_at,
        updated_at=updated_at,
        request_digest=request_digest,
    )


@pytest.fixture
def owner_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    db_path = tmp_path / "test_owner.db"
    db = Database(db_path)
    store.ensure_tables(db)
    ensure_capacity_tables(db)
    return db


def test_acquire_and_renew_lease_basic(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 single owner DB lease transactional claim and renewal."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "sched_1", ttl_seconds=30.0, now=t0)
    assert lease is not None
    assert lease.holder_id == "sched_1"
    assert lease.fencing_token == 1
    assert check_lease(owner_db, lease, now=t0) is True

    # Renew extends expires_at
    t1 = t0 + timedelta(seconds=10)
    renewed = renew_lease(owner_db, lease, ttl_seconds=30.0, now=t1)
    assert renewed is not None
    assert renewed.fencing_token == 1
    assert renewed.expires_at > lease.expires_at

    # Release frees the lease
    released = release_lease(owner_db, renewed, now=t1)
    assert released is True
    assert check_lease(owner_db, renewed, now=t1) is False


def test_racing_schedulers_only_one_wins_lease(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 two racing scheduler instances acquire only one fenced lease."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease1 = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease1 is not None

    # Instance B attempts to acquire while A's lease is valid
    lease2 = acquire_lease(owner_db, "instance_B", ttl_seconds=30.0, now=t0)
    assert lease2 is None


def test_lease_expiry_and_takeover_increments_fencing_token(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 lease expiry takeover by second instance with fenced token increment."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease1 = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease1 is not None
    assert lease1.fencing_token == 1

    # Time advances past lease1 expiration (30 seconds)
    t1 = t0 + timedelta(seconds=35)

    # Instance B takes over the expired lease
    lease2 = acquire_lease(owner_db, "instance_B", ttl_seconds=30.0, now=t1)
    assert lease2 is not None
    assert lease2.holder_id == "instance_B"
    assert lease2.fencing_token == 2

    # Instance A wakes up and is now fenced
    assert check_lease(owner_db, lease1, now=t1) is False
    assert renew_lease(owner_db, lease1, ttl_seconds=30.0, now=t1) is None


def test_loss_of_lease_before_launch_compensates(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 loss-of-lease before launch aborts dispatch and compensates."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_lease_loss",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig1",
    )
    store.insert_job(owner_db, job, now=t0)

    # Record 100% capacity
    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)

    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    # Simulate lease loss right at Step C (before launch)
    from saberops.capacity import loop
    real_check = loop.check_lease
    calls = 0

    def mock_check(db_arg: Any, lease_arg: Any, *, now: Any = None) -> bool:
        nonlocal calls
        calls += 1
        if calls >= 3:
            return False
        return real_check(db_arg, lease_arg, now=now)

    monkeypatch.setattr(loop, "check_lease", mock_check)

    launched_runs: list[str] = []

    def mock_launcher(j: Job, dec: Any, run_id: str) -> None:
        launched_runs.append(run_id)

    # Tick should notice lease check failure before launch, compensate, and NOT launch
    scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=mock_launcher,
        clock_fn=lambda: t0,
    )
    # The tick aborts cleanly
    assert len(launched_runs) == 0

    # Job is requeued (not left in RUNNING with no run!)
    updated_job = store.get_job(owner_db, "job_lease_loss")
    assert updated_job is not None
    assert updated_job.state == JobState.QUEUED
    assert updated_job.waiting_reason == WaitingReason.NO_CAPACITY

    # Reservation was compensated (RELEASED)
    with owner_connection(owner_db) as conn:
        rows = conn.execute("SELECT state FROM capacity_reservations").fetchall()
        for r in rows:
            assert r["state"] == "RELEASED"


def test_launch_failure_compensates_cas_and_reservation(owner_db: Database, tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 launch failure compensates partial dispatch and releases reservation."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_fail_launch",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig2",
    )
    store.insert_job(owner_db, job, now=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    def failing_launcher(j: Job, dec: Any, run_id: str) -> None:
        raise RuntimeError("simulated launch process crash")

    scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=failing_launcher,
        clock_fn=lambda: t0,
    )

    # Job must NOT be left RUNNING!
    updated_job = store.get_job(owner_db, "job_fail_launch")
    assert updated_job is not None
    assert updated_job.state == JobState.QUEUED
    assert updated_job.waiting_reason == WaitingReason.NO_CAPACITY

    # Reservation was marked RELEASED
    with owner_connection(owner_db) as conn:
        rows = conn.execute("SELECT state FROM capacity_reservations").fetchall()
        assert len(rows) > 0
        for r in rows:
            assert r["state"] == "RELEASED"


def test_reconcile_terminal_runs_settles_or_releases_reservations(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 startup reconciliation settles COMPLETED and releases FAILED."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Create 3 jobs in RUNNING state
    pairs = [("j_comp", "run_comp"), ("j_fail", "run_fail"), ("j_orph", "run_orph")]
    for idx, (jid, rid) in enumerate(pairs):
        j = _make_job(
            jid,
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id=rid,
            run_ids=(rid,),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest=f"dig_{idx}",
        )
        store.insert_job(owner_db, j, event_kind="dispatched", now=t0)
        reserve(
            owner_db,
            run_id=rid,
            job_id=jid,
            route="openai/gpt-5",
            draws=[PoolDraw("chatgpt-plus", "5h")],
            size_bucket="M",
            now=t0,
        )

    def mock_run_status(r_id: str) -> RunStatus | None:
        if r_id == "run_comp":
            return RunStatus.COMPLETED
        if r_id == "run_fail":
            return RunStatus.FAILED
        if r_id == "run_orph":
            return RunStatus.ORPHANED
        return None

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_status=mock_run_status,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    assert len(res.reconciled_changes) == 2

    # Check job states
    j_comp = store.get_job(owner_db, "j_comp")
    assert j_comp is not None and j_comp.state == JobState.SUCCEEDED

    j_fail = store.get_job(owner_db, "j_fail")
    assert j_fail is not None and j_fail.state == JobState.FAILED

    j_orph = store.get_job(owner_db, "j_orph")
    assert j_orph is not None and j_orph.state == JobState.RUNNING  # Not misclassified!

    # Check reservations
    with owner_connection(owner_db) as conn:
        comp_res = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_comp'"
        ).fetchone()
        assert comp_res["state"] == "SETTLED"

        fail_res = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_fail'"
        ).fetchone()
        assert fail_res["state"] == "RELEASED"

        orph_res = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_orph'"
        ).fetchone()
        assert orph_res["state"] == "RESERVED"


def test_scheduler_tick_dispatches_highest_priority_job(owner_db: Database, tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 scheduler tick end-to-end dispatch of queued job."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job_norm = _make_job(
        "job_norm",
        str(repo_dir),
        priority=Priority.NORMAL,
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="d_norm",
    )
    job_urg = _make_job(
        "job_urg",
        str(repo_dir),
        priority=Priority.URGENT,
        created_at=(t0 + timedelta(minutes=5)).isoformat(),
        updated_at=(t0 + timedelta(minutes=5)).isoformat(),
        request_digest="d_urg",
    )
    store.insert_job(owner_db, job_norm, now=t0)
    store.insert_job(owner_db, job_urg, now=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    dispatched_order: list[str] = []

    def mock_launcher(j: Job, dec: Any, run_id: str) -> None:
        dispatched_order.append(j.job_id)

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=mock_launcher,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    # URGENT must be dispatched before NORMAL
    assert dispatched_order[0] == "job_urg"
    assert dispatched_order[1] == "job_norm"
    assert res.dispatched == 2

    # Both jobs are in RUNNING state
    urg_job = store.get_job(owner_db, "job_urg")
    norm_job = store.get_job(owner_db, "job_norm")
    assert urg_job is not None and urg_job.state == JobState.RUNNING
    assert norm_job is not None and norm_job.state == JobState.RUNNING


def test_scheduler_tick_wait_and_probe_decisions(owner_db: Database, tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 scheduler tick applies wait reasons and runs forced probes."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_waiting",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="d_wait",
    )
    store.insert_job(owner_db, job, now=t0)

    # 0% capacity on chatgpt-plus pool
    record_owner_quota(
        owner_db,
        "openai",
        "chatgpt-plus",
        0.0,
        (t0 + timedelta(hours=2)).isoformat(),
        source="OWNER",
        now=t0,
    )
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert res.waited == 1

    updated = store.get_job(owner_db, "job_waiting")
    assert updated is not None
    assert updated.state == JobState.QUEUED
    assert updated.waiting_reason == WaitingReason.NO_CAPACITY


def test_run_scheduler_loop_once_clean_exit(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 scheduler loop --once execution exits cleanly and releases lease."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    res = run_scheduler_loop(owner_db, once=True, now_fn=lambda: t0)
    assert res is not None
    assert res.ran is True

    # Lease was released upon exit
    with owner_connection(owner_db) as conn:
        rows = conn.execute("SELECT * FROM capacity_scheduler_lease").fetchall()
        assert len(rows) == 0


def test_run_scheduler_loop_can_be_stopped(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 scheduler loop can be stopped via stop_event without busy spin."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    stop_event = threading.Event()

    def run_worker() -> None:
        run_scheduler_loop(
            owner_db,
            once=False,
            tick_interval=0.05,
            lease_ttl=10.0,
            stop_event=stop_event,
            now_fn=lambda: t0,
        )

    thread = threading.Thread(target=run_worker)
    thread.start()

    # Stop after a brief delay
    stop_event.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    # Lease was released
    with owner_connection(owner_db) as conn:
        rows = conn.execute("SELECT * FROM capacity_scheduler_lease").fetchall()
        assert len(rows) == 0


def test_valid_empty_chain_never_crashes_tick(owner_db: Database, tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 / R-002: valid empty worker chain does not crash scheduler tick."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_empty", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_empty_chain",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_empty",
    )
    store.insert_job(owner_db, job, now=t0)

    # Empty worker chain is an authorized valid owner decision (R-002)
    routes = _make_routes(())
    policy = _make_policy()

    # First tick: job is set to waiting without ValueError crash
    res1 = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        clock_fn=lambda: t0,
    )
    assert res1.ran is True
    assert res1.waited == 1
    assert res1.dispatched == 0

    j1 = store.get_job(owner_db, "job_empty_chain")
    assert j1 is not None
    assert j1.state == JobState.QUEUED
    assert j1.waiting_reason == WaitingReason.NO_CAPACITY

    # Subsequent tick: scheduler remains live and healthy
    res2 = scheduler_tick(
        owner_db,
        lease,
        now=t0 + timedelta(seconds=10),
        routes=routes,
        policy=policy,
        clock_fn=lambda: t0 + timedelta(seconds=10),
    )
    assert res2.ran is True
    assert res2.dispatched == 0


def test_simultaneous_first_acquire_race_returns_denial(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 first lease race returns denial instead of unhandled IntegrityError."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    ensure_capacity_tables(owner_db)

    # First process acquires lease
    lease1 = acquire_lease(owner_db, "holder_1", ttl_seconds=60.0, now=t0)
    assert lease1 is not None

    # Second process concurrent acquire returns denial (None)
    lease2 = acquire_lease(owner_db, "holder_2", ttl_seconds=60.0, now=t0)
    assert lease2 is None


def test_cancel_running_job_releases_ledger_reservation(owner_db: Database, tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 cancellation of running job releases ledger reservation."""
    from saberops.jobs.service import cancel_job

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_cancel_res",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_cancel",
    )
    store.insert_job(owner_db, job, now=t0)
    dispatched = store.mark_dispatched(owner_db, "job_cancel_res", run_id="run_cancel_1", now=t0)
    assert dispatched.state == JobState.RUNNING

    draws = [PoolDraw(pool_id="chatgpt-plus", window_id="ALL", multiplier=1.0)]
    reserve(
        owner_db,
        run_id="run_cancel_1",
        job_id="job_cancel_res",
        route="openai/gpt-5",
        draws=draws,
        size_bucket="M",
        now=t0,
    )

    # Cancel the running job
    res = cancel_job(owner_db, "job_cancel_res", run_canceller=lambda r_id: True)
    assert res.cancelled is True
    assert res.job.state == JobState.CANCELLED

    # Reservation state is now RELEASED
    with owner_connection(owner_db) as conn:
        q = "SELECT state FROM capacity_reservations WHERE run_id = 'run_cancel_1'"
        rows = conn.execute(q).fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "RELEASED"


def test_mid_tick_lease_expiry_fences_subsequent_launches(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 mid-tick lease expiry / takeover fences subsequent dispatches."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    current_time = [t0]

    def mock_clock() -> datetime:
        return current_time[0]

    lease = acquire_lease(owner_db, "sched_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Two queued jobs
    j1 = _make_job(
        "job_mid_1",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        priority=Priority.URGENT,
        request_digest="dig_mid1",
    )
    j2 = _make_job(
        "job_mid_2",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        priority=Priority.NORMAL,
        request_digest="dig_mid2",
    )
    store.insert_job(owner_db, j1, now=t0)
    store.insert_job(owner_db, j2, now=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    launched: list[str] = []

    def expiring_launcher(j: Job, dec: Any, run_id: str) -> None:
        launched.append(run_id)
        # Advance clock past lease TTL (30s) so lease expires mid-tick before next job
        current_time[0] = current_time[0] + timedelta(seconds=45)

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=expiring_launcher,
        clock_fn=mock_clock,
    )
    # First job dispatched; second job fenced due to lease expiry
    assert len(launched) == 1
    assert res.dispatched == 1

    # Second job was NOT dispatched and remains QUEUED
    updated_j2 = store.get_job(owner_db, "job_mid_2")
    assert updated_j2 is not None
    assert updated_j2.state == JobState.QUEUED


def test_partial_launch_with_existing_run_does_not_requeue(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 launcher failure after run row creation does not requeue job."""
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "sched_partial", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_partial_launch",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_partial",
    )
    store.insert_job(owner_db, job, now=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    def partial_fail_launcher(j: Job, dec: Any, run_id: str) -> None:
        # Launcher creates durable run row in DB, then raises (e.g. process supervisor failure)
        insert_sql = (
            "INSERT INTO runs (id, task, target_repo, base_commit, tier, "
            "training_allowed, gate_command, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with owner_db._get_connection() as conn:
            conn.execute(
                insert_sql,
                (run_id, j.goal, j.repo, j.base_sha, "T1", 0, "test", "RUNNING", t0.isoformat()),
            )
            conn.commit()
        raise RuntimeError("crashed after run row created")

    scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=partial_fail_launcher,
        clock_fn=lambda: t0,
    )

    # Job must NOT be requeued! It stays in RUNNING with current_run_id for reconciler recovery
    updated = store.get_job(owner_db, "job_partial_launch")
    assert updated is not None
    assert updated.state == JobState.RUNNING
    assert updated.current_run_id is not None

    # Reservation was NOT released (remains for recovery/settlement)
    with owner_connection(owner_db) as conn:
        q = "SELECT state FROM capacity_reservations WHERE job_id = 'job_partial_launch'"
        rows = conn.execute(q).fetchall()
        assert len(rows) > 0
        assert all(r["state"] == "RESERVED" for r in rows)


def test_readiness_unavailable_executable_does_not_dispatch(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F12: route with unavailable executable is denied without CAS."""
    from saberops.model_access.readiness_service import ReadinessAdmission
    from saberops.models import ProviderReadiness

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "sched_readiness", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_readiness_check",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_ready",
    )
    store.insert_job(owner_db, job, now=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    # Mock readiness service returning unavailable executable
    class MockReadinessService:
        def admission_for(
            self, *, provider: str, profile_id: str = "", backend_ref: str = ""
        ) -> ReadinessAdmission:
            return ReadinessAdmission(
                state=ProviderReadiness.NOT_READY,
                reason="EXECUTABLE_MISSING",
                executable_available=False,
            )

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        readiness_service=MockReadinessService(),
        clock_fn=lambda: t0,
    )

    assert res.dispatched == 0
    assert res.waited == 1

    # Job was NOT claimed and has no run_id
    j = store.get_job(owner_db, "job_readiness_check")
    assert j is not None
    assert j.state == JobState.QUEUED
    assert j.current_run_id is None


def test_production_shape_lease_expiry_without_clock_fn_fences_launches(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F10: call without clock_fn fences launches on lease expiry."""
    # 1. Lease expired in the past: production semantics = concrete now, clock_fn=None
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "sched_prod", ttl_seconds=0.001, now=t0)
    assert lease is not None

    launched: list[str] = []

    def mock_launcher(j: Job, dec: Any, run_id: str) -> None:
        launched.append(run_id)

    # Real time is far past expires_at; clock_fn is omitted (production CLI shape).
    # Must notice lease expiry immediately and abort with zero launches!
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=mock_launcher,
        # clock_fn omitted!
    )
    assert res.ran is False
    assert res.reason == "lost_lease"
    assert len(launched) == 0

    # 2. Mid-tick advancing wall-clock without clock_fn fences subsequent dispatches
    from saberops.capacity import loop

    real_utcnow = loop._utcnow
    now_fresh = datetime.now(UTC)
    live_lease = acquire_lease(owner_db, "sched_live", ttl_seconds=30.0, now=now_fresh)
    assert live_lease is not None

    repo_dir = tmp_path / "repo2"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    j1 = _make_job(
        "j_prod_1",
        str(repo_dir),
        created_at=now_fresh.isoformat(),
        updated_at=now_fresh.isoformat(),
        priority=Priority.URGENT,
        request_digest="dp1",
    )
    j2 = _make_job(
        "j_prod_2",
        str(repo_dir),
        created_at=now_fresh.isoformat(),
        updated_at=now_fresh.isoformat(),
        priority=Priority.NORMAL,
        request_digest="dp2",
    )
    store.insert_job(owner_db, j1, now=now_fresh)
    store.insert_job(owner_db, j2, now=now_fresh)

    record_owner_quota(
        owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=now_fresh
    )
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    dispatched_live: list[str] = []
    current_wall = [now_fresh]

    def advancing_launcher(j: Job, dec: Any, run_id: str) -> None:
        dispatched_live.append(run_id)
        # First job launched! Wall-clock now advances 60s past the 30s lease TTL
        current_wall[0] = current_wall[0] + timedelta(seconds=60)

    def mock_advancing_utcnow(dt: Any) -> datetime:
        if dt is not None:
            return real_utcnow(dt)
        return current_wall[0]

    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(loop, "_utcnow", mock_advancing_utcnow)
        res_mid = scheduler_tick(
            owner_db,
            live_lease,
            now=now_fresh,
            routes=routes,
            policy=policy,
            run_launcher=advancing_launcher,
            # clock_fn omitted (production CLI shape)!
        )
        # First job dispatched; second job was fenced mid-tick by advancing wall clock
        assert len(dispatched_live) == 1
        assert res_mid.dispatched == 1
        # Second job remains QUEUED
        j2_db = store.get_job(owner_db, "j_prod_2")
        assert j2_db is not None
        assert j2_db.state == JobState.QUEUED
    finally:
        monkeypatch.undo()


def test_simultaneous_first_acquire_race_catches_integrity_error(
    owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 / F6: first acquisition race catches IntegrityError on insert."""
    import sqlite3

    from saberops.capacity import loop
    from saberops.capacity.store import owner_connection

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    ensure_capacity_tables(owner_db)

    # Pre-seed holder_1's live lease into DB
    with owner_connection(owner_db) as conn:
        conn.execute(
            "INSERT INTO capacity_scheduler_lease "
            "(lease_name, holder_id, acquired_at, expires_at, fencing_token) "
            "VALUES (?, ?, ?, ?, ?)",
            ("scheduler", "holder_1", t0.isoformat(), (t0 + timedelta(seconds=60)).isoformat(), 1),
        )
        conn.commit()

    orig_owner_conn = loop.owner_connection  # type: ignore[attr-defined]

    # Simulate race: holder_2's initial SELECT sees row=None, but INSERT fails with IntegrityError
    class ConflictConnection:
        def __init__(self, real_conn: Any) -> None:
            self._real = real_conn
            self._first_select = True

        def execute(self, sql: str, *args: Any) -> Any:
            if "SELECT lease_name" in sql and self._first_select:
                self._first_select = False
                class DummyCursor:
                    def fetchone(self) -> Any:
                        return None
                return DummyCursor()
            if "INSERT INTO capacity_scheduler_lease" in sql:
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: capacity_scheduler_lease.lease_name"
                )
            return self._real.execute(sql, *args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    from contextlib import contextmanager

    @contextmanager
    def conflicting_owner_connection(db: Any) -> Any:
        with orig_owner_conn(db) as conn:
            yield ConflictConnection(conn)

    monkeypatch.setattr(loop, "owner_connection", conflicting_owner_connection)

    # holder_2 hits IntegrityError on insert, rolls back, reads holder_1's live lease, returns None
    lease2 = acquire_lease(owner_db, "holder_2", ttl_seconds=60.0, now=t0)
    assert lease2 is None


def test_scheduler_tick_aborts_on_ledger_read_error(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 scheduler_tick aborts fail-closed on ledger read error."""
    import saberops.capacity.loop as loop_mod
    from saberops.capacity.loop import acquire_lease, scheduler_tick

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_err", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_err"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    job = _make_job(
        "job_ledger_err",
        str(repo_dir),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_err",
    )
    store.insert_job(owner_db, job, now=t0)

    def failing_charges(db: Any, now: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(loop_mod, "post_observation_charges", failing_charges)

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        clock_fn=lambda: t0,
    )
    assert res.ran is False
    assert res.reason == "ledger_read_error"

    # Verify job remained queued, zero jobs dispatched
    j_after = store.get_job(owner_db, "job_ledger_err")
    assert j_after is not None
    assert j_after.state == JobState.QUEUED


def test_scheduler_tick_default_global_ceiling_blocks_16th_job(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F3 scheduler_tick resolves the deterministic interim
    default global ceiling (15) from owner policy (max_global_concurrent absent)
    and blocks the next top-level job without launching it.
    """
    from saberops.models import RunStatus

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_g", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_global"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    for i in range(15):
        store.insert_job(
            owner_db,
            _make_job(
                f"running_{i}",
                str(repo_dir),
                state=JobState.RUNNING,
                current_run_id=f"run_{i}",
                created_at=t0.isoformat(),
                updated_at=t0.isoformat(),
                request_digest=f"dg_run_{i}",
            ),
            now=t0,
        )
        # Setup-only realism: a real scheduler dispatch always holds a pool
        # reservation for its RUNNING job, so the D7 fail-closed RUNNING
        # accounting guard (legacy reservations) sees 15 claimed slots, not
        # 15 unreserved legacy rows.  Assertions below are unchanged.
        reserve(
            owner_db,
            run_id=f"run_{i}",
            job_id=f"running_{i}",
            route="openai/gpt-5",
            draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
            size_bucket="M",
            now=t0,
        )
    store.insert_job(
        owner_db,
        _make_job(
            "queued_over_ceiling",
            str(repo_dir),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_queued",
        ),
        now=t0,
    )

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()
    assert policy.max_global_concurrent is None  # owner key absent -> default 15

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        run_status=lambda rid: RunStatus.RUNNING,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert launched == []
    assert any(
        isinstance(d, WaitDecision) and d.reason == WaitingReason.CONCURRENCY.value
        for d in res.decisions
    )
    queued = store.get_job(owner_db, "queued_over_ceiling")
    assert queued is not None
    assert queued.state == JobState.QUEUED


def _seed_cap_jobs(
    owner_db: Database,
    repo_dir: Path,
    running: int,
    queued: int,
    t0: datetime,
    *,
    reserve_running: bool = False,
) -> None:
    """Seed durable RUNNING and QUEUED jobs for global-capacity tick tests.

    ``reserve_running`` also records the pool reservation every real scheduler
    dispatch holds for a RUNNING job.  The global-ceiling fixtures use it so
    their synthetic RUNNING jobs model dispatched jobs; without it they would
    be legacy RUNNING rows with no reservation, which the D7 fail-closed
    accounting guard now (correctly) refuses to schedule past.
    """
    for i in range(running):
        store.insert_job(
            owner_db,
            _make_job(
                f"cap_running_{i}",
                str(repo_dir),
                state=JobState.RUNNING,
                current_run_id=f"cap_run_{i}",
                created_at=t0.isoformat(),
                updated_at=t0.isoformat(),
                request_digest=f"cap_rg_{i}",
            ),
            now=t0,
        )
    for i in range(queued):
        store.insert_job(
            owner_db,
            _make_job(
                f"cap_queued_{i}",
                str(repo_dir),
                created_at=t0.isoformat(),
                updated_at=t0.isoformat(),
                request_digest=f"cap_dg_{i}",
            ),
            now=t0,
        )
    if reserve_running:
        for i in range(running):
            reserve(
                owner_db,
                run_id=f"cap_run_{i}",
                job_id=f"cap_running_{i}",
                route="openai/gpt-5",
                draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
                size_bucket="M",
                now=t0,
            )


def _cap_tick_kwargs(routes: RoutingSnapshot, policy: ControlPlanePolicy) -> dict[str, Any]:
    from saberops.models import RunStatus

    return {
        "routes": routes,
        "policy": policy,
        "run_status": lambda rid: RunStatus.RUNNING,
    }


def test_scheduler_tick_explicit_limit_cannot_exceed_owner_cap(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F3 a programmatic global_slot_limit cannot raise
    admission above the owner-configured concurrency.max_global cap.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_cap_owner", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_cap_owner"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_cap_jobs(owner_db, repo_dir, running=3, queued=1, t0=t0, reserve_running=True)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = replace(_make_policy(), max_global_concurrent=3)

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
        global_slot_limit=99,  # attempt to raise far above the owner cap of 3
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert launched == []
    assert any(
        isinstance(d, WaitDecision) and d.reason == WaitingReason.CONCURRENCY.value
        for d in res.decisions
    )
    queued = store.get_job(owner_db, "cap_queued_0")
    assert queued is not None and queued.state == JobState.QUEUED


def test_scheduler_tick_explicit_limit_lowers_below_default(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F3 an explicit injection limit lowers the effective
    ceiling below the 15 default (2 running with an injected limit of 2 waits).
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_cap_lower", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_cap_lower"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_cap_jobs(owner_db, repo_dir, running=2, queued=1, t0=t0, reserve_running=True)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()  # no owner key -> deterministic default 15
    assert policy.max_global_concurrent is None

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        clock_fn=lambda: t0,
        global_slot_limit=2,
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert any(
        isinstance(d, WaitDecision)
        and d.reason == WaitingReason.CONCURRENCY.value
        and d.details.get("limit") == 2
        for d in res.decisions
    )


def test_scheduler_tick_cli_and_explicit_limits_both_lower(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F3 the effective ceiling is the minimum of the owner
    cap, the CLI override and any explicit injection.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_cap_min", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_cap_min"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_cap_jobs(owner_db, repo_dir, running=2, queued=1, t0=t0, reserve_running=True)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = replace(_make_policy(), max_global_concurrent=15)

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        clock_fn=lambda: t0,
        global_slot_limit=99,
        max_concurrent_override=2,
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert any(
        isinstance(d, WaitDecision)
        and d.reason == WaitingReason.CONCURRENCY.value
        and d.details.get("limit") == 2
        for d in res.decisions
    )


@pytest.mark.parametrize("bad", [0, -1, True])
def test_scheduler_tick_invalid_explicit_limit_fails_closed(
    owner_db: Database, tmp_path: Path, bad: int
) -> None:
    """Covers: ADR-0002 D7 / F3 an invalid explicit limit fails closed and is
    never interpreted as unlimited.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_cap_bad", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_cap_bad"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_cap_jobs(owner_db, repo_dir, running=0, queued=1, t0=t0)

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = replace(_make_policy(), max_global_concurrent=15)

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
        global_slot_limit=bad,
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is False
    assert res.reason == "invalid_global_concurrency"
    assert res.dispatched == 0
    assert launched == []
    queued = store.get_job(owner_db, "cap_queued_0")
    assert queued is not None and queued.state == JobState.QUEUED


# ---------------------------------------------------------------------------
# R3-M1 — per-pool RUNNING occupancy read must fail closed
# ---------------------------------------------------------------------------

#: Unique fragment of the per-RUNNING-job pool occupancy SELECT in
#: ``scheduler_tick`` (the debt/charge read uses a different WHERE clause).
_OCCUPANCY_SQL_NEEDLE = "WHERE run_id = ? AND state = 'RESERVED'"


class _OccupancyFailingConn:
    """Forwarding connection that fails only the pool-occupancy SELECT.

    ``fail_on=None`` fails every occupancy read; ``fail_on=k`` fails the k-th
    occupancy read across all connections (the counter is shared by the
    injector), so a partial first-succeeded/next-failed scan can be reproduced
    deterministically.
    """

    def __init__(self, real: Any, state: dict[str, int], fail_on: int | None = None) -> None:
        self._real = real
        self._state = state
        self._fail_on = fail_on

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if _OCCUPANCY_SQL_NEEDLE in sql:
            self._state["seen"] += 1
            if self._fail_on is None or self._state["seen"] == self._fail_on:
                raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _install_occupancy_fault(
    owner_db: Database, monkeypatch: pytest.MonkeyPatch, *, fail_on: int | None = None
) -> None:
    """Wrap the owner DB connection so the occupancy scan fails as configured."""
    owner = owner_db.owner_db
    original = owner._get_connection
    state: dict[str, int] = {"seen": 0}

    @contextmanager
    def failing() -> Any:
        with original() as real:
            yield _OccupancyFailingConn(real, state, fail_on)

    monkeypatch.setattr(owner, "_get_connection", failing)


def _mc1_policy() -> ControlPlanePolicy:
    """Policy whose `chatgpt-plus` pool allows exactly one concurrent job."""
    plan = PoolPlan(
        pool_id="chatgpt-plus",
        members=(
            PoolMember(route="openai", multiplier=1.0),
            PoolMember(route="codex", multiplier=1.0),
        ),
        windows=(Window(id="5h", duration_min=300),),
        mode="STRICT",
        red_line_pct=10.0,
        overage_possible=False,
        max_concurrent=1,
    )
    return _make_policy({"chatgpt-plus": plan})


def _seed_reserved_pool_jobs(
    owner_db: Database, repo_dir: Path, t0: datetime, *, running: int, queued: int
) -> None:
    """Seed RUNNING jobs (each holding a RESERVED chatgpt-plus row) plus queued jobs."""
    _seed_cap_jobs(owner_db, repo_dir, running=running, queued=queued, t0=t0)
    for i in range(running):
        reserve(
            owner_db,
            run_id=f"cap_run_{i}",
            job_id=f"cap_running_{i}",
            route="openai/gpt-5",
            draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
            size_bucket="M",
            now=t0,
        )
    record_owner_quota(
        owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0
    )


def test_scheduler_tick_fails_closed_on_pool_occupancy_read_error(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 / R3-M1 an unreadable per-pool RUNNING occupancy scan
    aborts the tick with `ledger_read_error` — never zero or partial occupancy.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "occupancy_err", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_occupancy_err"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    # One RUNNING job already occupies the max_concurrent=1 pool.
    _seed_reserved_pool_jobs(owner_db, repo_dir, t0, running=1, queued=1)

    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _mc1_policy()
    _install_occupancy_fault(owner_db, monkeypatch)

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
        **_cap_tick_kwargs(routes, policy),
    )
    # Fail closed: zero dispatches, no launch, queued job untouched.
    assert res.ran is False
    assert res.reason == "ledger_read_error"
    assert res.dispatched == 0
    assert launched == []
    queued = store.get_job(owner_db, "cap_queued_0")
    assert queued is not None
    assert queued.state == JobState.QUEUED


def test_scheduler_tick_partial_pool_occupancy_read_fails_closed(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 / R3-M1 a partially counted occupancy scan (first
    running job read, next read fails) must abort, never admit on partial counts.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "occupancy_partial", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_occupancy_partial"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_reserved_pool_jobs(owner_db, repo_dir, t0, running=2, queued=1)

    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _mc1_policy()
    # First occupancy scan succeeds, the second raises.
    _install_occupancy_fault(owner_db, monkeypatch, fail_on=2)

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is False
    assert res.reason == "ledger_read_error"
    assert res.dispatched == 0
    assert launched == []
    queued = store.get_job(owner_db, "cap_queued_0")
    assert queued is not None
    assert queued.state == JobState.QUEUED


def test_scheduler_tick_pool_max_concurrent_waits_when_occupancy_read_succeeds(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 / F3 normal case — when the occupancy read succeeds the
    owner per-pool `max_concurrent=1` is honoured (queued job waits, no dispatch).
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "occupancy_ok", ttl_seconds=30.0, now=t0)
    assert lease is not None
    repo_dir = tmp_path / "repo_occupancy_ok"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    _seed_reserved_pool_jobs(owner_db, repo_dir, t0, running=1, queued=1)

    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _mc1_policy()

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
        **_cap_tick_kwargs(routes, policy),
    )
    assert res.ran is True
    assert res.dispatched == 0
    assert launched == []
    assert any(
        isinstance(d, WaitDecision) and d.reason == WaitingReason.CONCURRENCY.value
        for d in res.decisions
    )
    queued = store.get_job(owner_db, "cap_queued_0")
    assert queued is not None
    assert queued.state == JobState.QUEUED





def test_scheduler_tick_reconciles_preempted_run_and_releases_reservation(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D8 tick completes a crashed preemption and releases.

    A RUNNING job whose run is conclusively PREEMPTED (crash between run
    terminalization and job requeue) is requeued QUEUED/PREEMPTED with its
    resume capsule, and the old run's reservation is released exactly once
    by tick settlement. No provider, model, or network is involved.
    """
    import subprocess

    from saberops import preemption
    from saberops.git import GitManager
    from saberops.ownership import ProcessIdentity

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "instance_A", ttl_seconds=30.0, now=t0)
    assert lease is not None

    # Setup-only realism: recovery re-verifies the WIP anchor (a real direct
    # checkpoint ref in a real repository) and the resume capsule (a real
    # handoff row).  Only the fixture became realistic; assertions unchanged.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "loop@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Loop"], cwd=repo_dir, check=True)
    (repo_dir / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=repo_dir, check=True, capture_output=True
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    job = _make_job(
        "job_prem",
        str(repo_dir),
        state=JobState.RUNNING,
        current_run_id="run_old",
        run_ids=("run_old",),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_prem",
    )
    store.insert_job(owner_db, job, event_kind="dispatched", now=t0)
    owner_db.create_run(
        Run(
            id="run_old",
            task="task for job_prem",
            target_repo=str(repo_dir),
            base_commit=base_sha,
            tier=Tier.T2,
            training_allowed=True,
            gate_command="make gate",
            status=RunStatus.RUNNING,
            created_at=t0.isoformat(),
            config_json="{}",
        )
    )
    # Setup-only realism: recovery only finishes an *authenticated* bound
    # transaction, so seed the real protocol state (claimed executor
    # generation, authentic decision over the current attempt snapshot, real
    # direct anchor ref, real capsule) before the crash to PREEMPTED.
    owner = owner_db.claim_execution_owner(
        "run_old",
        identity=ProcessIdentity(
            host=ProcessIdentity.for_self().host,
            boot_id="00000000-0000-0000-0000-000000000000",
            pid=2147483646,
            start_ticks=None,
            session_id=None,
            process_group_id=None,
        ),
    )
    job_after = store.get_job(owner_db, "job_prem")
    assert job_after is not None
    decision = preemption.build_preemption_decision(
        owner_db, run_id="run_old", job=job_after,
        owner_id=owner.owner_id, owner_generation=owner.generation,
        decision_ref="test:tick-bound",
    )
    assert preemption.record_preparation_decision(owner_db, decision)
    GitManager.anchor_checkpoint_ref(repo_dir, "job_prem", 0, base_sha)
    from saberops.handoff import create_handoffs

    _, capsule = create_handoffs(owner_db, "run_old")
    owner_db.record_event(
        "run_old",
        "preemption_committed",
        payload={
            "version": 2,
            "job_id": "job_prem",
            "checkpoint_sha": base_sha,
            "checkpoint_ref": "refs/saberops/checkpoints/job_prem/0",
            "handoff_id": capsule.id,
            "not_before": None,
            "preemptions_before": 0,
            "preemptions_next": 1,
            "owner_id": owner.owner_id,
            "owner_generation": owner.generation,
        },
    )
    owner_db.update_run(
        run_id="run_old",
        status=RunStatus.PREEMPTED,
        completed_at=t0.isoformat(),
        result_summary="crashed mid-protocol",
    )
    reserve(
        owner_db,
        run_id="run_old",
        job_id="job_prem",
        route="openai/gpt-5",
        draws=[PoolDraw("chatgpt-plus", "5h")],
        size_bucket="M",
        now=t0,
    )

    launched: list[str] = []

    def mock_launcher(j: Job, dec: Any, run_id: str) -> None:
        launched.append(j.job_id)

    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=_make_routes(()),
        policy=_make_policy(),
        run_status=lambda r_id: RunStatus.PREEMPTED if r_id == "run_old" else None,
        run_launcher=mock_launcher,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    assert len(res.reconciled_changes) == 1
    assert res.reconciled_changes[0].job_id == "job_prem"
    assert res.reconciled_changes[0].to_state == JobState.QUEUED

    current = store.get_job(owner_db, "job_prem")
    assert current is not None
    assert current.state == JobState.QUEUED
    assert current.preemptions == 1
    assert current.resume_from is not None
    assert current.resume_from.run_id == "run_old"
    # The empty worker chain parks the job under the pre-existing M2-4b wait
    # semantics; resume capsule, bound count, and timing survive relabeling.
    assert current.resume_from.checkpoint_sha == base_sha

    with owner_connection(owner_db) as conn:
        row = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_old'"
        ).fetchone()
    assert row is not None and row["state"] == "RELEASED"


def test_default_run_launcher_stages_verified_resume(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D8 dispatch wires the verified capsule into the new run.

    The launcher anchors the new run at the original job base and stages the
    checkpoint plus capsule through the run's ordinary worker-checkpoint
    channel; admission, posture, and lease behavior are untouched (a stubbed
    supervisor launch proves no execution policy is decided here).
    """
    import subprocess

    from saberops.capacity.scheduler import DispatchDecision
    from saberops.config import resolve_run_config

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "loop@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Loop"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    from saberops import preemption as _preemption

    job = _make_job(
        "job_resume",
        str(repo),
        created_at=t0.isoformat(),
        updated_at=t0.isoformat(),
        request_digest="dig_resume",
    )
    # The stored base must resolve for resume verification.
    job = replace(job, base_sha=base)
    store.insert_job(owner_db, job, now=t0)
    store.mark_dispatched(owner_db, "job_resume", "run_old", now=t0)
    owner_db.create_run(
        Run(
            id="run_old",
            task="task for job_resume",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T2,
            training_allowed=True,
            gate_command="make gate",
            status=RunStatus.RUNNING,
            created_at=t0.isoformat(),
            config_json="{}",
        )
    )
    owner_db.record_event(
        "run_old",
        "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )
    owner_db.record_event(
        "run_old", "no_eligible_candidate", payload={"outcome": "NO_ELIGIBLE_CANDIDATE"}
    )
    owner_db.create_attempt(
        Attempt(
            id="run_old_a1",
            run_id="run_old",
            attempt_number=1,
            provider="synthetic",
            model="synthetic",
            worktree_path=str(repo),
            branch_name="orch/run_old/a1",
            status=AttemptStatus.FAILED,
            created_at=t0.isoformat(),
            completed_at=t0.isoformat(),
            interruption_reason="QUOTA_EXHAUSTED",
        )
    )
    out = _preemption.complete_preemption(
        owner_db, run_id="run_old", repo_path=repo, base_sha=base, now=t0
    )
    assert out.owned
    queued = store.get_job(owner_db, "job_resume")
    assert queued is not None and queued.resume_from is not None

    monkeypatch.setattr(
        "saberops.run_creation.resolve_effective_run_config",
        lambda req: resolve_run_config(
            task=req.task,
            target_repo=req.repo_path,
            gate_command="make gate",
        ),
    )
    monkeypatch.setattr(
        "saberops.supervisor.RunSupervisor.ensure_running",
        lambda self, run_id, config=None: True,
    )
    decision = DispatchDecision(
        job=queued,
        candidate=_make_candidate("openai", "gpt-5"),
        pool_id=None,
        binding_window=None,
        score=1.0,
        cost_estimate=1.0,
    )
    _default_run_launcher(owner_db, queued, decision, "run_new")

    assert owner_db.get_run("run_new") is not None
    staged = owner_db.get_latest_worker_checkpoint("run_new")
    assert staged is not None
    assert staged.checkpoint_sha == queued.resume_from.checkpoint_sha
    assert staged.handoff_id == queued.resume_from.handoff_id
    prepared = owner_db.get_events_by_type("run_new", "resume_prepared")
    assert len(prepared) == 1



# ---------------------------------------------------------------------------
# ADR-0002 D7 — legacy RUNNING without a trustworthy reservation fails closed
# ---------------------------------------------------------------------------


def test_scheduler_tick_fails_closed_on_legacy_running_without_reservation(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 a RUNNING job with no live RESERVED row holds the
    whole tick: no reconcile side effect, no schedule, no dispatch, no CAS, no
    provider call and no ledger write.  A successful empty reservation read is
    not proof the pool is free.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "legacy_hold", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_legacy"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # The offending legacy row: RUNNING with a run id but zero RESERVED rows.
    store.insert_job(
        owner_db,
        _make_job(
            "legacy_no_reservation",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id="run_legacy_nores",
            run_ids=("run_legacy_nores",),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_legacy_nores",
        ),
        now=t0,
    )
    # A fully valid RUNNING job whose run looks terminal.  Its reservation is
    # live, so only the legacy row above holds the tick; the guard must run
    # BEFORE reconciliation, or this job would be settled to SUCCEEDED.
    store.insert_job(
        owner_db,
        _make_job(
            "valid_before_reconcile",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id="run_valid_completed",
            run_ids=("run_valid_completed",),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_valid_before",
        ),
        now=t0,
    )
    reserve(
        owner_db,
        run_id="run_valid_completed",
        job_id="valid_before_reconcile",
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=t0,
    )
    store.insert_job(
        owner_db,
        _make_job(
            "legacy_queued",
            str(repo_dir),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_legacy_queued",
        ),
        now=t0,
    )

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy()

    def legacy_status(rid: str) -> RunStatus:
        return RunStatus.COMPLETED if rid == "run_valid_completed" else RunStatus.RUNNING

    launched: list[str] = []

    def _tick() -> Any:
        return scheduler_tick(
            owner_db,
            lease,
            now=t0,
            routes=routes,
            policy=policy,
            run_launcher=lambda j, d, r: launched.append(j.job_id),
            run_status=legacy_status,
            clock_fn=lambda: t0,
        )

    res = _tick()
    assert res.ran is False
    assert res.reason == "legacy_running_without_reservation"
    assert res.dispatched == 0
    assert res.decisions == ()
    assert res.reconciled_changes == ()
    assert launched == []
    assert res.legacy_running_jobs == ("legacy_no_reservation",)

    # No reconciliation ran: the valid terminal-status job is still RUNNING and
    # its reservation is untouched, and the queued job was never touched.
    hold_after = store.get_job(owner_db, "valid_before_reconcile")
    assert hold_after is not None
    assert hold_after.state == JobState.RUNNING
    with owner_connection(owner_db) as conn:
        valid_state = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_valid_completed'"
        ).fetchone()["state"]
        n_res = conn.execute(
            "SELECT COUNT(*) AS n FROM capacity_reservations"
        ).fetchone()["n"]
        n_events = conn.execute(
            "SELECT COUNT(*) AS n FROM job_events WHERE job_id = 'legacy_queued'"
        ).fetchone()["n"]
    assert valid_state == "RESERVED"
    assert n_res == 1  # only the valid job's own reservation; no backfill
    assert n_events == 1  # the submit event only; the tick added nothing

    queued_after = store.get_job(owner_db, "legacy_queued")
    assert queued_after is not None
    assert queued_after.state == JobState.QUEUED
    assert queued_after.waiting_reason == WaitingReason.NEW
    assert queued_after.current_run_id is None

    # Idempotence: a second tick reports exactly the same hold, with no new
    # side effects anywhere.
    res2 = _tick()
    assert res2.ran is False
    assert res2.reason == res.reason
    assert res2.legacy_running_jobs == res.legacy_running_jobs
    assert res2.dispatched == 0
    assert launched == []
    with owner_connection(owner_db) as conn:
        n_res2 = conn.execute(
            "SELECT COUNT(*) AS n FROM capacity_reservations"
        ).fetchone()["n"]
    assert n_res2 == 1


@pytest.mark.parametrize("blank_run_id", [None, ""])
def test_scheduler_tick_fails_closed_on_running_job_without_run_id(
    owner_db: Database, tmp_path: Path, blank_run_id: str | None
) -> None:
    """Covers: ADR-0002 D7 a *real* legacy RUNNING row persisted with a NULL or
    empty ``current_run_id`` is never silently treated as zero occupancy and
    never aborts the tick by raising.

    This drives the genuine integration boundary: the row is written straight
    into owner SQLite (the ``Job`` model itself refuses to represent RUNNING
    without a run id) and ``scheduler_tick`` is called with NO monkeypatch of
    ``store.list_jobs_raw``.  The tick must hold with an owner-visible
    diagnostic, zero dispatch, and no side effects, and stay stable across a
    repeated tick.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "legacy_null_run", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_legacy_null"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # The offending legacy row: RUNNING with a NULL/empty run id, written as a
    # real SQLite row because the model cannot represent it.
    with owner_connection(owner_db) as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, caller_id, repo, base_ref, base_sha,"
            " goal, requirements_json, priority, state, current_run_id,"
            " created_at, updated_at, request_digest, run_ids_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_null_run",
                "test",
                str(repo_dir),
                "main",
                "abc1234",
                "legacy row without a run id",
                "{}",
                "NORMAL",
                "RUNNING",
                blank_run_id,
                t0.isoformat(),
                t0.isoformat(),
                "dg_null_run",
                "[]",
            ),
        )
        conn.commit()
        present = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs"
            " WHERE job_id = 'legacy_null_run'"
            " AND (current_run_id IS NULL OR current_run_id = '')"
        ).fetchone()["n"]
    assert present == 1  # the legacy row really exists in owner storage

    # A valid RUNNING job with a live reservation and a queued job: neither may
    # be touched while the legacy row holds the tick.
    store.insert_job(
        owner_db,
        _make_job(
            "valid_reserved",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id="run_valid_reserved",
            run_ids=("run_valid_reserved",),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_valid_reserved",
        ),
        now=t0,
    )
    reserve(
        owner_db,
        run_id="run_valid_reserved",
        job_id="valid_reserved",
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=t0,
    )
    store.insert_job(
        owner_db,
        _make_job(
            "legacy_queued",
            str(repo_dir),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_legacy_queued",
        ),
        now=t0,
    )

    launched: list[str] = []

    def _tick() -> Any:
        return scheduler_tick(
            owner_db,
            lease,
            now=t0,
            run_launcher=lambda j, d, r: launched.append(j.job_id),
            probes={},
            clock_fn=lambda: t0,
        )

    res = _tick()
    assert res.ran is False
    assert res.reason == "legacy_running_without_reservation"
    assert res.legacy_running_jobs == ("legacy_null_run",)
    assert res.dispatched == 0
    assert launched == []

    # No reconcile/schedule side effects: the valid reservation is untouched
    # and the queued job is unchanged.
    with owner_connection(owner_db) as conn:
        valid_state = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_valid_reserved'"
        ).fetchone()["state"]
        n_res = conn.execute(
            "SELECT COUNT(*) AS n FROM capacity_reservations"
        ).fetchone()["n"]
    assert valid_state == "RESERVED"
    assert n_res == 1  # no backfill for the legacy row
    queued_after = store.get_job(owner_db, "legacy_queued")
    assert queued_after is not None
    assert queued_after.state == JobState.QUEUED
    assert queued_after.current_run_id is None

    # Repeated tick is stable and side-effect free.
    res2 = _tick()
    assert res2.ran is False
    assert res2.reason == res.reason
    assert res2.legacy_running_jobs == res.legacy_running_jobs
    assert res2.dispatched == 0
    assert launched == []
    with owner_connection(owner_db) as conn:
        n_res2 = conn.execute(
            "SELECT COUNT(*) AS n FROM capacity_reservations"
        ).fetchone()["n"]
    assert n_res2 == 1


def test_scheduler_tick_legacy_guard_allows_valid_reserved_running_job(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7 the legacy-RUNNING guard does not block a valid
    in-use pool: a RUNNING job with a live RESERVED row still counts against
    its pool, stays untouched, and an eligible queued job still dispatches
    when a pool slot is free.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "legacy_guard_ok", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_legacy_ok"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    store.insert_job(
        owner_db,
        _make_job(
            "valid_running",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id="run_valid",
            run_ids=("run_valid",),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_valid_run",
        ),
        now=t0,
    )
    reserve(
        owner_db,
        run_id="run_valid",
        job_id="valid_running",
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=t0,
    )
    store.insert_job(
        owner_db,
        _make_job(
            "valid_queued",
            str(repo_dir),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_valid_queued",
        ),
        now=t0,
    )

    record_owner_quota(owner_db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=t0)
    # Owner plan allows two concurrent jobs on the pool, so one live RUNNING
    # occupant must not block the queued job.
    plan = PoolPlan(
        pool_id="chatgpt-plus",
        members=(PoolMember(route="openai", multiplier=1.0),),
        windows=(Window(id="5h", duration_min=300),),
        mode="STRICT",
        red_line_pct=10.0,
        overage_possible=False,
        max_concurrent=2,
    )
    policy = _make_policy({"chatgpt-plus": plan})
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        routes=routes,
        policy=policy,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        run_status=lambda rid: RunStatus.RUNNING,
        clock_fn=lambda: t0,
    )
    assert res.ran is True
    assert res.legacy_running_jobs == ()
    assert res.unresolved_running_jobs == ()
    assert res.dispatched == 1
    assert launched == ["valid_queued"]

    running_after = store.get_job(owner_db, "valid_running")
    assert running_after is not None
    assert running_after.state == JobState.RUNNING
    assert running_after.current_run_id == "run_valid"
    with owner_connection(owner_db) as conn:
        state = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = 'run_valid'"
        ).fetchone()["state"]
    assert state == "RESERVED"
    queued_after = store.get_job(owner_db, "valid_queued")
    assert queued_after is not None and queued_after.state == JobState.RUNNING


def test_scheduler_tick_holds_split_store_running_run_instead_of_failing_it(
    owner_db: Database, tmp_path: Path
) -> None:
    """Covers: ADR-0002 D7/D8 an owner-only status lookup cannot see a
    project-local execution run held in a second SQLite store, so the tick must
    fail closed: no false FAILED job, no RELEASED reservation, no dispatch.

    Two real, separate SQLite files: the job row and its RESERVED capacity row
    live in the owner DB, while the live RUNNING run row lives in another DB.
    ``reconcile_jobs`` alone would read the invisible run as RUN_MISSING.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "split_hold", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_split"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    exec_db = Database(tmp_path / "project_local.db")
    assert exec_db.db_path != owner_db.db_path
    run_id = "run_project_local_0001"
    exec_db.create_run(
        Run(
            id=run_id,
            task="project-local run",
            target_repo=str(repo_dir),
            base_commit="abc1234",
            tier=Tier.T2,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=t0.isoformat(),
            config_json="{}",
        )
    )

    store.insert_job(
        owner_db,
        _make_job(
            "split_running",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id=run_id,
            run_ids=(run_id,),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_split",
        ),
        now=t0,
    )
    reserve(
        owner_db,
        run_id=run_id,
        job_id="split_running",
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=t0,
    )
    # The split really exists: the owner DB cannot see the run.
    assert owner_db.get_run(run_id) is None
    assert exec_db.get_run(run_id) is not None

    def owner_only_status(rid: str) -> RunStatus | None:
        run = owner_db.get_run(rid)
        return run.status if run is not None else None

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_status=owner_only_status,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        probes={},
        clock_fn=lambda: t0,
    )
    assert res.ran is False
    assert res.reason == "running_run_status_unresolved"
    assert res.unresolved_running_jobs == ("split_running",)
    assert res.dispatched == 0
    assert launched == []

    # The valid job and its live reservation are retained, not falsely failed.
    job_after = store.get_job(owner_db, "split_running")
    assert job_after is not None
    assert job_after.state == JobState.RUNNING
    assert job_after.current_run_id == run_id
    assert job_after.failure_reason is None
    with owner_connection(owner_db) as conn:
        rows = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = ?", (run_id,)
        ).fetchall()
    assert [r["state"] for r in rows] == ["RESERVED"]
    # The run itself is untouched in its own store.
    run_after = exec_db.get_run(run_id)
    assert run_after is not None
    assert run_after.status == RunStatus.RUNNING


class _ReservationSetFailingConn:
    """Forwarding connection that fails only the guard's reservation lookup."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if "SELECT DISTINCT run_id FROM capacity_reservations" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_scheduler_tick_legacy_guard_reservation_read_error_fails_closed(
    owner_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0002 D7 an unreadable RUNNING reservation-occupancy snapshot
    aborts the tick with ``ledger_read_error`` before any reconcile or
    dispatch -- an unknown reservation map is never treated as free capacity.
    """
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(owner_db, "guard_read_err", ttl_seconds=30.0, now=t0)
    assert lease is not None

    repo_dir = tmp_path / "repo_guard_err"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    store.insert_job(
        owner_db,
        _make_job(
            "guard_err_running",
            str(repo_dir),
            state=JobState.RUNNING,
            current_run_id="run_guard_err",
            run_ids=("run_guard_err",),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_guard_err",
        ),
        now=t0,
    )
    reserve(
        owner_db,
        run_id="run_guard_err",
        job_id="guard_err_running",
        route="openai/gpt-5",
        draws=[PoolDraw(pool_id="chatgpt-plus", window_id="5h", multiplier=1.0)],
        size_bucket="M",
        now=t0,
    )
    store.insert_job(
        owner_db,
        _make_job(
            "guard_err_queued",
            str(repo_dir),
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
            request_digest="dg_guard_err_q",
        ),
        now=t0,
    )

    owner = owner_db.owner_db
    original = owner._get_connection

    @contextmanager
    def failing() -> Any:
        with original() as real:
            yield _ReservationSetFailingConn(real)

    monkeypatch.setattr(owner, "_get_connection", failing)

    launched: list[str] = []
    res = scheduler_tick(
        owner_db,
        lease,
        now=t0,
        run_launcher=lambda j, d, r: launched.append(j.job_id),
        clock_fn=lambda: t0,
    )
    assert res.ran is False
    assert res.reason == "ledger_read_error"
    assert res.dispatched == 0
    assert launched == []
    queued_after = store.get_job(owner_db, "guard_err_queued")
    assert queued_after is not None and queued_after.state == JobState.QUEUED
