"""Contract test: two dispatch points share canonical admit() and score() (ADR-0002 D7).

Verifies:
1. Shared canonical admission and scoring policy across queued scheduler and in-run dispatch.
2. Graceful degradation when capacity subsystem is unconfigured or tables do not exist.
3. Cross-harness shared pool exhaustion rejects both routes at both dispatch points.
4. Reviewer ladder maintains independent model family exclusion authority.
5. In-run fallback groups order candidates by intra-group pacing score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.capacity.admission import (
    admit,
    is_capacity_subsystem_configured,
    order_candidates_for_role,
)
from saberops.capacity.ledger import record_owner_quota
from saberops.capacity.scheduler import DispatchDecision, WaitDecision, schedule
from saberops.capacity.store import (
    ensure_tables as ensure_capacity_tables,
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
from saberops.db import Database
from saberops.dispatch import (
    SKIP_QUOTA_BLOCKED,
    evaluate_eligibility,
)
from saberops.jobs.models import (
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import WorkerCandidate
from saberops.routing_config import RoutingSnapshot

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _make_candidate(provider: str, model: str, binding_id: str | None = None) -> WorkerCandidate:
    return WorkerCandidate(provider=provider, model=model, binding_id=binding_id)


def _make_policy(*, today_str: str = "2026-10-14") -> ControlPlanePolicy:
    postures = {
        "codex": PostureRecord("codex", "A", ttl_days=30, verified_on=today_str),
        "openai": PostureRecord("openai", "A", ttl_days=30, verified_on=today_str),
        "opencode": PostureRecord("opencode", "A", ttl_days=30, verified_on=today_str),
    }
    providers = {
        name: ProviderPolicy(name, ProviderPolicyState.ALLOWED)
        for name in ("codex", "openai", "opencode")
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
            members=(PoolMember(route="opencode", multiplier=1.0),),
            windows=(Window(id="daily", duration_min=1440),),
            mode="STRICT",
            red_line_pct=15.0,
            overage_possible=False,
        ),
    }
    return ControlPlanePolicy(
        provider_policies=providers,
        quota_pools={},
        content_hash="test-policy-hash",
        plans=PlanSet(accounts={}, pool_plans=base_plans, postures=postures),
    )


def _make_job(
    job_id: str,
    *,
    priority: Priority = Priority.NORMAL,
    role: JobRole = JobRole.WORKER,
    created_at: datetime = _NOW,
) -> Job:
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="owner",
        repo="/tmp/repo",
        base_ref="HEAD",
        base_sha="abc123",
        goal=f"goal for {job_id}",
        acceptance=None,
        requirements=JobRequirements(role=role),
        priority=priority,
        deadline=None,
        not_before=None,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        failure_reason=None,
        current_run_id=None,
        run_ids=(),
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
    )


def _make_routes(
    groups: tuple[tuple[WorkerCandidate, ...], ...],
    *,
    role: JobRole = JobRole.WORKER,
) -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        source="memory",
        source_path="",
        content_hash="h1",
        role_chains=((role.value, groups),),
        pacing_metadata=((role.value, tuple(0 for _ in groups)),),
    )


@pytest.fixture()
def owner_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    cfg = tmp_path / "cfg"
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    cfg.mkdir()
    state.mkdir()
    cache.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    db_file = tmp_path / "owner.sqlite3"
    db = Database(db_file)
    ensure_capacity_tables(db)
    return db


class _AvailableAdapter:
    def is_available(self) -> bool:
        return True

    def max_prompt_bytes(self) -> int | None:
        return None


def test_two_dispatch_points_share_canonical_admit_score(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 queued scheduler and in-run dispatch reach identical admission."""
    now = _NOW
    cand = _make_candidate("openai", "gpt-5")
    policy = _make_policy()

    # 1. 100% capacity: both dispatch points admit
    record_owner_quota(
        owner_db,
        "openai",
        "chatgpt-plus",
        100.0,
        (now + timedelta(hours=2)).isoformat(),
        source="OWNER",
        now=now,
    )
    from saberops.capacity.snapshot import effective_quota_states

    quotas = effective_quota_states(owner_db, now=now)

    # Dispatch point 1: pure schedule (scheduler)
    job = _make_job("job_shared_1")
    routes = _make_routes(((cand,),))
    decisions = schedule(
        now=now,
        queued=[job],
        running=[],
        routing_snapshot=routes,
        policy=policy,
        quota_states=quotas,
    )
    assert len(decisions) == 1
    assert isinstance(decisions[0], DispatchDecision)
    assert decisions[0].candidate.provider == "openai"
    assert decisions[0].candidate.model == "gpt-5"

    # Dispatch point 2: in-run evaluate_eligibility
    skip_reason = evaluate_eligibility(
        candidate=cand,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=True,
        quota_states=quotas,
        prompt_bytes=500,
        policy=policy,
        db=owner_db,
        now=now,
    )
    assert skip_reason is None

    # Decision details match canonical admit
    direct_admit = admit(
        cand,
        now=now,
        policy=policy,
        quota_states=quotas,
        db=owner_db,
    )
    assert direct_admit.admitted is True
    assert direct_admit.is_allowed is True
    assert direct_admit.pool_id == "chatgpt-plus"


