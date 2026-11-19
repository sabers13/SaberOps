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
        "roles": {"BUILD": [{"routes": [B]}, {"routes": [A]}, {"routes": [C]}]},
        "strong_start": [{"routes": [C]}, {"routes": [A]}],
    }


def test_v2_to_v3_migration_is_exact() -> None:
    """Covers: ADR-0002 §11 item 2 (WORKER union order, SURGEON = T3)."""
    old = v2_document()
    original = copy.deepcopy(old)
    current = canonical_routing_payload(old)
    assert old == original
    # BUILD = stable union of T2, T1, T3 (T2 order first, then unseen).
    assert role_chain_ids(current, "BUILD") == [B, A, C]
    # strong_start = the T3 order (a setting on the build route).
    assert [group["routes"][0] for group in current["strong_start"]] == [C, A]
    # INTEGRATOR retired: landing is a deterministic engine stage, not a role.
    assert set(current["roles"]) == {"BUILD"}
    # Every route is its own fallback group (today's ordered fallback).
    assert all(
        len(group["routes"]) == 1
        for groups in (current["roles"]["BUILD"], current["strong_start"])
        for group in groups
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
    build_ids = [cid for group in current["roles"]["BUILD"] for cid in group["routes"]]
    assert approval.candidate_id in build_ids


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
    payload = snapshot_to_payload(snapshot, "now")
    rendered = payload["roles"]
    as_ids = {
        role: [
            {"routes": [f"{r['provider']}/{r['model']}" for r in group["routes"]]}
            for group in groups
        ]
        for role, groups in rendered.items()
    }
    assert as_ids == expected["roles"]
    expected_strong = [
        {"routes": [f"{r['provider']}/{r['model']}" for r in group["routes"]]}
        for group in payload["strong_start"]
    ]
    assert expected_strong == expected["strong_start"]
    build = role_chain_ids(expected, "BUILD")
    assert len(build) == 17
    assert build[0] == "opencode/opencode/muse-spark-1.2-contributor-free"
    assert build[-2:] == ["opencode/opencode-go/mimo-v2.5", "codex/gpt-5.6-sol"]
    strong = [group["routes"][0] for group in expected["strong_start"]]
    assert len(strong) == 8
    assert strong[0] == "opencode/opencode-go/deepseek-v4-pro"


def test_schema3_validation_failures_and_passes() -> None:
    """Covers: routing schema 3 validation (fail closed)."""
    base = canonical_routing_payload(v2_document())
    bad = copy.deepcopy(base)
    bad["bogus"] = 1
    with pytest.raises(RoutingConfigError, match="unknown top-level keys"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    del bad["roles"]["BUILD"]
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
    bad["roles"]["BUILD"] = [{"routes": []}]
    with pytest.raises(RoutingConfigError, match="non-empty list"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["BUILD"] = [{"routes": [A]}, {"routes": [A]}]
    with pytest.raises(RoutingConfigError, match="duplicate candidate"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["roles"]["BUILD"] = [{"routes": ["nope/unknown-model"]}]
    with pytest.raises(RoutingConfigError, match="unknown candidate id"):
        canonical_routing_payload(bad)
    bad = copy.deepcopy(base)
    bad["strong_start"] = [{"routes": [A]}, {"routes": [A]}]
    with pytest.raises(RoutingConfigError, match="duplicate candidate"):
        canonical_routing_payload(bad)
    # Retired role keys are migrated, not rejected: WORKER becomes BUILD,
    # SURGEON becomes the strong start, INTEGRATOR is dropped.
    legacy = {
        "schema_version": 3,
        "roles": {
            "WORKER": [{"routes": [A]}],
            "SURGEON": [{"routes": [B]}],
            "INTEGRATOR": [{"routes": [C]}],
        },
    }
    migrated = canonical_routing_payload(legacy)
    assert migrated["roles"] == {"BUILD": [{"routes": [A]}]}
    assert migrated["strong_start"] == [{"routes": [B]}]
    # An empty strong start is a valid owner decision.
    ok = copy.deepcopy(base)
    ok["strong_start"] = []
    assert canonical_routing_payload(ok)["strong_start"] == []
    # An approved dynamic id is admissible.
    approval = _approval()
    ok = copy.deepcopy(base)
    ok["approved_candidates"] = {approval.candidate_id: approval.to_dict()}
    ok["roles"]["BUILD"].append({"routes": [approval.candidate_id]})
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
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.BUILD)] == [B, A, C]
    assert [
        f"{c.provider}/{c.model}"
        for c in snapshot.chain_for_role(JobRole.BUILD, strong_start=True)
    ] == [C, A]
    payload = load_effective_routing_payload()
    assert set(payload["roles"]) == {"BUILD"}
    assert payload["strong_start"] == [{"routes": [C]}, {"routes": [A]}]
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
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.BUILD)] == [B, A, C]
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
    assert set(payload["roles"]) == {"BUILD"}
    assert payload["roles"]["BUILD"][0] == {
        "routes": [{"provider": "opencode", "model": "opencode/muse-spark-1.2-contributor-free"}]
    }
    rebuilt = payload_to_snapshot(payload, run_id="run_test")
    assert rebuilt.content_hash == payload["content_hash"]
    assert rebuilt.chain_for_role(JobRole.BUILD) == fresh.chain_for_role(JobRole.BUILD)
    assert rebuilt.chain_for_role(JobRole.BUILD, strong_start=True) == (
        fresh.chain_for_role(JobRole.BUILD, strong_start=True)
    )
    tampered = copy.deepcopy(payload)
    tampered["roles"]["BUILD"] = tampered["roles"]["BUILD"][1:]
    with pytest.raises(RoutingConfigError, match="content_hash mismatch"):
        payload_to_snapshot(tampered)


