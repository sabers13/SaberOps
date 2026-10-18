"""Adversarial Git checkpoint-ref safety regressions (ADR-0002 D8 R3).

R3: the engine-owned checkpoint ref (``refs/saberops/checkpoints/<job_id>/<n>``)
was written with a bare ``git update-ref <ref> <sha>`` -- no compare-and-swap
and no ``--no-deref``.  An injected ref at the allocated name could therefore be
overwritten, and an injected *symbolic* ref pointing at a user branch was
transparently dereferenced, advancing that branch with ungated WIP.  The read
and list helpers then followed the same symlink and reported the branch commit
as a durable checkpoint anchor.

Hermetic by construction: every case builds a real isolated ``tmp_path`` Git
repository and drives the local ``git`` binary only.  No remotes, no network,
no provider/model/credential/billing state, no external process other than Git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saberops import preemption
from saberops.git import (
    CandidatePublicationError,
    GitManager,
    GitRefRaceError,
)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "orch@localhost"], path)
    _git(["config", "user.name", "Orchestrator MVP"], path)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-qm", "base"], path)
    return _git(["rev-parse", "HEAD"], path)


def _commit(path: Path, name: str, content: str) -> str:
    (path / name).write_text(content, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-qm", name], path)
    return _git(["rev-parse", "HEAD"], path)


def _ref_file(repo: Path, ref: str) -> Path:
    return repo / ".git" / ref


def _branch(value_ref: str = "refs/heads/valuable") -> str:
    return value_ref.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Symbolic-ref injection: never dereference, never advance a user branch.
# ---------------------------------------------------------------------------


def test_anchor_refuses_symbolic_ref_to_user_branch(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 symbolic-ref injection must not move a branch."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(["branch", "valuable"], repo)
    valuable = _git(["rev-parse", "refs/heads/valuable"], repo)

    checkpoint_ref = "refs/saberops/checkpoints/job1/0"
    _git(["symbolic-ref", checkpoint_ref, "refs/heads/valuable"], repo)

    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    # The user branch is byte-for-byte unchanged and the symlink was not
    # rewritten into a direct anchor.
    assert _git(["rev-parse", "refs/heads/valuable"], repo) == valuable
    assert (
        _ref_file(repo, checkpoint_ref).read_text(encoding="utf-8").strip()
        == "ref: refs/heads/valuable"
    )


def test_anchor_refuses_symbolic_injected_between_listing_and_create(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R3 ref injected after enumeration must fail closed.

    The preemption coordinator enumerates the job's checkpoint refs and then
    anchors the newest SHA.  A symbolic ref installed inside that window must
    lose the create-only compare-and-swap, leaving the branch untouched.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(["branch", "valuable"], repo)
    valuable = _git(["rev-parse", "refs/heads/valuable"], repo)

    assert GitManager.list_checkpoint_refs(repo, "job1") == {}
    _git(
        ["symbolic-ref", "refs/saberops/checkpoints/job1/0", "refs/heads/valuable"],
        repo,
    )

    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    assert _git(["rev-parse", "refs/heads/valuable"], repo) == valuable
    assert (
        _git(["symbolic-ref", "--quiet", "refs/saberops/checkpoints/job1/0"], repo)
        == "refs/heads/valuable"
    )


def test_reuse_path_refuses_symbolic_checkpoint_ref(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 coordinator reuse path must not trust a symlink."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(["branch", "valuable"], repo)
    valuable = _git(["rev-parse", "refs/heads/valuable"], repo)
    _git(
        ["symbolic-ref", "refs/saberops/checkpoints/job1/0", "refs/heads/valuable"],
        repo,
    )

    with pytest.raises(CandidatePublicationError):
        preemption._anchor_verified_ref(repo, job_id="job1", checkpoint_sha=base)

    assert _git(["rev-parse", "refs/heads/valuable"], repo) == valuable


def test_dangling_symbolic_ref_is_refused_not_rewritten(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 dangling symref is still a symbolic ref."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    checkpoint_ref = "refs/saberops/checkpoints/job1/0"
    _git(["symbolic-ref", checkpoint_ref, "refs/heads/not_yet_created"], repo)

    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    assert _git(["symbolic-ref", "--quiet", checkpoint_ref], repo) == (
        "refs/heads/not_yet_created"
    )


# ---------------------------------------------------------------------------
# Direct-ref collision: create-only CAS must never overwrite.
# ---------------------------------------------------------------------------


def test_anchor_refuses_direct_ref_collision_and_keeps_old_sha(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R3 create-only CAS preserves a colliding anchor."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    newer = _commit(repo, "wip.txt", "wip\n")

    checkpoint_ref = "refs/saberops/checkpoints/job1/0"
    _git(["update-ref", checkpoint_ref, base], repo)

    with pytest.raises(GitRefRaceError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, newer)

    assert _git(["rev-parse", checkpoint_ref], repo) == base


def test_anchor_refuses_direct_ref_injected_between_listing_and_create(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R3 a direct ref injected after listing is not taken."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    newer = _commit(repo, "wip.txt", "wip\n")

    assert GitManager.list_checkpoint_refs(repo, "job1") == {}
    checkpoint_ref = "refs/saberops/checkpoints/job1/0"
    _git(["update-ref", checkpoint_ref, newer], repo)

    with pytest.raises(GitRefRaceError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    assert _git(["rev-parse", checkpoint_ref], repo) == newer


def test_anchor_reuses_identical_direct_ref_and_refuses_packed_collision(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R3 packed refs: same-SHA retry reuses, collision fails."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    newer = _commit(repo, "wip.txt", "wip\n")
    checkpoint_ref = "refs/saberops/checkpoints/job1/0"

    _git(["update-ref", checkpoint_ref, newer], repo)
    _git(["pack-refs", "--all"], repo)
    # The ref is now packed (no loose file) but still a first-class direct ref.
    assert not _ref_file(repo, checkpoint_ref).exists()

    # A retry that anchors the identical SHA is idempotent reuse.
    assert GitManager.anchor_checkpoint_ref(repo, "job1", 0, newer) == checkpoint_ref
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == newer
    assert GitManager.list_checkpoint_refs(repo, "job1") == {0: newer}

    # A different target loses the create-only compare-and-swap.
    with pytest.raises(GitRefRaceError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    assert _git(["rev-parse", checkpoint_ref], repo) == newer


# ---------------------------------------------------------------------------
# Read/list must never present a symbolic ref as a verified anchor.
# ---------------------------------------------------------------------------


def test_read_and_list_refuse_symbolic_checkpoint_ref(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 read/list must not dereference a symlink anchor."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(["branch", "valuable"], repo)
    valuable = _git(["rev-parse", "refs/heads/valuable"], repo)
    _git(
        ["symbolic-ref", "refs/saberops/checkpoints/job1/0", "refs/heads/valuable"],
        repo,
    )

    with pytest.raises(CandidatePublicationError):
        GitManager.read_checkpoint_ref(repo, "job1", 0)
    with pytest.raises(CandidatePublicationError):
        GitManager.list_checkpoint_refs(repo, "job1")

    # A legitimate direct sibling anchor does not make the listing trustworthy;
    # the whole job namespace stays fail-closed while the symlink is present.
    GitManager.anchor_checkpoint_ref(repo, "job1", 1, base)
    with pytest.raises(CandidatePublicationError):
        GitManager.list_checkpoint_refs(repo, "job1")

    assert _git(["rev-parse", "refs/heads/valuable"], repo) == valuable


def test_read_checkpoint_ref_returns_none_when_absent(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 absent direct ref reads as None, not an error."""
    repo = tmp_path / "repo"
    _repo(repo)
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) is None
    assert GitManager.list_checkpoint_refs(repo, "job1") == {}


