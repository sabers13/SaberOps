"""Comprehensive tests for canonical admission and scoring policy (ADR-0002 D7).

All tests carry docstring `Covers: ...` and run purely in-process without I/O or network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.capacity.admission import (
    admit,
    matches_model_family,
    reserve_pct,
    score,
)
from saberops.control_plane.plans import (
    PlanSet,
    PoolMember,
    PoolPlan,
    PostureRecord,
    Reserve,
    Window,
)
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicy,
    ProviderPolicyState,
)
from saberops.control_plane.posture import (
    POSTURE_ATTENDED_ONLY,
    POSTURE_EXPIRED,
    POSTURE_FORBIDDEN,
    POSTURE_METERED,
    POSTURE_UNVERIFIED,
)
from saberops.control_plane.quota_policy import (
    POOL_OFF,
    QUOTA_EVIDENCE_REQUIRED,
    QUOTA_EXCLUDED_UNTIL_RESET,
)
from saberops.jobs.models import JobRequirements, JobRole
from saberops.models import QuotaState, RoutingState, WorkerCandidate


def _make_candidate(
    provider: str = "codex",
    model: str = "gpt-5.6-sol",
    role: str | None = None,
) -> WorkerCandidate:
    c = WorkerCandidate(provider=provider, model=model)
    if role is not None:
        object.__setattr__(c, "role", role)
    return c


def _make_policy(
    *,
    postures: dict[str, PostureRecord] | None = None,
    pool_plans: dict[str, PoolPlan] | None = None,
    reserves: tuple[Reserve, ...] = (),
    provider_policies: dict[str, ProviderPolicy] | None = None,
) -> ControlPlanePolicy:
    plan_set = PlanSet(
        accounts={},
        pool_plans=pool_plans or {},
        postures=postures or {},
        reserves=reserves,
    )
    provs = provider_policies or {
        "codex": ProviderPolicy("codex", ProviderPolicyState.ALLOWED),
        "opencode": ProviderPolicy("opencode", ProviderPolicyState.ALLOWED),
        "anthropic": ProviderPolicy("anthropic", ProviderPolicyState.ALLOWED),
        "cline": ProviderPolicy("cline", ProviderPolicyState.ALLOWED),
    }
    return ControlPlanePolicy(
        provider_policies=provs,
        quota_pools={},
        content_hash="test-policy-hash",
        plans=plan_set,
    )


def test_admit_purity_and_determinism() -> None:
    """Covers: ADR-0002 D7 purity and determinism."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    candidate = _make_candidate()
    today_str = (now.date() - timedelta(days=5)).isoformat()
    postures = {
        "codex": PostureRecord(
            selector="codex",
            posture_class="A",
            ttl_days=30,
            verified_on=today_str,
        )
    }
    pool_plans = {
        "chatgpt-plus": PoolPlan(
            pool_id="chatgpt-plus",
            members=(PoolMember(route="codex", multiplier=1.0),),
            windows=(Window(id="5h"),),
            mode="STRICT",
            red_line_pct=15.0,
            overage_possible=False,
        )
    }
    policy = _make_policy(postures=postures, pool_plans=pool_plans)
    states = [
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=80.0,
            reset_at=(now + timedelta(hours=3)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now.isoformat(),
        )
    ]

    decisions = [
        admit(candidate, now=now, policy=policy, quota_states=states)
        for _ in range(5)
    ]
    first = decisions[0]
    assert first.admitted is True
    assert first.score is not None
    assert first.binding_window == "5h"
    for other in decisions[1:]:
        assert other == first


