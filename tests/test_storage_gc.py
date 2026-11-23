"""GS-1 guarded GC planner/executor contracts.

Covers: Guarded lifecycle: exact native Git order, dry-run
no-op, scoped owner authority, refusal on every race) and D6 (retention classes,
evidence-deletion hold).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.db import Database
from saberops.git import GitManager
from saberops.models import Attempt, AttemptStatus, CheckResult, Run, RunStatus, Tier
from saberops.storage_cleanup import (
    LANDED_CLEANUP_RETENTION_DAYS,
    ArtifactClass,
    Consumer,
    GcAction,
    GcCandidate,
    GcPolicy,
    GcPolicyError,
    GcPreview,
    Inventory,
    InventoryItem,
    OwnershipState,
    StorageAuditEvent,
    apply_gc,
    build_inventory,
    load_gc_policy,
    preview_gc,
    retire_landed_worktrees,
    retire_run_worktrees,
)

_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
_CREATED = "2026-09-24T10:00:00+00:00"


def _approved(**overrides: object) -> GcPolicy:
    base: dict[str, object] = {
        "approved": True,
        "version": "test",
        "source": "test",
        "allow_worktree_retirement": True,
        "allow_branch_retirement": True,
    }
    base.update(overrides)
    return GcPolicy(**base)  # type: ignore[arg-type]


def _audit_sink() -> tuple[list[StorageAuditEvent], Callable[[StorageAuditEvent], None]]:
    events: list[StorageAuditEvent] = []

    def sink(event: StorageAuditEvent) -> None:
        events.append(event)

    return events, sink


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


_AUTHOR = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", env=_AUTHOR)
    return repo


def _terminal_run(
    db: Database, *, repo: Path, run_id: str, wt: Path, branch: str, accepted: bool = True
) -> str:
    base_sha = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id=run_id,
            task="gc",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="p",
            model="m",
            worktree_path=str(wt),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=base_sha,
            created_at=_CREATED,
            base_sha=base_sha,
        )
    )
    db.create_check(
        CheckResult(
            id=f"{run_id}-c1",
            attempt_id=f"{run_id}-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )
    db.record_candidate_rejection_once(run_id, base_sha)
    return base_sha


def _item(
    artifact_class: ArtifactClass,
    reference: str,
    *,
    ownership: OwnershipState = OwnershipState.OWNED_TERMINAL,
    owner: str | None = "run1",
    reason: str = "test",
    size: int | None = 100,
    reclaimable: int | None = 100,
    consumers: tuple[Consumer, ...] = (),
    detail: dict[str, str] | None = None,
) -> InventoryItem:
    return InventoryItem(
        artifact_class=artifact_class,
        reference=reference,
        identity=reference,
        owner=owner,
        ownership=ownership,
        reason=reason,
        size_bytes=size,
        reclaimable_bytes=reclaimable,
        consumers=consumers,
        detail=detail or {},
    )


def _inventory(repo: Path, items: tuple[InventoryItem, ...]) -> Inventory:
    return Inventory(
        generated_at=_NOW, repo_root=str(repo.resolve()), project_id=None, items=items
    )


# --------------------------------------------------------------------------
# preview no-op and policy gating
# --------------------------------------------------------------------------


def test_preview_changes_nothing_on_disk(tmp_path: Path) -> None:
    """Covers: §Acceptance -- dry-run is a true no-op."""
    repo = _repo(tmp_path)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    wal = Path(str(db_path) + "-wal")
    before_mtime = db_path.stat().st_mtime_ns
    before_stat = wt.stat().st_mtime_ns

    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())

    assert preview.candidates  # something is projected
    assert db_path.stat().st_mtime_ns == before_mtime
    assert wt.exists() and wt.stat().st_mtime_ns == before_stat
    if not wal.exists():
        assert not wal.exists()
    assert db.list_storage_events() == []


def test_no_policy_authorizes_nothing(tmp_path: Path) -> None:
    """Covers: absent standing policy stays report-only."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=None)
    assert preview.candidates == ()
    assert any(e.reason == "no-standing-policy" for e in preview.exclusions)
    events, sink = _audit_sink()
    result = apply_gc(preview, (), db=db, repo_root=repo, audit=sink)
    assert result.applied == ()
    assert events == []


def test_pending_review_worktree_is_never_a_candidate(tmp_path: Path) -> None:
    """Covers: a candidate awaiting review keeps its worktree."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    base = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id="run1",
            task="gc",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )
    db.create_attempt(
        Attempt(
            id="run1-a1",
            run_id="run1",
            attempt_number=1,
            provider="p",
            model="m",
            worktree_path=str(wt),
            branch_name="orch/run1/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_CREATED,
            base_sha=base,
        )
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
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())
    assert preview.candidates == ()
    assert any(e.reference == str(wt.resolve()) for e in preview.exclusions)


# --------------------------------------------------------------------------
# exact Git retirement
# --------------------------------------------------------------------------


def test_terminal_worktree_and_branch_retire_by_native_git(tmp_path: Path) -> None:
    """Covers: worktree remove then branch -d, no force."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    sha = _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())
    actions = {c.action for c in preview.candidates}
    assert GcAction.REMOVE_WORKTREE in actions and GcAction.DELETE_BRANCH in actions

    events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert [o.reason for o in result.applied] == ["worktree-removed", "branch-deleted"]
    assert not wt.exists()
    assert GitManager.branch_tip(repo, "orch/run1/a1") is None
    # Immutable SHA survives: the commit is still reachable.
    assert GitManager.commit_exists(repo, sha)
    phases = [e.phase for e in events]
    assert phases == ["intent", "result", "intent", "result"]


