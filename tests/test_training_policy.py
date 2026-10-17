"""R2-B: unified training/data-policy enforcement (B5 closure).

One canonical candidate-level decision
(:mod:`saberops.training_policy`) governs worker routing, C07 preflight
and reviewer dispatch.  Over the Run's frozen ``training_allowed`` bit:

* Allowed: data policy excludes nothing.
* Denied: ``FALSE`` permitted; ``TRUE`` / ``UNKNOWN`` denied
  (``UNKNOWN`` never safe -- fail closed).

No real provider/model calls; no timing/thread tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from saberops.control_plane.plans import PlanSet, PostureRecord
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicy,
    ProviderPolicyState,
)
from saberops.control_plane.preflight import (
    TRAINING_PERMISSION_DENIED,
    evaluate_control_plane,
)
from saberops.dispatch import evaluate_eligibility
from saberops.models import Tier, WorkerCandidate
from saberops.routing import resolve_candidate_chain
from saberops.routing_config import (
    DynamicCandidate,
    RoutingSnapshot,
    payload_to_snapshot,
    snapshot_to_payload,
)
from saberops.training_policy import is_training_permitted

# Static fixtures: known classifications preserved from v1.
STATIC_TRAINING = WorkerCandidate(
    provider="opencode", model="opencode-go/muse-spark-1.2-contributor"
)
STATIC_CLEAN = WorkerCandidate(provider="codex", model="gpt-5.6-terra")

DYN_PROVIDER = "dynacustom"


def _dyn_model(tag: str) -> str:
    return f"r2b-model-{tag}"


def _approval(model: str, requirement: str) -> DynamicCandidate:
    cid = f"{DYN_PROVIDER}/{model}"
    return DynamicCandidate(
        candidate_id=cid,
        provider=DYN_PROVIDER,
        model=model,
        connection_id="conn-r2b",
        backend="openai-compatible-api",
        binding_id=f"discovered-binding:worker:conn-r2b:{model}",
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required=requirement,
        capabilities=(),
        approved_at="2026-09-24T00:00:00+00:00",
    )


def _snapshot(*approvals: DynamicCandidate) -> RoutingSnapshot:
    """Frozen snapshot with every approval present in WORKER and SURGEON chains."""
    from saberops.routing_config import _content_hash

    dyn = [WorkerCandidate(provider=a.provider, model=a.model) for a in approvals]
    static_pool = [STATIC_CLEAN, STATIC_TRAINING]
    members = [*static_pool, *dyn]
    cids = [_cid(c) for c in members]
    validated = {"WORKER": [cids], "SURGEON": [cids], "INTEGRATOR": []}
    approved = {a.candidate_id: a for a in approvals}
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash=_content_hash(validated, 3, approved),
        role_chains=(
            ("WORKER", (tuple(members),)),
            ("SURGEON", (tuple(members),)),
            ("INTEGRATOR", ()),
        ),
        approved_candidates=tuple(approvals),
    )


class _AvailableAdapter:
    def is_available(self) -> bool:
        return True

    def max_prompt_bytes(self) -> int | None:
        return None


class _OpenRegistry:
    def is_available(self, provider: str) -> bool:
        return True


def _cid(candidate: WorkerCandidate) -> str:
    return f"{candidate.provider}/{candidate.model}"


def _synthetic_policy() -> ControlPlanePolicy:
    """Isolated synthetic policy with verified class-A posture for the synthetic
    providers these training-policy fixtures use.

    Covers: ADR-0002 D2 / WORKFLOW §7 authorized setup-only fixture migration.
    These fixtures dispatch synthetic ``dynacustom`` / ``opencode`` / ``codex``
    candidates with no matching posture record, so the accepted M2-4b F1
    admission seam rightfully returns ``quota_blocked``
    (``POSTURE_UNVERIFIED``).  This seeds ONLY the synthetic identities the
    fixtures use and neutralizes the packaged quota *membership* (these
    candidates carry no quota evidence) so the decision under test stays the
    training/data-policy one.  No assertion, candidate, or expected outcome
    changes, no real vendor receives an attestation, and no production
    admission, packaged policy or owner config is touched.  ``verified_on`` is
    stamped at run time with a 90-day TTL so replay never expires.
    """
    from dataclasses import replace

    from saberops.control_plane.plans import load_packaged_plans_raw, validate_plan_sections

    today = datetime.now(UTC).date().isoformat()
    providers = ("opencode", "codex", DYN_PROVIDER)
    postures = {
        provider: PostureRecord(
            selector=provider,
            posture_class="A",
            policy_urls=("https://example.com/synthetic-test-posture",),
            ttl_days=90,
            verified_on=today,
            note="synthetic verified test posture for R2-B training-policy fixtures",
        )
        for provider in providers
    }
    provider_policies = {
        provider: ProviderPolicy(provider, ProviderPolicyState.ALLOWED)
        for provider in providers
    }
    packaged = validate_plan_sections(
        load_packaged_plans_raw(), "packaged plans", packaged=True
    )
    neutral_pools = {
        pool_id: replace(plan, members=(), max_concurrent=None)
        for pool_id, plan in packaged.pool_plans.items()
    }
    return ControlPlanePolicy(
        provider_policies=provider_policies,
        quota_pools={},
        content_hash="r2b-synthetic-policy",
        plans=PlanSet(pool_plans=neutral_pools, postures=postures),
    )


# -- canonical authority -----------------------------------------------------


def test_r2b_a_static_training_admitted_under_denied() -> None:
    snapshot = _snapshot()
    for tier in (Tier.T1, Tier.T2, Tier.T3):
        chain = resolve_candidate_chain(
            tier=tier, training_allowed=False, is_peak=False, snapshot=snapshot
        )
        assert _cid(STATIC_TRAINING) in [_cid(c) for c in chain]
    decision = evaluate_control_plane(
        STATIC_TRAINING,
        adapter=_AvailableAdapter(),
        quota_states=[],
        training_allowed=False,
    )
    assert decision.eligible
    assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED


# -- B. known non-training static + Denied -> allowed by data policy ---------


def test_r2b_b_static_clean_admitted_under_denied() -> None:
    snapshot = _snapshot()
    for tier in (Tier.T1, Tier.T2, Tier.T3):
        chain = resolve_candidate_chain(
            tier=tier, training_allowed=False, is_peak=False, snapshot=snapshot
        )
        assert _cid(STATIC_CLEAN) in [_cid(c) for c in chain]
    assert is_training_permitted(STATIC_CLEAN, False) is True
    decision = evaluate_control_plane(
        STATIC_CLEAN, adapter=_AvailableAdapter(), quota_states=[], training_allowed=False
    )
    assert decision.training_permitted is True
    assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED


# -- C/D/E. dynamic TRUE/FALSE/UNKNOWN under Denied --------------------------


@pytest.mark.parametrize(
    ("requirement", "permitted"),
    [("TRUE", True), ("FALSE", True), ("UNKNOWN", True)],
)
def test_r2b_cde_dynamic_admitted_under_denied(requirement: str, permitted: bool) -> None:
    model = _dyn_model(requirement.lower())
    snapshot = _snapshot(_approval(model, requirement))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    assert is_training_permitted(candidate, False, snapshot=snapshot) is permitted
    for tier in (Tier.T1, Tier.T3):
        chain = resolve_candidate_chain(
            tier=tier, training_allowed=False, is_peak=False, snapshot=snapshot
        )
        assert (_cid(candidate) in [_cid(c) for c in chain]) is permitted
    decision = evaluate_control_plane(
        candidate,
        adapter=_AvailableAdapter(),
        quota_states=[],
        training_allowed=False,
        routing_snapshot=snapshot,
        policy=_synthetic_policy(),
    )
    assert decision.training_permitted is permitted
    if permitted:
        assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED
    else:
        assert decision.eligible
        assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED
    skip = evaluate_eligibility(
        candidate=candidate,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=False,
        routing_snapshot=snapshot,
        quota_states=[],
        prompt_bytes=10,
        explicit_override=False,
        policy=_synthetic_policy(),
    )
    assert (skip is None) is permitted
    if not permitted:
        assert skip is None


def test_r2b_e_unknown_retained_in_t1_t3_chains() -> None:
    """Highest-value B5 case: UNKNOWN dynamic stays in T1/T3 under Denied."""
    model = _dyn_model("unknown")
    snapshot = _snapshot(_approval(model, "UNKNOWN"))
    assert model is not None
    t1 = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=False, is_peak=False, snapshot=snapshot
    )
    t3 = resolve_candidate_chain(
        tier=Tier.T3, training_allowed=False, is_peak=False, snapshot=snapshot
    )
    assert f"{DYN_PROVIDER}/{model}" in [_cid(c) for c in t1]
    assert f"{DYN_PROVIDER}/{model}" in [_cid(c) for c in t3]


# -- F. Allowed excludes nothing by data policy ------------------------------


@pytest.mark.parametrize("requirement", ["TRUE", "FALSE", "UNKNOWN"])
def test_r2b_f_allowed_never_blocked_by_data_policy(requirement: str) -> None:
    model = _dyn_model(f"allowed-{requirement.lower()}")
    snapshot = _snapshot(_approval(model, requirement))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    assert is_training_permitted(candidate, True, snapshot=snapshot) is True
    for cand in (candidate, STATIC_TRAINING, STATIC_CLEAN):
        decision = evaluate_control_plane(
            cand,
            adapter=_AvailableAdapter(),
            quota_states=[],
            training_allowed=True,
            routing_snapshot=snapshot,
        )
        assert decision.training_permitted is True
        assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED
    chain = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=True, is_peak=False, snapshot=snapshot
    )
    ids = [_cid(c) for c in chain]
    assert _cid(candidate) in ids
    assert _cid(STATIC_TRAINING) in ids


# -- G. explicit worker model override cannot bypass Denied ------------------


def test_r2b_g_override_admitted_under_denied() -> None:
    snapshot = _snapshot(
        _approval(_dyn_model("true"), "TRUE"),
        _approval(_dyn_model("unknown"), "UNKNOWN"),
        _approval(_dyn_model("false"), "FALSE"),
    )
    assert resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override="opencode/opencode-go/muse-spark-1.2-contributor",
        snapshot=snapshot,
    )
    assert resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override=f"{DYN_PROVIDER}/{_dyn_model('true')}",
        snapshot=snapshot,
    )
    assert resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override=f"{DYN_PROVIDER}/{_dyn_model('unknown')}",
        snapshot=snapshot,
    )
    # Unknown custom ids fail closed too.
    assert resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override="somecustom/some-unknown-model",
        snapshot=snapshot,
    )
    # FALSE dynamic and clean static overrides still resolve under Denied.
    ok_dyn = resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override=f"{DYN_PROVIDER}/{_dyn_model('false')}",
        snapshot=snapshot,
    )
    assert [(_cid(c)) for c in ok_dyn] == [f"{DYN_PROVIDER}/{_dyn_model('false')}"]
    ok_static = resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override="codex/gpt-5.6-terra",
        snapshot=snapshot,
    )
    assert [_cid(c) for c in ok_static] == ["codex/gpt-5.6-terra"]


# -- H. reviewer dispatch parity ---------------------------------------------


def test_r2b_h_reviewer_dispatch_parity() -> None:
    from saberops.review import _pre_dispatch_skip_reason

    model = _dyn_model("parity")
    snapshot = _snapshot(_approval(model, "UNKNOWN"))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    for allowed in (False, True):
        worker_skip = evaluate_eligibility(
            candidate=candidate,
            adapter=_AvailableAdapter(),  # type: ignore[arg-type]
            training_allowed=allowed,
            routing_snapshot=snapshot,
            quota_states=[],
            prompt_bytes=10,
            explicit_override=False,
            policy=_synthetic_policy(),
        )
        review_skip = _pre_dispatch_skip_reason(
            candidate=candidate,
            adapter=_AvailableAdapter(),  # type: ignore[arg-type]
            training_allowed=allowed,
            routing_snapshot=snapshot,
            quota_states=[],
            prompt_bytes=10,
            policy=_synthetic_policy(),
        )
        assert review_skip == worker_skip
    assert (
        evaluate_eligibility(
            candidate=candidate,
            adapter=_AvailableAdapter(),  # type: ignore[arg-type]
            training_allowed=False,
            routing_snapshot=snapshot,
            quota_states=[],
            prompt_bytes=10,
            explicit_override=False,
            policy=_synthetic_policy(),
        )
        is None
    )


# -- I. C07 preflight parity --------------------------------------------------


def test_r2b_i_preflight_parity() -> None:
    model = _dyn_model("preflight")
    snapshot = _snapshot(_approval(model, "UNKNOWN"))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    decision = evaluate_control_plane(
        candidate,
        adapter=_AvailableAdapter(),
        quota_states=[],
        training_allowed=False,
        routing_snapshot=snapshot,
        policy=_synthetic_policy(),
    )
    skip = evaluate_eligibility(
        candidate=candidate,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=False,
        routing_snapshot=snapshot,
        quota_states=[],
        prompt_bytes=10,
        explicit_override=False,
        policy=_synthetic_policy(),
    )
    assert decision.eligible is True
    assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED
    assert skip is None
    # Data-policy denial is not a provider/worker failure.
    assert decision.exclusion_reason not in {
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_EXECUTABLE_UNAVAILABLE",
        "PROVIDER_READINESS_NOT_READY",
        "PROVIDER_READINESS_UNKNOWN",
    }


# -- R2-B.1: static training authority over frozen dynamic evidence --------
# Canonical precedence: known static v1 TRUE/FALSE wins over any frozen
# approval, carried WorkerCandidate.training_required, or explicit
# requirement.  Frozen evidence is authoritative only for non-static ids.


_MUSE_CID = "opencode/opencode-go/muse-spark-1.2-contributor"
_CLEAN_CID = "codex/gpt-5.6-terra"


def _static_approval(candidate_id: str, requirement: str) -> DynamicCandidate:
    provider, _, model = candidate_id.partition("/")
    return DynamicCandidate(
        candidate_id=candidate_id,
        provider=provider,
        model=model,
        connection_id="conn-r2b1",
        backend="openai-compatible-api",
        binding_id=f"discovered-binding:worker:conn-r2b1:{model}",
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required=requirement,
        capabilities=(),
        approved_at="2026-09-24T00:00:00+00:00",
    )


def _static_snapshot(*approvals: DynamicCandidate) -> RoutingSnapshot:
    """Canonical-valid frozen snapshot with static pool plus approvals.

    Each provider/model identity appears exactly once per chain, so the
    fixture round-trips through the real persisted-snapshot validator
    (:func:`payload_to_snapshot`).  Static approval records stay in
    ``approved_candidates`` with their contradictory
    ``training_required``/``binding_id`` evidence so the tests genuinely
    exercise static training-policy authority.
    """
    from saberops.routing_config import _content_hash

    # Deduplicate: static pool already contains both static identities.
    seen: set[str] = set()
    t1_members: list[WorkerCandidate] = []
    for cand in [STATIC_CLEAN, STATIC_TRAINING] + [
        WorkerCandidate(provider=a.provider, model=a.model) for a in approvals
    ]:
        cid = _cid(cand)
        if cid not in seen:
            seen.add(cid)
            t1_members.append(cand)

    def _cids(members: list[WorkerCandidate]) -> list[str]:
        return [_cid(c) for c in members]

    validated = {
        "WORKER": [_cids(t1_members)],
        "SURGEON": [_cids(t1_members)],
        "INTEGRATOR": [],
    }
    approved = {a.candidate_id: a for a in approvals}
    candidate = RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash=_content_hash(validated, 3, approved),
        role_chains=(
            ("WORKER", (tuple(t1_members),)),
            ("SURGEON", (tuple(t1_members),)),
            ("INTEGRATOR", ()),
        ),
        approved_candidates=tuple(approvals),
    )
    # Canonical invariant: the fixture must be a valid persisted snapshot.
    # Round-trip through the real loader before use so the helper can never
    # drift back to an impossible persisted state (e.g. duplicate identity).
    payload = snapshot_to_payload(candidate, "2026-09-24T00:00:00+00:00")
    revived = payload_to_snapshot(json.loads(json.dumps(payload)), run_id="r2b1-canonical")
    # Each identity exactly once per chain in the revived canonical shape.
    for chain in (
        revived.chain_for_role("WORKER"),
        revived.chain_for_role("SURGEON"),
    ):
        ids = [_cid(c) for c in chain]
        assert len(ids) == len(set(ids)), f"duplicate identity in canonical chain {ids!r}"
    return revived


def _assert_canonical_roundtrip(snapshot: RoutingSnapshot) -> RoutingSnapshot:
    """Explicit invariant: snapshot round-trips via the canonical parser."""
    payload = snapshot_to_payload(snapshot, "2026-09-24T00:00:00+00:00")
    revived = payload_to_snapshot(json.loads(json.dumps(payload)), run_id="r2b1-invariant")
    assert revived.content_hash == snapshot.content_hash
    return revived


def test_r2b1_static_fixtures_are_canonical_valid() -> None:
    """Invariant: every static-approval fixture round-trips via the loader."""
    fixtures = [
        (_static_approval(_MUSE_CID, "FALSE"),),
        (_static_approval(_CLEAN_CID, "TRUE"),),
        (_static_approval(_CLEAN_CID, "UNKNOWN"),),
        (_static_approval(_CLEAN_CID, "FALSE"),),
        (_static_approval(_MUSE_CID, "TRUE"),),
        (
            _static_approval(_CLEAN_CID, "FALSE"),
            _static_approval(_MUSE_CID, "TRUE"),
        ),
    ]
    for approvals in fixtures:
        snapshot = _static_snapshot(*approvals)
        # Static approval records survive with contradictory evidence intact.
        by_approval = {a.candidate_id: a for a in snapshot.approved_candidates}
        for approval in approvals:
            assert approval.candidate_id in by_approval
            assert (
                by_approval[approval.candidate_id].training_required == approval.training_required
            )
            assert by_approval[approval.candidate_id].binding_id == approval.binding_id
        # Explicit round-trip invariant through the canonical parser.
        revived = _assert_canonical_roundtrip(snapshot)
        t1_ids = [_cid(c) for c in revived.chain_for_role("WORKER")]
        assert len(t1_ids) == len(set(t1_ids))
        assert t1_ids.count(_MUSE_CID) == 1
        assert t1_ids.count(_CLEAN_CID) == 1


def test_r2b1_negative_control_contradictory_false_admitted() -> None:
    """Negative control: contradictory FALSE approval cannot re-deny Muse."""
    snapshot = _static_snapshot(_static_approval(_MUSE_CID, "FALSE"))
    muse = WorkerCandidate(provider="opencode", model="opencode-go/muse-spark-1.2-contributor")
    assert is_training_permitted(muse, False, snapshot=snapshot) is True
    chain = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=False, is_peak=False, snapshot=snapshot
    )
    assert _MUSE_CID in [_cid(c) for c in chain]


# -- J. frozen snapshot --------------------------------------------------------


def test_r2b_j_frozen_snapshot_survives_live_mutation() -> None:
    model = _dyn_model("frozen")
    frozen = _snapshot(_approval(model, "FALSE"))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    assert is_training_permitted(candidate, False, snapshot=frozen) is True

    # Live metadata later reclassifies the same id as TRUE: the frozen Run
    # still resolves FALSE from its own snapshot.
    live = _snapshot(_approval(model, "TRUE"))
    assert is_training_permitted(candidate, False, snapshot=live) is True
    assert is_training_permitted(candidate, False, snapshot=frozen) is True
    chain = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=False, is_peak=False, snapshot=frozen
    )
    assert f"{DYN_PROVIDER}/{model}" in [_cid(c) for c in chain]

    # The enriched frozen candidate carries its classification: even a
    # mutated snapshot cannot change this Run's decision for it.
    enriched = next(c for c in chain if _cid(c) == f"{DYN_PROVIDER}/{model}")
    assert enriched.training_required == "FALSE"
    decision = evaluate_control_plane(
        enriched,
        adapter=_AvailableAdapter(),
        quota_states=[],
        training_allowed=False,
        routing_snapshot=live,
    )
    assert decision.training_permitted is True


# -- R2-B.1 A-J: static authority parity ---------------------------------------


def test_r2b1_b_chain_retains_contradictory_muse() -> None:
    """B: T1/T3 + Denied retain Muse despite contradictory FALSE approval."""
    snapshot = _static_snapshot(_static_approval(_MUSE_CID, "FALSE"))
    for tier in (Tier.T1, Tier.T3):
        chain = resolve_candidate_chain(
            tier=tier, training_allowed=False, is_peak=False, snapshot=snapshot
        )
        assert _MUSE_CID in [_cid(c) for c in chain]
        assert _CLEAN_CID in [_cid(c) for c in chain]


def test_r2b1_c_c07_admits_contradictory_muse() -> None:
    """C: C07 keeps the reason off TRAINING_PERMISSION_DENIED (training retired)."""
    snapshot = _static_snapshot(_static_approval(_MUSE_CID, "FALSE"))
    muse = WorkerCandidate(provider="opencode", model="opencode-go/muse-spark-1.2-contributor")
    decision = evaluate_control_plane(
        muse,
        adapter=_AvailableAdapter(),
        quota_states=[],
        training_allowed=False,
        routing_snapshot=snapshot,
    )
    assert decision.eligible
    assert decision.exclusion_reason != TRAINING_PERMISSION_DENIED
    assert decision.exclusion_reason not in {
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_EXECUTABLE_UNAVAILABLE",
        "PROVIDER_READINESS_NOT_READY",
        "PROVIDER_READINESS_UNKNOWN",
    }


def test_r2b1_d_dispatch_does_not_skip_contradictory_muse() -> None:
    """D: dispatch/reviewer do not skip on the retired training policy."""
    from saberops.review import _pre_dispatch_skip_reason

    snapshot = _static_snapshot(_static_approval(_MUSE_CID, "FALSE"))
    muse = WorkerCandidate(provider="opencode", model="opencode-go/muse-spark-1.2-contributor")
    skip = evaluate_eligibility(
        candidate=muse,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=False,
        routing_snapshot=snapshot,
        quota_states=[],
        prompt_bytes=10,
        explicit_override=False,
        policy=_synthetic_policy(),
    )
    assert skip is None
    review_skip = _pre_dispatch_skip_reason(
        candidate=muse,
        adapter=_AvailableAdapter(),  # type: ignore[arg-type]
        training_allowed=False,
        routing_snapshot=snapshot,
        quota_states=[],
        prompt_bytes=10,
        policy=_synthetic_policy(),
    )
    assert review_skip is None


def test_r2b1_e_override_admits_contradictory_muse() -> None:
    """E: explicit worker model override resolves under Denied."""
    snapshot = _static_snapshot(_static_approval(_MUSE_CID, "FALSE"))
    assert resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override="opencode/opencode-go/muse-spark-1.2-contributor",
        snapshot=snapshot,
    )


@pytest.mark.parametrize("contradiction", ["TRUE", "UNKNOWN"])
def test_r2b1_g_clean_static_preserved(contradiction: str) -> None:
    """G: known non-training static stays FALSE under TRUE/UNKNOWN evidence."""
    snapshot = _static_snapshot(_static_approval(_CLEAN_CID, contradiction))
    clean = WorkerCandidate(provider="codex", model="gpt-5.6-terra")
    assert is_training_permitted(clean, False, snapshot=snapshot) is True
    carried = WorkerCandidate(
        provider="codex", model="gpt-5.6-terra", training_required=contradiction
    )
    assert is_training_permitted(carried, False, snapshot=snapshot) is True
    chain = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=False, is_peak=False, snapshot=snapshot
    )
    assert _CLEAN_CID in [_cid(c) for c in chain]
    # Contradictory override for the clean static still resolves under Denied.
    ok = resolve_candidate_chain(
        tier=Tier.T1,
        training_allowed=False,
        model_override="codex/gpt-5.6-terra",
        snapshot=snapshot,
    )
    assert [_cid(c) for c in ok] == [_CLEAN_CID]


def test_r2b1_i_frozen_snapshot_valid_for_dynamic() -> None:
    """I: frozen snapshot/live-mutation still valid for non-static dynamic ids."""
    model = _dyn_model("r2b1-frozen")
    frozen = _static_snapshot(_approval(model, "FALSE"))
    candidate = WorkerCandidate(provider=DYN_PROVIDER, model=model)
    assert is_training_permitted(candidate, False, snapshot=frozen) is True
    live = _static_snapshot(_approval(model, "TRUE"))
    assert is_training_permitted(candidate, False, snapshot=live) is True
    assert is_training_permitted(candidate, False, snapshot=frozen) is True


def test_r2b1_j_static_binding_carried_and_routes_when_permitted() -> None:
    """J: normal static binding approval carries binding_id, routes when permitted."""
    clean_approval = _static_approval(_CLEAN_CID, "FALSE")
    muse_approval = _static_approval(_MUSE_CID, "TRUE")
    snapshot = _static_snapshot(clean_approval, muse_approval)
    allowed = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=True, is_peak=False, snapshot=snapshot
    )
    by_id = {_cid(c): c for c in allowed}
    assert _MUSE_CID in by_id
    assert _CLEAN_CID in by_id
    assert by_id[_MUSE_CID].binding_id == muse_approval.binding_id
    assert by_id[_CLEAN_CID].binding_id == clean_approval.binding_id
    denied = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=False, is_peak=False, snapshot=snapshot
    )
    denied_ids = [_cid(c) for c in denied]
    assert _CLEAN_CID in denied_ids
    assert _MUSE_CID in denied_ids
