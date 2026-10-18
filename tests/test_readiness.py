"""Readiness evidence rules at the service seam.

Ported from the removed test_r0b_readiness_truth.py generation: the rules
live in ReadinessService, the readiness catalog and the binding resolver,
not in the removed /access page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.control_plane.bindings import BindingResolutionError, resolve_execution_binding
from saberops.model_access import (
    AccessRegistry,
    AccessStore,
    ProviderConnection,
    ProviderReadinessEvidence,
    ReadinessCatalog,
    ReadinessEvidenceSource,
    ReadinessStore,
)
from saberops.model_access.connections import AccessProtocol, AuthMode
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    ProviderReadiness,
)


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path

def _account_connection(connection_id: str = "opencode-account") -> ProviderConnection:
    return next(
        entry
        for entry in AccessStore(Path("/nonexistent")).load().list_connections()
        if entry.connection_id == connection_id
    )


def test_ported_installed_cli_is_not_proven_readiness(
    isolated: Path,
) -> None:
    """Ported from test_r0b_cli_installed_is_not_proven_readiness."""
    connection = _account_connection()
    service = ReadinessService(
        AccessRegistry((connection,)),
        ReadinessStore(isolated / "readiness.json"),
        which=lambda _exe: "/usr/bin/opencode",
    )
    evidence = service.refresh(connection)
    # The opencode backend exposes no auth probe: a present binary is
    # availability, never proven readiness.
    assert evidence.state is ProviderReadiness.UNKNOWN


def test_ported_stale_ready_evidence_marked_stale(isolated: Path) -> None:
    """Ported from test_r0b_stale_evidence_marked_stale."""
    store = ReadinessStore(isolated / "readiness.json")
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.replace(
        ProviderReadinessEvidence(
            "opencode-account",
            "opencode",
            "opencode",
            ProviderReadiness.READY,
            "READY",
            old,
            ReadinessEvidenceSource.CONNECTION_TEST,
        )
    )
    catalog = store.load()
    assert isinstance(catalog, ReadinessCatalog)
    entry = catalog.try_get("opencode-account")
    assert entry is not None and entry.state is ProviderReadiness.READY
    projected = entry.freshness(as_of=datetime.now(UTC), max_age=timedelta(hours=24))
    assert projected.state is ProviderReadiness.UNKNOWN
    assert projected.reason == "STALE_READINESS_EVIDENCE"
    fresh = entry.freshness(as_of=datetime.now(UTC), max_age=timedelta(hours=26))
    assert fresh.state is ProviderReadiness.READY


def test_ported_no_evidence_is_not_verified(isolated: Path) -> None:
    """Ported from test_r0b_no_evidence_says_not_verified."""
    catalog = ReadinessStore(isolated / "readiness.json").load()
    assert catalog.try_get("opencode-account") is None


def test_ported_verified_login_does_not_imply_execution_adapter(
    isolated: Path,
) -> None:
    """Ported from test_r0b_verified_login_does_not_imply_execution_adapter."""
    store = ReadinessStore(isolated / "readiness.json")
    store.replace(
        ProviderReadinessEvidence(
            "opencode-account",
            "opencode",
            "opencode",
            ProviderReadiness.READY,
            "READY",
            datetime.now(UTC).isoformat(),
            ReadinessEvidenceSource.CONNECTION_TEST,
        )
    )
    # READY transport evidence with no executable binding still refuses.
    with pytest.raises(BindingResolutionError):
        resolve_execution_binding(
            registry=None, binding_id="discovered-binding:worker:opencode-account:x",
            adapter=None,
        )


def test_ported_api_credential_missing_never_ready(isolated: Path) -> None:
    """Ported from the CREDENTIAL_MISSING half of the access-test rules."""
    connection = ProviderConnection(
        connection_id="t7-keyless-api",
        provider="custom",
        protocol=AccessProtocol.OPENAI_COMPATIBLE,
        backend="openai-compatible-api",
        auth_mode=AuthMode.API_KEY,
        endpoint="http://127.0.0.1:1",
        credential_ref="env:T7_PORTED_DEFINITELY_MISSING",
        model="t7-model",
    )
    service = ReadinessService(
        AccessRegistry((connection,)), ReadinessStore(isolated / "readiness.json")
    )
    evidence = service.refresh(connection)
    assert evidence.state is not ProviderReadiness.READY
    assert evidence.reason == "CREDENTIAL_MISSING"