def test_nondefault_target_branch_is_handled(tmp_path: Path) -> None:
    """Covers: a non-``main`` target retires cleanly."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "develop")
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "dev", env=_AUTHOR)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert len(result.applied) == 2
    assert not wt.exists()


def test_already_missing_and_repeat_invocation_are_idempotent(tmp_path: Path) -> None:
    """Covers: §Acceptance -- interrupted/repeat cleanup is safe."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())
    worktree_candidate = next(
        c for c in preview.candidates if c.action is GcAction.REMOVE_WORKTREE
    )
    _events, sink = _audit_sink()
    # Apply only the worktree step, simulating a crash before the branch step.
    first = apply_gc(preview, [worktree_candidate], db=db, repo_root=repo, audit=sink)
    assert first.applied[0].reason == "worktree-removed"
    assert GitManager.branch_tip(repo, "orch/run1/a1") is not None
    # Re-running the same preview resumes the branch step without error.
    second = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert any(o.reason == "already-missing" for o in second.applied)
    assert GitManager.branch_tip(repo, "orch/run1/a1") is None


# --------------------------------------------------------------------------
# revalidation refusals (synthetic inventory on a real repository)
# --------------------------------------------------------------------------


def _worktree_item_with_consumer(repo: Path, wt: Path, consumer: Consumer) -> InventoryItem:
    return _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(wt.resolve()),
        ownership=OwnershipState.OWNED_LIVE,
        consumers=(consumer,),
        detail={
            "run_id": "run1",
            "run_status": "COMPLETED",
            "attempt_terminal": "1",
            "candidate_state": "REJECTED",
        },
    )


def test_dirty_worktree_is_refused(tmp_path: Path) -> None:
    """Covers: dirty worktrees are preserved."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    (wt / "a.txt").write_text("changed\n", encoding="utf-8")
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(wt.resolve()),
        detail={"candidate_state": "REJECTED", "attempt_terminal": "1", "run_status": "COMPLETED"},
    )
    inventory = _inventory(repo, (item,))
    preview = preview_gc(inventory, policy=_approved())
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.applied == ()
    assert result.refused[0].reason == "dirty-worktree"
    assert wt.exists()


def test_unique_ignored_content_is_refused(tmp_path: Path) -> None:
    """Covers: unique ignored content keeps the worktree."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ignore", env=_AUTHOR)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    (wt / "build").mkdir()
    (wt / "build" / "artifact.bin").write_text("unique\n", encoding="utf-8")
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(wt.resolve()),
        detail={"candidate_state": "REJECTED", "attempt_terminal": "1", "run_status": "COMPLETED"},
    )
    preview = preview_gc(_inventory(repo, (item,)), policy=_approved())
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.refused[0].reason == "unique-ignored-content"
    assert wt.exists()


def test_symlinked_worktree_path_is_refused(tmp_path: Path) -> None:
    """Covers: never follow a symlink into user data."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    base = GitManager.get_default_worktree_base(repo)
    base.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "user-data"
    real.mkdir()
    link = base / "run1"
    link.symlink_to(real)
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(link.resolve()),
        detail={"candidate_state": "REJECTED", "attempt_terminal": "1", "run_status": "COMPLETED"},
    )
    preview = preview_gc(
        _inventory(repo, (item,)), policy=_approved(), owned_roots=(str(tmp_path),)
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.refused[0].reason == "outside-worktree-base"
    assert real.exists()


def test_candidate_outside_worktree_base_is_refused(tmp_path: Path) -> None:
    """Covers: outside-root paths are never eligible."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(outside.resolve()),
        detail={"candidate_state": "REJECTED", "attempt_terminal": "1", "run_status": "COMPLETED"},
    )
    preview = preview_gc(
        _inventory(repo, (item,)), policy=_approved(), owned_roots=(str(tmp_path),)
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.refused[0].reason == "outside-worktree-base"


def test_branch_tip_change_after_preview_is_refused(tmp_path: Path) -> None:
    """Covers: a moved branch tip fails closed."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    _git(repo, "branch", "orch/run1/a1")
    item = _item(
        ArtifactClass.LOCAL_BRANCH,
        "orch/run1/a1",
        ownership=OwnershipState.OWNED_TERMINAL,
        detail={"registered": "1", "run_id": "run1", "approved_target_ref": ""},
    )
    preview = preview_gc(_inventory(repo, (item,)), policy=_approved())
    assert preview.candidates
    # Move the tip after the preview was computed.
    (repo / "c.txt").write_text("z\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move", env=_AUTHOR)
    _git(repo, "branch", "-f", "orch/run1/a1", "HEAD", env=_AUTHOR)
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.refused[0].reason == "branch-tip-changed"
    assert GitManager.branch_tip(repo, "orch/run1/a1") is not None


def test_selection_widening_a_preview_is_refused(tmp_path: Path) -> None:
    """Covers: apply cannot widen a stale preview."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    preview = preview_gc(_inventory(repo, ()), policy=_approved())
    rogue = GcCandidate(
        artifact_class=ArtifactClass.LOCAL_BRANCH,
        action=GcAction.DELETE_BRANCH,
        reference="orch/rogue/a9",
        identity="orch/rogue/a9",
        owner="run9",
        reason="branch-retirement",
        estimated_bytes=0,
        reclaimable=True,
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, [rogue], db=db, repo_root=repo, audit=sink)
    assert result.refused[0].reason == "selection-widens-preview"


def test_audit_failure_surfaces_uncertainty_and_stops(tmp_path: Path) -> None:
    """Covers: removal with failed audit is uncertain."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved())

    def failing_sink(event: object) -> None:
        if getattr(event, "phase", None) == "result":
            raise RuntimeError("disk full writing audit")

    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=failing_sink)
    assert result.audit_uncertain is True
    assert result.applied[-1].reason == "audit-uncertain"


# --------------------------------------------------------------------------
# policy validation and retention rules
# --------------------------------------------------------------------------


def test_evidence_deletion_requires_a_settled_anchor(tmp_path: Path) -> None:
    """Covers: retention-anchor caveat -- no assumed anchor."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps({"approved": True, "allow_evidence_deletion": True}), encoding="utf-8"
    )
    with pytest.raises(GcPolicyError):
        load_gc_policy(policy_file)


