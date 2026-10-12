"""I-003 membership glue and cross-lane selector contracts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.capacity.probes import handle_probe_result
from saberops.capacity.snapshot import effective_quota_states
from saberops.capacity.store import list_active_exclusions
from saberops.control_plane.plans import (
    effective_quota_pools,
    match_route_selector,
    validate_selector,
)
from saberops.control_plane.policy import ControlPlanePolicy, load_control_plane_policy
from saberops.control_plane.quota_policy import (
    QuotaPoolMode,
    QuotaPoolPolicy,
    _matches,
    quota_decision,
)
from saberops.db import Database
from saberops.models import QuotaState, RoutingState, WorkerCandidate
from saberops.workers.exhaustion import record_exhaustion


@pytest.mark.parametrize("selector", [
    "codex", "opencode/opencode-go/deepseek-v4-pro", "opencode/opencode-go/*",
])
@pytest.mark.parametrize("candidate", [
    WorkerCandidate(provider="codex", model="gpt-x"),
    WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-pro"),
    WorkerCandidate(provider="opencode", model="opencode/big-pickle"),
    WorkerCandidate(provider="opencode", model="opencode-go-extra/model"),
    WorkerCandidate(provider="opencode", model="opencode-go"),
    WorkerCandidate(provider="OPENCODE", model="OPENCODE-GO/DEEPSEEK-V4-PRO"),
])
def test_selector_grammar_agreement(selector: str, candidate: WorkerCandidate) -> None:
    normalized = validate_selector(selector, "quota_pools.sample.members")
    policy = QuotaPoolPolicy(pool_id="sample", members=(normalized,))
    state = QuotaState(
        provider="opencode", pool="sample", remaining_percent=50, reset_at=None,
        routing_state=RoutingState.NORMAL, updated_at="2026-10-12T12:00:00+00:00",
    )
    assert _matches(state, candidate, policy) == match_route_selector(
        normalized, candidate.provider, candidate.model
    )


def test_mid_string_glob_rejected() -> None:
    with pytest.raises(ValueError, match="selector"):
        validate_selector("opencode/open*code/model", "quota_pools.sample.members")


def test_backend_selector_is_posture_only() -> None:
    assert validate_selector("backend:DIRECT_API", "postures", allow_backend=True)
    with pytest.raises(ValueError, match="only allowed in postures"):
        validate_selector("backend:DIRECT_API", "quota_pools.sample.members")


@pytest.fixture
def packaged_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControlPlanePolicy:
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    return load_control_plane_policy()


def test_packaged_codex_exhaustion_shared_pool(
    tmp_path: Path, packaged_policy: ControlPlanePolicy,
) -> None:
    db = Database(tmp_path / "capacity.db")
    pools = effective_quota_pools(packaged_policy)
    now = datetime.now(UTC)
    record_exhaustion(db, provider="codex", model="gpt-x", pool_policies=pools)
    states = effective_quota_states(db, now=now, run_probes=False)
    shared = quota_decision(
        WorkerCandidate(provider="opencode", model="openai/gpt-x"),
        states, pool_policies=pools, now=now,
    )
    go = quota_decision(
        WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-pro"),
        states, pool_policies=pools, now=now,
    )
    assert not shared.eligible and shared.reason == "QUOTA_EXCLUDED_UNTIL_RESET"
    assert go.eligible


def test_packaged_go_zero_probe_does_not_exclude_zen(
    tmp_path: Path, packaged_policy: ControlPlanePolicy,
) -> None:
    db = Database(tmp_path / "capacity.db")
    pools = effective_quota_pools(packaged_policy)
    now = datetime.now(UTC)
    # No injected policy: exercise the production probe loader.
    handle_probe_result(
        db, harness="opencode-go", windows=[("5h", 0.0, None, "UNKNOWN")], now=now,
    )
    assert {row["pool_id"] for row in list_active_exclusions(db, now=now)} == {"opencode-go"}
    states = effective_quota_states(db, now=now, run_probes=False)
    for model, eligible in [
        ("opencode-go/deepseek-v4-pro", False), ("opencode/big-pickle", True),
    ]:
        assert quota_decision(
            WorkerCandidate(provider="opencode", model=model), states,
            pool_policies=pools, now=now,
        ).eligible is eligible


def test_clinepass_route_clears_after_backoff_until_m2_4b(
    tmp_path: Path, packaged_policy: ControlPlanePolicy,
) -> None:
    # Interim behaviour while every effective pool has overage_possible=False;
    # it flips to "kept" when M2-4b activates the evidence rule. (Orchestrator
    # decision 2026-10-12: safe, because the Cline credit balance is $0 and
    # ClinePass exhaustion is a hard stop.)
    db = Database(tmp_path / "capacity.db")
    pools = effective_quota_pools(packaged_policy)
    now = datetime.now(UTC)
    record_exhaustion(
        db, provider="cline", model="cline-pass/test-model", pool_policies=pools
    )
    candidate = WorkerCandidate(provider="cline", model="cline-pass/test-model")
    early = effective_quota_states(db, now=now, run_probes=False)
    assert any(
        state.exclusion_id and state.pool_id == "clinepass" for state in early
    )
    assert not quota_decision(
        candidate, early, pool_policies=pools, now=now,
    ).eligible
    later = now + timedelta(days=2)
    cleared = effective_quota_states(db, now=later, run_probes=False)
    assert [
        state for state in cleared if getattr(state, "exclusion_id", None)
    ] == []
    assert quota_decision(
        candidate, cleared, pool_policies=pools, now=later,
    ).eligible


def test_packaged_codex_ten_pct_red_line_shared_pool(
    tmp_path: Path, packaged_policy: ControlPlanePolicy,
) -> None:
    db = Database(tmp_path / "capacity.db")
    pools = effective_quota_pools(packaged_policy)
    now = datetime.now(UTC)
    handle_probe_result(
        db, harness="codex", windows=[("5h", 10.0, None, "UNKNOWN")],
        now=now, pool_policies=pools,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    denied = [
        WorkerCandidate(provider="codex", model="gpt-x"),
        WorkerCandidate(provider="opencode", model="openai/gpt-x"),
    ]
    for candidate in denied:
        assert not quota_decision(
            candidate, states, pool_policies=pools, now=now,
        ).eligible
    assert quota_decision(
        WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-pro"),
        states, pool_policies=pools, now=now,
    ).eligible


def test_probe_no_membership_writes_provider_labelled_row(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "capacity.db")
    now = datetime.now(UTC)
    handle_probe_result(
        db, harness="claude", windows=[("weekly", 0.0, None, "UNKNOWN")],
        now=now, pool_policies={},
    )
    rows = db.list_quota()
    assert len(rows) == 1
    assert rows[0].provider == "anthropic"
    assert rows[0].pool == "anthropic"


def test_owner_pool_overrides_plan_and_overage_is_deferred(
    packaged_policy: ControlPlanePolicy,
) -> None:
    owner = QuotaPoolPolicy(
        pool_id="chatgpt-plus", members=("opencode/owner/*",),
        mode=QuotaPoolMode.RESERVE, red_line_pct=23, overage_possible=True,
    )
    policy = ControlPlanePolicy(
        packaged_policy.provider_policies, {owner.pool_id: owner},
        packaged_policy.content_hash, packaged_policy.plans,
    )
    pools = effective_quota_pools(policy)
    assert pools[owner.pool_id].members == owner.members
    assert pools[owner.pool_id].mode == owner.mode
    assert pools[owner.pool_id].red_line_pct == owner.red_line_pct
    assert all(pool.overage_possible is False for pool in pools.values())
    assert owner.overage_possible is True
    assert policy.content_hash == packaged_policy.content_hash
