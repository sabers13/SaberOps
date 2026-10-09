"""New project selection creates a usable baseline without admitting user files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from saberops.project_bootstrap import prepare_project
from saberops.projects import ProjectRegistry


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, check=True, text=True, capture_output=True
    ).stdout.strip()


def test_new_folder_gets_baseline_and_supports_worktrees(tmp_path: Path) -> None:
    project = tmp_path / "new" / "Project with spaces"
    registry = ProjectRegistry(tmp_path / "projects.json")
    assert registry.select(project, initialize=True)[0]
    assert registry.active() == project
    assert registry.validate_project(project).ready
    assert git(project, "rev-list", "--count", "HEAD") == "1"
    assert git(project, "ls-tree", "HEAD") == ""
    worktree = tmp_path / "worktree"
    git(project, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()
    before = git(project, "rev-parse", "HEAD")
    assert registry.select(project, initialize=True)[0]
    assert git(project, "rev-parse", "HEAD") == before


def test_non_git_folder_keeps_existing_files_uncommitted(tmp_path: Path) -> None:
    project = tmp_path / "files"
    project.mkdir()
    original = b"existing content\x00"
    (project / "notes.txt").write_bytes(original)
    assert prepare_project(project)[0]
    assert (project / "notes.txt").read_bytes() == original
    assert git(project, "ls-tree", "HEAD") == ""
    assert "?? notes.txt" in git(project, "status", "--porcelain")


def test_unborn_repository_keeps_index_and_works_without_configured_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "unborn"
    project.mkdir()
    git(project, "init", "--quiet")
    (project / "staged.txt").write_text("owner staged content")
    git(project, "add", "staged.txt")
    index = (project / ".git" / "index").read_bytes()
    assert prepare_project(project)[0]
    assert git(project, "ls-tree", "HEAD") == ""
    assert git(project, "diff", "--cached", "--name-only") == "staged.txt"
    assert (project / ".git" / "index").read_bytes() == index


def test_existing_git_connects_without_changing_history_remote_or_index(tmp_path: Path) -> None:
    project = tmp_path / "existing"
    project.mkdir()
    git(project, "init", "--quiet")
    git(
        project,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@example.test",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--allow-empty",
        "-m",
        "Owner's commit",
    )
    git(project, "remote", "add", "origin", "https://example.test/repository.git")
    (project / "work.txt").write_text("staged")
    git(project, "add", "work.txt")
    before = git(project, "rev-parse", "HEAD")
    config = (project / ".git" / "config").read_bytes()
    index = (project / ".git" / "index").read_bytes()
    assert prepare_project(project)[0]
    assert git(project, "rev-parse", "HEAD") == before
    assert (project / ".git" / "config").read_bytes() == config
    assert (project / ".git" / "index").read_bytes() == index


def test_invalid_git_metadata_does_not_repair_or_switch_active_project(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "projects.json")
    good = tmp_path / "good"
    assert registry.select(good, initialize=True)[0]
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("invalid existing metadata")
    assert not registry.select(broken, initialize=True)[0]
    assert (broken / ".git").read_text() == "invalid existing metadata"
    assert registry.active() == good


def test_validation_does_not_initialize_folders(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json")
    assert not registry.validate_project(folder).is_git
    assert not registry.select(folder)[0]
    assert not (folder / ".git").exists()
