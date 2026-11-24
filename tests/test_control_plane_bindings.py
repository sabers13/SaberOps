"""C16-B2: provider + model owner workflow persistence / restart proof.

Proves, against isolated XDG roots (never the real owner state):

1. a provider connection/catalog exists (access store + model catalog);
2. discovered BUILD models are added to a routing chain;
3. stores/services are reconstructed from disk (simulated restart);
4. the chain and the exact binding identities are still present and resolve.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    OwnerBindingStore,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.binding_store import BindingStoreError
from saberops.control_plane.bindings import resolve_execution_binding
from saberops.model_access import (
    AccessStore,
    ModelCatalogStore,
    apply_discovery_result,
    build_access_registry,
    default_account_connections,
    discover_connection_models,
)
from saberops.project import owner_state_dir
from saberops.routing_config import (
    DynamicCandidate,
    add_candidate_to_chain,
    format_candidate_id,
    load_effective_routing,
    load_effective_routing_payload,
    move_group_in_chain,
    save_user_routing,
)
from saberops.workers.registry import AdapterRegistry

_WORKER_MODEL = "b2-persist-worker-model"
_SECOND_MODEL = "b2-persist-second-model"
_CONNECTION_ID = "opencode-account"
_PROVIDER = "opencode"


def _approve_and_add_candidate(
    payload: dict[str, Any], *, role: str, candidate: DynamicCandidate
) -> dict[str, Any]:
    """Add an owner-approved discovered model through the production path.

    The removed console helper (``add_dynamic_candidate_to_chain``) recorded
    the approval in the canonical routing document and then added its identity.
    This composes the same two production steps: the canonical
    ``approved_candidates`` field plus :func:`add_candidate_to_chain`.
    """
    updated = dict(payload)
    approved = dict(updated.get("approved_candidates") or {})
    approved[candidate.candidate_id] = candidate.to_dict()
    updated["approved_candidates"] = approved
    return add_candidate_to_chain(
        updated, role=role, candidate_id=candidate.candidate_id
    )


def _materialize_worker_role(
    owner: OwnerBindings,
    *,
    connection_id: str,
    provider: str,
    model_id: str,
    registry: AdapterRegistry,
) -> tuple[OwnerBindings, str]:
    """Materialize one BUILD-role binding through the real discovery seam."""
    adapter = registry.get(provider)
    assert adapter is not None
    profile = capability_profile_for(adapter)
    worker, execution_profile, pool = materialize_discovered_binding(
        connection_id=connection_id,
        provider=provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.BUILD,
    )
    candidate = upsert_owner_binding(
        owner, binding=worker, profile=execution_profile, pool=pool
    )
    resolve_execution_binding(
        registry=candidate.registry,
        binding_id=worker.binding_id,
        adapter=adapter,
    )
    return candidate, worker.binding_id


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
        list_models=lambda: [_WORKER_MODEL, _SECOND_MODEL],
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

    # -- the discovery path materializes one exact BUILD binding per model --
    owner = OwnerBindingStore(get_binding_store_path()).load()
    owner, worker_binding_id = _materialize_worker_role(
        owner,
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        model_id=_WORKER_MODEL,
        registry=registry,
    )
    owner, _ = _materialize_worker_role(
        owner,
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        model_id=_SECOND_MODEL,
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
    updated = _approve_and_add_candidate(
        payload, role="WORKER", candidate=approval
    )
    save_user_routing(updated)
    snapshot = load_effective_routing()
    assert candidate_id in [f"{c.provider}/{c.model}" for c in snapshot.chain_for_role("WORKER")]

    # -- reorder keeps membership, changes order ----------------------------
    second_candidate = format_candidate_id(_PROVIDER, _SECOND_MODEL)
    second_approval = DynamicCandidate(
        candidate_id=second_candidate,
        provider=_PROVIDER,
        model=_SECOND_MODEL,
        connection_id=_CONNECTION_ID,
        backend=connection.backend,
        binding_id=(
            f"discovered-binding:worker:{_CONNECTION_ID}:{_SECOND_MODEL}"
        ),
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at=discovered_at,
    )
    payload = load_effective_routing_payload()
    payload = _approve_and_add_candidate(
        payload, role="WORKER", candidate=second_approval
    )

    def _group_of(payload: dict[str, Any], candidate_id: str) -> int:
        for number, group in enumerate(payload["roles"]["BUILD"], start=1):
            if candidate_id in group["routes"]:
                return number
        raise AssertionError(f"{candidate_id} missing from WORKER chain")

    moved = move_group_in_chain(
        payload,
        role="WORKER",
        group=_group_of(payload, second_candidate),
        direction="up",
    )
    save_user_routing(moved)
    reordered = [
        cid
        for group in load_effective_routing_payload()["roles"]["BUILD"]
        for cid in group["routes"]
    ]
    assert isinstance(reordered, list)
    assert reordered.index(second_candidate) < reordered.index(candidate_id)

    # -- 3. reconstruct every store/service from disk (restart) -------------
    del access_store, catalog_store, owner, catalog, result

    reloaded_access = AccessStore(
        owner_state_dir() / AccessStore.FILENAME
    ).load()
    reloaded_catalog = ModelCatalogStore(
        owner_state_dir() / ModelCatalogStore.FILENAME
    ).load()
    reloaded_owner = OwnerBindingStore(get_binding_store_path()).load()
    reloaded_snapshot = load_effective_routing()

    # -- 4. the chain and the exact binding identity are still present -------
    assert reloaded_access.try_get(_CONNECTION_ID) is not None
    entry = reloaded_catalog.try_get(
        connection_id=_CONNECTION_ID,
        provider=_PROVIDER,
        backend=connection.backend,
        model_id=_WORKER_MODEL,
    )
    assert entry is not None and not entry.is_stale

    chain_ids = [f"{c.provider}/{c.model}" for c in reloaded_snapshot.chain_for_role("WORKER")]
    assert candidate_id in chain_ids

    reloaded_registry = AdapterRegistry.default()
    worker_binding = reloaded_owner.registry.try_get(worker_binding_id)
    assert worker_binding is not None
    assert worker_binding.binding_role is BindingRole.BUILD
    assert worker_binding.provider == _PROVIDER
    assert worker_binding_id == (
        f"discovered-binding:worker:{_CONNECTION_ID}:{_WORKER_MODEL}"
    )
    resolve_execution_binding(
        registry=reloaded_owner.registry,
        binding_id=worker_binding_id,
        adapter=reloaded_registry.get(_PROVIDER),
    )


def _seed_two_routing_candidates() -> tuple[str, str]:
    first = format_candidate_id(_PROVIDER, _WORKER_MODEL)
    second = format_candidate_id(_PROVIDER, _SECOND_MODEL)
    for candidate_id, model in ((first, _WORKER_MODEL), (second, _SECOND_MODEL)):
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
            _approve_and_add_candidate(
                load_effective_routing_payload(),
                role="WORKER",
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness admission for a discovered profile uses stored evidence.

    Hermetic by construction: executable availability is provided through
    the service's ``which`` seam (``/fake/opencode``), so the assertion
    proves the discovered profile resolves to its real stored
    connection/readiness evidence on any machine, whether or not the
    ``opencode`` executable happens to be installed.  Production
    semantics are unchanged: a genuinely missing executable is still
    NOT_READY (covered by the readiness-truth suite).
    """
    from saberops.model_access import ReadinessService
    from saberops.models import ProviderReadiness
    from saberops.project import owner_state_dir

    AccessStore(owner_state_dir() / AccessStore.FILENAME).save(
        build_access_registry(default_account_connections())
    )
    service = ReadinessService.for_owner_state(owner_state_dir())
    # Deterministic executable-availability evidence: the discovered
    # opencode profile must resolve against installed=TRUE regardless
    # of the machine running pytest (GitHub runners have no opencode).
    # Bound on the instance because ``which`` defaults are captured at
    # import time; patching ``shutil.which`` would not reach them.
    monkeypatch.setattr(service, "which", lambda _exe: "/fake/opencode")
    service.refresh(
        next(
            c
            for c in default_account_connections()
            if c.connection_id == "opencode-account"
        )
    )
    # A successful execution marks the real connection READY ...
    service.record_success("opencode-account")

    admission = service.admission_for(
        provider="opencode", profile_id="discovered-profile:opencode-account"
    )
    assert admission.connection_id == "opencode-account"
    assert admission.executable_available is True
    assert admission.state is ProviderReadiness.READY


