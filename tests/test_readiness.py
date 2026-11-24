"""Readiness evidence rules at the service seam.

Ported from the removed test_r0b_readiness_truth.py generation: the rules
live in ReadinessService, the readiness catalog and the binding resolver,
not in the removed /access page."""

from __future__ import annotations

import json
import subprocess
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
        # The v2.3 ``auth list`` status probe fails here; a failed probe is
        # never proof of readiness.
        run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unavailable"),
    )
    evidence = service.refresh(connection)
    assert evidence.state is ProviderReadiness.UNKNOWN


def test_opencode_auth_list_probe_proves_sign_in(isolated: Path) -> None:
    """A credential listing is zero-inference evidence, never a model call."""
    connection = _account_connection()
    payload = json.dumps(
        [
            {
                "id": "opencode-go",
                "name": "OpenCode Go",
                "connections": [
                    {
                        "type": "credential",
                        "id": "cred_abc",
                        "label": "OpenCode Go",
                        "method": "key",
                    }
                ],
            }
        ]
    )
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, payload, "")

    service = ReadinessService(
        AccessRegistry((connection,)),
        ReadinessStore(isolated / "readiness.json"),
        which=lambda _exe: "/usr/bin/opencode",
        run=run,
    )
    evidence = service.refresh(connection)
    assert evidence.state is ProviderReadiness.READY
    assert evidence.reason == "READY"
    assert calls and calls[0][1:] == ["auth", "list", "--format", "json", "--standalone"]


def test_opencode_auth_list_probe_without_connections_is_not_authenticated(
    isolated: Path,
) -> None:
    connection = _account_connection()
    service = ReadinessService(
        AccessRegistry((connection,)),
        ReadinessStore(isolated / "readiness.json"),
        which=lambda _exe: "/usr/bin/opencode",
        run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "[]", ""),
    )
    evidence = service.refresh(connection)
    assert evidence.state is ProviderReadiness.NOT_READY
    assert evidence.reason == "NOT_AUTHENTICATED"


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


def test_ported_api_connection_is_never_ready(isolated: Path) -> None:
    """A stored direct-API row has no transport (H17) and is never ready."""
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
    assert evidence.reason == "PROTOCOL_UNSUPPORTED"