def test_default_policy_disables_evidence_deletion(tmp_path: Path) -> None:
    """Covers: evidence deletion stays disabled by default."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"approved": True}), encoding="utf-8")
    policy = load_gc_policy(policy_file)
    assert policy.approved and not policy.allow_evidence_deletion


def test_corrupt_policy_fails_closed(tmp_path: Path) -> None:
    """Covers: corrupt/unknown policy is never silently adopted."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(GcPolicyError):
        load_gc_policy(policy_file)
    empty_file = tmp_path / "empty.json"
    empty_file.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(GcPolicyError):
        load_gc_policy(empty_file)


def test_unknown_owner_is_never_a_candidate(tmp_path: Path) -> None:
    """Covers: unknown ownership fails closed to KEEP."""
    repo = _repo(tmp_path)
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str((GitManager.get_default_worktree_base(repo) / "x").resolve()),
        ownership=OwnershipState.UNKNOWN,
        owner=None,
        reason="unregistered-worktree",
        detail={"candidate_state": "unknown"},
    )
    preview = preview_gc(_inventory(repo, (item,)), policy=_approved())
    assert preview.candidates == ()


def _preview_refs(
    repo: Path, *items: InventoryItem, **policy_overrides: object
) -> tuple[str, ...]:
    """Preview ``items`` under an approved policy confined to ``repo``."""
    preview = preview_gc(
        _inventory(repo, items),
        policy=_approved(**policy_overrides),
        owned_roots=(str(repo.resolve()),),
        now=_NOW,
    )
    return tuple(sorted(candidate.reference for candidate in preview.candidates))


def _worktree_item(repo: Path, **detail: str) -> InventoryItem:
    values = {
        "candidate_state": "ACCEPTED",
        "attempt_terminal": "1",
        "preempted": "0",
        "run_status": "COMPLETED",
    }
    values.update(detail)
    return _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(repo / "worktrees" / "attempt-7"),
        detail=values,
    )


def test_worktree_retention_requires_a_terminal_candidate(tmp_path: Path) -> None:
    """Covers: worktree retention (pending/preempted/unknown kept)."""
    repo = _repo(tmp_path)
    path = str(repo / "worktrees" / "attempt-7")
    assert _preview_refs(repo, _worktree_item(repo)) == (path,)
    for detail in (
        {"candidate_state": state} for state in ("READY", "IN_REVIEW", "REVIEWED", "unknown")
    ):
        assert _preview_refs(repo, _worktree_item(repo, **detail)) == ()
    assert _preview_refs(repo, _worktree_item(repo, attempt_terminal="0")) == ()
    assert _preview_refs(repo, _worktree_item(repo, candidate_state="CLEANED")) == ()
    preempted = _worktree_item(repo, preempted="1", run_status="PREEMPTED")
    assert _preview_refs(repo, preempted) == ()
    preempted_terminal = _worktree_item(repo, preempted="1", run_status="COMPLETED")
    assert _preview_refs(repo, preempted_terminal) == (path,)
    live = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        path,
        ownership=OwnershipState.OWNED_LIVE,
        detail={"candidate_state": "ACCEPTED", "attempt_terminal": "1"},
    )
    assert _preview_refs(repo, live) == ()