def test_routed_roles_vocabulary() -> None:
    """Covers: routing schema 3 routed roles (one role vocabulary)."""
    assert tuple(ROUTED_ROLES) == (JobRole.BUILD,)
    assert sorted(role.value for role in JobRole) == ["BUILD", "CHECK", "PLAN"]


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
                    "BUILD": [
                        group["routes"] for group in v3["roles"]["BUILD"]
                    ],
                    "strong_start": [
                        group["routes"] for group in v3["strong_start"]
                    ],
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
            "strong_start": v3["strong_start"],
        }
    )
    chain = resolve_candidate_chain(role=JobRole.BUILD, snapshot=snapshot)
    candidate = next(c for c in chain if c.model == "claude-test")
    assert candidate.binding_id == approval.binding_id


def test_editors_add_remove_move_reorder() -> None:
    """Covers: role/group routing editors (add, remove, move, reorder)."""
    base = canonical_routing_payload(v2_document())
    # Add defaults to a new single-route group at the end.
    added = add_candidate_to_chain(base, role="build", candidate_id="codex/gpt-5.6-luna")
    build = added["roles"]["BUILD"]
    assert build[-1] == {"routes": ["codex/gpt-5.6-luna"]}
    # Adding an already-present candidate of that role is a no-op.
    assert add_candidate_to_chain(base, role="BUILD", candidate_id=A) == base
    # A retired role name edits the build route.
    assert add_candidate_to_chain(base, role="worker", candidate_id=A) == base
    # Add into an existing group (1-based).
    grouped = add_candidate_to_chain(
        base, role="BUILD", candidate_id="codex/gpt-5.6-luna", group=1
    )
    assert grouped["roles"]["BUILD"][0]["routes"] == [B, "codex/gpt-5.6-luna"]
    # Remove drops the emptied group.
    removed = remove_candidate_from_chain(grouped, role="BUILD", candidate_id=B)
    assert removed["roles"]["BUILD"][0]["routes"] == ["codex/gpt-5.6-luna"]
    # Removing the last route leaves a valid empty chain.
    emptied = remove_candidate_from_chain(base, role="BUILD", candidate_id=B)
    emptied = remove_candidate_from_chain(emptied, role="BUILD", candidate_id=A)
    emptied = remove_candidate_from_chain(emptied, role="BUILD", candidate_id=C)
    assert emptied["roles"]["BUILD"] == []
    assert canonical_routing_payload(emptied)
    # Move group one place; edges are no-ops.
    moved = move_group_in_chain(base, role="BUILD", group=1, direction="down")
    assert [g["routes"][0] for g in moved["roles"]["BUILD"]] == [A, B, C]
    assert move_group_in_chain(base, role="BUILD", group=1, direction="up") == base
    assert move_group_in_chain(base, role="BUILD", group=3, direction="down") == base
    # Reorder with a permutation; anything else fails.
    reordered = reorder_chain_groups(base, role="BUILD", order=[3, 1, 2])
    assert [g["routes"][0] for g in reordered["roles"]["BUILD"]] == [C, B, A]
    with pytest.raises(RoutingConfigError, match="exactly once"):
        reorder_chain_groups(base, role="BUILD", order=[1, 1, 2])
    with pytest.raises(RoutingConfigError, match="exactly once"):
        reorder_chain_groups(base, role="BUILD", order=[1, 2])
    # Every editor result re-validates.
    for doc in (added, grouped, removed, emptied, moved, reordered):
        assert canonical_routing_payload(doc)["schema_version"] == 3


