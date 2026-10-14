"""Comprehensive tests for pure deterministic pacing scheduler (ADR-0002 D7).

All tests carry docstring `Covers: ...` and execute purely in-memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from saberops.capacity.scheduler import (
    DispatchDecision,
    ProbeDecision,
    WaitDecision,
    schedule,
)
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
from saberops.jobs.models import (
    NO_ROUTE_FOR_REQUIREMENTS,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import QuotaState, RoutingState, WorkerCandidate
from saberops.routing_config import RoutingSnapshot

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _cand(provider: str, model: str) -> WorkerCandidate:
    return WorkerCandidate(provider=provider, model=model)


def _job(
    job_id: str,
    *,
    priority: Priority = Priority.NORMAL,
    deadline: datetime | None = None,
    created_at: datetime = _NOW,
    role: JobRole = JobRole.WORKER,
    not_before: datetime | None = None,
    size_hint: str | None = None,
) -> Job:
    req = JobRequirements(
        role=role,
        size_hint=size_hint,
    )
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="owner",
        repo="/tmp/repo",
        base_ref="HEAD",
        base_sha="abc123",
        goal=f"goal for {job_id}",
        acceptance=None,
        requirements=req,
        priority=priority,
        deadline=deadline.isoformat() if deadline else None,
        not_before=not_before.isoformat() if not_before else None,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        failure_reason=None,
        current_run_id=None,
        run_ids=(),
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
        request_digest=f"digest-{job_id}",
    )


def _make_policy(
    pool_plans: dict[str, PoolPlan] | None = None,
    postures: dict[str, PostureRecord] | None = None,
) -> ControlPlanePolicy:
    today_str = (_NOW.date() - timedelta(days=5)).isoformat()
    if postures is None:
        postures = {
            "codex": PostureRecord("codex", "A", ttl_days=30, verified_on=today_str),
            "opencode": PostureRecord("opencode", "A", ttl_days=30, verified_on=today_str),
        }
    providers = {
        name: ProviderPolicy(name, ProviderPolicyState.ALLOWED)
        for name in ("codex", "opencode", "anthropic", "cline")
    }
    base_plans: dict[str, PoolPlan] = {
        "chatgpt-plus": PoolPlan(
            pool_id="chatgpt-plus",
            members=(PoolMember(route="codex", multiplier=1.0),),
            windows=(Window(id="5h", duration_min=300),),
            mode="STRICT",
            red_line_pct=10.0,
            overage_possible=False,
        ),
        "chatgpt-app-opencode": PoolPlan(
            pool_id="chatgpt-app-opencode",
            members=(),
            windows=(),
            mode="OFF",
            overage_possible=False,
        ),
        "opencode-go": PoolPlan(
            pool_id="opencode-go",
            members=(PoolMember(route="opencode", multiplier=1.0),),
            windows=(Window(id="5h", duration_min=300),),
            mode="STRICT",
            red_line_pct=10.0,
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


def _make_pool_plan(
    pool_id: str,
    routes: list[tuple[str, float]],
    *,
    windows: tuple[Window, ...] | None = None,
    overage_possible: bool = False,
    max_concurrent: int | None = None,
    mode: str = "STRICT",
    red_line_pct: float = 10.0,
) -> PoolPlan:
    mem_objs = tuple(
        PoolMember(route=route, multiplier=mult)
        for route, mult in routes
    )
    win_objs = windows if windows else (
        Window(id="5h", duration_min=300),
    )
    return PoolPlan(
        pool_id=pool_id,
        members=mem_objs,
        windows=win_objs,
        mode=mode,
        red_line_pct=red_line_pct,
        overage_possible=overage_possible,
        max_concurrent=max_concurrent,
    )


def _make_quota_state(
    *,
    provider: str,
    pool: str,
    remaining_percent: float,
    reset_at: str,
    routing_state: RoutingState = RoutingState.NORMAL,
    freshness: str | None = None,
) -> QuotaState:
    qs = QuotaState(
        provider=provider,
        pool=pool,
        remaining_percent=remaining_percent,
        reset_at=reset_at,
        routing_state=routing_state,
        updated_at=_NOW.isoformat(),
    )
    if freshness is not None:
        object.__setattr__(qs, "freshness", freshness)
    return qs


def _make_snapshot(
    worker_groups: tuple[tuple[WorkerCandidate, ...], ...],
    pacing_metadata: tuple[int, ...] = (),
) -> RoutingSnapshot:
    pacing_tuples = (("WORKER", pacing_metadata),) if pacing_metadata else ()
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash="test-hash",
        role_chains=(
            ("WORKER", worker_groups),
            ("SURGEON", ((_cand("codex", "gpt-5.6-sol"),),)),
            ("INTEGRATOR", ((_cand("codex", "gpt-5.6-sol"),),)),
        ),
        pacing_metadata=pacing_tuples,
    )


def test_scheduler_determinism_and_purity() -> None:
    """Covers: D7 purity and byte-for-byte deterministic decision ordering."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    policy = _make_policy({"chatgpt-plus": pool})
    reset_at = (_NOW + timedelta(hours=3)).isoformat()
    q_state = _make_quota_state(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=reset_at,
    )
    jobs = [
        _job("j1", created_at=_NOW - timedelta(minutes=5)),
        _job("j2", created_at=_NOW - timedelta(minutes=2)),
    ]

    decisions_1 = schedule(
        now=_NOW,
        queued=jobs,
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q_state],
    )
    decisions_2 = schedule(
        now=_NOW,
        queued=jobs,
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q_state],
    )

    assert len(decisions_1) == 2
    assert len(decisions_2) == 2
    for d1, d2 in zip(decisions_1, decisions_2, strict=True):
        assert d1 == d2
        assert isinstance(d1, DispatchDecision)
        assert d1.candidate == c1