def test_checkpoint_retention_is_job_bound_and_time_bounded(tmp_path: Path) -> None:
    """Covers: checkpoint refs kept until 7d after their own job is terminal."""
    repo = _repo(tmp_path)
    ref = "refs/saberops/checkpoints/job-9/3"
    old = (_NOW - timedelta(days=8)).isoformat()
    recent = (_NOW - timedelta(days=6)).isoformat()

    def checkpoint(**detail: str) -> InventoryItem:
        values = {"job_id": "job-9", "job_terminal": "1", "terminal_at": old}
        values.update(detail)
        return _item(ArtifactClass.CHECKPOINT_REF, ref, detail=values)

    policy = {"allow_checkpoint_deletion": True}
    assert _preview_refs(repo, checkpoint(), **policy) == (ref,)
    assert _preview_refs(repo, checkpoint(terminal_at=recent), **policy) == ()
    assert _preview_refs(repo, checkpoint(terminal_at=""), **policy) == ()
    assert (
        _preview_refs(repo, checkpoint(), allow_checkpoint_deletion=False) == ()
    )
    assert _preview_refs(repo, checkpoint(job_terminal="0"), **policy) == ()
    assert _preview_refs(repo, checkpoint(job_id="job-other"), **policy) == ()
    escaped = _item(
        ArtifactClass.CHECKPOINT_REF,
        "refs/saberops/checkpoints/../../escape/job-9/3",
        detail={"job_id": "job-9", "job_terminal": "1", "terminal_at": old},
    )
    assert _preview_refs(repo, escaped, **policy) == ()


def test_evidence_retention_windows(tmp_path: Path) -> None:
    """Covers: evidence compress after 7d; delete 90d accepted / 30d other."""
    repo = _repo(tmp_path)
    path = str(repo / "evidence" / "run-1")

    def evidence(**detail: str) -> InventoryItem:
        values = {
            "created_at": (_NOW - timedelta(days=31)).isoformat(),
            "terminal_at": (_NOW - timedelta(days=31)).isoformat(),
            "run_accepted": "0",
            "compressed": "1",
        }
        values.update(detail)
        return _item(ArtifactClass.EVIDENCE, path, detail=values)

    deferred = {"allow_evidence_deletion": True, "evidence_anchor": "terminal"}

    def refs(item: InventoryItem, **policy: object) -> tuple[str, ...]:
        return _preview_refs(repo, item, **policy)

    # Terminal evidence is deleted at 30 days (other) / 90 days (accepted).
    assert refs(evidence(), **deferred) == (path,)
    assert refs(evidence(run_accepted="1"), **deferred) == ()
    accepted_old = evidence(
        run_accepted="1", terminal_at=(_NOW - timedelta(days=91)).isoformat()
    )
    assert refs(accepted_old, **deferred) == (path,)
    assert refs(evidence(terminal_at=(_NOW - timedelta(days=29)).isoformat()), **deferred) == ()
    assert refs(evidence(terminal_at=(_NOW + timedelta(days=1)).isoformat()), **deferred) == ()
    assert refs(evidence(terminal_at=""), **deferred) == ()
    # Deletion stays held without a settled anchor...
    assert refs(evidence()) == ()
    # ...while compression is non-destructive and anchored at creation.
    compress = {"allow_evidence_compression": True}
    uncompressed = evidence(
        compressed="0", terminal_at=(_NOW - timedelta(days=29)).isoformat()
    )
    assert refs(uncompressed, **compress) == (path,)
    assert refs(evidence(compressed="0", created_at=(_NOW - timedelta(days=6)).isoformat())) == ()
    assert refs(evidence(compressed="0"), allow_evidence_compression=False) == ()
    live = _item(
        ArtifactClass.EVIDENCE,
        path,
        ownership=OwnershipState.OWNED_LIVE,
        detail={"created_at": (_NOW - timedelta(days=31)).isoformat(), "compressed": "0"},
    )
    assert refs(live, **compress) == ()


def test_venv_follows_its_parent_worktree(tmp_path: Path) -> None:
    """Covers: venvs die with their worktree, never alone."""
    repo = _repo(tmp_path)
    path = str(repo / "worktrees" / "attempt-7" / ".venv")

    def venv(**detail: str) -> InventoryItem:
        values = {"parent_worktree_live": "0", "parent_eligible": "1"}
        values.update(detail)
        return _item(ArtifactClass.WORKTREE_VENV, path, detail=values)

    assert _preview_refs(repo, venv()) == (path,)
    assert _preview_refs(repo, venv(parent_worktree_live="1")) == ()
    assert _preview_refs(repo, venv(parent_eligible="0")) == ()


def test_owner_db_vacuum_needs_policy(tmp_path: Path) -> None:
    """Covers: the owner DB is only ever a VACUUM candidate, never a deletion."""
    repo = _repo(tmp_path)
    path = str(repo / "orch.db")
    item = _item(ArtifactClass.OWNER_DB, path, owner="engine")
    assert _preview_refs(repo, item) == ()
    preview = preview_gc(
        _inventory(repo, (item,)),
        policy=_approved(allow_vacuum=True),
        owned_roots=(str(repo.resolve()),),
        now=_NOW,
    )
    assert [(c.action, c.reference) for c in preview.candidates] == [
        (GcAction.VACUUM_DB, path)
    ]


