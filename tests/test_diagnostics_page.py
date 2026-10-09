"""Read-only Diagnostics page: doctor checks plus server identity."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.projects import ProjectRegistry
from saberops.web import create_app


def test_diagnostics_renders_doctor_checks_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    client = TestClient(create_app(projects=ProjectRegistry(path=tmp_path / "p.json"),
                                   seed_cwd_if_empty=False))
    page = client.get("/diagnostics")
    assert page.status_code == 200
    assert "orch doctor" in page.text and "This server" in page.text
    assert "Python" in page.text or "python" in page.text
    assert client.get("/health").json()["product"] == "SaberOps"
    assert 'href="/diagnostics"' in client.get("/").text