def test_admit_monotonicity_lower_capacity_never_admits_more() -> None:
    """Covers: ADR-0002 D7 parametric monotonicity."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    candidate = _make_candidate()
    postures = {
        "codex": PostureRecord(
            selector="codex",
            posture_class="A",
            ttl_days=30,
            verified_on="2026-10-01",
        )
    }
    pool_plans = {
        "chatgpt-plus": PoolPlan(
            pool_id="chatgpt-plus",
            members=(PoolMember(route="codex", multiplier=1.0),),
            windows=(Window(id="5h"),),
            mode="STRICT",
            red_line_pct=15.0,
            overage_possible=False,
        )
    }
    policy = _make_policy(postures=postures, pool_plans=pool_plans)

    capacities = [90.0, 50.0, 25.0, 20.0, 15.0, 10.0, 0.0]
    results: list[bool] = []
    for cap in capacities:
        states = [
            QuotaState(
                provider="codex",
                pool="chatgpt-plus",
                remaining_percent=cap,
                reset_at=(now + timedelta(hours=2)).isoformat(),
                routing_state=RoutingState.NORMAL,
                updated_at=now.isoformat(),
            )
        ]
        decision = admit(candidate, now=now, policy=policy, quota_states=states)
        results.append(decision.admitted)

    false_seen = False
    for res in results:
        if not res:
            false_seen = True
        if false_seen:
            assert res is False


def test_score_calculation_and_invalid_inputs_fail_closed() -> None:
    """Covers: ADR-0002 D7 score formula and fail closed."""
    res = score(
        remaining_pct=50.0,
        red_line_pct=15.0,
        reserve_pct=5.0,
        cost_estimate_pct=10.0,
        hours_to_reset=2.0,
    )
    assert res == pytest.approx(10.0)

    res_imminent = score(
        remaining_pct=50.0,
        red_line_pct=15.0,
        reserve_pct=5.0,
        cost_estimate_pct=10.0,
        hours_to_reset=0.1,
    )
    assert res_imminent == pytest.approx(80.0)

    with pytest.raises(ValueError, match="finite and non-NaN"):
        score(float("nan"), 15.0, 0.0, 5.0, 2.0)
    with pytest.raises(ValueError, match="finite and non-NaN"):
        score(50.0, 15.0, 0.0, 5.0, float("inf"))
    with pytest.raises(ValueError, match="non-negative"):
        score(50.0, 15.0, 0.0, 5.0, -1.0)


def test_admit_posture_fail_closed() -> None:
    """Covers: ADR-0002 D2 billing posture fail closed."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    fresh_state = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=(now + timedelta(hours=3)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(fresh_state, "freshness", "FRESH")
    states = [fresh_state]

    # Case 1: Posture A with valid TTL -> admitted unattended
    p_a = _make_policy(
        postures={
            "codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")
        }
    )
    d_a = admit(cand, now=now, policy=p_a, quota_states=states)
    assert d_a.admitted is True

    # Case 2: Posture B (ATTENDED_ONLY) -> denied unattended, admitted with explicit_override
    p_b = _make_policy(
        postures={
            "codex": PostureRecord("codex", "B", ttl_days=30, verified_on="2026-10-01")
        }
    )
    d_b_unattended = admit(cand, now=now, policy=p_b, quota_states=states, explicit_override=False)
    assert d_b_unattended.admitted is False
    assert d_b_unattended.reason == "POSTURE_ATTENDED_ONLY"
    d_b_attended = admit(cand, now=now, policy=p_b, quota_states=states, explicit_override=True)
    assert d_b_attended.admitted is True

    # Case 3: Posture D (METERED) -> denied unattended, admitted with explicit_override
    p_d = _make_policy(
        postures={
            "codex": PostureRecord("codex", "D", ttl_days=30, verified_on="2026-10-01")
        }
    )
    d_d_unattended = admit(cand, now=now, policy=p_d, quota_states=states, explicit_override=False)
    assert d_d_unattended.admitted is False
    assert d_d_unattended.reason == "POSTURE_METERED"
    d_d_attended = admit(cand, now=now, policy=p_d, quota_states=states, explicit_override=True)
    assert d_d_attended.admitted is True

    # Case 4: Posture X (FORBIDDEN) -> denied unattended AND denied with explicit_override
    p_x = _make_policy(
        postures={
            "codex": PostureRecord("codex", "X", ttl_days=30, verified_on="2026-10-01")
        }
    )
    res_x_1 = admit(cand, now=now, policy=p_x, quota_states=states, explicit_override=False).reason
    res_x_2 = admit(cand, now=now, policy=p_x, quota_states=states, explicit_override=True).reason
    assert res_x_1 == "POSTURE_FORBIDDEN"
    assert res_x_2 == "POSTURE_FORBIDDEN"

    # Case 5: Posture UNVERIFIED -> denied unattended AND with explicit_override
    p_unverified = _make_policy(
        postures={
            "codex": PostureRecord("codex", "A", ttl_days=30, verified_on=None)
        }
    )
    res_unv_1 = admit(
        cand, now=now, policy=p_unverified, quota_states=states, explicit_override=False
    ).reason
    res_unv_2 = admit(
        cand, now=now, policy=p_unverified, quota_states=states, explicit_override=True
    ).reason
    assert res_unv_1 == "POSTURE_UNVERIFIED"
    assert res_unv_2 == "POSTURE_UNVERIFIED"

    # Case 6: Posture EXPIRED -> denied unattended AND with explicit_override
    p_expired = _make_policy(
        postures={
            "codex": PostureRecord("codex", "A", ttl_days=10, verified_on="2026-09-01")
        }
    )
    res_exp_1 = admit(
        cand, now=now, policy=p_expired, quota_states=states, explicit_override=False
    ).reason
    res_exp_2 = admit(
        cand, now=now, policy=p_expired, quota_states=states, explicit_override=True
    ).reason
    assert res_exp_1 == "POSTURE_EXPIRED"
    assert res_exp_2 == "POSTURE_EXPIRED"


