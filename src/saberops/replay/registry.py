"""Real project registry for real-project replay.

Strictly adheres to Section 6:
- Only confirmed real projects are registered; no development project
  ships as a built-in default.
- Project identity is verified by durable repository history (root commit and remote identity),
  not merely by local filesystem directory paths.
- Fails closed if repository is replaced, retargeted, or mismatched.
- Owners register confirmed projects explicitly via
  ``RealProjectRegistry(projects=...)`` or ``register_project``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.project import (
    ProjectIdentity,
    ProjectIdentityError,
    compute_project_identity,
)
from saberops.replay.contracts import (
    ReplayProjectMismatchError,
    ReplaySafetyViolationError,
)


@dataclass(frozen=True)
class RegisteredProject:
    """Durable metadata defining a confirmed real project available for replay."""

    project_key: str
    remote_identity: str
    root_commit: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "project_key": self.project_key,
            "remote_identity": self.remote_identity,
            "root_commit": self.root_commit,
            "description": self.description,
        }


# Section 6: No development project ships as a built-in default. The
# default bundled registry is intentionally empty and fails closed for
# unregistered project keys. Owners register confirmed real projects
# explicitly; nothing here invents or bundles project provenance.
DEFAULT_CONFIRMED_PROJECTS: tuple[RegisteredProject, ...] = ()


class RealProjectRegistry:
    """Registry managing verified real projects."""

    def __init__(self, projects: tuple[RegisteredProject, ...] | None = None) -> None:
        raw_list = projects if projects is not None else DEFAULT_CONFIRMED_PROJECTS
        self._projects: dict[str, RegisteredProject] = {p.project_key: p for p in raw_list}

    def get_project(self, project_key: str) -> RegisteredProject | None:
        """Get registered project metadata by key."""
        return self._projects.get(project_key)

    def list_projects(self) -> tuple[RegisteredProject, ...]:
        """Return all registered projects in sorted key order."""
        return tuple(self._projects[k] for k in sorted(self._projects.keys()))

    def register_project(self, project: RegisteredProject) -> None:
        """Add a project to registry (used for extension or test fixtures)."""
        self._projects[project.project_key] = project

    def verify_project_repository(
        self,
        project_key: str,
        repo_path: Path | str,
        require_clean: bool = True,
    ) -> ProjectIdentity:
        """Verify repository matches registered real project history and is clean.

        Fails closed with ReplayProjectMismatchError or ReplaySafetyViolationError.
        """
        registered = self.get_project(project_key)
        if registered is None:
            raise ReplayProjectMismatchError(
                f"Project '{project_key}' is not registered in RealProjectRegistry."
            )

        resolved_path = Path(repo_path).resolve()
        try:
            ident = compute_project_identity(resolved_path)
        except ProjectIdentityError as exc:
            raise ReplayProjectMismatchError(
                f"Target path '{resolved_path}' is not a valid git repository: {exc}"
            ) from exc

        # Verify durable repository identity invariants
        if registered.root_commit and ident.root_commit != registered.root_commit:
            raise ReplayProjectMismatchError(
                f"Repository root commit '{ident.root_commit}' does not match registered "
                f"root commit '{registered.root_commit}' for project '{project_key}'."
            )

        # Canonical Project law (src/saberops/project.py) defines remote_identity
        # as diagnostic/corroborating metadata only, with continuity proven by canonical
        # path plus repository history (root commit).
        # Replay enforces:
        # 1. If a live remote IS present, it must NOT contradict registered.remote_identity.
        # 2. If the live remote is absent, root/history continuity is accepted because
        #    canonical Project law explicitly defines root/history as sufficient.
        if (
            registered.remote_identity
            and ident.remote_identity
            and ident.remote_identity != registered.remote_identity
        ):
            raise ReplayProjectMismatchError(
                f"Repository remote identity '{ident.remote_identity}' does not match "
                f"registered remote identity '{registered.remote_identity}' for '{project_key}'."
            )

        if require_clean and not GitManager.is_clean(resolved_path):
            raise ReplaySafetyViolationError(
                f"Authoritative repository at '{resolved_path}' is not clean."
            )

        return ident


def verify_project_repository(
    project_key: str, repo_path: Path | str, require_clean: bool = True
) -> ProjectIdentity:
    """Module-level convenience helper using default registry."""
    return RealProjectRegistry().verify_project_repository(
        project_key, repo_path, require_clean=require_clean
    )
