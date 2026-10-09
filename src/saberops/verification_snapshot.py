"""Verification against a disposable, fixed Git candidate, not a live worker tree."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationSnapshot:
    path: Path
    commit: str
    tree: str


def git_read(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout.strip()


def source_tree(path: Path) -> str | None:
    """Hash submitted source, including untracked additions and deletions.

    A temporary index avoids modifying the worker's staging area.
    Ignored runtime scratch is outside submitted source.
    """
    import os

    try:
        with tempfile.TemporaryDirectory(prefix="saberops-source-index-") as directory:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
            for args in (["git", "read-tree", "HEAD"], ["git", "add", "-A", "--", "."]):
                subprocess.run(args, cwd=path, env=env, capture_output=True, timeout=30, check=True)
            result = subprocess.run(
                ["git", "write-tree"],
                cwd=path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


@contextmanager
def fixed_snapshot(source: Path) -> Iterator[VerificationSnapshot]:
    commit = git_read(source, "rev-parse", "HEAD")
    tree = git_read(source, "rev-parse", "HEAD^{tree}")
    if source_tree(source) != tree:
        raise ValueError("Candidate has uncommitted changes; verification must be restarted.")
    with tempfile.TemporaryDirectory(prefix="saberops-verification-") as directory:
        target = Path(directory) / "candidate"
        git_read(source, "worktree", "add", "--detach", str(target), commit)
        try:
            yield VerificationSnapshot(target, commit, tree)
        finally:
            git_read(source, "worktree", "remove", "--force", str(target))
