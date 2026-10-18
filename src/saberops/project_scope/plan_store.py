"""Durable versioned cross-phase plan store (Step 015 / PL-1).

What this module is
-------------------
File-backed, tamper-evident storage for owner-approved plan revisions plus a
deterministic adapter into the authoritative snapshots consumed by
:func:`saberops.ready_frontier.project_ready_frontier`.

* :class:`PlanRevision` is immutable: version string, project ID, task
  definitions and a SHA-256 content hash. Loading recomputes the hash, so a
  hand-edited revision file is detected instead of silently trusted.
* :class:`PlanStore` saves immutable revisions (``<root>/<project>/<v>.json``),
  lists them, and tracks one active version per project. Revisions are never
  overwritten; a correction is a new revision and an explicit activation.
* Saving validates the dependency DAG fail-closed: duplicate or empty task
  IDs, references to unknown same-project tasks, self-dependencies and hard
  START cycles are all refused before anything is written.
* :func:`plan_revisions_to_snapshot` adapts stored revisions to the
  :class:`~saberops.ready_frontier.FrontierSnapshot` shape so the pure
  projection stays the single readiness authority; this module performs no
  scheduling and no launch.

No model, provider or network calls. The only clock read is the default
``created_at`` stamp, which callers (and every test) may pass explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saberops.ready_frontier import (
    FrontierSnapshot,
    PlanTask,
    ProjectPlan,
    SnapshotSource,
    TaskKind,
    TaskRef,
    TaskStatus,
)

__all__ = [
    "ACTIVE_FILENAME",
    "DEFAULT_PLAN_STORE_DIRNAME",
    "PlanNotFoundError",
    "PlanRevision",
    "PlanStore",
    "PlanStoreError",
    "PlanTamperError",
    "PlanTaskDef",
    "PlanValidationError",
    "compute_revision_hash",
    "default_plan_store_root",
    "plan_revisions_to_snapshot",
]

#: Name of the per-project file naming the active revision.
ACTIVE_FILENAME = "active"

#: Directory name of the plan store under the owner state dir.
DEFAULT_PLAN_STORE_DIRNAME = "plan-store"

#: Format version of each persisted revision document.
PLAN_REVISION_FORMAT_VERSION = 1

_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class PlanStoreError(ValueError):
    """Raised for plan-store I/O and lookup failures."""


class PlanNotFoundError(PlanStoreError):
    """Raised when a project, revision or active pointer does not exist."""


class PlanValidationError(PlanStoreError):
    """Raised when a plan DAG is invalid (fail closed, nothing written)."""


class PlanTamperError(PlanStoreError):
    """Raised when a stored revision fails its content-hash check."""


def _require_version(value: object) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise PlanValidationError(
            f"malformed plan version {value!r}: use 1-64 [A-Za-z0-9._-] "
            "starting with an alphanumeric"
        )
    return value


def _require_slug(value: object, field: str) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise PlanValidationError(
            f"malformed {field} {value!r}: use 1-64 lowercase letters, digits, "
            "and interior dashes only"
        )
    return value


def _require_task_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError("every plan task needs a non-empty task_id")
    return value.strip()


def _stripped(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Strip benign whitespace from string entries, passing others through."""
    return tuple(item.strip() if isinstance(item, str) else item for item in values)