def test_admit_requirements_role_and_capabilities() -> None:
    """Covers: ADR-0002 D7 requirements check."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")}
    )

    # Role mismatch
    cand_worker = _make_candidate(role="WORKER")
    req_surgeon = JobRequirements(role=JobRole.SURGEON)
    d_role = admit(cand_worker, now=now, policy=policy, requirements=req_surgeon)
    assert d_role.admitted is False
    assert d_role.reason == "ROLE_MISMATCH"

    # Required capabilities satisfied / unsatisfied
    # Codex lacks Capability.RESUME
    cand = _make_candidate()
    req_resume = JobRequirements(
        role=JobRole.WORKER,
        required_capabilities=("resume",),
    )
    d_cap = admit(cand, now=now, policy=policy, requirements=req_resume)
    assert d_cap.admitted is False
    assert d_cap.reason == "REQUIRED_CAPABILITY_UNSUPPORTED"


def test_admit_model_family_include_and_exclude() -> None:
    """Covers: ADR-0002 D7 model family include and exclude."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    policy = _make_policy(
        postures={
            "codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01"),
            "opencode": PostureRecord("opencode", "A", ttl_days=30, verified_on="2026-10-01"),
        }
    )

    cand_codex = _make_candidate(provider="codex", model="gpt-5.6-sol")
    cand_opencode = _make_candidate(provider="opencode", model="opencode-go/deepseek-v4-pro")

    assert matches_model_family(cand_codex, "gpt") is True
    assert matches_model_family(cand_codex, "codex") is True
    assert matches_model_family(cand_codex, "deepseek") is False
    assert matches_model_family(cand_opencode, "deepseek") is True

    # Quota states for both
    s_codex = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=80.0,
        reset_at=(now + timedelta(hours=3)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(s_codex, "freshness", "FRESH")
    s_opencode = QuotaState(
        provider="opencode",
        pool="opencode-go",
        remaining_percent=80.0,
        reset_at=(now + timedelta(hours=3)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(s_opencode, "freshness", "FRESH")
    states = [s_codex, s_opencode]

    # model_family_include
    req_include = JobRequirements(role=JobRole.WORKER, model_family_include=("deepseek",))
    res_codex = admit(
        cand_codex, now=now, policy=policy, quota_states=states, requirements=req_include
    ).reason
    assert res_codex == "MODEL_FAMILY_NOT_INCLUDED"
    assert admit(
        cand_opencode, now=now, policy=policy, quota_states=states, requirements=req_include
    ).admitted is True

    # model_family_exclude
    req_exclude = JobRequirements(role=JobRole.WORKER, model_family_exclude=("gpt",))
    res_exclude = admit(
        cand_codex, now=now, policy=policy, quota_states=states, requirements=req_exclude
    ).reason
    assert res_exclude == "MODEL_FAMILY_EXCLUDED"
    assert admit(
        cand_opencode, now=now, policy=policy, quota_states=states, requirements=req_exclude
    ).admitted is True


def test_admit_pool_mode_off() -> None:
    """Covers: ADR-0002 D4 pool mode off."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                mode="OFF",
            )
        },
    )
    decision = admit(cand, now=now, policy=policy)
    assert decision.admitted is False
    assert decision.reason == POOL_OFF


def test_admit_active_exclusion() -> None:
    """Covers: ADR-0002 D4 active exclusions."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                mode="STRICT",
            )
        },
    )
    reset_iso = (now + timedelta(hours=4)).isoformat()
    exclusion = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=0.0,
        reset_at=reset_iso,
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(exclusion, "exclusion_id", "exc-123")
    object.__setattr__(exclusion, "exclusion_until", reset_iso)
    object.__setattr__(exclusion, "until_confidence", "EXACT")

    decision = admit(cand, now=now, policy=policy, quota_states=[exclusion])
    assert decision.admitted is False
    assert decision.reason == QUOTA_EXCLUDED_UNTIL_RESET
    assert decision.reset_at == reset_iso
    assert decision.wait_confidence == "EXACT"