def test_scheduler_priority_and_deadline_ordering() -> None:
    """Covers: D7 job order: URGENT > NORMAL > BACKGROUND, deadline then FIFO."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)], max_concurrent=1)
    policy = _make_policy({"chatgpt-plus": pool})
    q_state = _make_quota_state(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=(_NOW + timedelta(hours=3)).isoformat(),
    )

    j_bg = _job(
        "bg_early", priority=Priority.BACKGROUND, created_at=_NOW - timedelta(hours=2)
    )
    j_norm_no_dl = _job(
        "norm_no_dl", priority=Priority.NORMAL, created_at=_NOW - timedelta(minutes=30)
    )
    j_norm_late_dl = _job(
        "norm_late_dl",
        priority=Priority.NORMAL,
        deadline=_NOW + timedelta(hours=5),
        created_at=_NOW - timedelta(minutes=10),
    )
    j_norm_early_dl = _job(
        "norm_early_dl",
        priority=Priority.NORMAL,
        deadline=_NOW + timedelta(hours=1),
        created_at=_NOW - timedelta(minutes=5),
    )
    j_urgent = _job("urgent_late", priority=Priority.URGENT, created_at=_NOW)

    queued = [j_bg, j_norm_no_dl, j_norm_late_dl, j_norm_early_dl, j_urgent]

    decisions = schedule(
        now=_NOW,
        queued=queued,
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q_state],
    )

    # Concurrency limit is 1, so exactly 1 job will dispatch, others will wait
    job_order = [
        d.job.job_id for d in decisions if isinstance(d, (DispatchDecision, WaitDecision))
    ]
    expected_order = [
        "urgent_late",       # URGENT > NORMAL
        "norm_early_dl",     # NORMAL with earlier deadline
        "norm_late_dl",      # NORMAL with later deadline
        "norm_no_dl",        # NORMAL without deadline
        "bg_early",          # BACKGROUND lowest priority
    ]
    assert job_order == expected_order
    # First job dispatched
    assert isinstance(decisions[0], DispatchDecision)
    assert decisions[0].job.job_id == "urgent_late"
    # Second job waited due to concurrency limit
    assert isinstance(decisions[1], WaitDecision)
    assert decisions[1].reason == WaitingReason.CONCURRENCY.value


def test_scheduler_fallback_first_group_wins() -> None:
    """Covers: D7 fallback: first admissible group beats a higher-score lower group."""
    c_g1 = _cand("codex", "gpt-5.6-sol")
    c_g2 = _cand("opencode", "gpt-5.6-sonnet")

    # Group 1 has c_g1, Group 2 has c_g2
    snapshot = _make_snapshot(((c_g1,), (c_g2,)))

    # Pool 1 (Group 1) has remaining capacity 30 (score lower)
    # Pool 2 (Group 2) has remaining capacity 90 (score much higher)
    p1 = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    p2 = _make_pool_plan("opencode-go", [("opencode", 1.0)])
    policy = _make_policy({"chatgpt-plus": p1, "opencode-go": p2})

    reset = (_NOW + timedelta(hours=2)).isoformat()
    q1 = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=30.0, reset_at=reset
    )
    q2 = _make_quota_state(
        provider="opencode", pool="opencode-go", remaining_percent=90.0, reset_at=reset
    )

    decisions = schedule(
        now=_NOW,
        queued=[_job("j1")],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q1, q2],
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, DispatchDecision)
    # Must choose c_g1 from Group 1, NOT c_g2 from Group 2 even though Group 2 score is higher!
    assert d.candidate == c_g1
    assert d.pool_id == "chatgpt-plus"


def test_scheduler_intra_group_scoring_and_tie_breaking() -> None:
    """Covers: D7 intra-group higher min-window pace wins, deterministic tie-breaking."""
    c1 = _cand("codex", "gpt-5.6-sol")
    c2 = _cand("opencode", "gpt-5.6-spark")
    snapshot = _make_snapshot(((c1, c2),))

    p1 = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    p2 = _make_pool_plan("opencode-go", [("opencode", 1.0)])
    policy = _make_policy({"chatgpt-plus": p1, "opencode-go": p2})

    reset = (_NOW + timedelta(hours=2)).isoformat()
    q1 = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=20.0, reset_at=reset
    )
    q2 = _make_quota_state(
        provider="opencode", pool="opencode-go", remaining_percent=80.0, reset_at=reset
    )

    decisions = schedule(
        now=_NOW,
        queued=[_job("j1")],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q1, q2],
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, DispatchDecision)
    # c2 has higher pace score in the same group -> c2 wins
    assert d.candidate == c2


def test_scheduler_background_min_pace_gating() -> None:
    """Covers: D7 background pace below threshold waits, normal dispatches."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    policy = _make_policy({"chatgpt-plus": pool})

    # Remaining=25.0, red_line=10.0, cost_est=8.0 -> remaining_usable = 7.0
    reset = (_NOW + timedelta(hours=2)).isoformat()
    q = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=25.0, reset_at=reset
    )

    # BACKGROUND job should wait
    bg_decisions = schedule(
        now=_NOW,
        queued=[_job("bg_job", priority=Priority.BACKGROUND)],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
        background_min_pace=4.0,
    )
    assert len(bg_decisions) == 1
    assert isinstance(bg_decisions[0], WaitDecision)
    assert bg_decisions[0].reason == WaitingReason.NO_CAPACITY.value
    assert bg_decisions[0].details.get("min_pace") == 4.0

    # NORMAL job under exact same conditions should dispatch!
    norm_decisions = schedule(
        now=_NOW,
        queued=[_job("norm_job", priority=Priority.NORMAL)],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
        background_min_pace=4.0,
    )
    assert len(norm_decisions) == 1
    assert isinstance(norm_decisions[0], DispatchDecision)
    assert norm_decisions[0].candidate == c1


