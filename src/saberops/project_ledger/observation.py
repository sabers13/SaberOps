"""Deterministic repository observation for genesis and takeover.

Everything a machine can read, a machine reads.  Repository identity, HEAD,
branch, root commit, and the existence and content hash of declared project
documents are all facts, not opinions, so no model is consulted for any of
them and none of them is ever recorded as an assumption.

The separation this module exists to enforce is the one takeover gets wrong by
default: *what the repository says* and *what somebody guessed the repository
is for* must never arrive through the same door.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.project import ProjectIdentity, compute_project_identity
from saberops.project_ledger.contracts import (
    ProjectIdentityMismatchError,
    ProjectState,
)

#: Project documents worth hashing when a repository is first observed.  The
#: list is deliberately short and declared: an observation must be cheap,
#: bounded and identical for every caller looking at the same commit.
DEFAULT_MARKER_PATHS: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "Makefile",
    "README.md",
    "pyproject.toml",
    ".orch/project.toml",
)

#: Directory holding declared module contracts, when the project has them.
_ARCHITECTURE_MODULES = "architecture/modules"


@dataclass(frozen=True)
class ObservedFile:
    """One declared project document that actually exists, with its hash."""

    path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering."""
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class RepositoryObservation:
    """Deterministic facts about a repository at one moment.

    Every field here is machine-read.  Nothing in this record is inferred, and
    nothing in it depends on a model, a provider or a previous Orch session.
    """

    identity: ProjectIdentity
    head_sha: str
    branch: str | None
    observed_at: str
    files: tuple[ObservedFile, ...] = ()
    declared_module_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used as durable genesis metadata."""
        return {
            "canonical_path": self.identity.canonical_path,
            "project_id": self.identity.project_id,
            "remote_identity": self.identity.remote_identity,
            "root_commit": self.identity.root_commit,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "observed_at": self.observed_at,
            "files": [record.to_dict() for record in self.files],
            "declared_module_files": sorted(self.declared_module_files),
        }

    def fact_statements(self) -> tuple[tuple[str, str], ...]:
        """Deterministic ``(statement, reference)`` pairs for durable facts.

        These become ``DETERMINISTIC_OBSERVATION`` facts.  They are accepted
        immediately because there is nothing to doubt: a SHA either is the
        recorded HEAD or it is not.
        """
        pairs: list[tuple[str, str]] = [
            ("repository canonical path", self.identity.canonical_path),
            ("repository root commit", self.identity.root_commit),
            ("repository HEAD at takeover", self.head_sha),
        ]
        if self.identity.remote_identity:
            pairs.append(("repository remote identity", self.identity.remote_identity))
        if self.branch:
            pairs.append(("repository branch at takeover", self.branch))
        for record in self.files:
            pairs.append((f"declared project document {record.path}", f"sha256:{record.sha256}"))
        if self.declared_module_files:
            pairs.append(
                (
                    "declared architecture module contracts",
                    ",".join(sorted(self.declared_module_files)),
                )
            )
        return tuple(pairs)


def _hash_file(path: Path) -> tuple[str, int]:
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def observe_repository(
    repo_path: Path | str,
    *,
    observed_at: str,
    marker_paths: tuple[str, ...] = DEFAULT_MARKER_PATHS,
) -> RepositoryObservation:
    """Read every deterministic fact available about ``repo_path``.

    Raises whatever :func:`compute_project_identity` raises when the target is
    not a repository: an unidentifiable repository is not observed with
    guesses, it is refused.
    """
    identity = compute_project_identity(repo_path)
    root = Path(identity.canonical_path)
    files: list[ObservedFile] = []
    for relative in sorted(set(marker_paths)):
        candidate = root / relative
        if candidate.is_file():
            digest, size = _hash_file(candidate)
            files.append(ObservedFile(path=relative, sha256=digest, size=size))
    modules_dir = root / _ARCHITECTURE_MODULES
    declared = (
        tuple(
            sorted(
                f"{_ARCHITECTURE_MODULES}/{entry.name}"
                for entry in modules_dir.iterdir()
                if entry.is_file() and entry.suffix == ".toml"
            )
        )
        if modules_dir.is_dir()
        else ()
    )
    return RepositoryObservation(
        identity=identity,
        head_sha=GitManager.get_head_commit(root),
        branch=GitManager.get_current_branch(root),
        observed_at=observed_at,
        files=tuple(files),
        declared_module_files=declared,
    )


def assert_project_repository(
    state: ProjectState, repo_path: Path | str | None = None
) -> ProjectIdentity:
    """Fail closed unless the repository still is the Project's own.

    Accepted identity never changes, so this re-derives identity from the
    repository and compares it with the ledger.  A different repository at the
    same path -- or the Project's ledger pointed at the wrong checkout -- is
    refused rather than allowed to inherit another Project's authoritative
    state.  ``root_commit`` is checked as recorded history, never as a live
    branch, remote or directory name.
    """
    target = repo_path if repo_path is not None else state.identity.canonical_path
    current = compute_project_identity(target)
    if current.project_id != state.project_id:
        raise ProjectIdentityMismatchError(
            f"repository at {current.canonical_path} is project "
            f"'{current.project_id}', but the ledger belongs to project "
            f"'{state.project_id}' at {state.identity.canonical_path}"
        )
    recorded_root = state.identity.root_commit
    if recorded_root and current.root_commit != recorded_root:
        raise ProjectIdentityMismatchError(
            f"repository at {current.canonical_path} no longer carries the "
            f"recorded root commit {recorded_root[:8]} of project "
            f"'{state.project_id}'"
        )
    return current
