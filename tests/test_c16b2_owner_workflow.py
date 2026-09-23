"""C16-B2: provider + model owner workflow persistence / restart proof.

Proves, against isolated XDG roots (never the real owner state):

1. a provider connection/catalog exists (access store + model catalog);
2. a discovered WORKER model is added to a routing chain;
3. an ORCHESTRATOR model is pinned;
4. stores/services are reconstructed from disk (simulated restart);
5. both selections are still present and resolve to the same exact
   identities (same binding ids, same connection/model).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.control_plane import (
    BindingRole,
    OrchBindingPolicy,
    OrchSelectionStatus,
    OwnerBindings,
    OwnerBindingStore,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    select_orch_binding,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import resolve_execution_binding
from saberops.model_access import (
    AccessStore,
    ModelCatalogStore,
    apply_discovery_result,
    build_access_registry,
    default_account_connections,
    discover_connection_models,
)
from saberops.models import OrchBindingMode
from saberops.project import owner_state_dir
from saberops.routing_config import (
    DynamicCandidate,
    add_dynamic_candidate_to_chain,
    format_candidate_id,
    load_effective_routing,
    load_effective_routing_payload,
    move_candidate_in_chain,
    save_user_routing,
)
from saberops.workers.registry import AdapterRegistry

_WORKER_MODEL = "b2-persist-worker-model"
_ORCH_MODEL = "b2-persist-orch-model"
_CONNECTION_ID = "opencode-account"
_PROVIDER = "opencode"


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("ORCH_ACCESS_STORE", raising=False)
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE", raising=False)
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)


def _materialize_both_roles(
    owner: OwnerBindings,
    *,
    connection_id: str,
    provider: str,
    model_id: str,
    registry: AdapterRegistry,
) -> tuple[OwnerBindings, str, str]:
    """Materialize WORKER + ORCHESTRATOR bindings sharing one pool/profile."""
    adapter = registry.get(provider)
    assert adapter is not None
    profile = capability_profile_for(adapter)
    worker, execution_profile, pool = materialize_discovered_binding(
        connection_id=connection_id,
        provider=provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.WORKER,
    )
    orch, _orch_profile, _orch_pool = materialize_discovered_binding(
        connection_id=connection_id,
        provider=provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.ORCHESTRATOR,
    )
    # Same real account / quota resource across roles: no pool duplication.
    assert worker.profile_id == orch.profile_id
    assert worker.quota_pool_id == orch.quota_pool_id
    assert worker.binding_id != orch.binding_id
    candidate = upsert_owner_binding(
        owner, binding=worker, profile=execution_profile, pool=pool
    )
    candidate = upsert_owner_binding(
        candidate, binding=orch, profile=execution_profile, pool=pool
    )
    resolve_execution_binding(
        registry=candidate.registry,
        binding_id=worker.binding_id,
        adapter=adapter,
    )
    resolve_execution_binding(
        registry=candidate.registry,
        binding_id=orch.binding_id,
        adapter=adapter,
    )
    return candidate, worker.binding_id, orch.binding_id


def test_c16b2_owner_workflow_persists_across_restart(
    isolated_xdg: None,
) -> None:
    registry = AdapterRegistry.default()

    # -- 1. provider connection + discovered catalog exist -----------------
    connection = next(
        c
        for c in default_account_connections()
        if c.connection_id == _CONNECTION_ID
    )
    access_store = AccessStore(owner_state_dir() / AccessStore.FILENAME)
    access_store.save(build_access_registry(default_account_connections()))
    assert access_store.load().try_get(_CONNECTION_ID) is not None

    discovered_at = datetime.now(UTC).isoformat()
    result = discover_connection_models(
        connection,
        list_models=lambda: [_WORKER_MODEL, _ORCH_MODEL],
        discovered_at=discovered_at,
    )
    assert result.ok
    catalog_store = ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)
    catalog_store.save(apply_discovery_result(catalog_store.load(), result))
    catalog = catalog_store.load()
    assert (
        catalog.try_get(
            connection_id=_CONNECTION_ID,
            provider=_PROVIDER,
            backend=connection.backend,
            model_id=_WORKER_MODEL,
        )
        is not None
    )

    # -- discovery path materializes both roles sharing one pool -----------
    owner = OwnerBindingStore(get_binding_store_path()).load()
    owner, worker_binding_id, _ = _materialize_both_roles(
        owner,
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        model_id=_WORKER_MODEL,
        registry=registry,
    )
    owner, _, orch_binding_id = _materialize_both_roles(
        owner,
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        model_id=_ORCH_MODEL,
        registry=registry,
    )
    save_owner_bindings(owner)

    # -- 2. discovered WORKER model added to a routing chain ---------------
    candidate_id = format_candidate_id(_PROVIDER, _WORKER_MODEL)
    approval = DynamicCandidate(
        candidate_id=candidate_id,
        provider=_PROVIDER,
        model=_WORKER_MODEL,
        connection_id=_CONNECTION_ID,
        backend=connection.backend,
        binding_id=worker_binding_id,
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at=discovered_at,
    )
    payload = load_effective_routing_payload()
    updated = add_dynamic_candidate_to_chain(
        payload, top="T1", sub="default", candidate=approval
    )
    save_user_routing(updated)
    snapshot = load_effective_routing()
    assert candidate_id in [f"{c.provider}/{c.model}" for c in snapshot.t1_default]

    # -- reorder keeps membership, changes order ----------------------------
    second_candidate = format_candidate_id(_PROVIDER, _ORCH_MODEL)
    second_approval = DynamicCandidate(
        candidate_id=second_candidate,
        provider=_PROVIDER,
        model=_ORCH_MODEL,
        connection_id=_CONNECTION_ID,
        backend=connection.backend,
        binding_id=(
            f"discovered-binding:worker:{_CONNECTION_ID}:{_ORCH_MODEL}"
        ),
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at=discovered_at,
    )
    payload = load_effective_routing_payload()
    payload = add_dynamic_candidate_to_chain(
        payload, top="T1", sub="default", candidate=second_approval
    )
    moved = move_candidate_in_chain(
        payload,
        top="T1",
        sub="default",
        candidate_id=second_candidate,
        direction="up",
    )
    save_user_routing(moved)
    reordered = load_effective_routing_payload()["T1"]["default"]
    assert isinstance(reordered, list)
    assert reordered.index(second_candidate) < reordered.index(candidate_id)

    # -- 3. ORCHESTRATOR model pinned ---------------------------------------
    policy = OrchBindingPolicy(
        mode=OrchBindingMode.PINNED, pinned_binding_id=orch_binding_id
    )
    selection = select_orch_binding(policy, owner.registry)
    assert selection.status is OrchSelectionStatus.RESOLVED
    save_owner_bindings(OwnerBindings(registry=owner.registry, policy=policy))

    # -- 4. reconstruct every store/service from disk (restart) -------------
    del access_store, catalog_store, owner, catalog, result

    reloaded_access = AccessStore(
        owner_state_dir() / AccessStore.FILENAME
    ).load()
    reloaded_catalog = ModelCatalogStore(
        owner_state_dir() / ModelCatalogStore.FILENAME
    ).load()
    reloaded_owner = OwnerBindingStore(get_binding_store_path()).load()
    reloaded_snapshot = load_effective_routing()

    # -- 5. both selections present and resolve to the same identities ------
    assert reloaded_access.try_get(_CONNECTION_ID) is not None
    entry = reloaded_catalog.try_get(
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        backend=connection.backend,
        model_id=_WORKER_MODEL,
    )
    assert entry is not None and not entry.is_stale

    chain_ids = [f"{c.provider}/{c.model}" for c in reloaded_snapshot.t1_default]
    assert candidate_id in chain_ids

    assert reloaded_owner.policy is not None
    assert reloaded_owner.policy.mode is OrchBindingMode.PINNED
    assert reloaded_owner.policy.pinned_binding_id == orch_binding_id

    reloaded_registry = AdapterRegistry.default()
    worker_binding = reloaded_owner.registry.try_get(worker_binding_id)
    orch_binding = reloaded_owner.registry.try_get(orch_binding_id)
    assert worker_binding is not None
    assert orch_binding is not None
    assert worker_binding.binding_role is BindingRole.WORKER
    assert orch_binding.binding_role is BindingRole.ORCHESTRATOR
    assert worker_binding.provider == orch_binding.provider == _PROVIDER
    # Same connection-scoped quota pool for both models on this account.
    assert worker_binding.quota_pool_id == orch_binding.quota_pool_id
    assert worker_binding_id == (
        f"discovered-binding:worker:{_CONNECTION_ID}:{_WORKER_MODEL}"
    )
    assert orch_binding_id == (
        f"discovered-binding:orchestrator:{_CONNECTION_ID}:{_ORCH_MODEL}"
    )
    resolve_execution_binding(
        registry=reloaded_owner.registry,
        binding_id=worker_binding_id,
        adapter=reloaded_registry.get(_PROVIDER),
    )
    resolved_selection = select_orch_binding(
        reloaded_owner.policy, reloaded_owner.registry
    )
    assert resolved_selection.status is OrchSelectionStatus.RESOLVED
    assert resolved_selection.binding is not None
    assert resolved_selection.binding.binding_id == orch_binding_id


def _seed_two_routing_candidates() -> tuple[str, str]:
    first = format_candidate_id(_PROVIDER, _WORKER_MODEL)
    second = format_candidate_id(_PROVIDER, _ORCH_MODEL)
    for candidate_id, model in ((first, _WORKER_MODEL), (second, _ORCH_MODEL)):
        approval = DynamicCandidate(
            candidate_id=candidate_id,
            provider=_PROVIDER,
            model=model,
            connection_id=_CONNECTION_ID,
            backend="opencode",
            binding_id=(
                f"discovered-binding:worker:{_CONNECTION_ID}:{model}"
            ),
            discovery_status="TRUE",
            execution_support="SUPPORTED",
            training_required="UNKNOWN",
            capabilities=(),
            approved_at=datetime.now(UTC).isoformat(),
        )
        save_user_routing(
            add_dynamic_candidate_to_chain(
                load_effective_routing_payload(),
                top="T1",
                sub="default",
                candidate=approval,
            )
        )
    return first, second


def test_c16b2_discovered_profile_resolves_to_real_connection(
    isolated_xdg: None,
) -> None:
    """C16-B2 §5: a discovered execution profile addresses its connection.

    Without this rule, readiness admission for an exact discovered
    binding stays UNKNOWN forever (the profile id never names a
    connection), so automatic dispatch can never select an
    owner-routed discovered model and the B2 workflow cannot execute.
    """
    from saberops.model_access import (
        AccessResolutionError,
        discovered_profile_connection_id,
        resolve_access,
    )

    assert (
        discovered_profile_connection_id("discovered-profile:opencode-account")
        == "opencode-account"
    )
    assert discovered_profile_connection_id("opencode-account") is None
    assert discovered_profile_connection_id("discovered-profile:") is None
    assert discovered_profile_connection_id("discovered-profile:  ") is None

    registry = build_access_registry(default_account_connections())

    resolved = resolve_access(
        registry=registry,
        provider="opencode",
        profile_id="discovered-profile:opencode-account",
    )
    assert resolved.connection.connection_id == "opencode-account"

    # Unknown suffixes still fail closed; provider mismatch is not mapped.
    with pytest.raises(AccessResolutionError):
        resolve_access(
            registry=registry,
            provider="opencode",
            profile_id="discovered-profile:no-such-connection",
        )
    with pytest.raises(AccessResolutionError):
        resolve_access(
            registry=registry,
            provider="codex",
            profile_id="discovered-profile:opencode-account",
        )

    # A disabled connection is refused, never silently admitted.
    disabled = replace(
        next(
            c
            for c in default_account_connections()
            if c.connection_id == "opencode-account"
        ),
        enabled=False,
    )
    disabled_registry = build_access_registry(
        tuple(
            disabled if c.connection_id == "opencode-account" else c
            for c in default_account_connections()
        )
    )
    with pytest.raises(AccessResolutionError):
        resolve_access(
            registry=disabled_registry,
            provider="opencode",
            profile_id="discovered-profile:opencode-account",
        )


def test_c16b2_admission_for_discovered_profile_reaches_store_evidence(
    isolated_xdg: None,
) -> None:
    """Readiness admission for a discovered profile uses stored evidence."""
    from saberops.model_access import ReadinessService
    from saberops.models import ProviderReadiness
    from saberops.project import owner_state_dir

    AccessStore(owner_state_dir() / AccessStore.FILENAME).save(
        build_access_registry(default_account_connections())
    )
    service = ReadinessService.for_owner_state(owner_state_dir())
    connection = next(
        c
        for c in default_account_connections()
        if c.connection_id == "opencode-account"
    )
    service.refresh(connection)
    # A successful execution marks the real connection READY ...
    service.record_success("opencode-account")

    admission = service.admission_for(
        provider="opencode", profile_id="discovered-profile:opencode-account"
    )
    assert admission.connection_id == "opencode-account"
    assert admission.state is ProviderReadiness.READY


def test_c16b2_routing_move_route_reorders_canonical_chain(
    isolated_xdg: None, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from saberops.web import create_app

    first, second = _seed_two_routing_candidates()
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    moved = client.post(
        "/routing/move",
        data={
            "context": "T1.default",
            "candidate_id": second,
            "direction": "down",
        },
        follow_redirects=False,
    )
    assert moved.status_code == 303
    chain = load_effective_routing_payload()["T1"]["default"]
    assert isinstance(chain, list)
    assert chain.index(first) < chain.index(second)

    moved_up = client.post(
        "/routing/move",
        data={
            "context": "T1.default",
            "candidate_id": second,
            "direction": "up",
        },
        follow_redirects=False,
    )
    assert moved_up.status_code == 303
    chain = load_effective_routing_payload()["T1"]["default"]
    assert isinstance(chain, list)
    assert chain.index(second) < chain.index(first)

    # The mutated canonical chain is rendered on the routing page.
    page = client.get("/routing")
    assert page.status_code == 200
    assert second in page.text

    refused = client.post(
        "/routing/move",
        data={
            "context": "T1.default",
            "candidate_id": second,
            "direction": "sideways",
        },
    )
    assert refused.status_code == 400

    refused_context = client.post(
        "/routing/move",
        data={
            "context": "T9.nope",
            "candidate_id": second,
            "direction": "up",
        },
    )
    assert refused_context.status_code == 400


def test_c16b2_owner_pages_are_reachable_from_dashboard(
    isolated_xdg: None, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from saberops.web import create_app

    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "/access" in dashboard.text
    assert "/routing" in dashboard.text
    assert "/orchestrator" in dashboard.text

    access = client.get("/access")
    assert access.status_code == 200
    assert "Refresh models" in access.text
    assert "routing-eligible" in access.text or "Models" in access.text

    routing = client.get("/routing")
    assert routing.status_code == 200
    assert "Move up" in routing.text

    orch = client.get("/orchestrator")
    assert orch.status_code == 200
    assert "Eligible bindings" in orch.text

