"""M2-1 quota_decision semantics (T-005 a-e + exclusions)."""

from __future__ import annotations

from datetime import UTC, datetime

from saberops.control_plane.quota_policy import (
    QUOTA_EVIDENCE_REQUIRED,
    QUOTA_EXCLUDED_UNTIL_RESET,
    WEEKLY_QUOTA_RED_LINE,
    QuotaDecision,
    QuotaPoolMode,
    QuotaPoolPolicy,
    pools_for,
    quota_decision,
)
from saberops.models import QuotaState, RoutingState, WorkerCandidate


def _state(
    provider: str = "codex",
    pool: str = "weekly",
    remaining: float = 5.0,
    *,
    pool_id: str | None = None,
    freshness: str | None = "FRESH",
    reset_at: str | None = None,
) -> QuotaState:
    return QuotaState(
        provider=provider,
        pool=pool,
        remaining_percent=remaining,
        reset_at=reset_at,
        routing_state=RoutingState.NORMAL,
        updated_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC).isoformat(),
        pool_id=pool_id or pool,
        normalized_remaining_pct=remaining,
        freshness=freshness,
    )


def _exclusion_state(
    pool_id: str = "p",
    provider: str = "codex",
    window: str = "weekly",
    until: str | None = "2026-10-12T00:00:00+00:00",
    scope: str | None = None,
) -> QuotaState:
    return QuotaState(
        provider=provider,
        pool=window,
        remaining_percent=0.0,
        reset_at=until,
        routing_state=RoutingState.NORMAL,
        updated_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC).isoformat(),
        pool_id=pool_id,
        normalized_remaining_pct=0.0,
        freshness="FRESH",
        exclusion_id="exc_test",
        exclusion_until=until,
        exclusion_confidence="EXACT",
        exclusion_model_scope=scope,
    )


def test_order_independence_two_windows() -> None:
    policies = {"p": QuotaPoolPolicy(pool_id="p", red_line_pct=15.0)}
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    low = _state(pool="weekly", remaining=5.0, pool_id="p", reset_at="W1")
    high = _state(pool="5h", remaining=10.0, pool_id="p", reset_at="H1")
    first = quota_decision(cand, [low, high], pool_policies=policies)
    second = quota_decision(cand, [high, low], pool_policies=policies)
    assert first.eligible is False and second.eligible is False
    assert first.normalized_remaining_pct == 5.0
    assert second.normalized_remaining_pct == 5.0
    assert first.reset_at == "W1" and second.reset_at == "W1"


def test_off_denies_even_with_override() -> None:
    policies = {"p": QuotaPoolPolicy(pool_id="p", mode=QuotaPoolMode.OFF)}
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    decision = quota_decision(
        cand, [_state(pool_id="p")], pool_policies=policies, reserve_override=True
    )
    assert decision.eligible is False
    assert decision.reason == "POOL_OFF"


def test_disabled_alias_reads_as_unenforced() -> None:
    assert QuotaPoolMode("DISABLED") is QuotaPoolMode.UNENFORCED
    assert QuotaPoolMode("DISABLED").value == "UNENFORCED"
    assert QuotaPoolMode("disabled") is QuotaPoolMode.UNENFORCED
    policy = QuotaPoolPolicy(pool_id="x", mode="DISABLED")  # type: ignore[arg-type]
    assert policy.mode is QuotaPoolMode.UNENFORCED


def test_overage_stale_denies_when_true_allows_when_false() -> None:
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    stale = _state(pool_id="p", freshness="STALE")
    deny = quota_decision(
        cand, [stale],
        pool_policies={"p": QuotaPoolPolicy(pool_id="p", overage_possible=True)},
    )
    assert deny.eligible is False
    assert deny.reason == QUOTA_EVIDENCE_REQUIRED
    allow = quota_decision(
        cand, [stale],
        pool_policies={"p": QuotaPoolPolicy(pool_id="p", overage_possible=False)},
    )
    assert allow.eligible is True


def test_no_matching_state_with_overage_true_denies() -> None:
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    decision = quota_decision(
        cand, [],
        pool_policies={"codex": QuotaPoolPolicy(pool_id="codex", overage_possible=True)},
    )
    assert decision.eligible is False
    assert decision.reason == QUOTA_EVIDENCE_REQUIRED


def test_reason_codes_per_window() -> None:
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    cases = {
        "5h": "RED_LINE_5H",
        "daily": "RED_LINE_DAILY",
        "weekly": "RED_LINE_WEEKLY",
        "monthly": "RED_LINE_MONTHLY",
        "something-else": "RED_LINE",
    }
    for pool, reason in cases.items():
        policies = {"p": QuotaPoolPolicy(pool_id="p")}
        decision = quota_decision(
            cand, [_state(pool=pool, remaining=1.0, pool_id="p")],
            pool_policies=policies,
        )
        assert decision.reason == reason, pool
    # Deprecated alias still importable.
    assert WEEKLY_QUOTA_RED_LINE == "RED_LINE_WEEKLY"


def test_reserve_override_bypasses_only_red_line() -> None:
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    policies = {"p": QuotaPoolPolicy(pool_id="p", mode=QuotaPoolMode.RESERVE)}
    state = _state(pool_id="p", remaining=1.0)
    denied = quota_decision(cand, [state], pool_policies=policies)
    assert denied.eligible is False
    bypassed: QuotaDecision = quota_decision(
        cand, [state], pool_policies=policies, reserve_override=True
    )
    assert bypassed.eligible is True
    # Override never bypasses OFF / exclusion / evidence.
    off = quota_decision(
        cand, [state],
        pool_policies={"p": QuotaPoolPolicy(pool_id="p", mode=QuotaPoolMode.OFF)},
        reserve_override=True,
    )
    assert off.eligible is False
    excl = quota_decision(
        cand, [_exclusion_state()],
        pool_policies={"p": QuotaPoolPolicy(pool_id="p", members=("codex",))},
        reserve_override=True,
    )
    assert excl.reason == QUOTA_EXCLUDED_UNTIL_RESET


def test_exclusion_denies_under_unenforced_and_with_override() -> None:
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    policies = {
        "p": QuotaPoolPolicy(pool_id="p", mode=QuotaPoolMode.UNENFORCED, members=("codex",))
    }
    plain = quota_decision(cand, [_exclusion_state()], pool_policies=policies)
    assert plain.eligible is False
    assert plain.reason == QUOTA_EXCLUDED_UNTIL_RESET
    assert plain.reset_at == "2026-10-12T00:00:00+00:00"
    forced = quota_decision(
        cand, [_exclusion_state()], pool_policies=policies, reserve_override=True
    )
    assert forced.eligible is False


def test_trailing_glob_and_pools_for() -> None:
    cand = WorkerCandidate(provider="opencode", model="openai/x")
    policies = {
        "shared": QuotaPoolPolicy(pool_id="shared", members=("codex", "opencode/openai/*"))
    }
    assert pools_for(cand, policies) == ["shared"]
    other = WorkerCandidate(provider="opencode", model="opencode-go/x")
    assert pools_for(other, policies) == ["opencode"]