def test_paths_outside_owned_roots_are_never_candidates(tmp_path: Path) -> None:
    """Covers: outside-root, traversal and root-itself escapes fail closed."""
    repo = _repo(tmp_path)
    outside = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        "/etc/saberops-worktree",
        detail={"candidate_state": "ACCEPTED", "attempt_terminal": "1"},
    )
    traversal = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(repo / ".." / "escape"),
        detail={"candidate_state": "ACCEPTED", "attempt_terminal": "1"},
    )
    at_root = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(repo.resolve()),
        detail={"candidate_state": "ACCEPTED", "attempt_terminal": "1"},
    )
    assert _preview_refs(repo, outside, traversal, at_root) == ()


def test_scratch_needs_a_provably_dead_owner_inside_the_roots(tmp_path: Path) -> None:
    """Covers: only dead-owned scratch under the owned roots is eligible."""
    repo = _repo(tmp_path)
    path = str(repo / "gate-scratch" / "run-1")
    dead = _item(ArtifactClass.GATE_SCRATCH, path)
    live = _item(ArtifactClass.GATE_SCRATCH, path, ownership=OwnershipState.OWNED_LIVE)
    outside = _item(ArtifactClass.GATE_SCRATCH, "/dev/shm/saberops-gate-x")
    policy = {"allow_scratch_deletion": True}
    assert _preview_refs(repo, dead, **policy) == (path,)
    assert _preview_refs(repo, live, **policy) == ()
    assert _preview_refs(repo, outside, **policy) == ()
    assert _preview_refs(repo, dead) == ()


# --------------------------------------------------------------------------
# legacy compatibility route
# --------------------------------------------------------------------------


def test_legacy_cleanup_has_no_prune_and_keeps_dirty(tmp_path: Path) -> None:
    """Covers: no repo-wide prune / force escape."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    clean = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    dirty = GitManager.get_default_worktree_base(repo) / "run1" / "a2"
    GitManager.create_worktree(repo, "orch/run1/a1", clean)
    GitManager.create_worktree(repo, "orch/run1/a2", dirty)
    base_sha = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id="run1",
            task="gc",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )
    for index, path in ((1, clean), (2, dirty)):
        db.create_attempt(
            Attempt(
                id=f"run1-a{index}",
                run_id="run1",
                attempt_number=index,
                provider="p",
                model="m",
                worktree_path=str(path),
                branch_name=f"orch/run1/a{index}",
                status=AttemptStatus.SUCCESS,
                commit_sha=base_sha,
                created_at=_CREATED,
                base_sha=base_sha,
            )
        )
    (dirty / "a.txt").write_text("dirty\n", encoding="utf-8")
    events, sink = _audit_sink()
    result = retire_run_worktrees(db, repo_root=repo, run_id="run1", audit=sink)
    assert str(clean.resolve()) in result["removed"]
    assert str(dirty.resolve()) in result["skipped_dirty"]
    assert dirty.exists()
    assert len(events) == 1  # one result event for the removed worktree


def test_legacy_cleanup_refuses_running_run(tmp_path: Path) -> None:
    """Covers: a running run is never cleaned."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    base_sha = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id="run1",
            task="gc",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=_CREATED,
            completed_at=None,
        )
    )
    with pytest.raises(RuntimeError, match="RUNNING"):
        retire_run_worktrees(db, repo_root=repo, run_id="run1")


# --------------------------------------------------------------------------
# F2 -- intent vs result vs failure audit semantics
# --------------------------------------------------------------------------


def test_intent_audit_failure_mutates_nothing_and_is_not_applied(tmp_path: Path) -> None:
    """Covers: no durable intent => no action/claim."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    preview = preview_gc(build_inventory(db, repo_root=repo), policy=_approved())

    def failing_intent(event: object) -> None:
        if getattr(event, "phase", None) == "intent":
            raise RuntimeError("intent cannot be persisted")

    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=failing_intent)
    assert result.applied == ()
    assert result.refused and result.refused[0].reason == "audit-intent-failed"
    assert wt.exists()
    assert GitManager.branch_tip(repo, "orch/run1/a1") is not None
    assert db.list_storage_events() == []


def test_result_audit_failure_after_action_is_uncertain_not_silent(tmp_path: Path) -> None:
    """Covers: action done + audit lost => uncertainty."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    preview = preview_gc(build_inventory(db, repo_root=repo), policy=_approved())

    def failing_result(event: object) -> None:
        if getattr(event, "phase", None) == "result":
            raise RuntimeError("audit result cannot be persisted")

    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=failing_result)
    assert result.audit_uncertain is True
    assert result.applied and result.applied[0].reason == "audit-uncertain"
    # The worktree action did run; the loop stopped before the branch step.
    assert not wt.exists()


