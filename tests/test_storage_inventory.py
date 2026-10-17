"""GS-1 real owned-artifact inventory contracts.

Covers: ADR-0003 Amendment 1 §1/§2 (ownership from real persisted provenance,
unknown => KEEP, no prune from missing registration) and §5 (conservative
reclaimable-byte accounting for hard-linked/shared data).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from saberops.db import Database
from saberops.git import GitManager
from saberops.models import Attempt, AttemptStatus, CheckResult, Run, RunStatus, Tier
from saberops.storage_inventory import (
    ArtifactClass,
    OwnershipState,
    build_inventory,
    measure_path_bytes,
)

_CREATED = "2026-09-24T10:00:00+00:00"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    author = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    _git(repo, "commit", "-qm", "init", env=author)
    return repo


def _seed_run(
    db: Database,
    *,
    repo: Path,
    run_id: str,
    status: RunStatus,
    worktree_path: Path | None,
    branch: str | None,
    attempt_status: AttemptStatus = AttemptStatus.SUCCESS,
) -> str:
    base_sha = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id=run_id,
            task="inventory",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
            created_at=_CREATED,
            completed_at=_CREATED if status is not RunStatus.RUNNING else None,
        )
    )
    if worktree_path is not None:
        db.create_attempt(
            Attempt(
                id=f"{run_id}-a1",
                run_id=run_id,
                attempt_number=1,
                provider="p",
                model="m",
                worktree_path=str(worktree_path),
                branch_name=branch or "",
                status=attempt_status,
                commit_sha=base_sha if attempt_status is AttemptStatus.SUCCESS else None,
                created_at=_CREATED,
                base_sha=base_sha,
            )
        )
    return base_sha


def _measure_hardlink_free(tmp_path: Path) -> None:
    del tmp_path


def test_owned_terminal_worktree_carries_provenance(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §1 -- ownership comes from real records."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    base = GitManager.get_head_commit(repo)
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _seed_run(
        db,
        repo=repo,
        run_id="run1",
        status=RunStatus.COMPLETED,
        worktree_path=wt,
        branch="orch/run1/a1",
    )
    db.create_check(
        CheckResult(
            id="c1",
            attempt_id="run1-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )
    db.record_candidate_rejection_once("run1", base)

    inventory = build_inventory(db, repo_root=repo)
    items = {
        (item.artifact_class, item.reference): item
        for item in inventory.items
    }
    worktree = items[(ArtifactClass.ATTEMPT_WORKTREE, str(wt.resolve()))]
    assert worktree.ownership is OwnershipState.OWNED_TERMINAL
    assert worktree.reason == "candidate-rejected"
    assert worktree.detail["run_id"] == "run1"
    assert worktree.detail["candidate_state"] == "REJECTED"
    assert worktree.detail["attempt_terminal"] == "1"
    assert not worktree.protected
    # A registered attempt branch inherits its run's ownership.
    branch = items[(ArtifactClass.LOCAL_BRANCH, "orch/run1/a1")]
    assert branch.owner == "run1"
    assert branch.ownership is OwnershipState.OWNED_TERMINAL
    # The owner database is always inventoried and never "absent".
    assert any(item.artifact_class is ArtifactClass.OWNER_DB for item in inventory.items)


def test_gate_passed_candidate_is_a_protected_consumer(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- pending review keeps the worktree."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run2" / "a1"
    GitManager.create_worktree(repo, "orch/run2/a1", wt)
    _seed_run(
        db,
        repo=repo,
        run_id="run2",
        status=RunStatus.COMPLETED,
        worktree_path=wt,
        branch="orch/run2/a1",
    )
    db.create_check(
        CheckResult(
            id="c2",
            attempt_id="run2-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )
    inventory = build_inventory(db, repo_root=repo)
    item = next(
        i for i in inventory.items if i.reference == str(wt.resolve())
    )
    assert item.protected
    assert item.ownership is OwnershipState.OWNED_LIVE
    assert any(consumer.kind == "candidate" for consumer in item.consumers)


def test_running_run_is_owned_live(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- an active run is never eligible."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run3" / "a1"
    GitManager.create_worktree(repo, "orch/run3/a1", wt)
    _seed_run(
        db,
        repo=repo,
        run_id="run3",
        status=RunStatus.RUNNING,
        worktree_path=wt,
        branch="orch/run3/a1",
        attempt_status=AttemptStatus.RUNNING,
    )
    inventory = build_inventory(db, repo_root=repo)
    item = next(i for i in inventory.items if i.reference == str(wt.resolve()))
    assert item.ownership is OwnershipState.OWNED_LIVE
    assert item.reason == "live-consumer"


def test_unregistered_worktree_and_branch_fail_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §1 -- unregistered objects are UNKNOWN/KEEP."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "ghost" / "a1"
    GitManager.create_worktree(repo, "orch/ghost/a1", wt)
    inventory = build_inventory(db, repo_root=repo)
    item = next(i for i in inventory.items if i.reference == str(wt.resolve()))
    assert item.ownership is OwnershipState.UNKNOWN
    assert item.reason == "unregistered-worktree"
    branch = next(
        i for i in inventory.items if i.artifact_class is ArtifactClass.LOCAL_BRANCH
    )
    assert branch.ownership is OwnershipState.UNKNOWN
    assert branch.reason == "unregistered-branch"


def test_foreign_branches_are_not_inventoried(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §3 -- only engine-owned refs are in scope."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "feature/manual")
    db = Database(tmp_path / "orch.db")
    inventory = build_inventory(db, repo_root=repo)
    refs = {i.reference for i in inventory.items}
    assert "feature/manual" not in refs
    assert "main" not in refs


def test_hardlinked_bytes_are_not_definitely_reclaimable(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §5 -- shared hard-linked bytes are not summed."""
    directory = tmp_path / "tree"
    directory.mkdir()
    original = tmp_path / "original.bin"
    original.write_bytes(b"x" * 4096)
    link = directory / "linked.bin"
    os.link(original, link)
    measure = measure_path_bytes(directory)
    assert measure.apparent_bytes == 4096
    assert measure.hardlinked_files == 1
    assert measure.reclaimable_bytes is None