def test_unconfigured_capacity_degrades_gracefully(tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 unconfigured capacity degrades gracefully with reason=unconfigured."""
    cand = _make_candidate("codex", "gpt-5.6-sol")
    db_file = tmp_path / "blank.sqlite3"
    blank_db = Database(db_file)

    # When capacity tables do not exist and no quota is recorded
    assert is_capacity_subsystem_configured(db=blank_db) is False

    admit_dec = admit(
        cand,
        now=_NOW,
        db=blank_db,
    )
    assert admit_dec.admitted is True
    assert admit_dec.is_allowed is True
    assert admit_dec.reason == "unconfigured"

    # In-run evaluate_eligibility returns None (eligible!)
    skip = evaluate_eligibility(
        candidate=cand,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=True,
        quota_states=[],
        prompt_bytes=100,
        db=blank_db,
        now=_NOW,
    )
    assert skip is None


def test_exhausted_shared_pool_denies_across_harnesses_at_both_dispatch_points(
    owner_db: Database,
) -> None:
    """Covers: ADR-0002 D7 quota exhaustion on shared pool denies all routes across harnesses."""
    now = _NOW
    policy = _make_policy()

    # 0% capacity on chatgpt-plus pool
    record_owner_quota(
        owner_db,
        "openai",
        "chatgpt-plus",
        0.0,
        (now + timedelta(hours=3)).isoformat(),
        source="OWNER",
        now=now,
    )
    from saberops.capacity.snapshot import effective_quota_states

    quotas = effective_quota_states(owner_db, now=now)

    cand_codex = _make_candidate("codex", "gpt-5.6-sol")
    cand_openai = _make_candidate("openai", "gpt-5")

    # Dispatch point 1 (scheduler): job with both routes cannot dispatch
    job = _make_job("job_exhausted_pool")
    routes = _make_routes(((cand_codex, cand_openai),))
    decisions = schedule(
        now=now,
        queued=[job],
        running=[],
        routing_snapshot=routes,
        policy=policy,
        quota_states=quotas,
    )
    assert len(decisions) == 1
    assert isinstance(decisions[0], WaitDecision)

    # Dispatch point 2 (in-run worker/continuation): both routes return SKIP_QUOTA_BLOCKED
    skip_codex = evaluate_eligibility(
        candidate=cand_codex,
        adapter=None,
        training_allowed=True,
        quota_states=quotas,
        prompt_bytes=100,
        policy=policy,
        db=owner_db,
        now=now,
    )
    skip_openai = evaluate_eligibility(
        candidate=cand_openai,
        adapter=None,
        training_allowed=True,
        quota_states=quotas,
        prompt_bytes=100,
        policy=policy,
        db=owner_db,
        now=now,
    )
    assert skip_codex == SKIP_QUOTA_BLOCKED
    assert skip_openai == SKIP_QUOTA_BLOCKED


def test_reviewer_dispatch_preserves_model_family_exclusion() -> None:
    """Covers: ADR-0002 D7 reviewer dispatch refuses candidates in the worker model family."""
    from saberops.review import _pre_dispatch_skip_reason

    worker = _make_candidate("codex", "gpt-5.6-sol")
    rev_same_family = _make_candidate("openai", "gpt-5.6-terra")
    rev_different_family = _make_candidate("opencode", "deepseek-v4-pro")

    policy = _make_policy()

    # Same model family -> refused with model_family_excluded
    skip_same = _pre_dispatch_skip_reason(
        candidate=rev_same_family,
        adapter=None,
        training_allowed=True,
        quota_states=[],
        prompt_bytes=100,
        policy=policy,
        worker_candidate=worker,
    )
    assert skip_same == "model_family_excluded"

    # Different model family -> permitted
    skip_diff = _pre_dispatch_skip_reason(
        candidate=rev_different_family,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=True,
        quota_states=[],
        prompt_bytes=100,
        policy=policy,
        worker_candidate=worker,
    )
    # Since capacity is unconfigured, it degrades gracefully to None
    assert skip_diff is None


def test_order_candidates_for_role_intra_group_pacing(owner_db: Database) -> None:
    """Covers: ADR-0002 D7 order_candidates_for_role orders by intra-group pacing score."""
    now = _NOW
    policy = _make_policy()

    # 90% capacity on chatgpt-plus, 20% capacity on opencode-go
    record_owner_quota(
        owner_db,
        "openai",
        "chatgpt-plus",
        90.0,
        (now + timedelta(hours=2)).isoformat(),
        source="OWNER",
        now=now,
    )
    record_owner_quota(
        owner_db,
        "opencode",
        "opencode-go",
        20.0,
        (now + timedelta(hours=2)).isoformat(),
        source="OWNER",
        now=now,
    )
    from saberops.capacity.snapshot import effective_quota_states

    quotas = effective_quota_states(owner_db, now=now)

    cand_open = _make_candidate("openai", "gpt-5")
    cand_code = _make_candidate("opencode", "deepseek-v4-pro")

    # Group has cand_code first, then cand_open
    snapshot = _make_routes(((cand_code, cand_open),))
    base = [cand_code, cand_open]

    ordered = order_candidates_for_role(
        JobRole.WORKER,
        snapshot=snapshot,
        base_candidates=base,
        now=now,
        policy=policy,
        quota_states=quotas,
        db=owner_db,
    )
    # Higher score (chatgpt-plus 90%) must be ordered before lower score (opencode-go 20%)
    assert ordered[0].provider == "openai"
    assert ordered[1].provider == "opencode"
