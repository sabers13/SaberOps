"""Canonical project (repository) identity and project-local runtime state layout.

One Orch installation is routinely pointed at several unrelated local
repositories at the same time.  Correctness-critical runtime state (runs,
transcripts, supervision/ownership records, worktrees) must therefore never be
shared between them, and any action on an *existing* run has to be resolved
from that run's persisted project identity rather than from whatever project
the UI currently happens to have selected.

The identity model is deliberately small:

``canonical_path``
    ``realpath`` of the repository's main working tree.  This is the primary
    discriminator: two clones of the same remote, and two repositories that
    merely share a directory basename, always live at different paths.
``project_id``
    ``sha256(canonical_path)`` truncated to 12 hex characters.  Stable for the
    lifetime of a checkout and short enough to embed in run IDs, namespaces,
    and worktree paths.
``root_commit``
    Persisted repository-history evidence: the lexicographically smallest root
    commit reachable from HEAD when the project was first seen.  It does not
    participate in the ID, but it is what makes a path-derived ID safe -- an
    unrelated repository created at the same canonical path has different
    history and is rejected instead of inheriting the previous repository's
    state.  Root commits are append-only in practice, so the recorded root
    only has to still be *present*, which stays stable when unrelated history
    is later merged in.
``remote_identity``
    Diagnostic/corroborating metadata only.  Adding, removing, or renaming a
    remote must not create a second project and must not invalidate an
    otherwise provably continuous checkout, so remote equality is deliberately
    *not* enforced anywhere.

The binding between a repository and its state directory is recorded once, in
``project.json``, and is immutable: see :func:`bind_project_state_dir`.

Owner-wide configuration (routing policy, provider authentication, quota) is
intentionally *not* project-scoped and keeps living in the global state root.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.process import run_process

PROJECT_ID_LENGTH = 12
OBJECTIVE_ID_LENGTH = 16
_STATE_APP_DIR = "orchestrator-mvp"
_PROJECTS_DIR = "projects"
_MARKER_NAME = "project.json"
_HEX = frozenset("0123456789abcdef")


class ProjectIdentityError(RuntimeError):
    """Raised when a repository cannot be identified or no longer matches."""


class ProjectStateConflictError(ProjectIdentityError):
    """Raised when a state directory is bound to an incompatible repository.

    The binding in ``project.json`` is never overwritten to resolve this: the
    previous repository's state is left untouched for inspection.
    """


class ProjectStateUnavailableError(ProjectIdentityError):
    """Raised when a project-scoped run's state directory cannot be resolved.

    This is never downgraded to a legacy-store lookup: a run ID that carries a
    project segment belongs to that project or to nothing.
    """


@dataclass(frozen=True)
class ProjectIdentity:
    """Stable identity of one local repository checkout."""

    project_id: str
    canonical_path: str
    remote_identity: str
    root_commit: str

    @property
    def namespace(self) -> str:
        """Filesystem-safe, human-recognizable per-project namespace segment."""
        return f"{_slugify(Path(self.canonical_path).name)}-{self.project_id}"

    def as_dict(self) -> dict[str, str]:
        """Serialize for durable storage next to a run."""
        return {
            "project_id": self.project_id,
            "canonical_path": self.canonical_path,
            "remote_identity": self.remote_identity,
            "root_commit": self.root_commit,
        }

    def to_json(self) -> str:
        """Serialize as compact deterministic JSON."""
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectIdentity:
        """Reconstruct from a persisted mapping, tolerating missing extras."""
        project_id = str(data.get("project_id", "")).strip()
        canonical_path = str(data.get("canonical_path", "")).strip()
        if not project_id or not canonical_path:
            raise ProjectIdentityError("persisted project identity is incomplete")
        return cls(
            project_id=project_id,
            canonical_path=canonical_path,
            remote_identity=str(data.get("remote_identity", "") or ""),
            root_commit=str(data.get("root_commit", "") or ""),
        )

    @classmethod
    def from_json(cls, raw: str | None) -> ProjectIdentity | None:
        """Reconstruct from persisted JSON; ``None`` for absent/unreadable data."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls.from_dict(data)
        except ProjectIdentityError:
            return None


def _slugify(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in name.lower())
    cleaned = cleaned.strip("-.") or "repo"
    return cleaned[:40]


def _git_text(args: list[str], cwd: Path) -> str:
    res = run_process(["git", *args], cwd=cwd)
    return res.stdout.strip() if res.passed else ""


