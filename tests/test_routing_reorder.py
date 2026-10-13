"""Drag reorder persists canonical order without admitting new candidates.

L-A step 5: the routing page is gone (owner routing lives in the `orch`
CLI + routing config file). These tests drive the same engine seam the
route used (`reorder_chain_groups` + `save_user_routing`).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from saberops.routing_config import (
    RoutingConfigError,
    load_effective_routing_payload,
    reorder_chain_groups,
    save_user_routing,
)


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_ACCESS_STORE", "ORCH_ACCESS_MODELS_STORE",
                "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)


def _worker_ids(payload: dict[str, Any]) -> list[str]:
    return [group["routes"][0] for group in payload["roles"]["WORKER"]]


def _reorder(payload: dict[str, Any], order: Any) -> dict[str, Any]:
    return reorder_chain_groups(payload, role="WORKER", order=order)


def test_drag_order_persists_and_preserves_other_chains(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    count = len(before["roles"]["WORKER"])
    order = list(reversed(range(1, count + 1)))
    updated = _reorder(before, order)
    save_user_routing(updated)
    expected = copy.deepcopy(before)
    expected["roles"]["WORKER"] = list(reversed(expected["roles"]["WORKER"]))
    assert load_effective_routing_payload() == expected


@pytest.mark.parametrize(
    "kind",
    ["unknown", "missing", "duplicate", "injected", "not-list", "not-string"],
)
def test_invalid_drag_never_writes(isolated_xdg: None, kind: str) -> None:
    before = load_effective_routing_payload()
    count = len(before["roles"]["WORKER"])
    save_user_routing(before)
    proposed: Any = list(reversed(range(1, count + 1)))
    if kind == "missing":
        proposed.pop()
    elif kind == "duplicate":
        proposed[0] = proposed[1]
    elif kind == "injected":
        proposed[0] = count + 1
    elif kind == "not-list":
        proposed = {"candidate": 1}
    elif kind == "not-string":
        proposed[0] = "1"
    role = "T9" if kind == "unknown" else "WORKER"
    with pytest.raises(RoutingConfigError):
        reorder_chain_groups(before, role=role, order=proposed)
    assert load_effective_routing_payload() == before


def test_malformed_order_payload_never_writes(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    with pytest.raises((RoutingConfigError, ValueError, TypeError)):
        reorder_chain_groups(before, role="WORKER",
                             order=json.loads("{"))
    assert load_effective_routing_payload() == before


def test_empty_chain_can_remain_empty(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    before["roles"]["WORKER"] = []
    save_user_routing(before)
    updated = _reorder(before, [])
    save_user_routing(updated)
    assert load_effective_routing_payload() == before