def test_resolve_candidate_chain_roles_and_alias() -> None:
    """Covers: role resolution, flattening order, tier alias, role+tier error."""
    from saberops.routing_config import load_packaged_default

    snapshot = load_packaged_default()
    worker = resolve_candidate_chain(role=JobRole.BUILD, snapshot=snapshot)
    assert [f"{c.provider}/{c.model}" for c in worker] == [
        f"{c.provider}/{c.model}" for group in snapshot.groups_for(JobRole.BUILD) for c in group
    ]
    assert [f"{c.provider}/{c.model}" for c in resolve_candidate_chain(snapshot=snapshot)] == [
        f"{c.provider}/{c.model}" for c in worker
    ]
    assert resolve_candidate_chain(tier=Tier.T1, snapshot=snapshot) == worker
    assert resolve_candidate_chain(tier=Tier.T2, snapshot=snapshot) == worker
    # A retired role name resolves to the build route, not a second role.
    assert resolve_candidate_chain(role="SURGEON", snapshot=snapshot) == worker
    assert resolve_candidate_chain(tier=Tier.T3, snapshot=snapshot) == worker
    # The stronger-model start is a setting on the build route.
    strong = resolve_candidate_chain(
        role=JobRole.BUILD, snapshot=snapshot, strong_start=True
    )
    assert [f"{c.provider}/{c.model}" for c in strong] == [
        f"{c.provider}/{c.model}" for group in snapshot.strong_start for c in group
    ]
    assert strong != worker
    with pytest.raises(ValueError, match="not both"):
        resolve_candidate_chain(role=JobRole.BUILD, tier=Tier.T1, snapshot=snapshot)
    # Attended-only filtering and provider-override behaviour are unchanged.
    assert all(c.provider != "antigravity" for c in worker)
    with pytest.raises(ValueError, match="attended-only"):
        resolve_candidate_chain(role="WORKER", provider_override="antigravity", snapshot=snapshot)
    with pytest.raises(ValueError, match="no eligible candidates"):
        resolve_candidate_chain(role="WORKER", provider_override="nope", snapshot=snapshot)
    codex = resolve_candidate_chain(role="WORKER", provider_override="codex", snapshot=snapshot)
    assert codex and all(c.provider == "codex" for c in codex)


def _legacy_v3_document() -> dict[str, Any]:
    """A router document exactly as a pre-ADR-0005 store wrote it."""
    return {
        "schema_version": 3,
        "roles": {
            "WORKER": [{"routes": [A]}, {"routes": [B]}],
            "SURGEON": [{"routes": [C]}],
            "INTEGRATOR": [{"routes": [B]}],
        },
    }