def test_c16b2_routing_move_route_reorders_canonical_chain(
    isolated_xdg: None, tmp_path: Path
) -> None:
    from saberops.routing_config import RoutingConfigError

    first, second = _seed_two_routing_candidates()

    def _chain_ids() -> list[str]:
        return [
            cid
            for group in load_effective_routing_payload()["roles"]["BUILD"]
            for cid in group["routes"]
        ]

    def _group_of(candidate_id: str) -> int:
        payload = load_effective_routing_payload()
        for number, group in enumerate(payload["roles"]["BUILD"], start=1):
            if candidate_id in group["routes"]:
                return number
        raise AssertionError(f"{candidate_id} missing from WORKER chain")

    def _move(candidate_id: str, direction: str) -> None:
        payload = load_effective_routing_payload()
        save_user_routing(
            move_group_in_chain(
                payload, role="WORKER",
                group=_group_of(candidate_id), direction=direction,
            )
        )

    _move(second, "down")
    chain = _chain_ids()
    assert isinstance(chain, list)
    assert chain.index(first) < chain.index(second)

    _move(second, "up")
    chain = _chain_ids()
    assert isinstance(chain, list)
    assert chain.index(second) < chain.index(first)

    with pytest.raises(RoutingConfigError):
        move_group_in_chain(
            load_effective_routing_payload(), role="WORKER",
            group=_group_of(second), direction="sideways",
        )

    with pytest.raises(RoutingConfigError):
        move_group_in_chain(
            load_effective_routing_payload(), role="T9",
            group=1, direction="up",
        )


# ---------------------------------------------------------------------------
# Ported from the removed test_chat_quick_access.py role-routing checks: the
# engine half of role-order append/reorder/remove/clear.
# ---------------------------------------------------------------------------


def test_ported_role_order_validation(isolated: Path) -> None:
    """Ported engine half of test_role_routing_append_reorder_remove_and_chat_scope."""
    owner = OwnerBindings()
    updated = owner.with_role_order("reviewer", ("first", "second"))
    assert updated.role_binding_ids("reviewer") == ("first", "second")
    assert updated.reviewer_binding_ids == ("first", "second")
    with pytest.raises(ValueError):
        updated.role_binding_ids("orchestrator")
    with pytest.raises(BindingStoreError):
        owner.with_role_order("reviewer", ("dup", "dup"))
    with pytest.raises(BindingStoreError):
        owner.with_role_order("reviewer", ("",))
    with pytest.raises(BindingStoreError):
        owner.with_role_order("no-such-role", ("x",))
    cleared = updated.with_role_order("reviewer", ())
    assert cleared.role_binding_ids("reviewer") == ()
    assert cleared.reviewer_binding_ids == ()