@dataclass(frozen=True)
class PlanTaskDef:
    """One task definition inside a stored plan revision.

    ``start_dependencies`` are hard START edges and
    ``integration_prerequisites`` are deferred integration edges, each either
    a same-project task ID (``"t2"``) or a cross-project reference
    (``"other-project/t9"``). ``owns`` declares write scopes exactly as
    :class:`~saberops.ready_frontier.PlanTask` expects them.
    """

    task_id: str
    title: str = ""
    phase: str = ""
    start_dependencies: tuple[str, ...] = ()
    integration_prerequisites: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Canonicalize benign spelling so hashing and validation agree."""
        if isinstance(self.task_id, str):
            object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "start_dependencies", _stripped(self.start_dependencies))
        object.__setattr__(
            self, "integration_prerequisites", _stripped(self.integration_prerequisites)
        )
        object.__setattr__(self, "owns", _stripped(self.owns))

    def as_dict(self) -> dict[str, object]:
        """Serialize deterministically for hashing and storage."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "phase": self.phase,
            "start_dependencies": list(self.start_dependencies),
            "integration_prerequisites": list(self.integration_prerequisites),
            "owns": list(self.owns),
        }

    @classmethod
    def from_dict(cls, data: object) -> PlanTaskDef:
        """Reconstruct from a persisted mapping, validating shapes."""
        if not isinstance(data, dict):
            raise PlanValidationError("plan task definition must be a mapping")
        task_id = _require_task_id(data.get("task_id"))
        title = data.get("title", "")
        phase = data.get("phase", "")
        if not isinstance(title, str) or not isinstance(phase, str):
            raise PlanValidationError(f"task {task_id!r}: title/phase must be strings")
        return cls(
            task_id=task_id,
            title=title,
            phase=phase,
            start_dependencies=_require_str_tuple(data.get("start_dependencies", ()), task_id),
            integration_prerequisites=_require_str_tuple(
                data.get("integration_prerequisites", ()), task_id
            ),
            owns=_require_str_tuple(data.get("owns", ()), task_id),
        )


