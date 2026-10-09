"""Tier-only migration and retirement of training/time admission filters."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane.preflight import evaluate_control_plane
from saberops.dispatch import routing_context_for
from saberops.models import Tier, WorkerCandidate
from saberops.routing import is_peak_hours, resolve_candidate_chain
from saberops.routing_config import (
    DynamicCandidate,
    RoutingConfigError,
    _content_hash,
    canonical_routing_payload,
    get_user_routing_path,
    load_effective_routing,
    load_effective_routing_payload,
    payload_to_snapshot,
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


def test_legacy_chains_merge_stably_without_mutating_source() -> None:
    old = legacy_document()
    original = copy.deepcopy(old)
    current = canonical_routing_payload(old)
    assert old == original
    assert current == {
        "schema_version": 2,
        "T1": {"default": [A, B]},
        "T2": {"default": [B, A, C]},
        "T3": {"default": [C, A]},
    }


def test_legacy_owner_file_loads_without_rewriting_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    path = get_user_routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(legacy_document())
    path.write_text(original)
    snapshot = load_effective_routing()
    assert snapshot.schema_version == 2
    assert snapshot_to_payload(snapshot, "now")["chains"].keys() == {
        "T1.default",
        "T2.default",
        "T3.default",
    }
    assert load_effective_routing_payload()["T2"]["default"] == [B, A, C]
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


@pytest.mark.parametrize("tier", list(Tier))
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("peak", [False, True])
def test_routing_ignores_both_retired_flags(tier: Tier, allowed: bool, peak: bool) -> None:
    from saberops.routing_config import load_packaged_default

    snapshot = load_packaged_default()
    expected = resolve_candidate_chain(tier=tier, snapshot=snapshot)
    assert (
        resolve_candidate_chain(
            tier=tier,
            training_allowed=allowed,
            is_peak=peak,
            snapshot=snapshot,
        )
        == expected
    )
    assert (
        routing_context_for(tier.value, training_allowed=allowed, is_peak=peak)
        == f"{tier.value}.default"
    )


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


def test_migration_preserves_exact_approval_and_binding() -> None:
    approval = DynamicCandidate(
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
    old = legacy_document()
    old["approved_candidates"] = {approval.candidate_id: approval.to_dict()}
    old["T2"]["training_allowed"].append(approval.candidate_id)
    canonical = canonical_routing_payload(old)
    assert canonical["approved_candidates"] == old["approved_candidates"]
    snapshot = payload_to_snapshot(
        {
            **canonical,
            "chains": {
                f"{tier}.default": canonical[tier]["default"] for tier in ("T1", "T2", "T3")
            },
        }
    )
    chain = resolve_candidate_chain(tier=Tier.T2, training_allowed=False, snapshot=snapshot)
    candidate = next(c for c in chain if c.model == "claude-test")
    assert candidate.binding_id == approval.binding_id