def test_measure_missing_path_is_unknown_not_zero(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- a missing/unmeasurable path is UNKNOWN, never 0."""
    measure = measure_path_bytes(tmp_path / "nope")
    assert measure.apparent_bytes is None
    assert measure.reclaimable_bytes is None


def test_missing_database_is_a_true_no_op(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §6 -- dry-run creates nothing, adopts nothing."""
    repo = _repo(tmp_path)
    db_path = tmp_path / "absent.db"
    inventory = build_inventory(None, repo_root=repo)
    assert "no-durable-state" in inventory.warnings
    assert not db_path.exists()
    assert not (tmp_path / "orch.db").exists()
    assert all(
        item.ownership is not OwnershipState.OWNED_ACCEPTED for item in inventory.items
    )


def test_not_a_git_repository_reports_and_stops(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §1 -- no repo identity means no inventory."""
    plain = tmp_path / "plain"
    plain.mkdir()
    inventory = build_inventory(None, repo_root=plain)
    assert inventory.items == ()
    assert "not-a-git-repository" in inventory.warnings


def test_checkpoint_refs_are_enumerated_read_only(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- checkpoint refs are inventoried with their job id."""
    repo = _repo(tmp_path)
    base = GitManager.get_head_commit(repo)
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    db = Database(tmp_path / "orch.db")
    inventory = build_inventory(db, repo_root=repo)
    checkpoints = inventory.by_class(ArtifactClass.CHECKPOINT_REF)
    assert len(checkpoints) == 1
    assert checkpoints[0].reference == "refs/saberops/checkpoints/job1/0"
    assert checkpoints[0].detail["job_id"] == "job1"


# --------------------------------------------------------------------------
# F4 -- gate scratch, evidence and checkpoint provenance
# --------------------------------------------------------------------------


def _scratch(root: Path, name: str, owner: dict[str, object]) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / ".saberops-scratch-owner.json").write_text(
        json.dumps(owner), encoding="utf-8"
    )
    return directory


def test_gate_scratch_is_inventoried_with_owner_liveness(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §1 -- scratch is discovered from real records."""
    import json as _json

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    state_root = tmp_path / "state"
    scratch_root = state_root / "gate-tmp"
    scratch_root.mkdir(parents=True)
    dead = _scratch(scratch_root, "orchestrator-gate-dead", {"pid": 999_999_999})
    live = _scratch(scratch_root, "orchestrator-gate-live", {"pid": os.getpid()})
    unowned = scratch_root / "orchestrator-gate-unowned"
    unowned.mkdir()
    other = scratch_root / "not-scratch"
    other.mkdir()
    _ = _json

    inventory = build_inventory(db, repo_root=repo, state_root=state_root)
    scratch = {
        item.reference: item
        for item in inventory.by_class(ArtifactClass.GATE_SCRATCH)
    }
    assert str(dead.resolve()) in scratch
    assert scratch[str(dead.resolve())].ownership is OwnershipState.OWNED_TERMINAL
    assert str(live.resolve()) in scratch
    live_item = scratch[str(live.resolve())]
    assert live_item.ownership is OwnershipState.OWNED_LIVE and live_item.protected
    assert str(unowned.resolve()) not in scratch
    assert str(other.resolve()) not in scratch


def test_evidence_is_inventoried_from_verified_provenance(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §1/§4 -- evidence linked to a real run record."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    base = GitManager.get_head_commit(repo)
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _seed_run(
        db, repo=repo, run_id="run1", status=RunStatus.COMPLETED,
        worktree_path=wt, branch="orch/run1/a1",
    )
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "run1.ansi").write_text("log\n", encoding="utf-8")
    (transcripts / "unknown-run.ansi").write_text("log\n", encoding="utf-8")
    state_root = tmp_path / "state"
    candidate_dir = state_root / "projects" / "p1" / "gate-evidence" / "candidates" / base
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "gate.log").write_text("ok\n", encoding="utf-8")

    inventory = build_inventory(db, repo_root=repo, state_root=state_root)
    evidence = {item.reference: item for item in inventory.by_class(ArtifactClass.EVIDENCE)}
    transcript = evidence[str((transcripts / "run1.ansi").resolve())]
    assert transcript.owner == "run1"
    assert transcript.ownership is OwnershipState.OWNED_TERMINAL
    assert transcript.detail["terminal_at"]
    candidate = evidence[str(candidate_dir.resolve())]
    assert candidate.owner == "run1"
    assert str((transcripts / "unknown-run.ansi").resolve()) not in evidence


def test_checkpoint_ref_links_to_terminal_job(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- checkpoint refs get real job terminal provenance."""
    from saberops.jobs.models import Job, JobRequirements, JobState
    from saberops.jobs.store import insert_job

    repo = _repo(tmp_path)
    base = GitManager.get_head_commit(repo)
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    db = Database(tmp_path / "orch.db")
    insert_job(
        db,
        Job(
            job_id="job1",
            idempotency_key=None,
            caller_id="owner",
            repo=str(repo.resolve()),
            base_ref="HEAD",
            base_sha=base,
            goal="g",
            acceptance=None,
            requirements=JobRequirements(),
            state=JobState.SUCCEEDED,
            created_at="2026-09-01T00:00:00+00:00",
            updated_at="2026-09-20T00:00:00+00:00",
        ),
    )
    inventory = build_inventory(db, repo_root=repo)
    checkpoint = inventory.by_class(ArtifactClass.CHECKPOINT_REF)[0]
    assert checkpoint.ownership is OwnershipState.OWNED_TERMINAL
    assert checkpoint.detail["job_terminal"] == "1"
    assert checkpoint.detail["terminal_at"]


def test_checkpoint_ref_without_job_stays_unknown(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- unlinked checkpoint refs fail closed to UNKNOWN."""
    repo = _repo(tmp_path)
    base = GitManager.get_head_commit(repo)
    GitManager.anchor_checkpoint_ref(repo, "ghost", 0, base)
    db = Database(tmp_path / "orch.db")
    inventory = build_inventory(db, repo_root=repo)
    checkpoint = inventory.by_class(ArtifactClass.CHECKPOINT_REF)[0]
    assert checkpoint.ownership is OwnershipState.UNKNOWN
    assert checkpoint.reason == "checkpoint-job-unknown"


def test_owned_caches_are_discovered_and_protected(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- engine-owned caches are inventoried and kept."""
    import subprocess as _subprocess

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    common = _subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common_path = Path(common) if Path(common).is_absolute() else repo / common
    mypy_cache = common_path / "saberops-mypy-cache"
    mypy_cache.mkdir(exist_ok=True)
    (mypy_cache / "probe.txt").write_text("cache\n", encoding="utf-8")
    test_map = common_path / "saberops-test-map"
    test_map.mkdir(exist_ok=True)
    (test_map / "probe.json").write_text("{}\n", encoding="utf-8")
    state_root = tmp_path / "state"
    state_cache = state_root / "cache"
    state_cache.mkdir(parents=True)
    (state_cache / "probe.txt").write_text("state\n", encoding="utf-8")
    inventory = build_inventory(db, repo_root=repo, state_root=state_root)
    caches = inventory.by_class(ArtifactClass.OWNED_CACHE)
    by_ref = {item.reference: item for item in caches}
    assert str(mypy_cache.resolve()) in by_ref
    assert str(test_map.resolve()) in by_ref
    assert str(state_cache.resolve()) in by_ref
    for item in caches:
        assert item.ownership is OwnershipState.OWNED_LIVE
        assert item.protected is True
        assert item.reason == "shared-engine-cache"
        assert item.reclaimable_bytes is None
