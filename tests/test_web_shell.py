"""R0-B / H23: one theme implementation in the surviving shell controller.

The run-page controller (``app.js``) retired with the run page, so the in-page
theme check narrows to the one file that still ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    monkeypatch.delenv("ORCH_READINESS_STORE", raising=False)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)


def test_r0b_one_theme_implementation(isolated_xdg: None, tmp_path: Path) -> None:
    """Theme read/toggle/persist/control-state lives in exactly one served file."""
    client = _client(tmp_path)
    console_js = client.get("/static/js/console.js").text
    assert "saberops-theme" in console_js
    assert "data-set-theme" in console_js
    assert 'setAttribute("data-theme"' in console_js
    assert "toggleTheme" in console_js
    # The retired run-page controller is gone, not merely unlinked.
    assert client.get("/static/js/app.js").status_code == 404