def test_scheduler_prefer_wait_minutes() -> None:
    """Covers: D7 optional prefer_wait_minutes with EXACT/PARSED ETA."""
    c_g1 = _cand("codex", "gpt-5.6-sol")
    c_g2 = _cand("opencode", "gpt-5.6-sonnet")

    # Group 1 has prefer_wait_minutes=20
    snapshot = _make_snapshot(((c_g1,), (c_g2,)), pacing_metadata=(20, 0))

    p1 = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    p2 = _make_pool_plan("opencode-go", [("opencode", 1.0)])
    policy = _make_policy({"chatgpt-plus": p1, "opencode-go": p2})

    # Group 1 is exhausted (remaining=0), reset in 10 minutes (<= 20 min)
    reset_in_10 = (_NOW + timedelta(minutes=10)).isoformat()
    q1 = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=0.0, reset_at=reset_in_10
    )
    # Group 2 has available capacity right now
    q2 = _make_quota_state(
        provider="opencode", pool="opencode-go", remaining_percent=50.0, reset_at=reset_in_10
    )

    # 1. Reset is in 10 min (<= 20 min): Should wait for Group 1 rather than fall back to Group 2!
    decisions = schedule(
        now=_NOW,
        queued=[_job("j1")],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q1, q2],
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, WaitDecision)
    assert d.candidate == c_g1
    assert d.details.get("prefer_wait_minutes") == 20

    # 2. If reset is in 30 min (> 20 min): Should fall back to Group 2 and dispatch!
    reset_in_30 = (_NOW + timedelta(minutes=30)).isoformat()
    q1_late = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=0.0, reset_at=reset_in_30
    )
    decisions_late = schedule(
        now=_NOW,
        queued=[_job("j1")],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q1_late, q2],
    )
    assert len(decisions_late) == 1
    d_late = decisions_late[0]
    assert isinstance(d_late, DispatchDecision)
    assert d_late.candidate == c_g2
    assert d_late.pool_id == "opencode-go"


