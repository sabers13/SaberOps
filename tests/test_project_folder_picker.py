"""Desktop folder selection stays local and reuses canonical project selection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops import folder_picker
from saberops.web import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    return TestClient(
        create_app(db_path=tmp_path / "ui.db", seed_cwd_if_empty=False),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 1234),
    )


@pytest.mark.parametrize(
    "platform,tool",
    [
        ("linux", "zenity"),
        ("linux", "kdialog"),
        ("darwin", "osascript"),
        ("win32", "powershell.exe"),
    ],
)
def test_native_picker_returns_exact_folder_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    tool: str,
) -> None:
    folder = tmp_path / "Project with spaces"
    folder.mkdir()
    monkeypatch.setattr("saberops.folder_picker.sys.platform", platform)
    monkeypatch.setattr(
        "saberops.folder_picker.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == tool else None,
    )
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert not kwargs.get("shell")
        assert kwargs["timeout"] == 120
        return subprocess.CompletedProcess(command, 0, f"{folder}\n", "")

    monkeypatch.setattr("saberops.folder_picker.subprocess.run", run)
    assert folder_picker.choose_folder() == str(folder)
    assert calls[0][0] == tool


def test_cancel_timeout_and_retry_release_picker_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("saberops.folder_picker.sys.platform", "linux")
    monkeypatch.setattr("saberops.folder_picker.shutil.which", lambda name: "/usr/bin/zenity")

    def cancel(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("saberops.folder_picker.subprocess.run", cancel)
    assert folder_picker.choose_folder() is None

    def timeout(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr("saberops.folder_picker.subprocess.run", timeout)
    with pytest.raises(folder_picker.FolderPickerError):
        folder_picker.choose_folder()
    monkeypatch.setattr("saberops.folder_picker.subprocess.run", cancel)
    assert folder_picker.choose_folder() is None


def test_local_picker_and_project_selection(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "chosen project"
    project.mkdir()
    subprocess.run(["git", "init", str(project)], capture_output=True, check=True)
    monkeypatch.setattr(folder_picker, "choose_folder", lambda: str(project))
    response = client.post("/projects/browse", headers={"Origin": "http://127.0.0.1"})
    assert response.json() == {"path": str(project)}
    response = client.post(
        "/projects/select?from=setup",
        data={"path": response.json()["path"]},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    assert str(project) in client.get("/setup").text
    monkeypatch.setattr(folder_picker, "choose_folder", lambda: None)
    assert client.post("/projects/browse").json() == {"path": None}
    assert str(project) in client.get("/setup").text
    empty_folder = tmp_path / "not a repository"
    empty_folder.mkdir()
    selected = client.post(
        "/projects/select?from=setup", data={"path": str(empty_folder)}, follow_redirects=False
    )
    assert selected.status_code == 303
    assert (empty_folder / ".git").is_dir()
    assert str(empty_folder) in client.get("/setup").text
    invalid_file = tmp_path / "file.txt"
    invalid_file.write_text("keep")
    invalid = client.post("/projects/select?from=setup", data={"path": str(invalid_file)})
    assert invalid.status_code == 400 and "setup-step-5" in invalid.text
    assert "Cannot select project" in invalid.text
    assert str(empty_folder) in client.get("/setup").text


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_foreign_page_cannot_open_desktop_dialog(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    def forbidden() -> None:
        raise AssertionError("dialog must not open")

    monkeypatch.setattr(folder_picker, "choose_folder", forbidden)
    assert client.post("/projects/browse", headers=headers).status_code == 403


def test_native_picker_unavailable_has_manual_fallback(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise folder_picker.FolderPickerError("Folder chooser unavailable. Use path instead.")

    monkeypatch.setattr(folder_picker, "choose_folder", unavailable)
    assert client.post("/projects/browse").status_code == 503
    page = client.get("/setup").text
    assert 'type="button" data-project-browse' in page
    assert "Use path" in page and "/static/js/project-picker.js" in page
