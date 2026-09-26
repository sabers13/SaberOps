"""R3 first-user UI dogfood checkpoint.

These tests prove the bounded product changes needed to start a genuinely empty
SaberOps owner profile and perform configuration through Web surfaces instead
of hidden JSON/DB edits.  Real provider authentication/discovery is certified
separately on the dogfood host because it depends on installed backends.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.paths import (
    active_profile,
    saberops_cache_dir,
    saberops_config_dir,
    saberops_state_dir,
)
from saberops.projects import ProjectRegistry
from saberops.web import create_app


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def test_profile_isolates_all_owner_namespaces(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_state = saberops_state_dir()
    default_config = saberops_config_dir()
    default_cache = saberops_cache_dir()

    monkeypatch.setenv("SABEROPS_PROFILE", "dogfood-zero")
    assert active_profile() == "dogfood-zero"
    assert saberops_state_dir() == default_state / "profiles" / "dogfood-zero"
    assert saberops_config_dir() == default_config / "profiles" / "dogfood-zero"
    assert saberops_cache_dir() == default_cache / "profiles" / "dogfood-zero"


@pytest.mark.parametrize("bad", ["../escape", "/tmp/nope", "has space", ""])
def test_profile_validation_fails_closed_for_invalid_nonempty_values(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("SABEROPS_PROFILE", bad)
    if not bad:
        assert active_profile() == ""
    else:
        with pytest.raises(ValueError):
            active_profile()


def test_fresh_ui_does_not_seed_current_directory(
    isolated_home: Path, tmp_path: Path
) -> None:
    projects = ProjectRegistry(path=tmp_path / "projects.json")
    client = TestClient(create_app(projects=projects, seed_cwd_if_empty=False))

    page = client.get("/")
    assert page.status_code == 200
    assert projects.active() is None
    assert "No project selected" in page.text
    assert "/setup" in page.text

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "First-run setup" in setup.text
    assert "SETUP INCOMPLETE" in setup.text

    refused = client.post("/runs", data={"task": "do not dispatch"})
    assert refused.status_code == 400
    assert "Select a project in the UI before starting a run." in refused.text


def test_project_can_be_selected_from_ui_and_appears_in_setup(
    isolated_home: Path, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path / "repo")
    projects = ProjectRegistry(path=tmp_path / "projects.json")
    client = TestClient(create_app(projects=projects))

    selected = client.post(
        "/projects/select", data={"path": str(repo)}, follow_redirects=False
    )
    assert selected.status_code == 303
    assert projects.active() == repo.resolve()

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert str(repo.resolve()) in setup.text
    assert "Clean Git repository ready for dispatch" in setup.text