def test_refused_action_with_audit_failure_stays_refused(tmp_path: Path) -> None:
    """Covers: a refusal is never relabelled applied."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    (wt / "a.txt").write_text("dirty\n", encoding="utf-8")
    item = _item(
        ArtifactClass.ATTEMPT_WORKTREE,
        str(wt.resolve()),
        detail={"candidate_state": "REJECTED", "attempt_terminal": "1", "run_status": "COMPLETED"},
    )
    preview = preview_gc(_inventory(repo, (item,)), policy=_approved())

    def failing_failure(event: object) -> None:
        if getattr(event, "phase", None) == "failure":
            raise RuntimeError("failure audit cannot be persisted")

    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=failing_failure)
    assert result.applied == ()
    assert result.refused and result.refused[0].reason == "audit-uncertain"
    assert result.audit_uncertain is True
    assert wt.exists()


# --------------------------------------------------------------------------
# F3 -- canonical preview authorization (action + authority attributes)
# --------------------------------------------------------------------------


def _preview_with_worktree(tmp_path: Path) -> tuple[Path, Database, Path, GcPreview]:
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo=repo, run_id="run1", wt=wt, branch="orch/run1/a1")
    preview = preview_gc(build_inventory(db, repo_root=repo), policy=_approved())
    return repo, db, wt, preview


def test_tampered_action_is_refused_not_executed(tmp_path: Path) -> None:
    """Covers: same class/ref/identity is not permission."""
    repo, db, wt, preview = _preview_with_worktree(tmp_path)
    original = preview.candidates[0]
    forged = GcCandidate(
        artifact_class=original.artifact_class,
        action=GcAction.COMPRESS_EVIDENCE,
        reference=original.reference,
        identity=original.identity,
        owner=original.owner,
        reason=original.reason,
        estimated_bytes=original.estimated_bytes,
        reclaimable=original.reclaimable,
        detail=original.detail,
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, [forged], db=db, repo_root=repo, audit=sink)
    assert result.applied == ()
    assert result.refused[0].reason == "selection-widens-preview"
    assert wt.exists()


def test_tampered_detail_is_refused(tmp_path: Path) -> None:
    """Covers: authority-relevant data is pinned."""
    repo, db, wt, preview = _preview_with_worktree(tmp_path)
    original = preview.candidates[0]
    forged = GcCandidate(
        artifact_class=original.artifact_class,
        action=original.action,
        reference=original.reference,
        identity=original.identity,
        owner=original.owner,
        reason=original.reason,
        estimated_bytes=original.estimated_bytes,
        reclaimable=original.reclaimable,
        detail={**dict(original.detail), "candidate_state": "ACCEPTED"},
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, [forged], db=db, repo_root=repo, audit=sink)
    assert result.applied == ()
    assert result.refused[0].reason == "selection-widens-preview"
    assert wt.exists()


def test_duplicate_selection_is_refused_once(tmp_path: Path) -> None:
    """Covers: a repeated target is not re-executed."""
    repo, db, wt, preview = _preview_with_worktree(tmp_path)
    candidate = preview.candidates[0]
    _events, sink = _audit_sink()
    result = apply_gc(preview, [candidate, candidate], db=db, repo_root=repo, audit=sink)
    assert len(result.applied) == 1
    assert any(outcome.reason == "duplicate-selection" for outcome in result.refused)
    assert not wt.exists()


# --------------------------------------------------------------------------
# F7 -- safe evidence compressor
# --------------------------------------------------------------------------


def _evidence_item(repo: Path, path: Path, *, age_days: int) -> InventoryItem:
    created = datetime(2026, 10, 17, 12, 0, tzinfo=UTC) - timedelta(days=age_days)
    return _item(
        ArtifactClass.EVIDENCE,
        str(path.resolve()),
        ownership=OwnershipState.OWNED_TERMINAL,
        owner="run1",
        size=100,
        reclaimable=100,
        detail={
            "run_id": "run1",
            "run_status": "COMPLETED",
            "run_accepted": "0",
            "compressed": "0",
            "created_at": created.isoformat(),
            "terminal_at": created.isoformat(),
        },
    )


def test_evidence_compression_round_trips_and_is_atomic(tmp_path: Path) -> None:
    """Covers: evidence compression preserves readable content."""
    import tarfile

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "gate.log").write_text("gate output\nline two\n", encoding="utf-8")
    (bundle / "receipt.json").write_text('{"ok": true}\n', encoding="utf-8")
    item = _evidence_item(repo, bundle, age_days=30)
    policy = _approved(allow_evidence_compression=True)
    preview = preview_gc(
        _inventory(repo, (item,)), policy=policy, owned_roots=(str(tmp_path),)
    )
    assert [c.action for c in preview.candidates] == [GcAction.COMPRESS_EVIDENCE]
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.applied and result.applied[0].reason == "evidence-compressed"
    assert not bundle.exists()
    archive = tmp_path / "bundle.tar.gz"
    assert archive.exists()
    with tarfile.open(archive, "r:gz") as tar:
        names = sorted(tar.getnames())
        assert names == ["gate.log", "receipt.json"]
        extracted = tar.extractfile("gate.log")
        assert extracted is not None
        assert extracted.read().decode() == "gate output\nline two\n"


def test_evidence_compression_is_idempotent(tmp_path: Path) -> None:
    """Covers: a repeated compression reports already-compressed."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    bundle = tmp_path / "bundle2"
    bundle.mkdir()
    (bundle / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "bundle2.tar.gz").write_text("exists", encoding="utf-8")
    item = _evidence_item(repo, bundle, age_days=30)
    policy = _approved(allow_evidence_compression=True)
    preview = preview_gc(
        _inventory(repo, (item,)), policy=policy, owned_roots=(str(tmp_path),)
    )
    _events, sink = _audit_sink()
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=sink)
    assert result.applied and result.applied[0].reason == "already-compressed"
    assert bundle.exists()


