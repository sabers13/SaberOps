"""R0-B / H23: one theme implementation in the surviving shell controller.

The run-page controller (``app.js``) retired with the run page, so the in-page
theme check narrows to the one file that still ships.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from saberops.web import create_app


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
