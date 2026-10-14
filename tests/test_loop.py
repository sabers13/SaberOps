"""Unit tests for scheduler loop, single-instance lease, and atomic dispatch (ADR-0002 D7)."""

from __future__ import annotations

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
