"""L-B adapter cut lane: Copilot removed, Antigravity attended-only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane.policy import packaged_control_plane_policy
from saberops.models import AttemptStatus, Tier, WorkerRequest
from saberops.process import ProcessResult
from saberops.routing import (
    ATTENDED_ONLY_REFUSAL,
    CANDIDATE_ANTIGRAVITY_FLASH,
    resolve_candidate_chain,
)
from saberops.routing_config import (
    ALLOWED_CANDIDATES,
    RoutingConfigError,
    RoutingSnapshot,
    canonical_routing_payload,
    load_packaged_default,
)
from saberops.workers.antigravity import AntigravityAdapter
from saberops.workers.registry import AdapterRegistry, attended_adapter


@pytest.mark.parametrize("tier", list(Tier))
def test_automatic_tier_chains_never_yield_antigravity(tier: Tier) -> None:
    chain = resolve_candidate_chain(tier=tier)
    assert chain
    assert all(candidate.provider != "antigravity" for candidate in chain)
    snapshot = load_packaged_default()
    resolved = resolve_candidate_chain(tier=tier, snapshot=snapshot)
    assert resolved
    assert all(candidate.provider != "antigravity" for candidate in resolved)


def test_configured_snapshot_still_never_yields_antigravity() -> None:
    snapshot = load_packaged_default()
    worker_groups = snapshot.groups_for("WORKER")
    injected = RoutingSnapshot(
        schema_version=snapshot.schema_version,
        source=snapshot.source,
        source_path=snapshot.source_path,
        content_hash=snapshot.content_hash,
        role_chains=(
            ("WORKER", ((CANDIDATE_ANTIGRAVITY_FLASH,),) + worker_groups),
            ("SURGEON", snapshot.groups_for("SURGEON")),
            ("INTEGRATOR", snapshot.groups_for("INTEGRATOR")),
        ),
        approved_candidates=snapshot.approved_candidates,
    )
    resolved = resolve_candidate_chain(role="WORKER", snapshot=injected)
    assert all(candidate.provider != "antigravity" for candidate in resolved)


def test_provider_only_antigravity_override_is_refused() -> None:
    with pytest.raises(ValueError, match="attended-only"):
        resolve_candidate_chain(tier=Tier.T1, provider_override="antigravity")
    with pytest.raises(ValueError, match="ADR-0001"):
        resolve_candidate_chain(tier=Tier.T1, provider_override="antigravity")
    assert "Amendment 1" in ATTENDED_ONLY_REFUSAL


def test_explicit_owner_override_still_resolves_antigravity() -> None:
    chain = resolve_candidate_chain(
        tier=Tier.T1,
        model_override="antigravity/gemini-3.7-flash",
        provider_override="antigravity",
    )
    assert [(c.provider, c.model) for c in chain] == [
        ("antigravity", "antigravity/gemini-3.7-flash")
    ]
    inferred = resolve_candidate_chain(tier=Tier.T1, model_override="gemini-3.7-flash")
    assert inferred[0].provider == "antigravity"


def test_explicit_override_dispatches_through_stub_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saberops.workers.antigravity.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "saberops.workers.antigravity.GitManager.get_changed_paths", lambda _: []
    )
    raw = json.dumps(
        {"event": "result", "result": {"status": "SUCCESS", "response": "Done"}}
    )

    def runner(args: list[str], **kwargs: Any) -> ProcessResult:
        assert kwargs["inherit_env"] is False
        return ProcessResult(args, 0, raw, "", 0.01)

    candidate = resolve_candidate_chain(
        tier=Tier.T1,
        model_override="antigravity/gemini-3.7-flash",
        provider_override="antigravity",
    )[0]
    adapter = AdapterRegistry.default().get(candidate.provider)
    assert isinstance(adapter, AntigravityAdapter)
    result = AntigravityAdapter(runner=runner).run(
        WorkerRequest("task", tmp_path, candidate.model, read_only=True)
    )
    assert result.status == AttemptStatus.SUCCESS
    assert result.provider == "antigravity"


def test_registry_never_auto_includes_attended_only_or_removed() -> None:
    registry = AdapterRegistry.default()
    assert "antigravity" not in registry.get_registered_providers()
    assert "copilot" not in registry.get_registered_providers()
    assert isinstance(registry.get("antigravity"), AntigravityAdapter)
    assert registry.get("copilot") is None
    assert isinstance(attended_adapter("antigravity"), AntigravityAdapter)
    assert attended_adapter("codex") is None


def test_copilot_adapter_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        __import__("saberops.workers.copilot")


def test_policy_defaults_exclude_cut_providers() -> None:
    policy = packaged_control_plane_policy()
    assert set(policy.provider_policies) == {"opencode", "codex", "cline"}


def test_closed_registry_and_packaged_default_exclude_antigravity() -> None:
    from saberops.jobs.models import JobRole

    assert "antigravity/gemini-3.7-flash" not in ALLOWED_CANDIDATES
    snapshot = load_packaged_default()
    for role in (JobRole.WORKER, JobRole.SURGEON, JobRole.INTEGRATOR):
        assert all(
            candidate.provider != "antigravity"
            for candidate in snapshot.chain_for_role(role)
        )
    # Automatic routing still has candidates after the cut.
    assert snapshot.chain_for_role(JobRole.WORKER)


def test_configured_antigravity_candidate_id_fails_closed() -> None:
    document = {
        "schema_version": 2,
        "T1": {"default": ["antigravity/gemini-3.7-flash"]},
        "T2": {"default": ["codex/gpt-5.6-terra"]},
        "T3": {"default": ["codex/gpt-5.6-terra"]},
    }
    with pytest.raises(RoutingConfigError, match="unknown candidate id"):
        canonical_routing_payload(document)