def normalize_remote_identity(url: str) -> str:
    """Normalize a Git remote URL into a comparable host/path identity.

    ``git@github.com:Owner/Repo.git``, ``https://github.com/owner/repo`` and
    ``ssh://git@github.com/owner/repo.git`` all normalize to
    ``github.com/owner/repo``.  Local filesystem remotes normalize to their
    resolved path.  Unparseable values are returned lowercased and stripped so
    comparison stays deterministic rather than silently permissive.
    """
    raw = url.strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for scheme in ("ssh://", "git+ssh://", "https://", "http://", "git://", "ftp://", "ftps://"):
        if lowered.startswith(scheme):
            lowered = lowered[len(scheme) :]
            break
    else:
        if lowered.startswith("file://"):
            lowered = lowered[len("file://") :]
        elif "://" not in lowered and "@" in lowered and ":" in lowered:
            # scp-like syntax: user@host:path
            host_part, _, path_part = lowered.partition(":")
            lowered = f"{host_part}/{path_part}"
    if "@" in lowered and "/" in lowered and lowered.index("@") < lowered.index("/"):
        lowered = lowered.split("@", 1)[1]
    lowered = lowered.rstrip("/")
    if lowered.endswith(".git"):
        lowered = lowered[: -len(".git")]
    return lowered


def _remote_identity(repo_root: Path) -> str:
    url = _git_text(["config", "--get", "remote.origin.url"], repo_root)
    if not url:
        listed = _git_text(["remote"], repo_root)
        names = sorted(n.strip() for n in listed.splitlines() if n.strip())
        if names:
            url = _git_text(["config", "--get", f"remote.{names[0]}.url"], repo_root)
    return normalize_remote_identity(url)


def _root_commit(repo_root: Path) -> str:
    """Return the lexicographically smallest root commit reachable from HEAD.

    Root commits are append-only in practice, so requiring the recorded root to
    still be *present* stays stable when unrelated history is later merged in,
    while a wholly different repository at the same path is detected.
    """
    listed = _git_text(["rev-list", "--max-parents=0", "HEAD"], repo_root)
    roots = sorted(line.strip() for line in listed.splitlines() if line.strip())
    return roots[0] if roots else ""


def _root_commits(repo_root: Path) -> frozenset[str]:
    listed = _git_text(["rev-list", "--max-parents=0", "HEAD"], repo_root)
    return frozenset(line.strip() for line in listed.splitlines() if line.strip())


def canonical_repo_root(repo_path: Path | str) -> Path | None:
    """Return the realpath of the main working tree for ``repo_path``.

    Linked worktrees resolve back to the repository they belong to, so a run
    launched from a worktree is never treated as a separate project.
    """
    path = Path(repo_path).expanduser()
    if not path.exists() or not path.is_dir():
        return None
    common = _git_text(["rev-parse", "--git-common-dir"], path)
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = path / common_path
    common_path = Path(os.path.realpath(common_path))
    root = common_path.parent if common_path.name == ".git" else common_path
    return Path(os.path.realpath(root))


def compute_project_identity(repo_path: Path | str) -> ProjectIdentity:
    """Derive the canonical identity of the repository at ``repo_path``."""
    root = canonical_repo_root(repo_path)
    if root is None:
        raise ProjectIdentityError(f"Target path is not a valid Git repository: {Path(repo_path)}")
    canonical = str(root)
    return ProjectIdentity(
        project_id=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:PROJECT_ID_LENGTH],
        canonical_path=canonical,
        remote_identity=_remote_identity(root),
        root_commit=_root_commit(root),
    )


def validate_project_identity(
    stored: ProjectIdentity, repo_path: Path | str | None = None
) -> ProjectIdentity:
    """Fail closed unless the repository still matches ``stored``.

    Basename, branch label, remote name, and the UI's active-project selection
    are deliberately never consulted.  The check is canonical path plus
    repository history only.
    """
    target = Path(repo_path) if repo_path is not None else Path(stored.canonical_path)
    root = canonical_repo_root(target)
    if root is None:
        raise ProjectIdentityError(
            f"Project '{stored.project_id}' is no longer a Git repository at {target}"
        )
    current = compute_project_identity(root)
    if current.project_id != stored.project_id:
        raise ProjectIdentityError(
            f"Repository at {root} is project '{current.project_id}', "
            f"but the run was created for project '{stored.project_id}' "
            f"at {stored.canonical_path}"
        )
    if stored.root_commit and stored.root_commit not in _root_commits(root):
        raise ProjectIdentityError(
            f"Repository at {root} no longer contains the recorded root commit "
            f"{stored.root_commit[:8]} of project '{stored.project_id}'"
        )
    # ``remote_identity`` is deliberately not compared: adding, removing, or
    # renaming a remote is a legitimate operation on a continuous checkout and
    # must not invalidate its runs.  Continuity is proven by canonical path
    # plus repository history.
    return current