def test_evidence_deletion_remains_held_without_anchor(tmp_path: Path) -> None:
    """Covers: no evidence deletion without a settled anchor."""
    repo = _repo(tmp_path)
    bundle = tmp_path / "bundle3"
    bundle.mkdir()
    (bundle / "a.log").write_text("x", encoding="utf-8")
    item = _evidence_item(repo, bundle, age_days=400)
    policy = _approved(allow_evidence_deletion=True, evidence_anchor=None)
    preview = preview_gc(
        _inventory(repo, (item,)), policy=policy, owned_roots=(str(tmp_path),)
    )
    assert preview.candidates == ()


def test_owned_cache_is_excluded_and_forged_apply_refused(tmp_path: Path) -> None:
    """Covers: owned caches surface bytes but never delete."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    cache = tmp_path / "saberops-mypy-cache"
    cache.mkdir()
    (cache / "probe.txt").write_text("cache\n", encoding="utf-8")
    item = _item(
        ArtifactClass.OWNED_CACHE,
        str(cache.resolve()),
        ownership=OwnershipState.OWNED_LIVE,
        owner="engine",
        reason="shared-engine-cache",
        size=123,
        reclaimable=None,
        consumers=(
            Consumer(
                kind="engine-cache",
                ref=str(cache.resolve()),
                state="LIVE",
                protected=True,
                reason="shared-engine-cache",
            ),
        ),
        detail={"cache_kind": "saberops-mypy-cache", "hardlinked_files": "0"},
    )
    policy = _approved()
    preview = preview_gc(
        _inventory(repo, (item,)), policy=policy, owned_roots=(str(tmp_path),)
    )
    assert preview.candidates == ()
    assert any(
        e.reference == str(cache.resolve()) and e.reason == "shared-cache-protected"
        for e in preview.exclusions
    )
    forged = GcCandidate(
        artifact_class=ArtifactClass.OWNED_CACHE,
        action=GcAction.DELETE_PATH,
        reference=str(cache.resolve()),
        identity=str(cache.resolve()),
        owner="engine",
        reason="shared-engine-cache",
        estimated_bytes=None,
        reclaimable=False,
        detail={"cache_kind": "saberops-mypy-cache"},
    )
    _events, sink = _audit_sink()
    result = apply_gc(
        preview, (forged,), db=db, repo_root=repo, audit=sink
    )
    assert result.applied == ()
    assert cache.exists()
    assert (cache / "probe.txt").exists()
    # Even a preview that illegally contains an OWNED_CACHE candidate refuses.
    from saberops.storage_cleanup import GcPreview as _GcPreview

    poisoned = _GcPreview(
        generated_at=preview.generated_at,
        repo_root=preview.repo_root,
        project_id=preview.project_id,
        policy_ref=preview.policy_ref,
        approved=True,
        scope_classes=(ArtifactClass.OWNED_CACHE,),
        candidates=(forged,),
        exclusions=(),
        estimated_reclaimable_bytes=0,
        preview_id=preview.preview_id,
    )
    _events2, sink2 = _audit_sink()
    poisoned_result = apply_gc(
        poisoned, (forged,), db=db, repo_root=repo, audit=sink2
    )
    assert poisoned_result.applied == ()
    assert poisoned_result.refused
    assert poisoned_result.refused[0].reason == "shared-cache-protected"
    assert cache.exists()


# --------------------------------------------------------------------------
# F6 -- opt-in cleanup of clean, verified-landed worktrees
# --------------------------------------------------------------------------


def _commit_in_worktree(repo: Path, wt: Path, branch: str, text: str) -> str:
    GitManager.create_worktree(repo, branch, wt)
    (wt / "a.txt").write_text(f"{text}\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", text, env=_AUTHOR)
    return _git(wt, "rev-parse", "HEAD")


def _seed_attempt_run(
    db: Database,
    *,
    repo: Path,
    run_id: str,
    wt: Path,
    branch: str,
    base_sha: str,
    candidate: str,
    accepted: bool,
    status: RunStatus = RunStatus.COMPLETED,
    created_at: str = _CREATED,
    completed_at: str | None = _CREATED,
) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="landed-cleanup",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
            created_at=created_at,
            completed_at=completed_at,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="p",
            model="m",
            worktree_path=str(wt),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=created_at,
            base_sha=base_sha,
        )
    )
    db.create_check(
        CheckResult(
            id=f"{run_id}-c1",
            attempt_id=f"{run_id}-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=created_at,
        )
    )
    if accepted:
        final_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        db.record_event(
            run_id,
            "run_accepted",
            payload={
                "candidate_sha": candidate,
                "final_branch": final_branch,
                "gate_command": "true",
                "provenance": {
                    "candidate_sha": candidate,
                    "run_id": run_id,
                    "attempt_id": f"{run_id}-a1",
                    "attempt_number": 1,
                },
            },
        )


def test_landed_cleanup_removes_only_verified_landed_clean_worktrees(
    tmp_path: Path,
) -> None:
    """F6: landed clean copies go; dirty, unlanded and failed copies stay."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    base = GitManager.get_default_worktree_base(repo)

    landed_wt = base / "run_landed" / "a1"
    landed = _commit_in_worktree(repo, landed_wt, "orch/run_landed/a1", "landed")
    _git(repo, "merge", "--ff-only", "orch/run_landed/a1")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_landed",
        wt=landed_wt,
        branch="orch/run_landed/a1",
        base_sha=_git(repo, "rev-parse", "HEAD~1"),
        candidate=landed,
        accepted=True,
    )

    dirty_wt = base / "run_dirty" / "a1"
    dirty = _commit_in_worktree(repo, dirty_wt, "orch/run_dirty/a1", "dirty")
    _git(repo, "merge", "--ff-only", "orch/run_dirty/a1")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_dirty",
        wt=dirty_wt,
        branch="orch/run_dirty/a1",
        base_sha=_git(repo, "rev-parse", "HEAD~1"),
        candidate=dirty,
        accepted=True,
    )
    (dirty_wt / "a.txt").write_text("uncommitted change\n", encoding="utf-8")

    unlanded_wt = base / "run_unlanded" / "a1"
    unlanded = _commit_in_worktree(repo, unlanded_wt, "orch/run_unlanded/a1", "unlanded")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_unlanded",
        wt=unlanded_wt,
        branch="orch/run_unlanded/a1",
        base_sha=GitManager.get_head_commit(repo),
        candidate=unlanded,
        accepted=True,
    )

    failed_wt = base / "run_failed" / "a1"
    failed = _commit_in_worktree(repo, failed_wt, "orch/run_failed/a1", "failed")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_failed",
        wt=failed_wt,
        branch="orch/run_failed/a1",
        base_sha=GitManager.get_head_commit(repo),
        candidate=failed,
        accepted=False,
    )

    events, sink = _audit_sink()
    report = retire_landed_worktrees(db, repo_root=repo, audit=sink)

    assert report.removed == (str(landed_wt.resolve()),)
    assert not landed_wt.exists()
    assert dirty_wt.exists()
    assert unlanded_wt.exists()
    assert failed_wt.exists()
    assert report.scanned_runs == 4
    assert report.retention_days == LANDED_CLEANUP_RETENTION_DAYS
    reasons = {kept.path: kept.reason for kept in report.kept}
    assert reasons[str(dirty_wt.resolve())] == "dirty"
    assert reasons[str(unlanded_wt.resolve())] == "candidate-not-landed"
    assert reasons[str(failed_wt.resolve())].startswith("candidate-not-accepted:")
    assert all(kept.beyond_retention for kept in report.kept)
    assert [event.reason for event in events] == ["landed-cleanup-removed"]
    assert events[0].run_id == "run_landed"