def test_retired_role_store_migrates_to_build_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Covers: ADR-0005 §3 migration of a store holding SURGEON and INTEGRATOR.

    Before: the owner routing file names WORKER, SURGEON and INTEGRATOR.
    After: the build route carries WORKER's chain, SURGEON becomes the build
    route's strong_start setting, and INTEGRATOR is dropped and reported.
    """
    from saberops.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    path = get_user_routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(_legacy_v3_document())
    path.write_text(original)

    snapshot = load_effective_routing()
    assert snapshot.migrated_roles == ("WORKER", "SURGEON", "INTEGRATOR")
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.BUILD)] == [
        A,
        B,
    ]
    assert [
        f"{c.provider}/{c.model}"
        for c in snapshot.chain_for_role(JobRole.BUILD, strong_start=True)
    ] == [C]
    # A load never rewrites the owner's file.
    assert path.read_text() == original

    # The migration command reports the before/after mapping.
    assert main(["routing", "migrate"]) == 0
    before_after = capsys.readouterr().out
    assert "old SURGEON: [['codex/gpt-5.6-sol']]" in before_after
    assert "-> BUILD strong_start" in before_after
    assert "old INTEGRATOR: [['opencode/opencode-go/muse-spark-1.2-contributor']]" in before_after
    assert "-> dropped" in before_after
    assert "new BUILD:" in before_after
    assert "new strong_start:" in before_after
    assert path.read_text() == original

    # --write persists the migrated document.
    assert main(["routing", "migrate", "--write"]) == 0
    payload = json.loads(path.read_text())
    assert payload["roles"] == {
        "BUILD": [{"routes": [A]}, {"routes": [B]}],
    }
    assert payload["strong_start"] == [{"routes": [C]}]


def test_persisted_snapshot_with_retired_roles_still_loads() -> None:
    """Covers: a stored run snapshot naming SURGEON/INTEGRATOR keeps its digest."""
    legacy_roles = {
        role: [group["routes"] for group in groups]
        for role, groups in _legacy_v3_document()["roles"].items()
    }
    payload: dict[str, Any] = {
        "schema_version": 3,
        "source": "user",
        "source_path": None,
        "content_hash": _content_hash(legacy_roles, 3),
        "roles": _legacy_v3_document()["roles"],
        "created_at": "2026-11-01T00:00:00+00:00",
    }
    snapshot = payload_to_snapshot(payload, run_id="run_legacy")
    assert snapshot.migrated_roles == ("WORKER", "SURGEON", "INTEGRATOR")
    assert [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role(JobRole.BUILD)] == [
        A,
        B,
    ]
    assert [
        f"{c.provider}/{c.model}"
        for c in snapshot.chain_for_role(JobRole.BUILD, strong_start=True)
    ] == [C]
    # Re-emitting the adopted snapshot is byte-stable (same keys, same digest).
    reemitted = snapshot_to_payload(snapshot, "2026-11-01T00:00:00+00:00")
    assert set(reemitted["roles"]) == {"WORKER", "SURGEON", "INTEGRATOR"}
    assert reemitted["content_hash"] == payload["content_hash"]
    rebuilt = payload_to_snapshot(reemitted, run_id="run_legacy")
    assert rebuilt.chain_for_role(JobRole.BUILD) == snapshot.chain_for_role(JobRole.BUILD)
    tampered = copy.deepcopy(payload)
    tampered["roles"]["SURGEON"] = [{"routes": [A]}]
    with pytest.raises(RoutingConfigError, match="content_hash mismatch"):
        payload_to_snapshot(tampered)


def test_strong_start_is_an_editable_build_route_setting() -> None:
    """The stronger-model start is a setting, not a fourth role."""
    base = canonical_routing_payload(v2_document())
    assert set(base["roles"]) == {"BUILD"}
    assert [group["routes"][0] for group in base["strong_start"]] == [C, A]

    added = add_candidate_to_chain(
        base, role="strong_start", candidate_id="codex/gpt-5.6-luna"
    )
    assert added["strong_start"][-1] == {"routes": ["codex/gpt-5.6-luna"]}
    assert set(added["roles"]) == {"BUILD"}

    removed = remove_candidate_from_chain(base, role="strong-start", candidate_id=C)
    assert [group["routes"][0] for group in removed["strong_start"]] == [A]
    moved = move_group_in_chain(base, role="strong_start", group=1, direction="down")
    assert [group["routes"][0] for group in moved["strong_start"]] == [A, C]
    reordered = reorder_chain_groups(base, role="strong_start", order=[2, 1])
    assert [group["routes"][0] for group in reordered["strong_start"]] == [A, C]


def test_legacy_role_names_map_onto_the_three_roles() -> None:
    """One vocabulary: persisted legacy names load as PLAN/BUILD/CHECK."""
    from saberops.jobs.models import coerce_job_role
    from saberops.models import coerce_binding_role, coerce_dispatch_role
    from saberops.questions import coerce_answer_authority, coerce_ask_role

    assert coerce_job_role("WORKER") is JobRole.BUILD
    assert coerce_job_role("SURGEON") is JobRole.BUILD
    assert coerce_job_role("INTEGRATOR") is JobRole.BUILD
    assert coerce_job_role("REVIEWER") is JobRole.CHECK
    assert coerce_job_role("ORCHESTRATOR") is JobRole.PLAN
    assert coerce_dispatch_role("BULK").value == "BUILD"
    assert coerce_dispatch_role("REPAIR").value == "BUILD"
    assert coerce_dispatch_role("SURGEON").value == "BUILD"
    assert coerce_dispatch_role("REVIEW").value == "CHECK"
    assert coerce_binding_role("WORKER").value == "BUILD"
    assert coerce_binding_role("REVIEWER").value == "CHECK"
    assert coerce_binding_role("ORCHESTRATOR").value == "PLAN"
    assert coerce_ask_role("ORCHESTRATOR").value == "PLAN"
    assert coerce_ask_role("WORKER").value == "BUILD"
    assert coerce_ask_role("REVIEWER").value == "CHECK"
    assert coerce_answer_authority("ORCHESTRATOR").value == "PLAN"
