"""OrchestratorService quota persistence (ported from the console cut).

Ported from test_c16c2_quota_set_round_trip: backend state, not HTML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saberops.db import Database


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


def test_ported_quota_set_round_trip(isolated: Path) -> None:
    """Ported from test_c16c2_quota_set_round_trip (backend state, not HTML)."""
    from saberops.service import OrchestratorService

    service = OrchestratorService(db=Database(isolated / "quota.db"))
    state = service.set_quota(
        provider="codex", pool="weekly", remaining_percent=42.5, reset_at=None
    )
    assert state.remaining_percent == 42.5
    fetched = service.get_quota(provider="codex", pool="weekly")
    assert fetched is not None
    assert fetched.remaining_percent == 42.5
    assert fetched.provider == "codex" and fetched.pool == "weekly"