def test_landed_cleanup_keeps_active_run_and_reports_retention(tmp_path: Path) -> None:
    """F6: a still-active run is never cleaned; the report carries the age bound."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run_active" / "a1"
    candidate = _commit_in_worktree(repo, wt, "orch/run_active/a1", "active")
    _git(repo, "merge", "--ff-only", "orch/run_active/a1")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_active",
        wt=wt,
        branch="orch/run_active/a1",
        base_sha=_git(repo, "rev-parse", "HEAD~1"),
        candidate=candidate,
        accepted=True,
        status=RunStatus.RUNNING,
        completed_at=None,
    )

    report = retire_landed_worktrees(db, repo_root=repo)

    assert report.removed == ()
    assert wt.exists()
    assert len(report.kept) == 1
    assert report.kept[0].reason == "run-active"
    assert report.kept[0].beyond_retention is True
    assert report.retention_days == LANDED_CLEANUP_RETENTION_DAYS


def test_cli_landed_cleanup_reports_what_it_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F6: `orch cleanup --landed` is opt-in and reports removals and keeps."""
    from saberops.cli import main as cli_main

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    landed_wt = GitManager.get_default_worktree_base(repo) / "run_landed" / "a1"
    landed = _commit_in_worktree(repo, landed_wt, "orch/run_landed/a1", "landed")
    _git(repo, "merge", "--ff-only", "orch/run_landed/a1")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_landed",
        wt=landed_wt,
        branch="orch/run_landed/a1",
        base_sha=_git(repo, "rev-parse", "HEAD~1"),
        candidate=landed,
        accepted=True,
    )
    failed_wt = GitManager.get_default_worktree_base(repo) / "run_failed" / "a1"
    failed = _commit_in_worktree(repo, failed_wt, "orch/run_failed/a1", "failed")
    _seed_attempt_run(
        db,
        repo=repo,
        run_id="run_failed",
        wt=failed_wt,
        branch="orch/run_failed/a1",
        base_sha=GitManager.get_head_commit(repo),
        candidate=failed,
        accepted=False,
    )

    code = cli_main(
        ["cleanup", "--landed", "--repo", str(repo), "--db", str(db.db_path)]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "Removed clean landed worktree(s) (1)" in out
    assert "candidate-not-accepted:" in out
    assert "removed 1, kept 1" in out
    assert not landed_wt.exists()
    assert failed_wt.exists()