def test_admit_overage_possible_requires_fresh_evidence() -> None:
    """Covers: ADR-0002 D4 overage evidence rule."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                overage_possible=True,
                windows=(Window(id="5h"),),
            )
        },
    )

    # 1. No evidence on overage_possible pool -> QUOTA_EVIDENCE_REQUIRED + needs_probe=True
    d_no_evidence = admit(cand, now=now, policy=policy, quota_states=[], activate_overage=True)
    assert d_no_evidence.admitted is False
    assert d_no_evidence.reason == QUOTA_EVIDENCE_REQUIRED
    assert d_no_evidence.needs_probe is True
    assert d_no_evidence.probe_harness == "codex"

    # 2. STALE evidence -> QUOTA_EVIDENCE_REQUIRED + needs_probe=True
    stale_state = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=90.0,
        reset_at=(now + timedelta(hours=1)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=(now - timedelta(hours=2)).isoformat(),
    )
    object.__setattr__(stale_state, "freshness", "STALE")
    d_stale = admit(
        cand, now=now, policy=policy, quota_states=[stale_state], activate_overage=True
    )
    assert d_stale.admitted is False
    assert d_stale.reason == QUOTA_EVIDENCE_REQUIRED
    assert d_stale.needs_probe is True

    # 3. FRESH evidence -> Admitted!
    fresh_state = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=90.0,
        reset_at=(now + timedelta(hours=1)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(fresh_state, "freshness", "FRESH")
    d_fresh = admit(
        cand, now=now, policy=policy, quota_states=[fresh_state], activate_overage=True
    )
    assert d_fresh.admitted is True


def test_admit_red_line_and_projected_cost() -> None:
    """Covers: ADR-0002 D4 red line and cost multiplier."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=2.0),),
                windows=(Window(id="5h"),),
                red_line_pct=15.0,
                overage_possible=False,
            )
        },
    )
    states = [
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=30.0,
            reset_at=(now + timedelta(hours=3)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now.isoformat(),
        )
    ]
    d_denied = admit(cand, now=now, policy=policy, quota_states=states)
    assert d_denied.admitted is False
    assert d_denied.reason == "RED_LINE_5H"
    assert d_denied.cost_estimate == pytest.approx(16.0)
    assert d_denied.multiplier == pytest.approx(2.0)

    states[0] = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=40.0,
        reset_at=(now + timedelta(hours=3)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    d_admitted = admit(cand, now=now, policy=policy, quota_states=states)
    assert d_admitted.admitted is True


def test_admit_reserves_time_of_day_and_tz() -> None:
    """Covers: ADR-0002 D5 time-of-day reserves."""
    now_in_reserve = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    now_outside_reserve = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)

    reserves = (
        Reserve(
            pool="chatgpt-plus",
            window="5h",
            pct=10.0,
            hours="09:00-17:00",
            days=("Mon", "Tue", "Wed", "Thu", "Fri"),
            tz="UTC",
        ),
    )
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                windows=(Window(id="5h"),),
                red_line_pct=15.0,
                overage_possible=False,
            )
        },
        reserves=reserves,
    )
    assert reserve_pct("chatgpt-plus", "5h", now_in_reserve, policy) == 10.0
    assert reserve_pct("chatgpt-plus", "5h", now_outside_reserve, policy) == 0.0

    cand = _make_candidate()
    states = [
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=30.0,
            reset_at=(now_in_reserve + timedelta(hours=2)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now_in_reserve.isoformat(),
        )
    ]
    d_in = admit(cand, now=now_in_reserve, policy=policy, quota_states=states)
    assert d_in.admitted is False
    assert d_in.reason == "RED_LINE_5H"

    d_out = admit(cand, now=now_outside_reserve, policy=policy, quota_states=states)
    assert d_out.admitted is True


