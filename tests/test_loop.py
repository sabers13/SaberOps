"""Unit tests for scheduler loop, single-instance lease, and atomic dispatch (ADR-0002 D7)."""

from __future__ import annotations

import sqlite3
import threading
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
    acquire_lease,
    check_lease,
    release_lease,
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
    Job,
    JobRequirements,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import RunStatus, WorkerCandidate
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



