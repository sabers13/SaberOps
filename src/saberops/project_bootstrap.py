"""Prepare explicitly selected local folders for Git-backed project runs."""

from __future__ import annotations

import os
from pathlib import Path

from saberops.git import GitManager
from saberops.process import run_process


def prepare_project(path: Path | str) -> tuple[bool, str]:
    """Create a repository and empty baseline when necessary; retain user files/index."""
    folder = Path(path).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Cannot create project folder: {exc}"
    if not folder.is_dir():
        return False, "Project path must be a folder"
    if not GitManager.is_git_repo(folder):
        git_entry = folder / ".git"
        if git_entry.exists() or git_entry.is_symlink():
            return False, "Existing Git metadata is invalid; repair it before selecting this folder"
        initialized = run_process(["git", "init", "--quiet"], cwd=folder, timeout=15)
        if not initialized.passed:
            return False, "Could not initialize Git: " + initialized.stderr.strip()
    head = run_process(["git", "rev-parse", "--verify", "HEAD"], cwd=folder, timeout=5)
    if head.passed:
        return True, "Existing Git repository connected"
    # Only a genuinely unborn branch may receive a baseline. Broken refs fail closed.
    branch = run_process(["git", "symbolic-ref", "-q", "HEAD"], cwd=folder, timeout=5)
    if not branch.passed:
        return False, "Git HEAD is invalid; repair it before selecting this folder"
    existing_ref = run_process(
        ["git", "show-ref", "--verify", "--quiet", branch.stdout.strip()], cwd=folder, timeout=5
    )
    if existing_ref.exit_code != 1:
        return False, "Git branch is invalid; repair it before selecting this folder"
    # Plumbing creates an empty tree independently of the owner's staged files.
    tree = run_process(
        ["git", "hash-object", "-w", "-t", "tree", "--stdin"], cwd=folder, timeout=5, input_text=""
    )
    if not tree.passed:
        return False, "Could not create the project's initial Git tree"
    commit = run_process(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit-tree",
            tree.stdout.strip(),
            "-m",
            "Initialize project",
        ],
        cwd=folder,
        timeout=10,
        env={
            "GIT_AUTHOR_NAME": "SaberOps",
            "GIT_AUTHOR_EMAIL": "saberops@localhost",
            "GIT_COMMITTER_NAME": "SaberOps",
            "GIT_COMMITTER_EMAIL": "saberops@localhost",
        },
    )
    if not commit.passed:
        return False, "Could not create the project's initial Git commit"
    # Empty expected old value prevents overwriting a concurrently created branch.
    updated = run_process(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "update-ref",
            "HEAD",
            commit.stdout.strip(),
            "",
        ],
        cwd=folder,
        timeout=5,
    )
    if not updated.passed:
        current = run_process(["git", "rev-parse", "--verify", "HEAD"], cwd=folder, timeout=5)
        if not current.passed:
            return False, "Could not attach the project's initial Git commit"
    return True, "Git repository initialized"