def test_admit_concurrency_limits() -> None:
    """Covers: ADR-0002 D4 concurrency limits."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                max_concurrent=2,
            )
        },
    )

    d1 = admit(cand, now=now, policy=policy, running_counts={"chatgpt-plus": 1})
    assert d1.admitted is True

    d2 = admit(cand, now=now, policy=policy, running_counts={"chatgpt-plus": 2})
    assert d2.admitted is False
    assert d2.reason == "CONCURRENCY_LIMIT"

    d_global = admit(cand, now=now, policy=policy, global_running=5, max_global_concurrent=5)
    assert d_global.admitted is False
    assert d_global.reason == "GLOBAL_CONCURRENCY_LIMIT"


def test_admit_multiple_windows_binds_at_minimum_pace() -> None:
    """Covers: ADR-0002 D7 multi-window binding pace."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-01")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                windows=(
                    Window(id="5h"),
                    Window(id="weekly"),
                ),
                red_line_pct=15.0,
                overage_possible=False,
            )
        },
    )

    states = [
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=80.0,
            reset_at=(now + timedelta(hours=3)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now.isoformat(),
        ),
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=20.0,
            reset_at=(now + timedelta(hours=50)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now.isoformat(),
        ),
    ]
    object.__setattr__(states[0], "window_id", "5h")
    object.__setattr__(states[1], "window_id", "weekly")

    decision = admit(cand, now=now, policy=policy, quota_states=states)
    assert decision.admitted is True
    assert decision.binding_window == "weekly"
    assert decision.score == pytest.approx(0.05)


def test_posture_first_prevents_unconfigured_fail_open() -> None:
    """Covers: ADR-0002 D2/F1 posture checked before unconfigured capacity grace (fail-closed)."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    cand = WorkerCandidate("codex", "gpt-5.6-sol")

    # Unverified posture with explicit capacity_configured=False must still deny
    unverified_policy = _make_policy()  # codex posture has verified_on=2026-10-14
    # Break verified_on to make it unverified
    unverified_policy.plans.postures["codex"] = PostureRecord(
        "codex", "A", ttl_days=30, verified_on=None
    )
    dec = admit(cand, now=now, policy=unverified_policy, capacity_configured=False)
    assert dec.admitted is False
    assert dec.reason == POSTURE_UNVERIFIED

    # Posture B (attended-only) with capacity_configured=False must still deny unattended
    b_policy = _make_policy()
    b_policy.plans.postures["codex"] = PostureRecord(
        "codex", "B", ttl_days=30, verified_on="2026-10-14"
    )
    dec_b = admit(cand, now=now, policy=b_policy, capacity_configured=False)
    assert dec_b.admitted is False
    assert dec_b.reason == POSTURE_ATTENDED_ONLY

    # Posture D (metered) with capacity_configured=False must still deny unattended
    d_policy = _make_policy()
    d_policy.plans.postures["codex"] = PostureRecord(
        "codex", "D", ttl_days=30, verified_on="2026-10-14"
    )
    dec_d = admit(cand, now=now, policy=d_policy, capacity_configured=False)
    assert dec_d.admitted is False
    assert dec_d.reason == POSTURE_METERED

    # Posture X with capacity_configured=False must still deny
    x_policy = _make_policy()
    x_policy.plans.postures["codex"] = PostureRecord(
        "codex", "X", ttl_days=30, verified_on="2026-10-14"
    )
    dec_x = admit(cand, now=now, policy=x_policy, capacity_configured=False)
    assert dec_x.admitted is False
    assert dec_x.reason == POSTURE_FORBIDDEN

    # Expired posture with capacity_configured=False must still deny
    exp_policy = _make_policy()
    exp_policy.plans.postures["codex"] = PostureRecord(
        "codex", "A", ttl_days=1, verified_on="2026-10-01"
    )
    dec_exp = admit(cand, now=now, policy=exp_policy, capacity_configured=False)
    assert dec_exp.admitted is False
    assert dec_exp.reason == POSTURE_EXPIRED

    # Verified A/C with capacity_configured=False gracefully admits with reason="unconfigured"
    ver_policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-14")}
    )
    dec_ver = admit(cand, now=now, policy=ver_policy, capacity_configured=False)
    assert dec_ver.admitted is True
    assert dec_ver.reason == "unconfigured"


def test_admit_subtracts_reservations_before_redline() -> None:
    """Covers: ADR-0002 D5/D7/F2 admit subtracts outstanding reservations before red-line check."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    cand = _make_candidate()
    policy = _make_policy(
        postures={"codex": PostureRecord("codex", "A", ttl_days=30, verified_on="2026-10-14")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="codex", multiplier=1.0),),
                windows=(Window(id="5h"),),
                red_line_pct=20.0,
                overage_possible=False,
            )
        },
    )
    # Observation says 50% remaining
    state = QuotaState(
        provider="codex",
        pool="chatgpt-plus",
        remaining_percent=50.0,
        reset_at=(now + timedelta(hours=3)).isoformat(),
        routing_state=RoutingState.NORMAL,
        updated_at=now.isoformat(),
    )
    object.__setattr__(state, "window_id", "5h")

    # Without reservations, 50% remaining - 5% cost = 45% >= 20% red-line: ADMITS
    dec_no_res = admit(cand, now=now, policy=policy, quota_states=[state])
    assert dec_no_res.admitted is True

    # With 30% prior reservations: effective remaining is 50 - 30 = 20%.
    # 20% - 5% cost = 15% < 20% red-line: DENIED with red-line reason!
    dec_with_res = admit(
        cand,
        now=now,
        policy=policy,
        quota_states=[state],
        reservations={("chatgpt-plus", "5h"): 30.0},
    )
    assert dec_with_res.admitted is False
    assert dec_with_res.reason == "RED_LINE_5H"


