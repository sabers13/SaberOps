"""Project-local runtime state resolution.

Every correctness-critical runtime store (run database, transcripts,
supervision/ownership records, per-run coordination locks) lives inside one
project-local directory::

    <state_home>/saberops/                        # canonical public root
        orchestrator.db                            # global store + owner-wide quota
        projects/<slug>-<project_id>/
            project.json                           # marker: which repository this is
            orchestrator.db                        # this project's runs
            transcripts/                           # this project's PTY transcripts

Owner-wide configuration is deliberately *not* duplicated per project: routing
policy and provider authentication already live in the owner's config, and
provider quota is delegated back to the single global database.

This is intentionally not a tenancy framework.  It is a cache of one
``ProjectRuntime`` per repository plus one lookup that answers "which project
does this run belong to?" from the run ID itself.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from saberops.db import Database, get_default_db_path
from saberops.project import (
    ProjectIdentity,
    ProjectIdentityError,
    ProjectStateUnavailableError,
    bind_project_state_dir,
    compute_project_identity,
    find_project_state_dir,
    project_id_from_run_id,
    projects_state_root,
    read_project_marker,
)
from saberops.service import OrchestratorService
from saberops.supervisor import RunSupervisor
from saberops.workers.registry import AdapterRegistry


@dataclass(frozen=True)
class InboxStore:
    """One genuinely read-only Inbox enumeration source.

    A narrow ``identity`` + ``db`` pair only: deliberately *not* a full
    :class:`ProjectRuntime`, so merely inspecting stored runs can never
    construct (and thereby create/migrate) project databases, owner
    databases, supervisors, or services.  The database is always opened
    with ``read_only=True`` and only when its file already exists.
    ``identity`` is ``None`` only for the preserved global store.
    """

    identity: ProjectIdentity | None
    db: Database


@dataclass(frozen=True)
class ProjectRuntime:
    """The isolated runtime state of exactly one repository.

    ``identity`` is ``None`` only for the preserved global store, which holds
    runs created before project isolation existed.
    """

    identity: ProjectIdentity | None
    db: Database
    service: OrchestratorService
    supervisor: RunSupervisor

    @property
    def project_id(self) -> str | None:
        """Return the owning project ID, or ``None`` for the legacy store."""
        return self.identity.project_id if self.identity is not None else None


class ProjectRuntimeManager:
    """Resolves and caches one :class:`ProjectRuntime` per repository.

    When ``pinned_db_path`` is supplied every project resolves to that single
    database.  That is the explicit ``--db``/``db_path`` contract: the caller
    asked for one specific store and gets exactly it, with no implicit
    project-local redirection.
    """

    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        worktree_base: Path | None = None,
        state_root: Path | str | None = None,
        pinned_db_path: Path | str | None = None,
        owner_db_path: Path | str | None = None,
    ) -> None:
        self.registry = registry if registry is not None else AdapterRegistry.default()
        self.worktree_base = Path(worktree_base) if worktree_base is not None else None
        self.pinned_db_path = Path(pinned_db_path).resolve() if pinned_db_path else None
        self._state_root = Path(state_root) if state_root is not None else None
        self._owner_db_path = Path(owner_db_path) if owner_db_path is not None else None
        self._lock = threading.Lock()
        self._runtimes: dict[str, ProjectRuntime] = {}
        self._owner_db: Database | None = None

    @property
    def state_root(self) -> Path:
        """Return the directory holding every project-local state directory."""
        return self._state_root if self._state_root is not None else projects_state_root()

    @property
    def owner_db(self) -> Database:
        """Return the global store holding owner-wide config and legacy runs."""
        with self._lock:
            if self._owner_db is None:
                path = self.pinned_db_path or self._owner_db_path or get_default_db_path()
                self._owner_db = Database(path)
            return self._owner_db

    def _worktree_base_for(self, identity: ProjectIdentity | None) -> Path | None:
        if self.worktree_base is None:
            return None
        if identity is None:
            return self.worktree_base
        # A single configured worktree root is still namespaced per project so
        # two same-named repositories can never share worktree paths.
        return self.worktree_base / identity.namespace

    def _build(self, identity: ProjectIdentity | None, db_path: Path) -> ProjectRuntime:
        owner = self.owner_db
        db = Database(db_path, owner_db=owner)
        worktree_base = self._worktree_base_for(identity)
        service = OrchestratorService(db=db, registry=self.registry, worktree_base=worktree_base)
        supervisor = RunSupervisor(db=db, worktree_base=worktree_base, registry=self.registry)
        return ProjectRuntime(identity=identity, db=db, service=service, supervisor=supervisor)

    def _cached(self, key: str, identity: ProjectIdentity | None, db_path: Path) -> ProjectRuntime:
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
        runtime = self._build(identity, db_path)
        with self._lock:
            # Another thread may have built the same runtime concurrently; keep
            # exactly one supervisor per project so process ownership is unique.
            return self._runtimes.setdefault(key, runtime)

    def legacy(self) -> ProjectRuntime:
        """Return the runtime for the preserved global store."""
        return self._cached("__legacy__", None, self.owner_db.db_path)

    def for_identity(self, identity: ProjectIdentity) -> ProjectRuntime:
        """Return (creating if needed) the runtime for an identified project.

        The state directory's identity binding is validated on every
        resolution, so a repository that has been replaced at the same
        canonical path fails closed instead of inheriting the previous
        repository's database.
        """
        if self.pinned_db_path is not None:
            # Explicit single-store contract: one database, one supervisor.
            return self.legacy()
        state_dir = bind_project_state_dir(identity, self.state_root)
        return self._cached(identity.project_id, identity, state_dir / "orchestrator.db")

    def for_repo(self, repo_path: Path | str) -> ProjectRuntime:
        """Return the runtime for the repository at ``repo_path``."""
        return self.for_identity(compute_project_identity(repo_path))

    def for_run(self, run_id: str) -> ProjectRuntime | None:
        """Resolve a run back to its own project runtime.

        Resolution uses the run ID's embedded project segment and the state
        directory's verified identity binding.  It never consults the
        active-project selection, so changing the selected project cannot
        retarget an existing run.

        A run ID that carries a project segment resolves to that project or to
        nothing: it is never downgraded to a lookup in the preserved global
        store, even if a row with the same ID happens to exist there.  Only
        genuinely legacy IDs (no project segment) use the global store.
        """
        if self.pinned_db_path is not None:
            runtime = self.legacy()
            return runtime if runtime.db.get_run(run_id) is not None else None
        project_id = project_id_from_run_id(run_id)
        if project_id is not None:
            state_dir = find_project_state_dir(project_id, self.state_root)
            if state_dir is None:
                return None
            identity = read_project_marker(state_dir)
            if identity is None:
                return None
            runtime = self._cached(identity.project_id, identity, state_dir / "orchestrator.db")
            return runtime if runtime.db.get_run(run_id) is not None else None
        legacy = self.legacy()
        return legacy if legacy.db.get_run(run_id) is not None else None

    def for_active(self, repo_path: Path | str | None) -> ProjectRuntime:
        """Return the runtime for the UI's currently selected project.

        This is a navigation convenience only: it decides what the dashboard
        *shows*, never what an existing run acts on.  When the selection cannot
        be resolved to a usable project the dashboard falls back to the
        preserved global store rather than erroring; every *action* goes
        through :meth:`for_repo` or :meth:`for_run`, which fail closed.
        """
        if repo_path is None:
            return self.legacy()
        try:
            return self.for_repo(repo_path)
        except ProjectIdentityError:
            return self.legacy()

    def _owner_db_resolved_path(self) -> Path:
        """Return the global-store path without creating anything."""
        if self.pinned_db_path is not None:
            return self.pinned_db_path
        if self._owner_db_path is not None:
            return Path(self._owner_db_path)
        return get_default_db_path()

    def list_inbox_stores(self) -> tuple[list[InboxStore], int]:
        """Enumerate read-only Inbox sources without any write side effect.

        Genuinely read-only cross-project discovery for the owner Inbox:
        every subdirectory of :attr:`state_root` holding a readable
        ``project.json`` marker *and* an already-existing usable
        ``orchestrator.db`` contributes one :class:`InboxStore` opened
        with ``read_only=True``.  The preserved global store is appended
        last, but only when its file already exists.

        A persisted marker with no existing usable database is skipped
        and counted as unreadable -- it never causes an empty database,
        schema, migration, directory, marker, supervisor, or service to
        be created.  Likewise an absent legacy/global database is simply
        skipped, never created.  A corrupt/unreadable store fails
        locally (counted in ``unreadable``) rather than breaking the
        whole Inbox; the failure surfaces when the store is read, so
        ``collect_project_inbox`` must let read failures propagate.

        In explicit ``pinned_db_path`` mode the answer is at most the
        one pinned store, read once when its file already exists (never
        created, never duplicated).
        """
        pinned = self.pinned_db_path
        if pinned is not None:
            if not pinned.is_file():
                return ([], 0)
            try:
                store = InboxStore(
                    identity=None,
                    db=Database(pinned, read_only=True),
                )
            except Exception:
                return ([], 1)
            return ([store], 0)
        stores: list[InboxStore] = []
        unreadable = 0
        try:
            entries = sorted(self.state_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                identity = read_project_marker(entry)
            except Exception:
                identity = None
            if identity is None:
                unreadable += 1
                continue
            db_path = entry / "orchestrator.db"
            if not db_path.is_file():
                # Persisted marker, no usable database: skip without
                # creating an empty store merely to inspect it.
                unreadable += 1
                continue
            try:
                stores.append(
                    InboxStore(
                        identity=identity,
                        db=Database(db_path, read_only=True),
                    )
                )
            except Exception:
                unreadable += 1
        legacy_path = self._owner_db_resolved_path()
        if legacy_path.is_file():
            try:
                stores.append(
                    InboxStore(
                        identity=None, db=Database(legacy_path, read_only=True)
                    )
                )
            except Exception:
                unreadable += 1
        # An absent legacy/global database is normal (never created by a
        # read); it contributes no source and no unreadable count.
        return (stores, unreadable)

    def list_known_runtimes(self) -> tuple[list[ProjectRuntime], int]:
        """Enumerate one runtime per persisted project state, plus legacy last.

        Read-only cross-project discovery for the owner Inbox: every
        subdirectory of :attr:`state_root` holding a readable
        ``project.json`` marker contributes its project runtime, resolved
        from the marker's own identity (never from the directory name).
        The preserved global store is appended last so genuine legacy runs
        are still visible.

        Returns ``(runtimes, unreadable)`` where ``unreadable`` counts
        state directories that could not be read (missing/corrupt marker
        or unusable database).  Unreadable directories are skipped
        locally; they never break -- and their rows are never attributed
        to -- other projects.

        Never creates or binds state directories, never changes the
        active project, and never mutates the project registry.  In
        explicit ``pinned_db_path`` mode the answer is exactly the one
        pinned store with zero unreadable directories (no duplicate
        scanning).
        """
        if self.pinned_db_path is not None:
            return ([self.legacy()], 0)
        runtimes: list[ProjectRuntime] = []
        unreadable = 0
        try:
            entries = sorted(self.state_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                identity = read_project_marker(entry)
            except Exception:
                identity = None
            if identity is None:
                unreadable += 1
                continue
            try:
                runtimes.append(
                    self._cached(
                        identity.project_id, identity, entry / "orchestrator.db"
                    )
                )
            except Exception:
                unreadable += 1
        try:
            runtimes.append(self.legacy())
        except Exception:
            unreadable += 1
        return (runtimes, unreadable)

    def shutdown(self) -> None:
        """Detach every cached supervisor without cancelling durable workers."""
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.supervisor.shutdown()


def project_db_path_for_repo(repo_path: Path | str, state_root: Path | str | None = None) -> Path:
    """Return the project-local database for a repository, binding it if new."""
    identity = compute_project_identity(repo_path)
    root = Path(state_root) if state_root is not None else projects_state_root()
    return bind_project_state_dir(identity, root) / "orchestrator.db"


def db_path_for_run(run_id: str, state_root: Path | str | None = None) -> Path | None:
    """Return the project-local database owning ``run_id``.

    ``None`` means the run ID carries no project segment, i.e. it is a genuine
    legacy ID and the caller should use the preserved global store.  A
    project-scoped ID whose state cannot be resolved raises
    :class:`ProjectStateUnavailableError` instead: it must never be treated as
    a legacy run.
    """
    project_id = project_id_from_run_id(run_id)
    if project_id is None:
        return None
    root = Path(state_root) if state_root is not None else projects_state_root()
    state_dir = find_project_state_dir(project_id, root)
    if state_dir is None:
        raise ProjectStateUnavailableError(
            f"Run '{run_id}' belongs to project '{project_id}', whose runtime state "
            f"is not available under {root}"
        )
    return state_dir / "orchestrator.db"
