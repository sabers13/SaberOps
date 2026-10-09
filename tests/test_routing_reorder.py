"""Drag reorder persists canonical order without admitting new candidates."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.routing_config import load_effective_routing_payload, save_user_routing
from saberops.web import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_ACCESS_STORE", "ORCH_ACCESS_MODELS_STORE",
                "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)
    return TestClient(create_app(db_path=tmp_path / "web.db", seed_cwd_if_empty=False))


def test_drag_order_persists_and_preserves_other_chains(client: TestClient) -> None:
    before = load_effective_routing_payload()
    original = before["T1"]["default"]
    order = list(reversed(original))
    response = client.post("/routing/reorder", data={
        "context": "T1.default", "order": json.dumps(order),
        "expected_order": json.dumps(original),
    })
    assert response.status_code == 200
    assert response.json()["order"] == order
    expected = copy.deepcopy(before)
    expected["T1"]["default"] = order
    assert load_effective_routing_payload() == expected
    assert client.get("/routing").text.index(order[0]) < client.get("/routing").text.index(order[1])


@pytest.mark.parametrize(
    "kind",
    ["unknown", "missing", "duplicate", "injected", "not-list", "malformed", "not-string"],
)
def test_invalid_drag_never_writes(client: TestClient, kind: str) -> None:
    before = load_effective_routing_payload()
    original = before["T1"]["default"]
    save_user_routing(before)
    proposed: Any = list(reversed(original))
    context = "T1.default"
    if kind == "unknown":
        context = "T9.default"
    elif kind == "missing":
        proposed.pop()
    elif kind == "duplicate":
        proposed[0] = proposed[1]
    elif kind == "injected":
        proposed[0] = "anthropic/unapproved-model"
    elif kind == "not-list":
        proposed = {"candidate": original[0]}
    elif kind == "not-string":
        proposed[0] = {"model": "unapproved"}
    response = client.post("/routing/reorder", data={
        "context": context, "order": "{" if kind == "malformed" else json.dumps(proposed),
        "expected_order": json.dumps(original),
    })
    assert response.status_code == 400
    assert "error" in response.json()
    assert load_effective_routing_payload() == before


def test_stale_order_is_refused(client: TestClient) -> None:
    before = load_effective_routing_payload()
    original = before["T1"]["default"]
    revised = copy.deepcopy(before)
    revised["T1"]["default"] = list(reversed(original))
    save_user_routing(revised)
    response = client.post("/routing/reorder", data={
        "context": "T1.default", "order": json.dumps(original[1:] + original[:1]),
        "expected_order": json.dumps(original),
    })
    assert response.status_code == 409
    assert load_effective_routing_payload() == revised


def test_empty_chain_can_remain_empty(client: TestClient) -> None:
    before = load_effective_routing_payload()
    before["T1"]["default"] = []
    save_user_routing(before)
    response = client.post("/routing/reorder", data={
        "context": "T1.default", "order": "[]", "expected_order": "[]",
    })
    assert response.status_code == 200
    assert load_effective_routing_payload() == before
