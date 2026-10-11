"""Drag reorder persists canonical order without admitting new candidates.

L-A step 5: the routing page is gone (owner routing lives in the `orch`
CLI + routing config file). These tests drive the same engine seam the
route used (`reorder_candidate_chain` + `save_user_routing`).
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
    reorder_candidate_chain,
    save_user_routing,
)


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_ACCESS_STORE", "ORCH_ACCESS_MODELS_STORE",
                "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)


def _reorder(payload: dict[str, Any], order: Any) -> dict[str, Any]:
    return reorder_candidate_chain(payload, top="T1", sub="default", order=order)


def test_drag_order_persists_and_preserves_other_chains(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    original = before["T1"]["default"]
    order = list(reversed(original))
    updated = _reorder(before, order)
    save_user_routing(updated)
    expected = copy.deepcopy(before)
    expected["T1"]["default"] = order
    assert load_effective_routing_payload() == expected


@pytest.mark.parametrize(
    "kind",
    ["unknown", "missing", "duplicate", "injected", "not-list", "not-string"],
)
def test_invalid_drag_never_writes(isolated_xdg: None, kind: str) -> None:
    before = load_effective_routing_payload()
    original = before["T1"]["default"]
    save_user_routing(before)
    proposed: Any = list(reversed(original))
    if kind == "missing":
        proposed.pop()
    elif kind == "duplicate":
        proposed[0] = proposed[1]
    elif kind == "injected":
        proposed[0] = "anthropic/unapproved-model"
    elif kind == "not-list":
        proposed = {"candidate": original[0]}
    elif kind == "not-string":
        proposed[0] = {"model": "unapproved"}
    top, sub = ("T9", "default") if kind == "unknown" else ("T1", "default")
    with pytest.raises(RoutingConfigError):
        reorder_candidate_chain(before, top=top, sub=sub, order=proposed)
    assert load_effective_routing_payload() == before


def test_malformed_order_payload_never_writes(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    with pytest.raises((RoutingConfigError, ValueError, TypeError)):
        reorder_candidate_chain(before, top="T1", sub="default",
                                order=json.loads("{"))
    assert load_effective_routing_payload() == before


def test_empty_chain_can_remain_empty(isolated_xdg: None) -> None:
    before = load_effective_routing_payload()
    before["T1"]["default"] = []
    save_user_routing(before)
    updated = _reorder(before, [])
    save_user_routing(updated)
    assert load_effective_routing_payload() == before
