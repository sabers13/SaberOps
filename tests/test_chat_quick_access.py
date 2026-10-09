"""Chat controls reuse exact routing and observational usage authorities."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saberops.control_plane.binding_store import (
    OwnerBindings,
    get_binding_store_path,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import BindingRole
from saberops.control_plane.capabilities import capability_profile_for
from saberops.web import create_app
from saberops.workers.registry import AdapterRegistry


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(name, raising=False)
    owner = OwnerBindings()
    adapter = AdapterRegistry.default().get("opencode")
    assert adapter is not None
    capabilities = capability_profile_for(adapter)
    for model, role in (
        ("chat-first", BindingRole.ORCHESTRATOR),
        ("chat-second", BindingRole.ORCHESTRATOR),
        ("worker-only", BindingRole.WORKER),
        ("unpicked", BindingRole.ORCHESTRATOR),
        ("review-first", BindingRole.REVIEWER),
        ("review-second", BindingRole.REVIEWER),
    ):
        binding, profile, pool = materialize_discovered_binding(
            connection_id="opencode-account",
            provider="opencode",
            model=model,
            provider_transport=capabilities.transport,
            harness_capabilities=capabilities.capabilities,
            binding_role=role,
        )
        owner = upsert_owner_binding(owner, binding=binding, profile=profile, pool=pool)
    owner = owner.with_role_order(
        "orchestrator",
        (
            "discovered-binding:orchestrator:opencode-account:chat-first",
            "discovered-binding:orchestrator:opencode-account:chat-second",
        ),
    )
    save_owner_bindings(owner)
    return TestClient(create_app(db_path=tmp_path / "ui.db", seed_cwd_if_empty=False))


def test_picker_only_lists_resolvable_orchestrator_bindings_without_mutation(
    client: TestClient,
) -> None:
    before = get_binding_store_path().read_bytes()
    response = client.get("/chat/models")
    assert response.status_code == 200
    data = response.json()
    assert data["selected_binding_id"].endswith(":chat-first")
    assert {row["model"] for row in data["bindings"]} == {"chat-first", "chat-second"}
    assert get_binding_store_path().read_bytes() == before
    assert "worker-only" not in response.text


def test_chat_selection_persists_and_reflects_on_next_load(client: TestClient) -> None:
    for model in ("chat-first", "chat-second"):
        identity = f"discovered-binding:orchestrator:opencode-account:{model}"
        response = client.post("/chat/model", data={"binding_id": identity})
        assert response.status_code == 200
        assert response.json()["selected_binding_id"] == identity
        owner = load_owner_bindings()
        assert owner.policy is not None and owner.policy.preferred_binding_ids[0] == identity
        assert client.get("/chat/models").json()["selected_binding_id"] == identity


@pytest.mark.parametrize(
    "identity",
    [
        "",
        "unknown",
        "discovered-binding:worker:opencode-account:worker-only",
        "discovered-binding:orchestrator:opencode-account:unpicked",
    ],
)
def test_invalid_chat_selection_never_mutates(client: TestClient, identity: str) -> None:
    before = get_binding_store_path().read_bytes()
    response = client.post("/chat/model", data={"binding_id": identity})
    assert response.status_code == 400
    assert get_binding_store_path().read_bytes() == before


def test_quick_usage_preserves_precision_and_does_not_change_owner_policy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, bool]] = []

    def snapshot(*, refresh: bool = False, force: bool = False) -> list[dict[str, Any]]:
        calls.append((refresh, force))
        return [
            {
                "provider": "codex",
                "name": "Codex",
                "installed": True,
                "windows": [
                    {
                        "label": "5 hour window",
                        "remaining": 59.25,
                        "unlimited": False,
                        "duration": 300,
                        "reset_at": "2026-10-06T12:00:00+00:00",
                    }
                ],
                "source": "Native CLI",
                "checked_at": "2026-10-05T12:00:00+00:00",
                "limits_error": "",
                "hint": "",
            },
            {
                "provider": "anthropic",
                "name": "Claude Code",
                "installed": True,
                "windows": [],
                "limits_error": "Login required",
                "hint": "",
            },
        ]

    monkeypatch.setattr(cast(FastAPI, client.app).state.cli_usage_service, "snapshot", snapshot)
    before = get_binding_store_path().read_bytes()
    quota_before = cast(FastAPI, client.app).state.db.list_quota()
    response = client.get("/quick-access/usage")
    assert response.status_code == 200
    assert "59.25% left" in response.text and 'value="59.25"' in response.text
    assert "Login required" in response.text and response.text.count("<progress") == 2
    assert client.post("/quick-access/usage", data={"force": "0"}).status_code == 200
    assert client.post("/quick-access/usage", data={"force": "1"}).status_code == 200
    assert calls == [(False, False), (True, False), (True, True)]
    assert get_binding_store_path().read_bytes() == before
    assert cast(FastAPI, client.app).state.db.list_quota() == quota_before


def test_chat_and_shared_quick_access_controls_render(client: TestClient) -> None:
    page = client.get("/").text
    assert 'id="quickAccessButton"' in page and 'id="chatModelButton"' in page
    assert 'id="quickUsageDetails"' in page
    assert "quick-access.js" in page
    routing = client.get("/routing").text
    assert 'id="quickAccessButton"' in routing
    assert 'id="chatModelButton"' not in routing


def test_history_has_quick_access_but_no_chat_model_picker(client: TestClient) -> None:
    page = client.get("/?pane=runs").text
    assert 'id="quickAccessButton"' in page
    assert 'id="chatModelButton"' not in page


@pytest.mark.parametrize(
    "role,models",
    [
        ("orchestrator", ("chat-first", "chat-second", "unpicked")),
        ("reviewer", ("review-first", "review-second")),
    ],
)
def test_role_routing_append_reorder_remove_and_chat_scope(
    client: TestClient,
    role: str,
    models: tuple[str, ...],
) -> None:
    identities = [f"discovered-binding:{role}:opencode-account:{model}" for model in models]
    for identity in identities:
        response = client.post(
            "/routing/assign",
            data={
                "target": role,
                f"{role}_binding_id": identity,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert load_owner_bindings().role_binding_ids(role) == tuple(identities)
    # Adding twice keeps one exact entry.
    client.post("/routing/assign", data={"target": role, f"{role}_binding_id": identities[0]})
    assert load_owner_bindings().role_binding_ids(role) == tuple(identities)
    import json

    reversed_ids = list(reversed(identities))
    response = client.post(
        "/routing/reorder",
        data={
            "context": role,
            "order": json.dumps(reversed_ids),
            "expected_order": json.dumps(identities),
        },
    )
    assert response.status_code == 200
    assert load_owner_bindings().role_binding_ids(role) == tuple(reversed_ids)
    assert (
        client.post(
            "/routing/reorder",
            data={
                "context": role,
                "order": json.dumps(identities),
                "expected_order": json.dumps(identities),
            },
        ).status_code
        == 409
    )
    before = get_binding_store_path().read_bytes()
    assert (
        client.post(
            "/routing/reorder",
            data={
                "context": role,
                "order": json.dumps(["unknown", *reversed_ids[1:]]),
                "expected_order": json.dumps(reversed_ids),
            },
        ).status_code
        == 400
    )
    assert get_binding_store_path().read_bytes() == before
    if role == "orchestrator":
        client.post("/chat/model", data={"binding_id": identities[0]})
        assert load_owner_bindings().role_binding_ids(role) == (
            identities[0],
            *[bid for bid in reversed_ids if bid != identities[0]],
        )
    for identity in identities:
        client.post("/routing/remove", data={"context": role, "candidate_id": identity})
    assert load_owner_bindings().role_binding_ids(role) == ()
    if role == "orchestrator":
        assert client.get("/chat/models").json()["bindings"] == []
        assert client.post("/chat/model", data={"binding_id": identities[0]}).status_code == 400
    else:
        assert load_owner_bindings().reviewer_binding_ids == ()


def test_legacy_role_selections_survive_reload_without_catalog_enrollment() -> None:
    from saberops.control_plane.orch_binding import OrchBindingPolicy
    from saberops.models import OrchBindingMode

    legacy = OwnerBindings(
        policy=OrchBindingPolicy(mode=OrchBindingMode.PINNED, pinned_binding_id="legacy-orch"),
        reviewer_binding_id="legacy-review",
    )
    payload = legacy.to_dict()
    payload.pop("reviewer_binding_ids")
    restored = OwnerBindings.from_mapping(payload)
    assert restored.role_binding_ids("orchestrator") == ("legacy-orch",)
    assert restored.role_binding_ids("reviewer") == ("legacy-review",)