def test_admit_computes_draws_for_all_drawn_pools() -> None:
    """Covers: ADR-0002 D1/D7/F2 route drawing multiple pools records draws for all pools."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    cand = _make_candidate("opencode", "gpt-5")
    policy = _make_policy(
        postures={
            "opencode": PostureRecord("opencode", "A", ttl_days=30, verified_on="2026-10-14")
        },
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="opencode", multiplier=1.0),),
                windows=(Window(id="5h"),),
                red_line_pct=10.0,
                overage_possible=False,
            ),
            "opencode-go": PoolPlan(
                pool_id="opencode-go",
                members=(PoolMember(route="opencode", multiplier=2.0),),
                windows=(Window(id="daily"),),
                red_line_pct=10.0,
                overage_possible=False,
            ),
        },
    )
    states = [
        QuotaState(
            "opencode",
            "chatgpt-plus",
            80.0,
            (now + timedelta(hours=3)).isoformat(),
            RoutingState.NORMAL,
            now.isoformat(),
        ),
        QuotaState(
            "opencode",
            "opencode-go",
            90.0,
            (now + timedelta(hours=20)).isoformat(),
            RoutingState.NORMAL,
            now.isoformat(),
        ),
    ]
    object.__setattr__(states[0], "window_id", "5h")
    object.__setattr__(states[1], "window_id", "daily")

    dec = admit(cand, now=now, policy=policy, quota_states=states)
    assert dec.admitted is True
    assert set(dec.drawn_pools) == {"chatgpt-plus", "opencode-go"}
    assert len(dec.draws) == 2
    draw_map = {(d.pool_id, d.window_id): d.multiplier for d in dec.draws}
    assert draw_map[("chatgpt-plus", "5h")] == 1.0
    assert draw_map[("opencode-go", "daily")] == 2.0


def test_in_run_unverified_policy_unknown_fails_closed_on_unconfigured_db(tmp_path: Path) -> None:
    """Covers: ADR-0002 D2 / F1 in-run unverified route fails closed even on unconfigured DB
    or with override.
    """
    from saberops.db import Database

    class _AvailableAdapter:
        is_live_dispatch = True

        def is_available(self) -> bool:
            return True

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    blank_db = Database(tmp_path / "blank.sqlite3")
    empty_policy = ControlPlanePolicy(
        provider_policies={},
        quota_pools={},
        content_hash="empty_hash",
        plans=PlanSet.empty(),
    )

    # 1. Candidate with no posture record at in-run seam (requirements=None) on unconfigured DB
    cand = WorkerCandidate("h3", "m1")
    dec = admit(
        cand,
        now=now,
        policy=empty_policy,
        requirements=None,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        db=blank_db,
    )
    assert dec.admitted is False
    assert dec.reason == POSTURE_UNVERIFIED

    # 2. Explicit owner override CANNOT bypass unverified posture (ADR-0002 D2)
    dec_override = admit(
        cand,
        now=now,
        policy=empty_policy,
        requirements=None,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        db=blank_db,
        explicit_override=True,
    )
    assert dec_override.admitted is False
    assert dec_override.reason == POSTURE_UNVERIFIED

    # 3. In-run evaluate_eligibility seam fails closed
    from saberops.dispatch import evaluate_eligibility

    skip = evaluate_eligibility(
        candidate=cand,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        quota_states=[],
        training_allowed=True,
        prompt_bytes=100,
        policy=empty_policy,
        requirements=None,
        db=blank_db,
    )
    assert skip is not None


def test_in_run_two_pool_binding_does_not_discard_second_pool() -> None:
    """Covers: ADR-0002 D1/D7 / F2 / M-3: binding_pool_id does not discard second pool
    constraints.
    """
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    cand = _make_candidate("opencode", "gpt-5")
    policy = _make_policy(
        postures={
            "opencode": PostureRecord("opencode", "A", ttl_days=30, verified_on="2026-10-14")
        },
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="opencode", multiplier=1.0),),
                windows=(Window(id="5h"),),
                red_line_pct=10.0,
                overage_possible=False,
            ),
            "opencode-go": PoolPlan(
                pool_id="opencode-go",
                members=(PoolMember(route="opencode", multiplier=1.0),),
                windows=(Window(id="daily"),),
                red_line_pct=50.0,
                overage_possible=False,
            ),
        },
    )
    # chatgpt-plus is healthy (80% rem), but opencode-go is exhausted (20% rem < 50% red-line)
    states = [
        QuotaState(
            "opencode",
            "chatgpt-plus",
            80.0,
            (now + timedelta(hours=3)).isoformat(),
            RoutingState.NORMAL,
            now.isoformat(),
        ),
        QuotaState(
            "opencode",
            "opencode-go",
            20.0,
            (now + timedelta(hours=20)).isoformat(),
            RoutingState.NORMAL,
            now.isoformat(),
        ),
    ]
    object.__setattr__(states[0], "window_id", "5h")
    object.__setattr__(states[1], "window_id", "daily")

    # When binding_pool_id is set to "chatgpt-plus", opencode-go MUST NOT be ignored
    dec = admit(
        cand,
        now=now,
        policy=policy,
        quota_states=states,
        requirements=None,
        binding_pool_id="chatgpt-plus",
    )
    assert dec.admitted is False
    assert dec.reason == "RED_LINE_DAILY"
    assert dec.pool_id == "opencode-go"


def test_in_run_safely_derives_db_reservations(tmp_path: Path) -> None:
    """Covers: ADR-0002 D7 / F2: in-run admission safely derives reservations from db."""
    from saberops.capacity.ledger import reserve
    from saberops.capacity.store import ensure_tables
    from saberops.db import Database

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    cand = _make_candidate("openai", "gpt-5")
    policy = _make_policy(
        postures={"openai": PostureRecord("openai", "A", ttl_days=30, verified_on="2026-10-14")},
        pool_plans={
            "chatgpt-plus": PoolPlan(
                pool_id="chatgpt-plus",
                members=(PoolMember(route="openai", multiplier=1.0),),
                windows=(Window(id="5h"),),
                red_line_pct=15.0,
                overage_possible=False,
            )
        },
    )
    state = QuotaState(
        "openai",
        "chatgpt-plus",
        25.0,
        (now + timedelta(hours=2)).isoformat(),
        RoutingState.NORMAL,
        now.isoformat(),
    )
    object.__setattr__(state, "window_id", "5h")

    db = Database(tmp_path / "res_test.sqlite3")
    ensure_tables(db)

    # Without reservations in db, 25% rem - 5% cost = 20% > 15% red-line: admitted
    dec_clean = admit(cand, now=now, policy=policy, quota_states=[state], requirements=None, db=db)
    assert dec_clean.admitted is True

    # Record 10% active reservation in db
    from saberops.capacity.ledger import PoolDraw
    reserve(
        db,
        run_id="run_existing_active",
        job_id="job_active_1",
        route="openai/gpt-5",
        draws=[PoolDraw("chatgpt-plus", "5h", 1.0)],
        size_bucket="M",
        now=now,
    )

    # Now in-run admit without explicit reservations derives the 10% charge from db:
    # 25% - 10% = 15% effective; 15% - 5% cost = 10% < 15% red-line -> DENIED
    dec_with_db_res = admit(
        cand, now=now, policy=policy, quota_states=[state], requirements=None, db=db
    )
    assert dec_with_db_res.admitted is False
    assert dec_with_db_res.reason == "RED_LINE_5H"