# ---------------------------------------------------------------------------
# Target verification, namespace conflicts, and reachability.
# ---------------------------------------------------------------------------


def test_anchor_requires_existing_exact_target_commit(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 unverified target SHA is refused before any write."""
    repo = tmp_path / "repo"
    _repo(repo)
    checkpoint_ref = "refs/saberops/checkpoints/job1/0"

    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, "0" * 40)

    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", checkpoint_ref],
        cwd=repo,
        capture_output=True,
        text=True,
    ).returncode != 0


def test_create_only_cas_fails_closed_on_namespace_conflict(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 R3 an unusable namespace cannot yield a false anchor."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    # ``.../job1/0`` becomes a directory, so the exact ref name cannot exist.
    _git(["update-ref", "refs/saberops/checkpoints/job1/0/sub", base], repo)

    with pytest.raises(GitRefRaceError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)

    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/saberops/checkpoints/job1/0"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )


def test_valid_anchor_keeps_wip_reachable_after_branch_moves_and_gc(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R3 a real direct anchor survives ref advance + gc."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    wip = _commit(repo, "wip.txt", "wip\n")
    checkpoint_ref = GitManager.anchor_checkpoint_ref(repo, "job1", 0, wip)

    # The checkpoint commit is no longer reachable from any branch...
    _git(["reset", "--hard", base], repo)
    _git(["gc", "--prune=now"], repo)

    # ...but the engine-owned direct ref keeps it alive and verified.
    assert GitManager.commit_exists(repo, wip)
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == wip
    assert GitManager.list_checkpoint_refs(repo, "job1") == {0: wip}
    assert checkpoint_ref == "refs/saberops/checkpoints/job1/0"