def test_scheduler_probe_demand_collection() -> None:
    """Covers: D4 probes: overage without fresh evidence demands probe."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)], overage_possible=True)
    policy = _make_policy({"chatgpt-plus": pool})

    # QuotaState has freshness=None (stale/unknown)
    q = _make_quota_state(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=(_NOW + timedelta(hours=2)).isoformat(),
    )

    decisions = schedule(
        now=_NOW,
        queued=[_job("j1")],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
    )

    # Must return ProbeDecision + WaitDecision(reason=PROBE_PENDING)
    assert len(decisions) == 2
    probe_dec = decisions[0]
    wait_dec = decisions[1]

    assert isinstance(probe_dec, ProbeDecision)
    assert probe_dec.pool_id == "chatgpt-plus"
    assert probe_dec.harness == "codex"

    assert isinstance(wait_dec, WaitDecision)
    assert wait_dec.reason == WaitingReason.PROBE_PENDING.value


def test_scheduler_not_before_delay() -> None:
    """Covers: D7 job order: not_before in the future emits WaitDecision(NEW)."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    policy = _make_policy({"chatgpt-plus": pool})
    q = _make_quota_state(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=(_NOW + timedelta(hours=2)).isoformat(),
    )

    future_time = _NOW + timedelta(minutes=45)
    j = _job("delayed", not_before=future_time)

    decisions = schedule(
        now=_NOW,
        queued=[j],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, WaitDecision)
    assert d.reason == WaitingReason.NEW.value
    assert d.eta == future_time
    assert d.confidence == "EXACT"


def test_scheduler_non_dispatchable_role() -> None:
    """Covers: D7 role validation: non-dispatchable role fails to NO_ROUTE_FOR_REQUIREMENTS."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    policy = _make_policy()

    j = _job("surgeon_job", role=JobRole.SURGEON)
    decisions = schedule(
        now=_NOW,
        queued=[j],
        routing_snapshot=snapshot,
        policy=policy,
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, WaitDecision)
    assert d.reason == WaitingReason.NO_CAPACITY.value
    assert d.details.get("diagnostic") == NO_ROUTE_FOR_REQUIREMENTS


def test_scheduler_monotonicity_lower_capacity_never_admits_more() -> None:
    """Covers: D7 purity: lower remaining capacity cannot admit MORE jobs."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)])
    policy = _make_policy({"chatgpt-plus": pool})
    reset = (_NOW + timedelta(hours=2)).isoformat()

    jobs = [_job(f"j{i}", created_at=_NOW - timedelta(minutes=i)) for i in range(5)]

    # Run across decreasing quota levels
    dispatched_counts: list[int] = []
    for cap in (100.0, 50.0, 20.0, 10.0, 5.0, 0.0):
        q = _make_quota_state(
            provider="codex", pool="chatgpt-plus", remaining_percent=cap, reset_at=reset
        )
        decisions = schedule(
            now=_NOW,
            queued=jobs,
            routing_snapshot=snapshot,
            policy=policy,
            quota_states=[q],
        )
        count = sum(1 for d in decisions if isinstance(d, DispatchDecision))
        dispatched_counts.append(count)

    # Monotonic non-increasing: count[i] >= count[i+1]
    for i in range(len(dispatched_counts) - 1):
        assert dispatched_counts[i] >= dispatched_counts[i + 1]