def _require_str_tuple(value: object, task_id: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PlanValidationError(f"task {task_id!r}: dependency/scope lists must hold strings")
    cleaned: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise PlanValidationError(f"task {task_id!r}: blank dependency/scope entry")
        cleaned.append(entry.strip())
    return tuple(cleaned)


def compute_revision_hash(
    version: str, project_id: str, tasks: tuple[PlanTaskDef, ...], created_at: str
) -> str:
    """Return the canonical SHA-256 over a revision's content fields."""
    canonical = json.dumps(
        {
            "version": version,
            "project_id": project_id,
            "tasks": [task.as_dict() for task in tasks],
            "created_at": created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanRevision:
    """One immutable, tamper-evident versioned plan for a single project."""

    version: str
    project_id: str
    tasks: tuple[PlanTaskDef, ...] = field(default=())
    created_at: str = ""
    sha256: str = ""

    def verify(self) -> bool:
        """Recompute the content hash; ``True`` only when untampered."""
        if not self.sha256:
            return False
        return self.sha256 == compute_revision_hash(
            self.version, self.project_id, self.tasks, self.created_at
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize for durable storage, including the content hash."""
        return {
            "format_version": PLAN_REVISION_FORMAT_VERSION,
            "version": self.version,
            "project_id": self.project_id,
            "tasks": [task.as_dict() for task in self.tasks],
            "created_at": self.created_at,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: object) -> PlanRevision:
        """Reconstruct from storage, raising :class:`PlanTamperError` on mismatch."""
        if not isinstance(data, dict):
            raise PlanTamperError("stored plan revision must be a mapping")
        version = _require_version(data.get("version"))
        project_id = _require_slug(data.get("project_id"), "project_id")
        raw_tasks = data.get("tasks", ())
        if not isinstance(raw_tasks, (list, tuple)):
            raise PlanTamperError(f"revision {version!r}: tasks must be a list")
        tasks = tuple(PlanTaskDef.from_dict(entry) for entry in raw_tasks)
        created_at = data.get("created_at", "")
        if not isinstance(created_at, str):
            raise PlanTamperError(f"revision {version!r}: created_at must be a string")
        sha256 = data.get("sha256", "")
        if not isinstance(sha256, str) or not sha256:
            raise PlanTamperError(f"revision {version!r}: missing content hash")
        revision = cls(
            version=version,
            project_id=project_id,
            tasks=tasks,
            created_at=created_at,
            sha256=sha256,
        )
        if not revision.verify():
            raise PlanTamperError(
                f"revision {version!r} of project {project_id!r} fails its "
                "content-hash check: the stored file was modified"
            )
        return revision


def _split_ref(ref: str, task_id: str, field: str) -> tuple[str | None, str]:
    """Split a dependency entry into ``(other_project_or_None, task)``."""
    if "/" in ref:
        project_part, _, task_part = ref.partition("/")
        if not project_part or not task_part or "/" in task_part:
            raise PlanValidationError(
                f"task {task_id!r}: malformed {field} reference {ref!r} "
                "(use 'task-id' or 'project-id/task-id')"
            )
        _require_slug(project_part, f"{field} project")
        return project_part, task_part.strip() or task_part
    return None, ref


def validate_plan_tasks(project_id: str, tasks: tuple[PlanTaskDef, ...]) -> None:
    """Validate one project's task DAG fail-closed (no I/O, no mutation).

    Refuses duplicate or blank task IDs, references to unknown same-project
    tasks, self-dependencies and hard START cycles. Cross-project references
    (``"other/task"``) are shape-checked; their targets live in another
    project's revision and are resolved by the snapshot adapter.
    """
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task.task_id, str) or not task.task_id:
            raise PlanValidationError("every plan task needs a non-empty task_id")
        if task.task_id in seen:
            raise PlanValidationError(f"duplicate task_id {task.task_id!r}")
        seen.add(task.task_id)
    for task in tasks:
        for edge_field, edges in (
            ("start_dependencies", task.start_dependencies),
            ("integration_prerequisites", task.integration_prerequisites),
        ):
            for edge in edges:
                if not isinstance(edge, str) or not edge:
                    raise PlanValidationError(f"task {task.task_id!r}: blank {edge_field} entry")
        for edge in task.start_dependencies:
            other, target = _split_ref(edge, task.task_id, "start_dependencies")
            if other is not None:
                continue
            if target == task.task_id:
                raise PlanValidationError(
                    f"task {task.task_id!r} depends on itself (hard START cycle)"
                )
            if target not in seen:
                raise PlanValidationError(
                    f"task {task.task_id!r} depends on unknown task {target!r}"
                )
    if _has_start_cycle(tasks):
        raise PlanValidationError("hard START dependencies contain a cycle")


def _has_start_cycle(tasks: tuple[PlanTaskDef, ...]) -> bool:
    """Deterministic DFS cycle check over same-project hard START edges."""
    edges: dict[str, list[str]] = {}
    for task in tasks:
        local: list[str] = []
        for edge in task.start_dependencies:
            other, target = _split_ref(edge, task.task_id, "start_dependencies")
            if other is None:
                local.append(target)
        edges[task.task_id] = sorted(local)
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for nxt in edges.get(node, ()):
            if nxt in edges and walk(nxt):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    return any(walk(task_id) for task_id in sorted(edges))


def default_plan_store_root() -> Path:
    """Return the canonical plan-store root under the owner state dir."""
    from saberops.paths import owner_state_dir

    return owner_state_dir() / DEFAULT_PLAN_STORE_DIRNAME


class PlanStore:
    """Durable file-backed store of versioned per-project plans.

    Layout is ``<root>/<project_id>/<version>.json`` plus an ``active`` file
    per project naming the active version. Writes are atomic; revisions are
    immutable once written (re-saving an existing version is refused).
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else default_plan_store_root()

    @property
    def root(self) -> Path:
        """Filesystem root holding every project's revisions."""
        return self._root

    def _project_dir(self, project_id: str) -> Path:
        return self._root / _require_slug(project_id, "project_id")

    def _revision_path(self, project_id: str, version: str) -> Path:
        name = _require_version(version) + ".json"
        if ".." in name or "/" in name:
            raise PlanValidationError(f"malformed plan version {version!r}")
        return self._project_dir(project_id) / name

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)

    def save_revision(
        self,
        project_id: str,
        tasks: Iterable[PlanTaskDef],
        *,
        version: str | None = None,
        created_at: str | None = None,
    ) -> PlanRevision:
        """Validate and persist a new immutable revision, returning it.

        ``version`` defaults to the next ``v<N>`` for the project;
        re-saving an existing version is refused. ``created_at`` defaults
        to the current UTC time; pass it explicitly for determinism.
        """
        slug = _require_slug(project_id, "project_id")
        task_tuple = tuple(tasks)
        validate_plan_tasks(slug, task_tuple)
        if version is None:
            existing = self.list_revisions(slug)
            taken = {
                int(name[1:]) for name in existing if name.startswith("v") and name[1:].isdigit()
            }
            version = f"v{(max(taken) + 1) if taken else 1}"
        else:
            _require_version(version)
        path = self._revision_path(slug, version)
        if path.exists():
            raise PlanStoreError(
                f"revision {version!r} of project {slug!r} already exists: "
                "revisions are immutable, save a new version"
            )
        from datetime import UTC, datetime

        stamp = created_at if created_at is not None else (datetime.now(UTC).isoformat())
        revision = PlanRevision(
            version=version,
            project_id=slug,
            tasks=task_tuple,
            created_at=stamp,
            sha256=compute_revision_hash(version, slug, task_tuple, stamp),
        )
        self._write_text(path, json.dumps(revision.as_dict(), indent=2, sort_keys=True) + "\n")
        return revision

    def get_revision(self, project_id: str, version: str) -> PlanRevision:
        """Load one revision, verifying its content hash (tamper-evident)."""
        path = self._revision_path(project_id, version)
        if not path.is_file():
            raise PlanNotFoundError(
                f"revision {version!r} of project {project_id!r} does not exist"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlanTamperError(
                f"revision {version!r} of project {project_id!r} is unreadable: {exc}"
            ) from exc
        return PlanRevision.from_dict(data)

    def list_revisions(self, project_id: str) -> tuple[str, ...]:
        """Return stored version strings for a project in sorted order."""
        directory = self._project_dir(project_id)
        if not directory.is_dir():
            return ()
        versions = [
            entry.name[:-5]
            for entry in sorted(directory.iterdir())
            if entry.is_file() and entry.name.endswith(".json")
        ]
        return tuple(versions)

    def active_version(self, project_id: str) -> str | None:
        """Return the active version, or ``None`` when none is set."""
        pointer = self._project_dir(project_id) / ACTIVE_FILENAME
        if not pointer.is_file():
            return None
        try:
            return pointer.read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise PlanStoreError(
                f"cannot read active pointer for project {project_id!r}: {exc}"
            ) from exc

    def set_active(self, project_id: str, version: str) -> PlanRevision:
        """Point a project at an existing, hash-verified revision."""
        revision = self.get_revision(project_id, version)
        self._write_text(self._project_dir(project_id) / ACTIVE_FILENAME, version + "\n")
        return revision

    def load_active(self, project_id: str) -> PlanRevision:
        """Load the active revision, failing when none is set."""
        version = self.active_version(project_id)
        if version is None:
            raise PlanNotFoundError(f"project {project_id!r} has no active plan revision")
        return self.get_revision(project_id, version)


def plan_revisions_to_snapshot(
    revisions: Mapping[str, PlanRevision] | Iterable[PlanRevision],
    *,
    owner_id: str,
) -> FrontierSnapshot:
    """Adapt stored revisions to the authoritative snapshot shape.

    Every stored task becomes a ``PENDING`` child task row; dependency
    entries keep their ``(project_id, task_id)`` meaning (same-project IDs
    resolve against the revision's own project). The result feeds
    :func:`saberops.ready_frontier.project_ready_frontier` unchanged, so all
    readiness semantics stay in that single authority.
    """
    items = revisions.values() if isinstance(revisions, Mapping) else revisions
    projects: list[ProjectPlan] = []
    for revision in items:
        rows: list[PlanTask] = []
        for task in revision.tasks:
            rows.append(
                PlanTask(
                    task_id=task.task_id,
                    phase=task.phase,
                    status=TaskStatus.PENDING.value,
                    kind=TaskKind.CHILD_TASK.value,
                    title=task.title,
                    start_dependencies=tuple(
                        _ref(revision.project_id, edge) for edge in task.start_dependencies
                    ),
                    integration_prerequisites=tuple(
                        _ref(revision.project_id, edge) for edge in task.integration_prerequisites
                    ),
                    owns=tuple(task.owns),
                )
            )
        projects.append(ProjectPlan(project_id=revision.project_id, tasks=tuple(rows)))
    projects.sort(key=lambda project: project.project_id)
    return FrontierSnapshot(
        owner_id=owner_id,
        source=SnapshotSource.OWNER_DB.value,
        complete=True,
        projects=tuple(projects),
    )


def _ref(home_project: str, edge: str) -> TaskRef:
    """Resolve one stored dependency entry against its home project."""
    project_part, _, task_part = edge.partition("/")
    if task_part and project_part and "/" not in task_part:
        return TaskRef(project_id=project_part, task_id=task_part)
    return TaskRef(project_id=home_project, task_id=edge)
