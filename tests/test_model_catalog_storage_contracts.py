"""Retained evidence contracts that used to share the direct-API test module.

The direct-API worker was removed (H17); these catalog and storage contracts
guarded other modules and stay covered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from saberops.db import DB_SCHEMA_VERSION, Database


def _synthetic_catalog_model() -> Any:
    """One synthetic, non-secret catalog entry (no provider/account call)."""
    from saberops.model_access.connections import TriState
    from saberops.model_access.discovery import DiscoveredModel, ExecutionSupport

    return DiscoveredModel(
        connection_id="local-catalog",
        provider="testapi",
        backend="openai-compatible-api",
        model_id="test-model",
        display_name="Test model",
        discovery_status=TriState.TRUE,
        execution_support=ExecutionSupport.SUPPORTED,
        capabilities=(("tools", TriState.TRUE),),
        discovered_at="2026-01-02T03:04:05+00:00",
    )


def test_model_catalog_digest_and_serialization_contract() -> None:
    """Catalog construction/serialization/digest identity is deterministic.

    Covers: D-0045 retained-behavior repair of ``ModelCatalog.digest`` and
    ``build_model_catalog`` (the deleted A35 test was their only cover).
    Synthetic entries only; never a real account or provider.
    """
    from dataclasses import replace

    from saberops.model_access.discovery import ModelCatalog, build_model_catalog

    model = _synthetic_catalog_model()
    plain = replace(model, model_id="test-model-2", discovered_at=None)
    first = build_model_catalog((model, plain))
    same = ModelCatalog(models=(model, plain))
    assert first.to_dict() == same.to_dict()
    assert model.digest == same.models[0].digest
    assert len(model.digest) == 64

    payload = first.to_dict()
    serialized = json.dumps(payload)
    assert "api_key" not in serialized.lower()
    assert "credential" not in serialized.lower()
    restored = ModelCatalog.from_mapping(payload)
    assert restored == first
    assert restored.models[0].digest == model.digest
    # An entry with no discovery timestamp stays absent, not an empty string.
    assert restored.models[1].discovered_at is None

    # Digest identity follows observable content: a changed flag changes it.
    assert replace(model, stale=True).digest != model.digest


def test_model_catalog_store_override_pins_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ORCH_ACCESS_MODELS_STORE`` pins the explicit store path.

    Covers: D-0045 retained-behavior repair of ``web._model_catalog_store``'s
    override path (the deleted A35 test was its only cover) and the concrete
    default location under the owner state dir.
    """
    from saberops.model_access.discovery import ModelCatalogStore, build_model_catalog
    from saberops.web import _model_catalog_store

    explicit = tmp_path / "pinned" / "models.json"
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(explicit))
    store = _model_catalog_store()
    assert isinstance(store, ModelCatalogStore)
    assert store.path == explicit

    catalog = build_model_catalog((_synthetic_catalog_model(),))
    store.save(catalog)
    assert store.path == explicit
    assert store.load() == catalog

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE")
    default = _model_catalog_store()
    assert default.path == tmp_path / "state" / "saberops" / ModelCatalogStore.FILENAME


def test_storage_audit_events_round_trip(tmp_path: Path) -> None:
    """GS-1 storage audit persistence is additive and idempotent.

    Covers: (bounded write transaction) and Amendment 1 §4
    (durable audit with task/run/artifact identity, bytes and reason).
    """
    db = Database(tmp_path / "orch.db")
    assert DB_SCHEMA_VERSION == 1  # additive tables never bump the schema version
    first = db.record_storage_event(
        at="2026-10-17T12:00:00+00:00",
        phase="intent",
        action="remove-worktree",
        artifact_class="attempt-worktree",
        artifact_ref="/tmp/wt/a1",
        artifact_identity="/tmp/wt/a1",
        reason="worktree-terminal-eligible",
        run_id="run1",
        project_id="proj-1",
        sha="a" * 40,
        policy_ref="owner-file:1:/tmp/gc.json",
        estimated_bytes=1024,
        detail={"branch": "orch/run1/a1"},
    )
    second = db.record_storage_event(
        at="2026-10-17T12:00:01+00:00",
        phase="result",
        action="remove-worktree",
        artifact_class="attempt-worktree",
        artifact_ref="/tmp/wt/a1",
        artifact_identity="/tmp/wt/a1",
        reason="worktree-removed",
        run_id="run1",
        project_id="proj-1",
        sha="a" * 40,
        estimated_bytes=900,
        actual_bytes=900,
    )
    assert second == first + 1

    events = db.list_storage_events()
    assert [event["phase"] for event in events] == ["intent", "result"]
    assert events[0]["run_id"] == "run1" and events[0]["project_id"] == "proj-1"
    assert events[0]["sha"] == "a" * 40
    assert json.loads(events[0]["detail_json"]) == {"branch": "orch/run1/a1"}
    assert db.list_storage_events(run_id="other") == []
    assert len(db.list_storage_events(artifact_ref="/tmp/wt/a1")) == 2

    # The record is durable across reopen, and the DB still opens at version 1.
    reopened = Database(tmp_path / "orch.db")
    assert len(reopened.list_storage_events()) == 2
    assert reopened.get_storage_vacuum_at("owner") is None
    reopened.record_storage_vacuum("owner", __import__("datetime").datetime(
        2026, 10, 17, 12, 0, tzinfo=__import__("datetime").UTC
    ))
    assert reopened.get_storage_vacuum_at("owner") is not None