def test_schedule_simulates_reservations_across_batch() -> None:
    """Covers: ADR-0002 D5/D7/F2 batch simulated reservations prevent over-admitting."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)], red_line_pct=15.0)
    policy = _make_policy({"chatgpt-plus": pool})
    reset = (_NOW + timedelta(hours=2)).isoformat()

    # Remaining is 35%. Red line is 15%. Cost per job is 8.0%.
    # Job 0: 35 - 8 = 27 >= 15 -> dispatches.
    # Job 1: Simulated remaining becomes 35 - 8 = 27. 27 - 8 = 19 >= 15 -> dispatches.
    # Job 2: Simulated remaining becomes 35 - 16 = 19. 19 - 8 = 11 < 15 -> must WAIT!
    q = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=35.0, reset_at=reset
    )
    jobs = [_job(f"j{i}", created_at=_NOW - timedelta(minutes=10 - i)) for i in range(3)]

    decisions = schedule(
        now=_NOW,
        queued=jobs,
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
    )
    assert len(decisions) == 3
    assert isinstance(decisions[0], DispatchDecision)
    assert isinstance(decisions[1], DispatchDecision)
    assert isinstance(decisions[2], WaitDecision)
    assert decisions[2].reason == WaitingReason.NO_CAPACITY.value


def test_schedule_respects_running_counts() -> None:
    """Covers: ADR-0002 D7/F3 running counts per pool enforce max_concurrent."""
    c1 = _cand("codex", "gpt-5.6-sol")
    snapshot = _make_snapshot(((c1,),))
    pool = _make_pool_plan("chatgpt-plus", [("codex", 1.0)], max_concurrent=1)
    policy = _make_policy({"chatgpt-plus": pool})
    reset = (_NOW + timedelta(hours=2)).isoformat()
    q = _make_quota_state(
        provider="codex", pool="chatgpt-plus", remaining_percent=100.0, reset_at=reset
    )
    job = _job("j1")

    # When 1 job is already running on chatgpt-plus, slot is full
    decisions = schedule(
        now=_NOW,
        queued=[job],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q],
        running_counts={"chatgpt-plus": 1},
    )
    assert len(decisions) == 1
    assert isinstance(decisions[0], WaitDecision)
    assert decisions[0].reason == WaitingReason.CONCURRENCY.value


def test_wait_eta_picks_min_from_exact_or_parsed_decisions() -> None:
    """Covers: ADR-0002 D7 / F9: wait ETA picks minimum reset ETA over posture-valid decisions."""
    c1 = _cand("codex", "gpt-5.6-sol")
    c2 = _cand("opencode", "claude-sonnet")
    snapshot = _make_snapshot(((c1,), (c2,)))
    pool1 = _make_pool_plan("codex-pool", [("codex", 1.0)])
    pool2 = _make_pool_plan("opencode-pool", [("opencode", 1.0)])
    policy = _make_policy({"codex-pool": pool1, "opencode-pool": pool2})

    eta1 = _NOW + timedelta(hours=3)
    eta2 = _NOW + timedelta(hours=1)
    q1 = _make_quota_state(
        provider="codex", pool="codex-pool", remaining_percent=0.0, reset_at=eta1.isoformat()
    )
    q2 = _make_quota_state(
        provider="opencode", pool="opencode-pool", remaining_percent=0.0, reset_at=eta2.isoformat()
    )

    job = _job("j_eta")
    decisions = schedule(
        now=_NOW,
        queued=[job],
        routing_snapshot=snapshot,
        policy=policy,
        quota_states=[q1, q2],
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert isinstance(d, WaitDecision)
    assert d.reason == WaitingReason.NO_CAPACITY.value
    # Minimum ETA (1 hour from c2) is picked
    assert d.eta == eta2
    assert d.candidate == c2