def state_home() -> Path:
    """Return the XDG state home used for all orchestrator runtime state."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home and xdg_state_home.strip():
        return Path(xdg_state_home).expanduser()
    return Path.home() / ".local" / "state"


def owner_state_dir() -> Path:
    """Return the global owner-wide state directory (also the legacy root)."""
    return state_home() / _STATE_APP_DIR


def projects_state_root() -> Path:
    """Return the parent directory holding every project-local state dir."""
    return owner_state_dir() / _PROJECTS_DIR


def project_state_dir(identity: ProjectIdentity, root: Path | str | None = None) -> Path:
    """Return the project-local runtime state directory for ``identity``."""
    base = Path(root) if root is not None else projects_state_root()
    return base / identity.namespace


def project_db_path(identity: ProjectIdentity, root: Path | str | None = None) -> Path:
    """Return the project-local run database path."""
    return project_state_dir(identity, root) / "orchestrator.db"


def bind_project_state_dir(identity: ProjectIdentity, root: Path | str | None = None) -> Path:
    """Return the state directory bound to ``identity``, creating it if absent.

    ``project.json`` is an identity *binding*, not a mutable cache.  It is
    written exactly once, atomically, when a project's state directory is first
    created, and is never rewritten afterwards.  If a binding already exists it
    is validated before the directory may be used.

    This is what keeps a path-derived project ID safe.  If repository A is
    removed and an unrelated repository B is created at the same canonical
    path, B derives the same project ID but has different history, so the
    existing binding fails to validate and :class:`ProjectStateConflictError`
    is raised.  A's database, transcripts, and marker are left exactly as they
    were: nothing is deleted, migrated, renamed, or overwritten automatically.

    The one deliberately weak case is a project first bound while its
    repository had no commits at all: there is no history to record, so the
    binding carries no evidence.  Such a repository cannot start a run anyway
    (a run requires a HEAD to record as its base commit).
    """
    state_dir = project_state_dir(identity, root)
    marker = state_dir / _MARKER_NAME
    if marker.exists():
        _assert_binding(marker, identity)
        return state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # Another process bound this directory first; its binding decides.
        _assert_binding(marker, identity)
        return state_dir
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(identity.to_json())
    return state_dir


def _assert_binding(marker: Path, identity: ProjectIdentity) -> None:
    """Fail closed unless an existing marker still binds this repository."""
    stored = ProjectIdentity.from_json(_read_text(marker))
    if stored is None:
        raise ProjectStateConflictError(
            f"Project state directory {marker.parent} has an unreadable identity "
            f"binding ({marker}); refusing to reuse it for {identity.canonical_path}"
        )
    if stored.project_id != identity.project_id or (
        stored.canonical_path != identity.canonical_path
    ):
        raise ProjectStateConflictError(
            f"Project state directory {marker.parent} is bound to project "
            f"'{stored.project_id}' at {stored.canonical_path}, not to project "
            f"'{identity.project_id}' at {identity.canonical_path}"
        )
    try:
        validate_project_identity(stored, identity.canonical_path)
    except ProjectIdentityError as exc:
        raise ProjectStateConflictError(
            f"Project state directory {marker.parent} is bound to a different "
            f"repository than the one now at {identity.canonical_path}: {exc}. "
            "The existing state is left untouched; move or remove it explicitly "
            "if that repository is genuinely gone."
        ) from exc


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_project_marker(state_dir: Path | str) -> ProjectIdentity | None:
    """Read a persisted project marker, or ``None`` when absent/unreadable."""
    return ProjectIdentity.from_json(_read_text(Path(state_dir) / _MARKER_NAME))


def find_project_state_dir(project_id: str, root: Path | str | None = None) -> Path | None:
    """Locate the state directory whose *marker* claims ``project_id``.

    The directory name is never trusted on its own: a directory is only
    accepted when it holds a readable binding naming exactly this project.  A
    missing, corrupt, or mismatched marker makes the directory unresolvable
    rather than usable.
    """
    base = Path(root) if root is not None else projects_state_root()
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        marker = read_project_marker(entry)
        if marker is not None and marker.project_id == project_id:
            return entry
    return None


def new_run_id(identity: ProjectIdentity | None = None) -> str:
    """Mint a run ID that carries its own project identity.

    Legacy IDs (``run_<10 hex>``) remain valid and resolve to the preserved
    global store; new IDs are ``run_<project_id>_<10 hex>`` so that any later
    action can find the owning project without consulting the UI selection.
    """
    suffix = uuid.uuid4().hex[:10]
    if identity is None:
        return f"run_{suffix}"
    return f"run_{identity.project_id}_{suffix}"


def project_id_from_run_id(run_id: str) -> str | None:
    """Extract the embedded project ID, or ``None`` for legacy run IDs."""
    parts = run_id.split("_")
    if len(parts) != 3 or parts[0] != "run":
        return None
    candidate = parts[1]
    if len(candidate) != PROJECT_ID_LENGTH or not set(candidate) <= _HEX:
        return None
    return candidate


def objective_id_for(project_id: str, task: str) -> str:
    """Return the immutable identity of an objective within one project.

    Derived rather than random so a retry of the same objective keeps its
    identity, while a *different* objective -- even in the same project -- can
    never collide with it.
    """
    digest = hashlib.sha256(f"{project_id}\n{task.strip()}".encode()).hexdigest()
    return f"obj_{digest[:OBJECTIVE_ID_LENGTH]}"
