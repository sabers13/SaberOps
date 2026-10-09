"""Owner authority mode is visible and settable from the Work pane."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.authority import AuthorityMode, HostAccessMode, load_authority_settings
from saberops.projects import ProjectRegistry
from saberops.web import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    projects = ProjectRegistry(path=tmp_path / "projects.json")
    return TestClient(create_app(projects=projects, seed_cwd_if_empty=False))


def test_work_pane_shows_default_mode_with_honest_effects(client: TestClient) -> None:
    page = client.get("/").text
    assert 'id="authority"' in page and "Authority · Supervised" in page
    assert 'value="supervised" checked' in page
    assert "Automatic acceptance is not wired yet." in page
    assert 'action="/settings/authority"' in page


def test_setting_mode_and_host_access_persists_like_the_cli(client: TestClient) -> None:
    response = client.post(
        "/settings/authority",
        data={"mode": "autonomous", "host_access": "unrestricted"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "authority_saved=1" in response.headers["location"]
    settings = load_authority_settings()
    assert settings.mode is AuthorityMode.AUTONOMOUS
    assert settings.host_access is HostAccessMode.UNRESTRICTED
    page = client.get("/?authority_saved=1").text
    assert "Authority · Autonomous" in page and "Authority settings saved." in page


def test_unknown_mode_is_refused_without_writing(client: TestClient) -> None:
    response = client.post("/settings/authority", data={"mode": "yolo"})
    assert response.status_code == 400
    assert load_authority_settings().mode is AuthorityMode.SUPERVISED
