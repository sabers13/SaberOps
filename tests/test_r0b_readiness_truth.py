"""R0-B: the Access page shows persisted readiness evidence truthfully.

Display only: no refresh, no probing, no invented readiness.  Uses
temporary stores; providers are never contacted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.model_access import (
    ACCOUNT_BACKENDS,
    ProviderReadinessEvidence,
    ReadinessEvidenceSource,
    ReadinessStore,
)
from saberops.models import ProviderReadiness
from saberops.web import create_app


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
    readiness_path = tmp_path / "readiness" / "access-readiness.json"
    monkeypatch.setenv("ORCH_READINESS_STORE", str(readiness_path))
    return None


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)


def _evidence(
    connection_id: str,
    state: ProviderReadiness,
    reason: str,
    observed_at: str,
) -> ProviderReadinessEvidence:
    descriptor = next(d for d in ACCOUNT_BACKENDS if d.connection_id == connection_id)
    return ProviderReadinessEvidence(
        connection_id=connection_id,
        provider=descriptor.provider,
        backend=descriptor.backend,
        state=state,
        reason=reason,
        observed_at=observed_at,
        source=ReadinessEvidenceSource.CONNECTION_TEST,
    )


def _seed(store_path: Path, entries: list[ProviderReadinessEvidence]) -> None:
    store = ReadinessStore(store_path)
    for entry in entries:
        store.replace(entry)


def test_r0b_ready_evidence_shown(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted READY evidence is shown with reason, time, and age."""
    store_path = Path(os.environ["ORCH_READINESS_STORE"])
    first = ACCOUNT_BACKENDS[0].connection_id
    observed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _seed(store_path, [_evidence(first, ProviderReadiness.READY, "READY", observed)])

    page = _client(tmp_path).get("/access")
    assert page.status_code == 200
    assert first in page.text
    assert "READY" in page.text
    # Age/timestamp derives from the evidence rather than fabricated.
    assert observed in page.text
    assert "ago" in page.text


def test_r0b_non_ready_evidence_shown(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted non-ready evidence is shown, not collapsed into UNKNOWN."""
    store_path = Path(os.environ["ORCH_READINESS_STORE"])
    second = ACCOUNT_BACKENDS[1].connection_id
    observed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _seed(
        store_path,
        [
            _evidence(
                second,
                ProviderReadiness.NOT_READY,
                "BACKEND_NOT_INSTALLED",
                observed,
            )
        ],
    )

    page = _client(tmp_path).get("/access")
    assert page.status_code == 200
    assert "NOT_READY" in page.text
    assert "BACKEND_NOT_INSTALLED" in page.text


def test_r0b_no_evidence_says_not_verified(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """No evidence renders an explicit 'Not verified yet', not a failure."""
    page = _client(tmp_path).get("/access")
    assert page.status_code == 200
    assert "Not verified yet" in page.text


def test_r0b_stale_evidence_marked_stale(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25h-old READY evidence is stale per the existing freshness policy."""
    store_path = Path(os.environ["ORCH_READINESS_STORE"])
    first = ACCOUNT_BACKENDS[0].connection_id
    observed = (datetime.now(UTC) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    _seed(store_path, [_evidence(first, ProviderReadiness.READY, "READY", observed)])

    page = _client(tmp_path).get("/access")
    assert page.status_code == 200
    assert "STALE_READINESS_EVIDENCE" in page.text
    assert "STALE" in page.text


def test_r0b_cli_installed_is_not_proven_readiness(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary on PATH alone is never represented as proven readiness."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _exe: "/usr/bin/fake-backend")
    page = _client(tmp_path).get("/access")
    assert page.status_code == 200
    # Every account row claims installed TRUE now...
    assert "not on PATH" not in page.text
    # ...but with no evidence none claims proven model readiness.
    assert "Not verified yet" in page.text
    readiness_cells = page.text.count("Not verified yet")
    assert readiness_cells == len(ACCOUNT_BACKENDS)
