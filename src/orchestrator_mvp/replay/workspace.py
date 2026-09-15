"""Disposable workspace manager enforcing replay isolation and safety invariants.

Enforces Section 5 Safety Invariants:
A. Never run replay mutation directly in authoritative project checkout.
B. Every mutable replay execution uses a disposable isolated branch/worktree.
C. Starting Git SHA is exact and verified before execution.
D. Project identity is verified before replay.
E. Replay cleanup cannot mutate/delete authoritative worktrees or branches.
H. No replay arm may silently inherit mutable state from another arm.
I. Paired arms each start independently from the exact same frozen starting state.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from orchestrator_mvp.git import GitManager
from orchestrator_mvp.process import run_process
from orchestrator_mvp.project import (
    ProjectIdentity,
    compute_project_identity,
)
from orchestrator_mvp.replay.contracts import (
    FrozenStartingState,
    ReplayProjectMismatchError,
    ReplaySafetyViolationError,
    ReplayStateMismatchError,
)


def get_replay_worktree_base(repo_path: Path | str) -> Path:
    """Compute the safe, dedicated parent directory for all replay worktrees."""
    resolved = Path(repo_path).resolve()
    try:
        namespace = compute_project_identity(resolved).namespace
    except Exception:
        namespace = resolved.name
    return resolved.parent / ".orch-worktrees" / namespace / "replay"


@dataclass
class DisposableWorkspace:
    """An isolated, disposable Git worktree created for one replay execution."""

    _repo_path: Path
    execution_id: str
    starting_sha: str
    worktree_path: Path
    branch_name: str
    project_identity: ProjectIdentity
    replay_base: Path
    _is_active: bool = True

    @property
    def is_active(self) -> bool:
        """True if the disposable workspace exists and has not been cleaned up."""
        return self._is_active

    @classmethod
    def create(
        cls,
        repo_path: Path | str,
        starting_state: FrozenStartingState,
        execution_id: str,
        base_dir: Path | str | None = None,
    ) -> DisposableWorkspace:
        """Create a new disposable worktree checked out to a disposable branch."""
        repo = Path(repo_path).resolve()

        # Invariant D: Project identity is verified before replay
        ident = compute_project_identity(repo)
        if starting_state.project_id and ident.project_id != starting_state.project_id:
            raise ReplaySafetyViolationError(
                f"Repository project_id '{ident.project_id}' does not match "
                f"frozen starting state project_id '{starting_state.project_id}'."
            )

        # Invariant D2: Root commit check (refuses same-path replacement with different history)
        if starting_state.root_commit and ident.root_commit != starting_state.root_commit:
            raise ReplayProjectMismatchError(
                f"Repository root commit '{ident.root_commit}' does not match "
                f"frozen starting state root commit '{starting_state.root_commit}'."
            )

        # Invariant C: Starting Git SHA is exact and verified
        target_commit = GitManager.get_ref_commit(repo, starting_state.starting_git_sha)
        if not target_commit:
            raise ReplayStateMismatchError(
                f"Starting commit '{starting_state.starting_git_sha}' does not exist in {repo}."
            )

        # Determine and validate workspace base locations
        if base_dir is not None:
            replay_base = Path(base_dir).resolve()
            if replay_base == repo or replay_base.is_relative_to(repo):
                raise ReplaySafetyViolationError(
                    "Refusing custom workspace base inside authoritative repository!"
                )
        else:
            replay_base = get_replay_worktree_base(repo).resolve()

        replay_base.mkdir(parents=True, exist_ok=True)

        branch_name = f"replay/{execution_id}"
        worktree_path = (replay_base / execution_id).resolve()

        # Invariant A: Never run replay mutation directly in authoritative project checkout
        if worktree_path == repo or worktree_path.is_relative_to(repo):
            raise ReplaySafetyViolationError(
                "Refusing to create disposable workspace directly on authoritative repository!"
            )

        if worktree_path.exists():
            raise FileExistsError(f"Disposable worktree path already exists: {worktree_path}")

        # Invariant B: Create isolated worktree from the exact frozen start SHA
        created_path = GitManager.create_worktree(
            repo_path=repo,
            branch_name=branch_name,
            worktree_path=worktree_path,
            start_point=target_commit,
        )

        # Verify created worktree HEAD matches starting commit
        wt_head = GitManager.get_head_commit(created_path)
        if wt_head != target_commit:
            # Emergency cleanup
            try:
                GitManager.remove_worktree(
                    repo, created_path, force=True, worktree_base=replay_base
                )
            except Exception:
                pass
            raise ReplayStateMismatchError(
                f"Created worktree HEAD '{wt_head}' does not match target commit '{target_commit}'."
            )

        return cls(
            _repo_path=repo,
            execution_id=execution_id,
            starting_sha=target_commit,
            worktree_path=created_path,
            branch_name=branch_name,
            project_identity=ident,
            replay_base=replay_base,
            _is_active=True,
        )

    @property
    def quarantine_path(self) -> Path:
        """Dedicated quarantine location outside the workspace sandbox view."""
        return self.replay_base / ".quarantine" / f"{self.execution_id}.git_pointer"

    def reconcile_stale_quarantine(self) -> bool:
        """Reconcile stale quarantine state deterministically if left by prior interruption.

        Returns True if stale state was recovered, False if nothing to reconcile.
        Raises ReplaySafetyViolationError if both pointer and quarantine exist ambiguously.
        """
        git_ptr = self.worktree_path / ".git"
        quarantine = self.quarantine_path

        if quarantine.exists() and git_ptr.exists() and git_ptr.is_file():
            raise ReplaySafetyViolationError(
                f"Ambiguous quarantine state: both git pointer '{git_ptr}' and "
                f"quarantine file '{quarantine}' exist. Failing closed."
            )

        if quarantine.exists():
            if git_ptr.exists() and git_ptr.is_dir():
                try:
                    git_ptr.rmdir()
                except OSError as exc:
                    raise ReplaySafetyViolationError(
                        f"Failed to reconcile stale quarantine: {git_ptr} "
                        f"is non-empty directory: {exc}"
                    ) from exc
            os.replace(str(quarantine), str(git_ptr))
            return True
        return False

    def quarantine_git_pointer(self) -> None:
        """Atomically quarantine linked worktree .git pointer and present empty .git location.

        Enforces:
        - validate <worktree>/.git is a regular, non-symlink linked-worktree pointer;
        - validate its gitdir target belongs to the expected authoritative repo;
        - atomically quarantine only the pointer file outside sandbox visibility;
        - present mask-compatible empty .git location to the child;
        - reconcile deterministic stale quarantine state or fail closed on ambiguity;
        - never move or mutate authoritative/common Git metadata.
        """
        self.reconcile_stale_quarantine()

        git_ptr = self.worktree_path / ".git"
        if git_ptr.is_symlink():
            raise ReplaySafetyViolationError(
                f"Refusing symlinked .git pointer at '{git_ptr}': "
                "must be a regular linked-worktree pointer."
            )
        if not git_ptr.is_file():
            raise ReplaySafetyViolationError(
                f"Expected regular linked-worktree .git pointer file at '{git_ptr}'."
            )

        # Validate gitdir target belongs to expected authoritative repo
        content = git_ptr.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            raise ReplaySafetyViolationError(
                f"Invalid linked-worktree .git pointer content at '{git_ptr}': "
                "missing 'gitdir:' prefix."
            )
        raw_target = content.split("gitdir:", 1)[1].strip()
        target_path = Path(raw_target)
        if not target_path.is_absolute():
            target_path = (self.worktree_path / target_path).resolve()
        else:
            target_path = target_path.resolve()

        repo_git_dir = (self._repo_path / ".git").resolve()
        if not (target_path == repo_git_dir or target_path.is_relative_to(repo_git_dir)):
            raise ReplaySafetyViolationError(
                f"Linked worktree gitdir target '{target_path}' does not belong to "
                f"authoritative repository '{self._repo_path}'."
            )

        # Atomically quarantine only the pointer file
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(git_ptr), str(self.quarantine_path))

        # Present mask-compatible empty .git location to the child
        git_ptr.mkdir()

        # Present mask-compatible empty .github location if not present
        gh_dir = self.worktree_path / ".github"
        if not gh_dir.exists():
            gh_dir.mkdir()
            self._synthesized_github_dir = True
        else:
            self._synthesized_github_dir = False

    def restore_git_pointer(self) -> None:
        """Atomically restore quarantined .git pointer from replay-owned quarantine."""
        if getattr(self, "_synthesized_github_dir", False):
            gh_dir = self.worktree_path / ".github"
            if gh_dir.exists() and gh_dir.is_dir():
                try:
                    gh_dir.rmdir()
                except OSError:
                    pass
            self._synthesized_github_dir = False

        git_ptr = self.worktree_path / ".git"
        if git_ptr.exists() and git_ptr.is_dir():
            try:
                git_ptr.rmdir()
            except OSError as exc:
                raise ReplaySafetyViolationError(
                    f"Cannot restore .git pointer: {git_ptr} is non-empty directory: {exc}"
                ) from exc

        if self.quarantine_path.exists():
            os.replace(str(self.quarantine_path), str(git_ptr))
            # Deliberately do NOT rmdir the shared quarantine directory:
            # it is shared by every concurrent DisposableWorkspace under the
            # same replay base, and removing it here raced with another
            # workspace's in-flight quarantine_git_pointer() os.replace(),
            # making that rename fail with FileNotFoundError (C12-D
            # deterministic-parallel arm surfaced this latent shared-state
            # race).  The directory lives in the disposable replay base,
            # outside all workspaces and outside authoritative Project truth.

    @contextmanager
    def quarantined_git(self) -> Iterator[None]:
        """Context manager guaranteeing .git pointer quarantine and cleanup."""
        self.quarantine_git_pointer()
        try:
            yield
        finally:
            self.restore_git_pointer()

    def cleanup(self, force: bool = True) -> None:
        """Safely discard disposable worktree and branch.

        Enforces Invariant E: Replay cleanup cannot delete/mutate authoritative repo.
        """
        if not self._is_active:
            return

        # Restore git pointer if left in quarantine before removing worktree
        if self.quarantine_path.exists():
            try:
                self.restore_git_pointer()
            except Exception:
                pass

        replay_base = self.replay_base.resolve()
        wt = self.worktree_path.resolve()
        repo = self._repo_path.resolve()

        # Invariant E: Refuse to remove paths outside replay worktree base
        try:
            is_subpath = wt.is_relative_to(replay_base) and wt != replay_base
        except (ValueError, AttributeError):
            is_subpath = False

        if not is_subpath:
            raise ReplaySafetyViolationError(
                f"Refusing to delete unsafe worktree path outside replay base: {wt}"
            )

        # Invariant E: Safety guard against deleting authoritative repository
        if wt == repo or wt.is_relative_to(repo):
            raise ReplaySafetyViolationError(
                "CRITICAL: Attempted to cleanup authoritative repo path!"
            )

        # Invariant E: Branch safety check - only delete disposable replay/* branches
        if not self.branch_name.startswith("replay/"):
            raise ReplaySafetyViolationError(
                f"Refusing to delete non-replay branch: '{self.branch_name}'"
            )

        # Remove git worktree
        if wt.exists():
            GitManager.remove_worktree(
                repo_path=self._repo_path,
                worktree_path=wt,
                force=force,
                worktree_base=replay_base,
            )

        # Delete disposable git branch
        run_process(["git", "branch", "-D", self.branch_name], cwd=self._repo_path)
        self._is_active = False

    def __enter__(self) -> DisposableWorkspace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.cleanup()


def compute_workspace_result_digest(workspace_path: Path | str) -> str:
    """Compute deterministic digest of workspace result content.

    Binds:
    - relative path
    - file bytes (sha256)
    - executable bit
    - symlink target
    - directory presence (type only, no timestamps) [F-02]
    - additions, removals, modifications

    Excludes:
    - .git (and .git/**)
    - .pytest_cache (and .pytest_cache/**)
    - __pycache__ (and __pycache__/**)
    - *.pyc
    - synthesized empty .github (if created for mask)
    - replay scratch metadata
    """
    root = Path(workspace_path).resolve()
    if not root.exists():
        return hashlib.sha256(b"").hexdigest()

    entries: list[dict[str, Any]] = []

    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        parts = rel.parts
        if not parts:
            continue

        # Exclude git metadata
        if parts[0] == ".git":
            continue
        # Exclude pytest cache and bytecode
        if parts[0] == ".pytest_cache":
            continue
        if any(p == "__pycache__" for p in parts):
            continue
        if path.name.endswith(".pyc"):
            continue
        # Exclude synthesized empty .github mask directory
        if parts[0] == ".github" and path.is_dir() and not any(path.iterdir()):
            continue
        # Exclude replay scratch/quarantine if rooted here
        if parts[0] in (".quarantine", ".orch-worktrees"):
            continue

        posix_rel = rel.as_posix()
        if path.is_symlink():
            target = os.readlink(str(path))
            entries.append({
                "exec": False,
                "path": posix_rel,
                "sha256": None,
                "target": target,
                "type": "symlink",
            })
        elif path.is_dir():
            # F-02: Record directory presence deterministically so that adding
            # or removing an otherwise-empty meaningful directory changes the
            # digest.  No timestamps, no execution IDs — only path and type.
            entries.append({
                "exec": False,
                "path": posix_rel,
                "sha256": None,
                "target": None,
                "type": "directory",
            })
        elif path.is_file():
            is_exec = os.access(str(path), os.X_OK)
            data = path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()
            entries.append({
                "exec": is_exec,
                "path": posix_rel,
                "sha256": content_hash,
                "target": None,
                "type": "file",
            })

    # Sort entries deterministically by path
    entries.sort(key=lambda e: e["path"])
    canonical_bytes = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()
