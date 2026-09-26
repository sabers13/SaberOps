"""Git worktree manager ensuring safe isolation for worker attempts."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

try:
    import fcntl
except ImportError:  # pragma: no cover - SaberOps runs on Linux; no POSIX locks here.
    fcntl = None  # type: ignore[assignment]

from saberops.process import run_process
from saberops.project import ProjectIdentityError, compute_project_identity


class CandidatePublicationError(RuntimeError):
    """Fail-closed error for a rejected or unverified candidate publication.

    This is an infrastructure/publication failure, never a worker capability,
    model, review, or gate failure.  Callers must not spend worker retry or
    escalation budget in response.
    """


class RevisionRaceError(RuntimeError):
    """The repository moved during project-graph analysis (C09 TOCTOU).

    ``build_project_graph`` reads the working tree while ``base_revision`` is
    caller-supplied metadata; a run's frozen graph must never be labeled with
    a revision the repository was not actually at throughout analysis.  This
    is raised, never repaired, when the pre- or post-analysis check fails.
    """


class GitRefRaceError(RuntimeError):
    """A compare-and-swap target ref no longer has its expected value."""


def assert_repository_at_revision(
    repo_path: Path | str, expected_revision: str, *, when: str
) -> None:
    """Fail-closed guard that ``repo_path`` is still clean at ``expected_revision``.

    Called both immediately before and immediately after project-graph
    construction so a repository move during analysis is never silently
    labeled as the revision the analysis started at.  ``when`` names the
    checkpoint (e.g. ``"before project-graph analysis"``) for the error
    message; this is a single shared helper so the two checkpoints -- and
    every call site that freezes a run's scope -- can never drift apart.
    """
    head = GitManager.get_head_commit(repo_path)
    if head != expected_revision:
        raise RevisionRaceError(
            f"repository HEAD moved {when}: expected base revision "
            f"{expected_revision!r}, found {head!r}"
        )
    if not GitManager.is_clean(repo_path):
        raise RevisionRaceError(f"repository is not clean {when}")


@dataclass(frozen=True)
class CandidatePublication:
    """Durable, credential-free evidence of one successful publication."""

    remote_name: str
    remote_branch: str  # fully qualified, e.g. refs/heads/orch/<run>/a1
    candidate_branch: str
    candidate_sha: str
    verified_remote_sha: str

    def as_dict(self) -> dict[str, str]:
        """Serialize for durable event payloads (never contains secrets)."""
        return {
            "remote_name": self.remote_name,
            "remote_branch": self.remote_branch,
            "candidate_branch": self.candidate_branch,
            "candidate_sha": self.candidate_sha,
            "verified_remote_sha": self.verified_remote_sha,
        }


# SaberOps-generated candidate branches only: ``orch/<run_id>/a<n>``.  Anything
# else is rejected before a refspec is ever constructed, so a user-controlled
# string can never become an arbitrary remote ref update.
_CANDIDATE_BRANCH_RE: Final = re.compile(r"^orch/[A-Za-z0-9._-]+/a[0-9]+$")

# Dangerous Git ref components/patterns beyond the shape check above.
_REF_DANGEROUS_RE: Final = re.compile(r"(\.\.|@{|[\\^:~?*\[\]\s\x00-\x1f\x7f])")

# Credential-bearing URL segment (scheme://user:password@host/...).  Git error
# output may echo the remote URL; these fragments must never reach logs or
# durable event payloads.
_CREDENTIAL_URL_RE: Final = re.compile(r"(?<=//)[^/@\s:?#]+:[^@\s/?#]+@")


def redact_remote_secrets(text: str) -> str:
    """Scrub credential-bearing URL fragments from Git output before storage."""
    return _CREDENTIAL_URL_RE.sub("***@", text)


def validate_candidate_branch(branch: object) -> str:
    """Validate that *branch* is an Orch-owned candidate branch, failing closed.

    Only branches shaped like ``orch/<run_id>/a<n>`` are accepted, and only
    when the value is also a syntactically safe single Git ref.  This happens
    *before* any refspec is constructed, so a malformed or unexpected ref can
    never reach ``git push``.
    """
    if not isinstance(branch, str):
        raise CandidatePublicationError(
            f"Candidate branch must be a string, got {type(branch).__name__}"
        )
    value = branch
    if not value or not _CANDIDATE_BRANCH_RE.match(value):
        raise CandidatePublicationError(
            f"Refusing to publish: '{value[:64]}' is not a SaberOps candidate "
            "branch of the form 'orch/<run_id>/a<n>'"
        )
    if value.startswith("-") or value.endswith((".", ".lock", "/")) or "//" in value:
        raise CandidatePublicationError(
            f"Refusing to publish: malformed candidate ref '{value[:64]}'"
        )
    if _REF_DANGEROUS_RE.search(value):
        raise CandidatePublicationError(
            f"Refusing to publish: unsafe characters in candidate ref '{value[:64]}'"
        )
    return value


def validate_candidate_sha(sha: object) -> str:
    """Validate that *sha* is a full 40-character hex commit SHA."""
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CandidatePublicationError(
            "Refusing to publish: candidate SHA must be a full 40-character "
            f"hex commit SHA, got {str(sha)[:64]!r}"
        )
    return sha


class WorktreeMetadataLockError(RuntimeError):
    """Repository worktree-metadata serialization could not be established.

    Raised fail-closed when the per-repository worktree-metadata lock cannot
    be acquired: callers must never proceed with an unserialized
    ``git worktree add`` / ``git worktree remove``.  A subclass of
    :class:`RuntimeError` so existing fail-closed handlers keep working.
    """


# Coordination file for repository-global Git worktree metadata mutation.
# It lives in the repository's common Git directory (``<repo>/.git`` for a
# standard repository): invisible to ``git status --porcelain`` (so it never
# dirties the authoritative checkout), keyed by repository identity rather
# than by branch or worktree path, and tied to the repository lifecycle.
# The file itself is never deleted: removing lock files races with live
# lock holders.  Only ``fcntl.flock`` exclusion is used for mutual
# exclusion; the file stays as an empty coordination inode.
_WORKTREE_METADATA_LOCK_FILENAME: Final = "orch-worktree-metadata.lock"


def _worktree_metadata_lock_path(repo: Path) -> Path:
    """Return the coordination-file path serializing this repository's worktree metadata.

    Resolved through ``git rev-parse --git-common-dir`` so every path spelling
    of the same repository (main checkout or linked worktree) maps to one
    authority.  Falls back to ``<repo>/.git`` when the plumbing query fails;
    that fallback only affects where the lock file lives, never whether
    locking happens.
    """
    res = run_process(["git", "rev-parse", "--git-common-dir"], cwd=repo)
    if res.passed and res.stdout.strip():
        common = Path(res.stdout.strip())
        if not common.is_absolute():
            common = repo / common
        return common.resolve() / _WORKTREE_METADATA_LOCK_FILENAME
    return (repo / ".git").resolve() / _WORKTREE_METADATA_LOCK_FILENAME


@contextmanager
def _serialized_worktree_metadata_mutation(repo_path: Path | str) -> Iterator[None]:
    """Serialize one repository-global Git worktree metadata mutation.

    Holds an exclusive ``fcntl.flock`` on the repository's coordination file
    for the duration of exactly one mutating Git invocation
    (``git worktree add`` / ``git worktree remove``).  The lock is:

    - keyed by authoritative repository (common Git dir): mutations against
      repo A serialize with each other; repo B remains independent;
    - cross-process safe: ``flock`` locks are kernel-managed, so a dead
      holder's lock is released automatically -- there is no stale-lock
      takeover, no force-unlock, and no lock-file deletion;
    - thread safe: each acquisition opens its own file description, and
      ``flock`` serializes those even within one process;
    - fail-closed: any acquisition failure raises
      :class:`WorktreeMetadataLockError` before Git is invoked, never a
      silent unserialized fallback.

    Only the mutating Git subprocess runs under the lock.  Read-only checks
    (branch existence, path validation) and the actual replay
    process/check bodies stay outside it, so deterministic parallel
    execution remains genuinely concurrent.
    """
    if fcntl is None:  # pragma: no cover - POSIX-only primitive.
        raise WorktreeMetadataLockError(
            "Cannot serialize worktree metadata mutation: no fcntl flock "
            "primitive on this platform; refusing to proceed unserialized."
        )
    repo = Path(repo_path).resolve()
    lock_path = _worktree_metadata_lock_path(repo)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeMetadataLockError(
            f"Cannot serialize worktree metadata mutation for repository '{repo}': "
            f"lock directory '{lock_path.parent}' is unusable: {exc}"
        ) from exc
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise WorktreeMetadataLockError(
            f"Cannot serialize worktree metadata mutation for repository '{repo}': "
            f"lock file '{lock_path}' cannot be opened: {exc}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise WorktreeMetadataLockError(
                f"Cannot serialize worktree metadata mutation for repository '{repo}': "
                f"exclusive lock on '{lock_path}' failed: {exc}"
            ) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor below releases the kernel lock
                # unconditionally; a failed explicit unlock loses nothing.
                pass
    finally:
        os.close(fd)


class GitManager:
    """Operations for Git repositories and isolated worktrees."""

    @staticmethod
    def is_git_repo(repo_path: Path | str) -> bool:
        """Check if target path is a valid Git repository."""
        path = Path(repo_path).resolve()
        if not path.exists() or not path.is_dir():
            return False
        res = run_process(["git", "rev-parse", "--git-dir"], cwd=path)
        return res.passed

    @staticmethod
    def get_head_commit(repo_path: Path | str) -> str:
        """Get the full SHA of HEAD."""
        res = run_process(["git", "rev-parse", "HEAD"], cwd=repo_path)
        if not res.passed:
            raise RuntimeError(f"Failed to get HEAD commit in {repo_path}: {res.stderr}")
        return res.stdout.strip()

    @staticmethod
    def get_current_branch(repo_path: Path | str) -> str | None:
        """Get current branch name or None if in detached HEAD."""
        res = run_process(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        if not res.passed:
            return None
        branch = res.stdout.strip()
        return branch if branch and branch != "HEAD" else None

    @staticmethod
    def is_clean(repo_path: Path | str) -> bool:
        """Return True if working tree and index have no modified/untracked files."""
        res = run_process(["git", "status", "--porcelain"], cwd=repo_path)
        return res.passed and len(res.stdout.strip()) == 0

    @staticmethod
    def get_default_worktree_base(repo_path: Path | str) -> Path:
        """Compute the default root directory for isolated worktrees.

        The namespace segment carries the canonical project ID, not just the
        repository basename, so two same-named checkouts can never share a
        worktree root even when a common worktree base is configured.
        """
        resolved = Path(repo_path).resolve()
        try:
            namespace = compute_project_identity(resolved).namespace
        except ProjectIdentityError:
            namespace = resolved.name
        return resolved.parent / ".orch-worktrees" / namespace

    @staticmethod
    def create_worktree(
        repo_path: Path | str,
        branch_name: str,
        worktree_path: Path | str,
        start_point: str | None = None,
    ) -> Path:
        """Create an isolated worktree checked out to a new branch."""
        repo = Path(repo_path).resolve()
        wt = Path(worktree_path).resolve()

        if wt.exists():
            raise FileExistsError(f"Target worktree path already exists: {wt}")

        # Check if branch exists - FAIL CLOSED rather than force-deleting
        branch_check = run_process(["git", "rev-parse", "--verify", branch_name], cwd=repo)
        if branch_check.passed:
            raise FileExistsError(f"Branch already exists in repository: {branch_name}")

        wt.parent.mkdir(parents=True, exist_ok=True)

        base_ref = start_point or "HEAD"
        args = ["git", "worktree", "add", "-b", branch_name, str(wt), base_ref]
        # Mutates repository-global worktree metadata under
        # ``<repo>/.git/worktrees/`` shared with every concurrent workspace
        # on this repository: serialize against sibling mutations.  The
        # read-only checks above stay outside the lock.
        with _serialized_worktree_metadata_mutation(repo):
            res = run_process(args, cwd=repo)
        if not res.passed:
            raise RuntimeError(f"Failed to create git worktree at {wt}: {res.stderr}")

        return wt

    @staticmethod
    def remove_worktree(
        repo_path: Path | str,
        worktree_path: Path | str,
        force: bool = True,
        worktree_base: Path | str | None = None,
    ) -> None:
        """Safely remove a single worktree verified to be within orchestrator worktree root.

        Uses ``git worktree remove --force`` to remove the exact worktree.
        Does NOT run ``git worktree prune`` because that repository-global
        operation can destroy linked-worktree metadata belonging to other
        concurrently active workspaces whose ``.git`` pointer may be
        temporarily quarantined.
        """
        repo = Path(repo_path).resolve()
        wt = Path(worktree_path).resolve()

        base = (
            Path(worktree_base).resolve()
            if worktree_base is not None
            else GitManager.get_default_worktree_base(repo).resolve()
        )
        try:
            is_subpath = wt.is_relative_to(base) and wt != base
        except (ValueError, AttributeError):
            is_subpath = False

        if not is_subpath:
            raise RuntimeError(
                f"Refusing to delete unverified worktree path outside worktree base: {wt}"
            )

        if not wt.exists():
            return

        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(wt))

        # Mutates the same shared ``<repo>/.git/worktrees/`` metadata as
        # creation: serialize against sibling mutations.  No ``git worktree
        # prune`` is performed here -- that repository-global operation can
        # destroy linked-worktree metadata belonging to concurrently active
        # workspaces.
        with _serialized_worktree_metadata_mutation(repo):
            res = run_process(cmd, cwd=repo)
        if not res.passed:
            raise RuntimeError(
                f"Git refused to remove worktree '{wt}' (exit code {res.exit_code}). "
                f"Worktree preserved for inspection. stderr: {res.stderr.strip()}"
            )

    @staticmethod
    def get_changed_paths(worktree_path: Path | str) -> list[str]:
        """Return list of modified or untracked file paths in the worktree."""
        res = run_process(["git", "status", "--porcelain=v1"], cwd=worktree_path)
        if not res.passed:
            return []
        paths: list[str] = []
        for raw_line in res.stdout.splitlines():
            if not raw_line or len(raw_line) < 4:
                continue
            path_part = raw_line[3:]
            if " -> " in path_part:
                path = path_part.split(" -> ")[-1]
            else:
                path = path_part
            path = path.strip()
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            if path:
                paths.append(path)
        return paths

    @staticmethod
    def commit_changes(
        worktree_path: Path | str,
        message: str,
        author_name: str = "Orchestrator MVP",
        author_email: str = "orch@localhost",
    ) -> str | None:
        """Stage all changes and commit in the worktree if dirty. Returns new HEAD SHA."""
        wt = Path(worktree_path).resolve()

        changed = GitManager.get_changed_paths(wt)
        if not changed:
            head_res = run_process(["git", "rev-parse", "HEAD"], cwd=wt)
            return head_res.stdout.strip() if head_res.passed else None

        add_res = run_process(["git", "add", "-A"], cwd=wt)
        if not add_res.passed:
            raise RuntimeError(f"Failed to git add in worktree: {add_res.stderr}")

        env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

        commit_res = run_process(["git", "commit", "-m", message], cwd=wt, env=env)
        if not commit_res.passed and "nothing to commit" not in commit_res.stdout:
            raise RuntimeError(f"Failed to commit in worktree: {commit_res.stderr}")

        head_res = run_process(["git", "rev-parse", "HEAD"], cwd=wt)
        return head_res.stdout.strip() if head_res.passed else None

    @staticmethod
    def create_checkpoint_commit(
        worktree_path: Path | str,
        message: str,
        author_name: str = "Orchestrator MVP",
        author_email: str = "orch@localhost",
    ) -> str | None:
        """Stage all changes and create a durable checkpoint commit in the worktree.

        Returns new commit SHA, or None if there are no workspace changes to checkpoint.
        """
        wt = Path(worktree_path).resolve()
        changed = GitManager.get_changed_paths(wt)
        if not changed:
            return None

        add_res = run_process(["git", "add", "-A"], cwd=wt)
        if not add_res.passed:
            raise RuntimeError(f"Failed to git add in worktree for checkpoint: {add_res.stderr}")

        env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

        commit_res = run_process(["git", "commit", "-m", message], cwd=wt, env=env)
        if not commit_res.passed and "nothing to commit" not in commit_res.stdout:
            err = commit_res.stderr
            raise RuntimeError(f"Failed to create checkpoint commit in worktree: {err}")

        head_res = run_process(["git", "rev-parse", "HEAD"], cwd=wt)
        return head_res.stdout.strip() if head_res.passed else None

    @staticmethod
    def get_diff(worktree_path: Path | str, base_commit: str | None = None) -> str:
        """Get git diff against a base commit or previous commit."""
        cmd = ["git", "diff"]
        if base_commit:
            cmd.append(base_commit)
        res = run_process(cmd, cwd=worktree_path)
        return res.stdout if res.passed else ""

    @staticmethod
    def has_diff_against_base(repo_or_wt_path: Path | str, base_commit: str) -> bool:
        """Check if HEAD or working tree has any changes compared to base_commit."""
        path = Path(repo_or_wt_path).resolve()
        # Check committed diff
        res_commit = run_process(["git", "diff", "--quiet", base_commit, "HEAD"], cwd=path)
        if res_commit.exit_code != 0:
            return True
        # Check uncommitted diff
        res_uncommitted = run_process(["git", "status", "--porcelain"], cwd=path)
        if res_uncommitted.passed and res_uncommitted.stdout.strip():
            return True
        return False

    @staticmethod
    def is_ancestor(repo_or_wt_path: Path | str, ancestor_sha: str, descendant_sha: str) -> bool:
        """Check if ancestor_sha is an ancestor of descendant_sha."""
        path = Path(repo_or_wt_path).resolve()
        res = run_process(
            ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            cwd=path,
        )
        return res.exit_code == 0

    @staticmethod
    def get_changed_paths_between(
        repo_or_wt_path: Path | str, base_sha: str, target_sha: str
    ) -> list[str]:
        """Get list of changed file paths between two commit SHAs."""
        path = Path(repo_or_wt_path).resolve()
        res = run_process(["git", "diff", "--name-only", base_sha, target_sha], cwd=path)
        if not res.passed or not res.stdout.strip():
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    @staticmethod
    def get_ref_commit(repo_path: Path | str, ref: str) -> str | None:
        """Get the commit SHA that a ref points to, or None if invalid."""
        res = run_process(["git", "rev-parse", "--verify", ref], cwd=repo_path)
        return res.stdout.strip() if res.passed else None

    @staticmethod
    def fast_forward_merge(repo_path: Path | str, target_ref: str) -> str:
        """Fast-forward merge target_ref into the current branch of repo_path."""
        repo = Path(repo_path).resolve()
        res = run_process(["git", "merge", "--ff-only", target_ref], cwd=repo)
        if not res.passed:
            raise RuntimeError(f"Fast-forward merge of '{target_ref}' failed: {res.stderr}")
        return GitManager.get_head_commit(repo)

    @staticmethod
    def compare_and_swap_ref(
        repo_path: Path | str,
        ref: str,
        *,
        expected_old_sha: str,
        new_target_sha: str,
    ) -> str:
        """Atomically update one safe ref only when it still equals ``expected_old_sha``."""
        if not isinstance(ref, str) or (
            ref != "HEAD" and not ref.startswith("refs/heads/")
        ):
            raise ValueError(f"Refusing compare-and-swap on unsafe ref {ref!r}")
        if _REF_DANGEROUS_RE.search(ref):
            raise ValueError(f"Refusing compare-and-swap on unsafe ref {ref!r}")
        expected = validate_candidate_sha(expected_old_sha)
        target = validate_candidate_sha(new_target_sha)
        result = run_process(
            ["git", "update-ref", ref, target, expected],
            cwd=Path(repo_path).resolve(),
        )
        if not result.passed:
            raise GitRefRaceError(
                f"ref '{ref}' changed before compare-and-swap: "
                f"{redact_remote_secrets(result.stderr).strip()[:500]}"
            )
        return target

    @staticmethod
    def sync_index_worktree(repo_path: Path | str) -> None:
        """Synchronize the index and worktree to HEAD WITHOUT a branch-ref update.

        The AcceptEngine's compare-and-swap has already selected the target
        branch commit with expected-old semantics; this helper performs the
        remaining index/worktree synchronization while never performing
        another ref update, so an external writer that advanced the branch
        inside the post-compare-and-swap window is never overwritten by a
        later unconditional branch-ref move.

        ``git read-tree -u --reset HEAD`` is pure index/worktree plumbing:
        unlike ``git reset --hard`` (which rewrites the current branch ref
        unconditionally), it never mutates any ref.  Tracked files are
        reset to HEAD's tree and index entries that disappear from that
        tree are removed; untracked files are left untouched.
        """
        repo = Path(repo_path).resolve()
        result = run_process(["git", "read-tree", "-u", "--reset", "HEAD"], cwd=repo)
        if not result.passed:
            raise RuntimeError(
                "index/worktree synchronization from HEAD failed: "
                f"{redact_remote_secrets(result.stderr).strip()[:500]}"
            )

    @staticmethod
    def publish_candidate(
        repo_path: Path | str,
        candidate_sha: str,
        candidate_branch: str,
        remote_name: str = "origin",
    ) -> CandidatePublication:
        """Publish an exact candidate SHA as a candidate branch to a remote.

        The smallest governed remote-mutation primitive: a create-only,
        fast-forward-only, non-force push of the exact candidate commit to the
        exact validated Orch candidate branch, followed by an independent
        ``ls-remote`` verification that the remote ref resolves to the exact
        candidate SHA.  Success is never inferred from the push exit code
        alone.

        Semantics:
          - refspec is ``<candidate_sha>:refs/heads/<candidate_branch>``, so
            the result never depends on whatever happens to be checked out;
          - no force flags exist on this code path; a non-fast-forward or
            already-existing divergent remote branch is a FAIL CLOSED error
            and the remote branch is left untouched;
          - no remote branch is ever deleted;
          - only the remote *name* is used in diagnostics; credential-bearing
            URLs are scrubbed from any Git output before it can be raised or
            persisted.

        Raises:
            CandidatePublicationError: on a malformed/unowned ref, a missing
                or unusable remote, a rejected push, or a remote SHA that
                does not match the candidate SHA.
        """
        repo = Path(repo_path).resolve()
        sha = validate_candidate_sha(candidate_sha)
        branch = validate_candidate_branch(candidate_branch)
        remote = remote_name if isinstance(remote_name, str) and remote_name.strip() else "origin"
        remote_branch = f"refs/heads/{branch}"

        # The candidate commit must exist locally before anything is pushed.
        sha_res = run_process(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=repo)
        if not sha_res.passed or sha_res.stdout.strip() != sha:
            raise CandidatePublicationError(
                f"Refusing to publish: candidate commit {sha[:8]} does not resolve "
                f"in repository {repo.name}"
            )

        # Local evidence consistency: if the local candidate branch exists it
        # must already point at the candidate commit.
        local_branch_res = run_process(
            ["git", "rev-parse", "--verify", branch], cwd=repo
        )
        if local_branch_res.passed and local_branch_res.stdout.strip() != sha:
            raise CandidatePublicationError(
                f"Refusing to publish: local branch '{branch}' points to "
                f"{local_branch_res.stdout.strip()[:8]}, not candidate {sha[:8]}"
            )

        # Fail clearly when the remote is absent or unusable -- never pretend
        # success.  Only the remote name is echoed, never its URL.
        url_res = run_process(["git", "remote", "get-url", remote], cwd=repo)
        if not url_res.passed:
            raise CandidatePublicationError(
                f"Candidate publication failed: remote '{remote}' does not exist "
                f"in repository {repo.name}"
            )

        # Create-only / fast-forward-only push.  No force flags are passed,
        # and none exist anywhere on this code path: Git itself rejects a
        # non-fast-forward update, which surfaces as a fail-closed error.
        push_res = run_process(
            ["git", "push", remote, f"{sha}:{remote_branch}"], cwd=repo
        )
        if not push_res.passed:
            raise CandidatePublicationError(
                "Candidate publication failed: remote rejected candidate push to "
                f"'{remote_branch}' on '{remote}': "
                f"{redact_remote_secrets(push_res.stderr.strip())[:400] or 'unknown git error'}"
            )

        # Independent verification with an exact ls-remote lookup.
        verify_res = run_process(
            ["git", "ls-remote", remote, remote_branch], cwd=repo
        )
        if not verify_res.passed:
            raise CandidatePublicationError(
                "Candidate publication failed: could not verify remote branch "
                f"'{remote_branch}' on '{remote}': "
                f"{redact_remote_secrets(verify_res.stderr.strip())[:400]}"
            )
        remote_sha = verify_res.stdout.strip().split()[0] if verify_res.stdout.strip() else ""
        if remote_sha != sha:
            raise CandidatePublicationError(
                "Candidate publication failed: remote branch "
                f"'{remote_branch}' on '{remote}' resolves to '{remote_sha[:8] or 'nothing'}', "
                f"not the candidate SHA {sha[:8]}"
            )

        return CandidatePublication(
            remote_name=remote,
            remote_branch=remote_branch,
            candidate_branch=branch,
            candidate_sha=sha,
            verified_remote_sha=remote_sha,
        )
