"""Role-chain routing configuration (schema 3) and migration from tiers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane.preflight import evaluate_control_plane
from saberops.dispatch import routing_context_for_role
from saberops.jobs.models import JobRole
from saberops.models import Tier, WorkerCandidate
from saberops.routing import is_peak_hours, resolve_candidate_chain
from saberops.routing_config import (
    ROUTED_ROLES,
    DynamicCandidate,
    RoutingConfigError,
    _content_hash,
    add_candidate_to_chain,
    canonical_routing_payload,
    get_user_routing_path,
    load_effective_routing,
    load_effective_routing_payload,
    move_group_in_chain,
    payload_to_snapshot,
    remove_candidate_from_chain,
    reorder_chain_groups,
    snapshot_to_payload,
)
from saberops.training_policy import is_training_permitted

A = "codex/gpt-5.6-terra"
B = "opencode/opencode-go/muse-spark-1.2-contributor"
C = "codex/gpt-5.6-sol"


def legacy_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "T1": {"default": [A, B]},
        "T2": {
            "training_allowed": [B, A],
            "training_denied_off_peak": [C],
            "training_denied_peak": [A, C],
        },
        "T3": {"off_peak": [C], "peak": [A, C]},
    }


def v2_document() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "T1": {"default": [A, B]},
        "T2": {"default": [B, A, C]},
        "T3": {"default": [C, A]},
    }


def role_chain_ids(payload: dict[str, Any], role: str) -> list[str]:
    return [group["routes"][0] for group in payload["roles"][role]]


def test_legacy_chains_merge_stably_without_mutating_source() -> None:
    """Covers: ADR-0002 §11 item 2 (v1 merge then v2 → v3 migration)."""
    old = legacy_document()
    original = copy.deepcopy(old)
    current = canonical_routing_payload(old)
    assert old == original
    assert current == {
        "schema_version": 3,
        "roles": {
            "WORKER": [{"routes": [B]}, {"routes": [A]}, {"routes": [C]}],
            "SURGEON": [{"routes": [C]}, {"routes": [A]}],
            "INTEGRATOR": [{"routes": [B]}],
        },
    }


def test_v2_to_v3_migration_is_exact() -> None:
    """Covers: ADR-0002 §11 item 2 (WORKER union order, SURGEON = T3)."""
    old = v2_document()
    original = copy.deepcopy(old)
    current = canonical_routing_payload(old)
    assert old == original
    # WORKER = stable union of T2, T1, T3 (T2 order first, then unseen).
    assert role_chain_ids(current, "WORKER") == [B, A, C]
    # SURGEON = the T3 order.
    assert role_chain_ids(current, "SURGEON") == [C, A]
    # INTEGRATOR = head of T2.
    assert role_chain_ids(current, "INTEGRATOR") == [B]
    # Every route is its own fallback group (today's ordered fallback).
    assert all(
        len(group["routes"]) == 1 for groups in current["roles"].values() for group in groups
    )


def test_v2_migration_preserves_approvals_verbatim() -> None:
    """Covers: ADR-0002 §11 item 2 (approved_candidates carried verbatim)."""
    approval = _approval()
    old = v2_document()
    old["approved_candidates"] = {approval.candidate_id: approval.to_dict()}
    old["T2"]["default"].append(approval.candidate_id)
    original = copy.deepcopy(old)
    current = canonical_routing_payload(old)
    assert old == original
    assert current["approved_candidates"] == old["approved_candidates"]
    worker_ids = [cid for group in current["roles"]["WORKER"] for cid in group["routes"]]
    assert approval.candidate_id in worker_ids


def test_packaged_default_is_migration_of_embedded_document() -> None:
    """Covers: ADR-0002 §11 item 2 (packaged default = migrated schema-2 file)."""
    embedded_32ab051 = {
        "schema_version": 2,
        "T1": {
            "default": [
                "opencode/opencode/nemotron-3.5-lightning-free",
                "opencode/opencode/mimo-v2.5-free",
                "opencode/opencode-go/mimo-v2.5",
                "opencode/opencode-go/deepseek-v4-flash",
                "opencode/opencode/minimax-m3",
                "opencode/opencode/qwen3.7-plus",
                "codex/gpt-5.6-luna",
            ]
        },
        "T2": {
            "default": [
                "opencode/opencode/muse-spark-1.2-contributor-free",
                "opencode/opencode/hy3-free",
                "opencode/opencode-go/muse-spark-1.2-contributor",
                "opencode/opencode-go/deepseek-v4-pro",
                "opencode/opencode/nemotron-3-ultra-free",
                "codex/gpt-5.6-terra",
                "opencode/opencode-go/deepseek-v4-flash",
                "opencode/opencode/mimo-v2.5-free",
                "opencode/opencode/nemotron-3.5-lightning-free",
                "opencode/opencode/big-pickle",
                "opencode/opencode/minimax-m3",
                "opencode/opencode/qwen3.7-plus",
                "codex/gpt-5.6-luna",
                "opencode/opencode/deepseek-v4-flash",
                "opencode/opencode/deepseek-v4-pro",
            ]
        },
        "T3": {
            "default": [
                "opencode/opencode-go/deepseek-v4-pro",
                "codex/gpt-5.6-terra",
                "codex/gpt-5.6-sol",
                "opencode/opencode/deepseek-v4-flash",
                "opencode/opencode/minimax-m3",
                "opencode/opencode/qwen3.7-plus",
                "opencode/opencode/deepseek-v4-pro",
                "codex/gpt-5.6-luna",
            ]
        },
    }
    from saberops.routing_config import load_packaged_default

    expected = canonical_routing_payload(embedded_32ab051)
    snapshot = load_packaged_default()
    assert snapshot.schema_version == 3
    rendered = snapshot_to_payload(snapshot, "now")["roles"]
    as_ids = {
        role: [
            {"routes": [f"{r['provider']}/{r['model']}" for r in group["routes"]]}
            for group in groups
        ]
        for role, groups in rendered.items()
    }
    assert as_ids == expected["roles"]
    worker = role_chain_ids(expected, "WORKER")
    assert len(worker) == 17
    assert worker[0] == "opencode/opencode/muse-spark-1.2-contributor-free"
    assert worker[-2:] == ["opencode/opencode-go/mimo-v2.5", "codex/gpt-5.6-sol"]
    surgeon = role_chain_ids(expected, "SURGEON")
    assert len(surgeon) == 8
    assert surgeon[0] == "opencode/opencode-go/deepseek-v4-pro"
    assert role_chain_ids(expected, "INTEGRATOR") == [
        "opencode/opencode/muse-spark-1.2-contributor-free"
    ]


def test_schema3_validation_failures_and_passes() -> None:
    """Covers: routing schema 3 validation (fail closed)."""
    base = canonical_routing_payload(v2_document())
    bad = copy.deepcopy(base)
    bad["bogus"] = 1
    with pytest.raises(RoutingConfigError, match="unknown top-level keys"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    del bad["roles"]["SURGEON"]
    with pytest.raises(RoutingConfigError, match="missing roles"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["ORCHESTRATOR"] = []
    with pytest.raises(RoutingConfigError, match="orch orchestrator show"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["REVIEWER"] = []
    with pytest.raises(RoutingConfigError, match="orch reviewer show"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["WORKER"] = [{"routes": []}]
    with pytest.raises(RoutingConfigError, match="non-empty list"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["WORKER"] = [{"routes": [A]}, {"routes": [A]}]
    with pytest.raises(RoutingConfigError, match="duplicate candidate"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["WORKER"] = [{"routes": ["nope/unknown-model"]}]
    with pytest.raises(RoutingConfigError, match="unknown candidate id"):
        canonical_routing_payload(bad)
    # Same id in two roles is fine.
    ok = copy.deepcopy(base)
    ok["roles"]["INTEGRATOR"] = [{"routes": [A]}]
    assert canonical_routing_payload(ok)["roles"]["INTEGRATOR"] == [{"routes": [A]}]
    # An empty chain is a valid owner decision.
    ok = copy.deepcopy(base)
    ok["roles"]["INTEGRATOR"] = []
    assert canonical_routing_payload(ok)["roles"]["INTEGRATOR"] == []
    # An approved dynamic id is admissible.
    approval = _approval()
    ok = copy.deepcopy(base)
    ok["approved_candidates"] = {approval.candidate_id: approval.to_dict()}
    ok["roles"]["WORKER"].append({"routes": [approval.candidate_id]})
    assert canonical_routing_payload(ok)


def test_legacy_owner_file_loads_without_rewriting_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: ADR-0002 §11 item 2 (schema-1/2 user file loads as schema 3)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    path = get_user_routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(legacy_document())
    path.write_text(original)
    snapshot = load_effective_routing()
    assert snapshot.schema_version == 3
    assert snapshot.migrated_from == 1
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.WORKER)] == [B, A, C]
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.SURGEON)] == [C, A]
    assert load_effective_routing_payload()["roles"]["INTEGRATOR"] == [{"routes": [B]}]
    assert path.read_text() == original


def test_legacy_v2_owner_file_loads_with_migrated_from_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: ADR-0002 §11 item 2 (schema-2 user file loads as schema 3)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    path = get_user_routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(v2_document())
    path.write_text(original)
    snapshot = load_effective_routing()
    assert snapshot.schema_version == 3
    assert snapshot.migrated_from == 2
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.WORKER)] == [B, A, C]
    assert path.read_text() == original


def test_legacy_snapshot_digest_still_checked_before_use() -> None:
    chains = {
        "T1.default": [A, B],
        "T2.training_allowed": [B, A],
        "T2.training_denied_off_peak": [C],
        "T2.training_denied_peak": [A, C],
        "T3.off_peak": [C],
        "T3.peak": [A, C],
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "chains": chains,
        "content_hash": _content_hash(chains, 1),
    }
    snapshot = payload_to_snapshot(payload)
    assert snapshot.content_hash == payload["content_hash"]
    assert snapshot_to_payload(snapshot, "now")["chains"].keys() == chains.keys()
    tampered = copy.deepcopy(payload)
    tampered["chains"]["T1.default"].reverse()
    with pytest.raises(RoutingConfigError, match="content_hash mismatch"):
        payload_to_snapshot(tampered)


def test_v3_snapshot_roundtrip_and_tamper() -> None:
    """Covers: schema-3 snapshot digest (persisted snapshots verify)."""
    from saberops.routing_config import load_packaged_default

    fresh = load_packaged_default()
    payload = snapshot_to_payload(fresh, "2026-10-12T00:00:00+00:00")
    assert payload["schema_version"] == 3
    assert set(payload["roles"]) == {"WORKER", "SURGEON", "INTEGRATOR"}
    assert payload["roles"]["WORKER"][0] == {
        "routes": [{"provider": "opencode", "model": "opencode/muse-spark-1.2-contributor-free"}]
    }
    rebuilt = payload_to_snapshot(payload, run_id="run_test")
    assert rebuilt.content_hash == payload["content_hash"]
    assert rebuilt.chain_for_role(JobRole.WORKER) == fresh.chain_for_role(JobRole.WORKER)
    tampered = copy.deepcopy(payload)
    tampered["roles"]["WORKER"] = tampered["roles"]["WORKER"][1:]
    with pytest.raises(RoutingConfigError, match="content_hash mismatch"):
        payload_to_snapshot(tampered)


def test_routed_roles_vocabulary() -> None:
    """Covers: routing schema 3 routed roles (one role vocabulary)."""
    assert tuple(ROUTED_ROLES) == (JobRole.WORKER, JobRole.SURGEON, JobRole.INTEGRATOR)


@pytest.mark.parametrize("role", list(ROUTED_ROLES))
def test_routing_ignores_both_retired_flags(role: JobRole) -> None:
    from saberops.routing_config import load_packaged_default

    snapshot = load_packaged_default()
    expected = resolve_candidate_chain(role=role, snapshot=snapshot)
    assert (
        resolve_candidate_chain(
            role=role,
            training_allowed=False,
            is_peak=True,
            snapshot=snapshot,
        )
        == expected
    )
    assert routing_context_for_role(role) == role.value


@pytest.mark.parametrize("requirement", ["TRUE", "FALSE", "UNKNOWN"])
def test_training_gate_is_retired_even_for_legacy_denied(requirement: str) -> None:
    candidate = WorkerCandidate(provider="codex", model="gpt-5.6-terra")
    assert is_training_permitted(requirement, False)
    decision = evaluate_control_plane(
        candidate, training_allowed=False, training_required=requirement
    )
    assert decision.training_permitted is True
    assert decision.reason != "TRAINING_PERMISSION_DENIED"
    assert is_peak_hours() is False


def _approval() -> DynamicCandidate:
    return DynamicCandidate(
        candidate_id="anthropic/claude-test",
        provider="anthropic",
        model="claude-test",
        connection_id="claude-code-subscription",
        backend="claude",
        binding_id="discovered-binding:worker:claude-code-subscription:claude-test",
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at="2026-10-05T00:00:00+00:00",
    )


def test_migration_preserves_exact_approval_and_binding() -> None:
    approval = _approval()
    old = v2_document()
    old["approved_candidates"] = {approval.candidate_id: approval.to_dict()}
    old["T2"]["default"].append(approval.candidate_id)
    v3 = canonical_routing_payload(old)
    assert v3["approved_candidates"] == old["approved_candidates"]
    snapshot = payload_to_snapshot(
        {
            "schema_version": 3,
            "source": "user",
            "source_path": None,
            "content_hash": _content_hash(
                {
                    role: [group["routes"] for group in groups]
                    for role, groups in v3["roles"].items()
                },
                3,
                {approval.candidate_id: approval},
            ),
            "approved_candidates": {approval.candidate_id: approval.to_dict()},
            "roles": {
                role: [
                    {
                        "routes": [
                            {"provider": cid.split("/", 1)[0], "model": cid.split("/", 1)[1]}
                            for cid in group["routes"]
                        ]
                    }
                    for group in groups
                ]
                for role, groups in v3["roles"].items()
            },
        }
    )
    chain = resolve_candidate_chain(role=JobRole.WORKER, snapshot=snapshot)
    candidate = next(c for c in chain if c.model == "claude-test")
    assert candidate.binding_id == approval.binding_id


def test_editors_add_remove_move_reorder() -> None:
    """Covers: role/group routing editors (add, remove, move, reorder)."""
    base = canonical_routing_payload(v2_document())
    # Add defaults to a new single-route group at the end.
    added = add_candidate_to_chain(base, role="worker", candidate_id="codex/gpt-5.6-luna")
    worker = added["roles"]["WORKER"]
    assert worker[-1] == {"routes": ["codex/gpt-5.6-luna"]}
    # Adding an already-present candidate of that role is a no-op.
    assert add_candidate_to_chain(base, role="WORKER", candidate_id=A) == base
    # Add into an existing group (1-based).
    grouped = add_candidate_to_chain(
        base, role="WORKER", candidate_id="codex/gpt-5.6-luna", group=1
    )
    assert grouped["roles"]["WORKER"][0]["routes"] == [B, "codex/gpt-5.6-luna"]
    # Remove drops the emptied group.
    removed = remove_candidate_from_chain(grouped, role="WORKER", candidate_id=B)
    assert removed["roles"]["WORKER"][0]["routes"] == ["codex/gpt-5.6-luna"]
    # Removing the last route leaves a valid empty chain.
    emptied = remove_candidate_from_chain(base, role="INTEGRATOR", candidate_id=B)
    assert emptied["roles"]["INTEGRATOR"] == []
    assert canonical_routing_payload(emptied)
    # Move group one place; edges are no-ops.
    moved = move_group_in_chain(base, role="WORKER", group=1, direction="down")
    assert [g["routes"][0] for g in moved["roles"]["WORKER"]] == [A, B, C]
    assert move_group_in_chain(base, role="WORKER", group=1, direction="up") == base
    assert move_group_in_chain(base, role="WORKER", group=3, direction="down") == base
    # Reorder with a permutation; anything else fails.
    reordered = reorder_chain_groups(base, role="WORKER", order=[3, 1, 2])
    assert [g["routes"][0] for g in reordered["roles"]["WORKER"]] == [C, B, A]
    with pytest.raises(RoutingConfigError, match="exactly once"):
        reorder_chain_groups(base, role="WORKER", order=[1, 1, 2])
    with pytest.raises(RoutingConfigError, match="exactly once"):
        reorder_chain_groups(base, role="WORKER", order=[1, 2])
    # Every editor result re-validates.
    for doc in (added, grouped, removed, emptied, moved, reordered):
        assert canonical_routing_payload(doc)["schema_version"] == 3


def test_resolve_candidate_chain_roles_and_alias() -> None:
    """Covers: role resolution, flattening order, tier alias, role+tier error."""
    from saberops.routing_config import load_packaged_default

    snapshot = load_packaged_default()
    worker = resolve_candidate_chain(role=JobRole.WORKER, snapshot=snapshot)
    assert [f"{c.provider}/{c.model}" for c in worker] == [
        f"{c.provider}/{c.model}" for group in snapshot.groups_for(JobRole.WORKER) for c in group
    ]
    assert [f"{c.provider}/{c.model}" for c in resolve_candidate_chain(snapshot=snapshot)] == [
        f"{c.provider}/{c.model}" for c in worker
    ]
    assert resolve_candidate_chain(tier=Tier.T1, snapshot=snapshot) == worker
    assert resolve_candidate_chain(tier=Tier.T2, snapshot=snapshot) == worker
    surgeon = resolve_candidate_chain(role="SURGEON", snapshot=snapshot)
    assert resolve_candidate_chain(tier=Tier.T3, snapshot=snapshot) == surgeon
    assert surgeon != worker
    with pytest.raises(ValueError, match="not both"):
        resolve_candidate_chain(role=JobRole.WORKER, tier=Tier.T1, snapshot=snapshot)
    # Attended-only filtering and provider-override behaviour are unchanged.
    assert all(c.provider != "antigravity" for c in worker)
    with pytest.raises(ValueError, match="attended-only"):
        resolve_candidate_chain(role="WORKER", provider_override="antigravity", snapshot=snapshot)
    with pytest.raises(ValueError, match="no eligible candidates"):
        resolve_candidate_chain(role="WORKER", provider_override="nope", snapshot=snapshot)
    codex = resolve_candidate_chain(role="WORKER", provider_override="codex", snapshot=snapshot)
    assert codex and all(c.provider == "codex" for c in codex)
