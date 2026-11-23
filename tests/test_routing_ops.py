"""Owner routing edits from the web UI (routing_ops + /routing/* routes).

Hermetic: isolated XDG roots, a catalog built in memory or in the isolated
store, bindings materialized through the real seam. No provider, model,
credential or network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saberops import routing_ops
from saberops.access_ops import model_catalog_store
from saberops.control_plane import (
    BindingRole,
    capability_profile_for,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.model_access.connections import TriState
from saberops.model_access.discovery import DiscoveredModel, ExecutionSupport, ModelCatalog
from saberops.routing_config import (
    get_user_routing_path,
    load_effective_routing_payload,
    load_packaged_default,
)
from saberops.workers.registry import AdapterRegistry

pytestmark = pytest.mark.packaged_routing_default

_CONNECTION = "opencode-account"
_PROVIDER = "opencode"
_MODELS = ("opencode-go/deepseek-v4-flash", "opencode/minimax-m3", "opencode/brand-new-model")


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("state", "config", "cache"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SABEROPS_MCP_LEASE", str(tmp_path / "state" / "lease.json"))
    for name in ("ORCH_BINDING_STORE", "ORCH_ACCESS_MODELS_STORE", "SABEROPS_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def _seed(models: tuple[str, ...] = _MODELS) -> ModelCatalog:
    """Materialize WORKER + REVIEWER bindings and fresh catalog evidence."""
    registry = AdapterRegistry.default()
    adapter = registry.get(_PROVIDER)
    assert adapter is not None
    profile = capability_profile_for(adapter)
    owner = load_owner_bindings()
    entries = []
    for model in models:
        for role in (BindingRole.BUILD, BindingRole.CHECK):
            binding, profile_row, pool = materialize_discovered_binding(
                connection_id=_CONNECTION,
                provider=_PROVIDER,
                model=model,
                provider_transport=profile.transport,
                harness_capabilities=profile.capabilities,
                binding_role=role,
            )
            owner = upsert_owner_binding(owner, binding=binding, profile=profile_row, pool=pool)
        entries.append(
            DiscoveredModel(
                connection_id=_CONNECTION,
                provider=_PROVIDER,
                backend="opencode",
                model_id=model,
                discovery_status=TriState.TRUE,
                execution_support=ExecutionSupport.SUPPORTED,
            )
        )
    save_owner_bindings(owner)
    catalog = ModelCatalog(models=tuple(entries))
    model_catalog_store().save(catalog)
    return catalog


def test_fresh_install_routes_nothing_by_default(isolated_xdg: None) -> None:
    """Every routed job starts empty; nothing is substituted."""
    snapshot = load_packaged_default()
    assert all(not groups for _role, groups in snapshot.role_chains)


def test_only_evidenced_models_are_offered(isolated_xdg: None) -> None:
    catalog = _seed()
    stale = DiscoveredModel(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        backend="opencode",
        model_id="opencode/no-binding",
        discovery_status=TriState.TRUE,
        execution_support=ExecutionSupport.SUPPORTED,
    )
    catalog = ModelCatalog(models=(*catalog.models, stale))
    keys = {
        o.key
        for o in routing_ops.chain_options(
            load_owner_bindings(), catalog, AdapterRegistry.default()
        )
    }
    assert keys == {f"{_PROVIDER}/{model}" for model in _MODELS}


def test_add_move_remove_writes_canonical_routing(isolated_xdg: None) -> None:
    catalog = _seed()
    registry = AdapterRegistry.default()
    for model in _MODELS:
        routing_ops.add_to_job(
            "write",
            f"{_PROVIDER}/{model}",
            owner=load_owner_bindings(),
            catalog=catalog,
            registry=registry,
        )
    payload = load_effective_routing_payload()
    chain = [group["routes"] for group in payload["roles"]["BUILD"]]
    assert chain == [[f"{_PROVIDER}/{m}"] for m in _MODELS]
    # A discovered model outside the static catalog carries its exact binding approval.
    approval = payload["approved_candidates"][f"{_PROVIDER}/opencode/brand-new-model"]
    assert (
        approval["binding_id"]
        == f"discovered-binding:worker:{_CONNECTION}:opencode/brand-new-model"
    )
    assert approval["training_required"] == "UNKNOWN"

    routing_ops.move_in_job("write", 3, "up", owner=load_owner_bindings())
    chain = [g["routes"][0] for g in load_effective_routing_payload()["roles"]["BUILD"]]
    assert chain[1] == f"{_PROVIDER}/opencode/brand-new-model"

    for model in _MODELS:
        routing_ops.remove_from_job("write", f"{_PROVIDER}/{model}", owner=load_owner_bindings())
    assert load_effective_routing_payload()["roles"]["BUILD"] == []


def test_unknown_model_is_refused_and_nothing_written(isolated_xdg: None) -> None:
    catalog = _seed()
    with pytest.raises(routing_ops.RoutingEditError):
        routing_ops.add_to_job(
            "repair",
            "opencode/invented-model",
            owner=load_owner_bindings(),
            catalog=catalog,
            registry=AdapterRegistry.default(),
        )
    assert not get_user_routing_path().exists()


def test_planner_job_cannot_be_edited(isolated_xdg: None) -> None:
    with pytest.raises(routing_ops.RoutingEditError):
        routing_ops.remove_from_job("plan", "anything", owner=load_owner_bindings())


def test_reviewer_order_uses_exact_reviewer_bindings(isolated_xdg: None) -> None:
    catalog = _seed()
    registry = AdapterRegistry.default()
    first = f"discovered-binding:reviewer:{_CONNECTION}:{_MODELS[0]}"
    second = f"discovered-binding:reviewer:{_CONNECTION}:{_MODELS[1]}"
    for key in (first, second):
        routing_ops.add_to_job(
            "check", key, owner=load_owner_bindings(), catalog=catalog, registry=registry
        )
    assert load_owner_bindings().role_binding_ids("reviewer") == (first, second)
    routing_ops.move_in_job("check", 2, "up", owner=load_owner_bindings())
    assert load_owner_bindings().role_binding_ids("reviewer") == (second, first)
    worker_id = f"discovered-binding:worker:{_CONNECTION}:{_MODELS[2]}"
    with pytest.raises(routing_ops.RoutingEditError):
        routing_ops.add_to_job(
            "check", worker_id, owner=load_owner_bindings(), catalog=catalog, registry=registry
        )







def test_auto_refresh_only_relists_missing_stale_or_old_lists(
    isolated_xdg: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from saberops import access_ops

    now = datetime.now(UTC)

    def model(cid: str, *, stale: bool = False, age_h: float = 1) -> DiscoveredModel:
        return DiscoveredModel(
            connection_id=cid,
            provider=_PROVIDER,
            backend="opencode",
            model_id=f"opencode/{cid}",
            discovery_status=TriState.TRUE,
            execution_support=ExecutionSupport.SUPPORTED,
            stale=stale,
            discovered_at=(now - timedelta(hours=age_h)).isoformat(),
        )

    connections = [
        SimpleNamespace(connection_id=cid, is_account_backed=True)
        for cid in ("codex-chatgpt-account", "opencode-account", "claude-code-subscription")
    ]
    catalog = ModelCatalog(
        models=(
            model("codex-chatgpt-account"),  # fresh
            model("opencode-account", stale=True),  # stale
            model("claude-code-subscription", age_h=30),  # old
        )
    )
    monkeypatch.setattr(
        access_ops,
        "_require_registry",
        lambda: SimpleNamespace(list_connections=lambda: connections),
    )
    monkeypatch.setattr(access_ops, "load_catalog_safely", lambda: catalog)
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/{name}")
    relisted: list[str] = []

    def fake_discover(cid: str) -> dict[str, bool]:
        relisted.append(cid)
        return {"ok": True}

    monkeypatch.setattr(access_ops, "discover_access_models", fake_discover)
    outcomes = access_ops.refresh_outdated_model_lists()
    assert relisted == ["opencode-account", "claude-code-subscription"]
    assert outcomes["codex-chatgpt-account"] == "fresh"
    assert outcomes["opencode-account"] == "refreshed"
