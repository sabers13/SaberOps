"""Deterministic path -> logical module ownership.

Ownership is the foundation every other devscope answer rests on: a path with
no owner, or with two, means the repository cannot be scoped safely.  Those
states are reported explicitly rather than guessed, and callers escalate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from saberops.devscope.manifest import ModuleManifest
from saberops.devscope.paths import PACKAGE_ROOT, iter_files, match_any


@dataclass(frozen=True)
class OwnershipReport:
    """Which module owns which production path, and where that broke down."""

    owner_by_path: dict[str, str]
    unowned: tuple[str, ...]
    overlapping: tuple[tuple[str, tuple[str, ...]], ...]
    production_paths: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Whether every production path has exactly one owner."""
        return not self.unowned and not self.overlapping

    def owner_of(self, path: str) -> str | None:
        """Return the single owning module of ``path``, or ``None``."""
        return self.owner_by_path.get(path)


def production_paths(root: Path) -> tuple[str, ...]:
    """Every production Python path that must carry exactly one owner."""
    return iter_files(root, PACKAGE_ROOT, suffix=".py")


def resolve_ownership(root: Path, manifests: tuple[ModuleManifest, ...]) -> OwnershipReport:
    """Match every production Python path against the declared owned patterns."""
    owner_by_path: dict[str, str] = {}
    unowned: list[str] = []
    overlapping: list[tuple[str, tuple[str, ...]]] = []

    paths = production_paths(root)
    for path in paths:
        owners = tuple(
            manifest.name for manifest in manifests if match_any(manifest.owned_paths, path)
        )
        if not owners:
            unowned.append(path)
        elif len(owners) > 1:
            overlapping.append((path, tuple(sorted(owners))))
        else:
            owner_by_path[path] = owners[0]

    return OwnershipReport(
        owner_by_path=dict(sorted(owner_by_path.items())),
        unowned=tuple(unowned),
        overlapping=tuple(overlapping),
        production_paths=paths,
    )


def owner_of_any_path(
    relative_path: str, manifests: tuple[ModuleManifest, ...]
) -> tuple[str, ...]:
    """Return every module claiming ``relative_path`` through owned or test globs.

    Unlike :func:`resolve_ownership` this also considers declared test globs,
    so a changed test file can be attributed to the module it verifies.
    """
    owners = {
        manifest.name
        for manifest in manifests
        if match_any(manifest.owned_paths, relative_path)
        or match_any(manifest.test_paths, relative_path)
    }
    return tuple(sorted(owners))
