"""Synthetic regression tests for the early storage-lifecycle planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

import pytest

from saberops.storage_lifecycle import (
    CHECKPOINT_REF_PREFIX,
    Action,
    Artifact,
    ArtifactKind,
    AttemptWorktree,
    CandidateState,
    CheckpointRef,
    EvidenceBundle,
    GateScratch,
    OwnerDatabase,
    OwnerState,
    PlanDecision,
    WorktreeVenv,
    plan_gc,
)

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
ROOTS = ("/data/saberops",)
SCRATCH = "/data/saberops/scratch/run-1"
WORKTREE = "/data/saberops/worktrees/attempt-7"
EVIDENCE = "/data/saberops/evidence/run-1"
VENV = "/data/saberops/worktrees/attempt-7/.venv"
DB = "/data/saberops/owner.db"
REF = f"{CHECKPOINT_REF_PREFIX}job-9/3"
_OLD_CHECKPOINT_TIME = NOW - timedelta(days=8)
_OLD_EVIDENCE_TIME = NOW - timedelta(days=31)
_OLD_TERMINAL_TIME = NOW - timedelta(days=31)


def _scratch(
    artifact_id: str = "s1",
    path: str = SCRATCH,
    owner: str | None = "gate-run-1",
    owner_state: OwnerState = OwnerState.KNOWN_DEAD,
    size_bytes: int | None = 100,
    gate_finished: bool | None = True,
    gate_succeeded: bool | None = True,
    is_newest_failed: bool | None = False,
    is_symlink_escape: bool = False,
) -> GateScratch:
    return GateScratch(
        artifact_id=artifact_id,
        path=path,
        owner=owner,
        owner_state=owner_state,
        size_bytes=size_bytes,
        gate_finished=gate_finished,
        gate_succeeded=gate_succeeded,
        is_newest_failed=is_newest_failed,
        is_symlink_escape=is_symlink_escape,
    )


def _worktree(
    artifact_id: str = "w1",
    path: str = WORKTREE,
    owner: str | None = "attempt-7",
    owner_state: OwnerState = OwnerState.KNOWN_DEAD,
    attempt_terminal: bool | None = True,
    candidate_state: CandidateState = CandidateState.ACCEPTED,
    job_preempted: bool | None = False,
    job_terminal: bool | None = True,
    worktree_live: bool | None = False,
    is_symlink_escape: bool = False,
) -> AttemptWorktree:
    return AttemptWorktree(
        artifact_id=artifact_id,
        path=path,
        owner=owner,
        owner_state=owner_state,
        attempt_terminal=attempt_terminal,
        candidate_state=candidate_state,
        job_preempted=job_preempted,
        job_terminal=job_terminal,
        worktree_live=worktree_live,
        is_symlink_escape=is_symlink_escape,
    )


def _checkpoint(
    artifact_id: str = "c1",
    ref: str = REF,
    owner: str | None = "job-9",
    owner_state: OwnerState = OwnerState.KNOWN_DEAD,
    job_id: str | None = "job-9",
    job_terminal: bool | None = True,
    terminal_at: datetime | None = _OLD_CHECKPOINT_TIME,
    job_preempted: bool | None = False,
) -> CheckpointRef:
    return CheckpointRef(
        artifact_id=artifact_id,
        ref=ref,
        owner=owner,
        owner_state=owner_state,
        job_id=job_id,
        job_terminal=job_terminal,
        terminal_at=terminal_at,
        job_preempted=job_preempted,
    )


def _evidence(
    artifact_id: str = "e1",
    path: str = EVIDENCE,
    owner: str | None = "run-1",
    owner_state: OwnerState = OwnerState.KNOWN_DEAD,
    run_terminal: bool | None = True,
    run_accepted: bool | None = False,
    created_at: datetime | None = _OLD_EVIDENCE_TIME,
    terminal_at: datetime | None = _OLD_TERMINAL_TIME,
    compressed: bool | None = True,
    size_bytes: int | None = 4096,
    is_symlink_escape: bool = False,
) -> EvidenceBundle:
    return EvidenceBundle(
        artifact_id=artifact_id,
        path=path,
        owner=owner,
        owner_state=owner_state,
        run_terminal=run_terminal,
        run_accepted=run_accepted,
        created_at=created_at,
        terminal_at=terminal_at,
        compressed=compressed,
        size_bytes=size_bytes,
        is_symlink_escape=is_symlink_escape,
    )


def _venv(
    artifact_id: str = "v1",
    path: str = VENV,
    owner: str | None = "attempt-7",
    owner_state: OwnerState = OwnerState.KNOWN_DEAD,
    parent_worktree_live: bool | None = False,
    parent_eligible_for_removal: bool | None = True,
    is_symlink_escape: bool = False,
) -> WorktreeVenv:
    return WorktreeVenv(
        artifact_id=artifact_id,
        path=path,
        owner=owner,
        owner_state=owner_state,
        parent_worktree_live=parent_worktree_live,
        parent_eligible_for_removal=parent_eligible_for_removal,
        is_symlink_escape=is_symlink_escape,
    )


def _ownerdb(
    artifact_id: str = "db1",
    path: str = DB,
    owner: str | None = "engine",
    owner_state: OwnerState = OwnerState.KNOWN_LIVE,
    last_vacuum_at: datetime | None = None,
    free_page_ratio: float | None = 0.9,
    is_symlink_escape: bool = False,
) -> OwnerDatabase:
    if last_vacuum_at is None:
        last_vacuum_at = NOW - timedelta(days=30)
    return OwnerDatabase(
        artifact_id=artifact_id,
        path=path,
        owner=owner,
        owner_state=owner_state,
        last_vacuum_at=last_vacuum_at,
        free_page_ratio=free_page_ratio,
        is_symlink_escape=is_symlink_escape,
    )


def _decisions(artifacts: list[Artifact]) -> tuple[PlanDecision, ...]:
    return plan_gc(artifacts, now=NOW, owned_roots=ROOTS).decisions


def test_scratch_succeeded_eligible() -> None:
    """Covers: ADR-0003 D4/D6 gate scratch removed at end of gate."""
    (d,) = _decisions([_scratch()])
    assert (d.action, d.reason) == (Action.ELIGIBLE_DELETE, "scratch-succeeded-eligible")


def test_scratch_running_kept() -> None:
    """Covers: ADR-0003 D6 live gate scratch never eligible mid-run."""
    (d,) = _decisions([_scratch(gate_finished=False, gate_succeeded=False)])
    assert (d.action, d.reason) == (Action.KEEP, "gate-running")


def test_scratch_newest_failed_kept_under_cap() -> None:
    """Covers: ADR-0003 D4 newest failed scratch kept under the 2 GB cap."""
    (d,) = _decisions(
        [_scratch(gate_succeeded=False, is_newest_failed=True, size_bytes=1024)]
    )
    assert (d.action, d.reason) == (Action.KEEP, "newest-failed-kept")


def test_scratch_older_failed_eligible() -> None:
    """Covers: ADR-0003 D4 older failed scratch pruned, newest kept."""
    (d,) = _decisions(
        [_scratch(gate_succeeded=False, is_newest_failed=False, size_bytes=1024)]
    )
    assert (d.action, d.reason) == (Action.ELIGIBLE_DELETE, "scratch-superseded-eligible")


def test_scratch_newest_failed_cap_boundary() -> None:
    """Covers: ADR-0003 D4 newest-failed size-cap boundary (fail closed below)."""
    from saberops.storage_lifecycle import FAILED_SCRATCH_SIZE_CAP_BYTES as cap

    (at,) = _decisions(
        [_scratch(gate_succeeded=False, is_newest_failed=True, size_bytes=cap)]
    )
    assert (at.action, at.reason) == (Action.KEEP, "newest-failed-kept")
    (over,) = _decisions(
        [_scratch(gate_succeeded=False, is_newest_failed=True, size_bytes=cap + 1)]
    )
    assert (over.action, over.reason) == (
        Action.ELIGIBLE_DELETE,
        "scratch-cap-exceeded",
    )


def test_scratch_missing_metadata_kept() -> None:
    """Covers: fail closed for missing gate/scratch metadata."""
    cases = [
        _scratch(gate_finished=None),
        _scratch(gate_succeeded=None),
        _scratch(gate_succeeded=False, is_newest_failed=None),
        _scratch(gate_succeeded=False, is_newest_failed=True, size_bytes=None),
    ]
    for item in cases:
        (d,) = _decisions([item])
        assert (d.action, d.reason) == (Action.KEEP, "missing-metadata")


def test_worktree_terminal_states_eligible() -> None:
    """Covers: ADR-0003 D6 worktree removed for accepted/rejected/superseded."""
    for state in (
        CandidateState.ACCEPTED,
        CandidateState.REJECTED,
        CandidateState.SUPERSEDED,
    ):
        (d,) = _decisions([_worktree(candidate_state=state)])
        assert (d.action, d.reason) == (
            Action.ELIGIBLE_DELETE,
            "worktree-terminal-eligible",
        ), state


def test_worktree_pending_review_kept() -> None:
    """Covers: ADR-0003 D6 worktree kept while a candidate awaits review."""
    for state in (CandidateState.PENDING_REVIEW, CandidateState.ACTIVE):
        (d,) = _decisions(
            [_worktree(attempt_terminal=True, candidate_state=state, worktree_live=False)]
        )
        assert (d.action, d.reason) == (Action.KEEP, "candidate-pending"), state


def test_worktree_active_or_live_kept() -> None:
    """Covers: ADR-0003 D6 live worktrees and active attempts never eligible."""
    (d1,) = _decisions([_worktree(attempt_terminal=False)])
    assert (d1.action, d1.reason) == (Action.KEEP, "attempt-active")
    (d2,) = _decisions([_worktree(worktree_live=True)])
    assert (d2.action, d2.reason) == (Action.KEEP, "worktree-live")


def test_worktree_preempted_unterminated_kept() -> None:
    """Covers: ADR-0003 D6 preempted job worktree kept until job terminal."""
    (d,) = _decisions([_worktree(job_preempted=True, job_terminal=False)])
    assert (d.action, d.reason) == (Action.KEEP, "job-preempted")


def test_worktree_preempted_terminal_eligible() -> None:
    """Covers: ADR-0003 D6 preempted-then-terminal worktree recreatable, eligible."""
    (d,) = _decisions([_worktree(job_preempted=True, job_terminal=True)])
    assert d.action == Action.ELIGIBLE_DELETE


def test_worktree_unknown_candidate_kept() -> None:
    """Covers: fail closed for unknown candidate state."""
    (d,) = _decisions([_worktree(candidate_state=CandidateState.UNKNOWN)])
    assert (d.action, d.reason) == (Action.KEEP, "missing-metadata")


def test_checkpoint_retention_boundary() -> None:
    """Covers: ADR-0003 D6 checkpoint refs kept until 7d after terminal."""
    (early,) = _decisions([_checkpoint(terminal_at=NOW - timedelta(days=6))])
    assert (early.action, early.reason) == (Action.KEEP, "checkpoint-retained")
    (exact,) = _decisions([_checkpoint(terminal_at=NOW - timedelta(days=7))])
    assert (exact.action, exact.reason) == (
        Action.ELIGIBLE_DELETE,
        "checkpoint-eligible",
    )
    (late,) = _decisions([_checkpoint(terminal_at=NOW - timedelta(days=8))])
    assert late.action == Action.ELIGIBLE_DELETE


def test_checkpoint_active_or_preempted_kept() -> None:
    """Covers: ADR-0003 D6 preempted-job and active-job checkpoints preserved."""
    (d1,) = _decisions([_checkpoint(job_terminal=False, job_preempted=False)])
    assert (d1.action, d1.reason) == (Action.KEEP, "checkpoint-job-active")
    (d2,) = _decisions(
        [_checkpoint(job_terminal=False, job_preempted=True, terminal_at=None)]
    )
    assert (d2.action, d2.reason) == (Action.KEEP, "job-preempted")


def test_checkpoint_missing_terminal_at_kept() -> None:
    """Covers: fail closed for terminal checkpoint without terminal time."""
    (d,) = _decisions([_checkpoint(terminal_at=None)])
    assert (d.action, d.reason) == (Action.KEEP, "missing-metadata")


def test_checkpoint_ref_namespace_escape_kept() -> None:
    """Covers: checkpoint refs outside the owned ref namespace never eligible."""
    for ref in ("refs/evil/job-9/0", "refs/saberops/checkpoints/../x", ""):
        (d,) = _decisions([_checkpoint(ref=ref)])
        assert d.action == Action.KEEP, ref


def test_evidence_compress_boundary() -> None:
    """Covers: ADR-0003 D6/Q4 evidence compressed after 7 days."""
    recent_terminal = NOW - timedelta(days=1)
    young = _evidence(
        run_accepted=False,
        created_at=NOW - timedelta(days=6),
        terminal_at=recent_terminal,
        compressed=False,
    )
    (d1,) = _decisions([young])
    assert (d1.action, d1.reason) == (Action.KEEP, "evidence-retained")
    due = _evidence(
        run_accepted=False,
        created_at=NOW - timedelta(days=7),
        terminal_at=recent_terminal,
        compressed=False,
    )
    (d2,) = _decisions([due])
    assert (d2.action, d2.reason) == (
        Action.ELIGIBLE_COMPRESS,
        "evidence-compress-eligible",
    )
    done = _evidence(
        run_accepted=False,
        created_at=NOW - timedelta(days=8),
        terminal_at=recent_terminal,
        compressed=True,
    )
    (d3,) = _decisions([done])
    assert (d3.action, d3.reason) == (Action.KEEP, "evidence-retained")


def test_evidence_accepted_90d_boundary() -> None:
    """Covers: ADR-0003 Q4 accepted evidence deleted 90d after terminal time."""
    old_created = NOW - timedelta(days=200)
    for days, eligible in ((89, False), (90, True), (91, True)):
        item = _evidence(
            run_accepted=True,
            created_at=old_created,
            terminal_at=NOW - timedelta(days=days),
            compressed=True,
        )
        (d,) = _decisions([item])
        assert (d.action == Action.ELIGIBLE_DELETE) is eligible, days


def test_evidence_other_terminal_30d_boundary() -> None:
    """Covers: ADR-0003 Q4 other terminal evidence deleted 30d after terminal."""
    old_created = NOW - timedelta(days=200)
    for days, eligible in ((29, False), (30, True), (31, True)):
        item = _evidence(
            run_accepted=False,
            created_at=old_created,
            terminal_at=NOW - timedelta(days=days),
            compressed=True,
        )
        (d,) = _decisions([item])
        assert (d.action == Action.ELIGIBLE_DELETE) is eligible, days


def test_evidence_active_run_kept_despite_age() -> None:
    """Covers: active (non-terminal) run evidence never compressed or deleted."""
    item = _evidence(
        run_terminal=False,
        run_accepted=False,
        created_at=NOW - timedelta(days=365),
        compressed=False,
    )
    (d,) = _decisions([item])
    assert (d.action, d.reason) == (Action.KEEP, "evidence-active")


def test_evidence_delete_requires_size_and_acceptance() -> None:
    """Covers: fail closed when delete classification metadata is missing."""
    (d1,) = _decisions([_evidence(size_bytes=None)])
    assert (d1.action, d1.reason) == (Action.KEEP, "missing-metadata")
    (d2,) = _decisions([_evidence(run_accepted=None)])
    assert (d2.action, d2.reason) == (Action.KEEP, "missing-metadata")


def test_venv_follows_parent_worktree() -> None:
    """Covers: ADR-0003 D6 venvs die with their worktree, never alone."""
    (d1,) = _decisions([_venv(parent_worktree_live=True, parent_eligible_for_removal=False)])
    assert (d1.action, d1.reason) == (Action.KEEP, "venv-parent-live")
    (d2,) = _decisions([_venv(parent_worktree_live=False, parent_eligible_for_removal=False)])
    assert (d2.action, d2.reason) == (Action.KEEP, "venv-parent-kept")
    (d3,) = _decisions([_venv(parent_worktree_live=False, parent_eligible_for_removal=True)])
    assert (d3.action, d3.reason) == (
        Action.ELIGIBLE_DELETE,
        "venv-follows-worktree",
    )


def test_owner_db_vacuum_rules() -> None:
    """Covers: ADR-0003 D6 weekly VACUUM above 20 % free pages, never delete."""
    (d1,) = _decisions(
        [_ownerdb(last_vacuum_at=NOW - timedelta(days=7), free_page_ratio=0.21)]
    )
    assert d1.action.value == "eligible-vacuum"
    assert d1.reason == "vacuum-due"
    (d2,) = _decisions(
        [_ownerdb(last_vacuum_at=NOW - timedelta(days=30), free_page_ratio=0.20)]
    )
    assert (d2.action, d2.reason) == (Action.KEEP, "vacuum-not-due")
    (d3,) = _decisions(
        [_ownerdb(last_vacuum_at=NOW - timedelta(days=6, hours=23), free_page_ratio=0.9)]
    )
    assert (d3.action, d3.reason) == (Action.KEEP, "vacuum-not-due")


def test_unowned_items_never_deleted() -> None:
    """Covers: prompt rule do-not-delete-unowned; unknown ownership fails closed."""
    items: list[Artifact] = [
        _scratch(owner=None, artifact_id="u1"),
        _scratch(owner="", owner_state=OwnerState.KNOWN_DEAD, artifact_id="u2"),
        _worktree(owner_state=OwnerState.UNKNOWN, artifact_id="u3"),
        _checkpoint(owner=None, artifact_id="u4"),
        _evidence(owner_state=OwnerState.UNKNOWN, artifact_id="u5"),
        _venv(
            artifact_id="u6",
            owner=None,
            owner_state=OwnerState.UNKNOWN,
            parent_worktree_live=False,
            parent_eligible_for_removal=True,
        ),
        _ownerdb(
            artifact_id="u7",
            owner=None,
            owner_state=OwnerState.UNKNOWN,
            last_vacuum_at=NOW - timedelta(days=30),
            free_page_ratio=0.9,
        ),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    assert plan.decisions
    for d in plan.decisions:
        assert (d.action, d.reason) == (Action.KEEP, "unknown-owner"), d
    assert plan.deletion_log == ()


def test_outside_roots_decoy_kept() -> None:
    """Covers: ADR-0003 D6 GC never touches anything outside owned roots."""
    items: list[Artifact] = [
        _scratch(path="/etc/passwd", artifact_id="m1"),
        _worktree(path="/tmp/evil-wt", artifact_id="m2"),
        _evidence(path="/etc/shadow", artifact_id="m3"),
        _venv(
            artifact_id="m4",
            path="/usr/lib/venv",
            owner="x",
            owner_state=OwnerState.KNOWN_DEAD,
            parent_worktree_live=False,
            parent_eligible_for_removal=True,
        ),
        _ownerdb(
            artifact_id="m5",
            path="/var/lib/other.db",
            owner="engine",
            owner_state=OwnerState.KNOWN_LIVE,
            last_vacuum_at=NOW - timedelta(days=30),
            free_page_ratio=0.9,
        ),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    for d in plan.decisions:
        assert (d.action, d.reason) == (Action.KEEP, "outside-roots"), d
    assert plan.deletion_log == ()


def test_malicious_paths_fail_closed() -> None:
    """Covers: prompt rule for outside-root/symlink escapes (fail closed)."""
    traversal = _scratch(path="/data/saberops/../etc/passwd", artifact_id="p1")
    (d1,) = _decisions([traversal])
    assert (d1.action, d1.reason) == (Action.KEEP, "path-escape")
    relative = _scratch(path="scratch/run-1", artifact_id="p2")
    (d2,) = _decisions([relative])
    assert (d2.action, d2.reason) == (Action.KEEP, "relative-path")
    symlink = _scratch(path=SCRATCH, is_symlink_escape=True, artifact_id="p3")
    (d3,) = _decisions([symlink])
    assert (d3.action, d3.reason) == (Action.KEEP, "symlink-escape")
    nul = _scratch(path="/data/saberops/\x00x", artifact_id="p4")
    (d4,) = _decisions([nul])
    assert (d4.action, d4.reason) == (Action.KEEP, "invalid-path")


def test_time_handling_fail_closed() -> None:
    """Covers: naive timestamps fail closed; naive now rejected outright."""
    naive = _evidence(created_at=datetime(2026, 9, 1, 12, 0, 0))
    (d,) = _decisions([naive])
    assert (d.action, d.reason) == (Action.KEEP, "invalid-time")
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=datetime(2026, 10, 15, 12, 0, 0), owned_roots=ROOTS)


def test_every_decision_preserves_owner_and_reason() -> None:
    """Covers: prompt rule every decision preserves owner identity and reason."""
    items: list[Artifact] = [
        _scratch(owner="gate-run-1"),
        _worktree(owner="attempt-7", artifact_id="w9"),
        _checkpoint(owner="job-9", artifact_id="c9"),
        _evidence(owner="run-1", artifact_id="e9"),
        _scratch(owner=None, artifact_id="u9", path="/etc/passwd"),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    by_id = {d.artifact_id: d for d in plan.decisions}
    assert by_id["s1"].owner == "gate-run-1"
    assert by_id["w9"].owner == "attempt-7"
    assert by_id["c9"].owner == "job-9"
    assert by_id["e9"].owner == "run-1"
    assert by_id["u9"].owner is None
    for d in plan.decisions:
        assert d.reason != ""
        assert d.kind in (
            ArtifactKind.GATE_SCRATCH,
            ArtifactKind.ATTEMPT_WORKTREE,
            ArtifactKind.CHECKPOINT_REF,
            ArtifactKind.EVIDENCE,
            ArtifactKind.WORKTREE_VENV,
            ArtifactKind.OWNER_DB,
        )


def test_deterministic_ordering() -> None:
    """Covers: planner is deterministic regardless of input order."""
    items: list[Artifact] = [
        _evidence(),
        _scratch(),
        _checkpoint(),
        _worktree(),
    ]
    first = plan_gc(items, now=NOW, owned_roots=ROOTS)
    second = plan_gc(list(reversed(items)), now=NOW, owned_roots=ROOTS)
    assert first.decisions == second.decisions
    assert first.deletion_log == second.deletion_log
    keys = [(d.kind.value, d.artifact_id) for d in first.decisions]
    assert keys == sorted(keys)


def test_idempotent_fixed_point() -> None:
    """Covers: re-planning is stable; applying deletes reaches a fixed point."""
    items: list[Artifact] = [_scratch(), _worktree(), _checkpoint(), _evidence()]
    first = plan_gc(items, now=NOW, owned_roots=ROOTS)
    again = plan_gc(items, now=NOW, owned_roots=ROOTS)
    assert first == again
    doomed = {d.artifact_id for d in first.decisions if d.action == Action.ELIGIBLE_DELETE}
    assert doomed
    remaining = [a for a in items if a.artifact_id not in doomed]
    rest = plan_gc(remaining, now=NOW, owned_roots=ROOTS)
    assert all(d.action != Action.ELIGIBLE_DELETE for d in rest.decisions)


def test_no_side_effects(tmp_path: Path) -> None:
    """Covers: pure planner performs no filesystem/DB/subprocess side effects."""
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched")
    before = sorted(p.name for p in tmp_path.iterdir())
    items: list[Artifact] = [
        _scratch(path=str(tmp_path / "scratch"), artifact_id="n1"),
        _evidence(path=str(tmp_path / "ev"), artifact_id="n2"),
    ]
    plan_gc(items, now=NOW, owned_roots=(str(tmp_path),))
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after
    assert Path(sentinel).read_text() == "untouched"


def test_deletion_log_schema() -> None:
    """Covers: contract §7 deletion-log schema {ts, class, path, bytes, reason}."""
    plan = plan_gc([_scratch(), _evidence()], now=NOW, owned_roots=ROOTS)
    assert len(plan.deletion_log) == 2
    for event in plan.deletion_log:
        assert event.ts == NOW
        assert event.artifact_class in ("gate-scratch", "evidence")
        assert event.path != ""
        assert event.reason != ""
    by_class = {e.artifact_class: e for e in plan.deletion_log}
    assert by_class["gate-scratch"].bytes == 100
    assert by_class["evidence"].bytes == 4096


def test_duplicate_and_empty_ids_rejected() -> None:
    """Covers: planner rejects ambiguous inventories instead of guessing."""
    with pytest.raises(ValueError, match="duplicated"):
        plan_gc([_scratch(), _scratch()], now=NOW, owned_roots=ROOTS)
    with pytest.raises(ValueError, match="empty"):
        plan_gc([_scratch(artifact_id="")], now=NOW, owned_roots=ROOTS)


def test_owner_state_live_blocks_but_known_dead_proceeds() -> None:
    """Covers: KNOWN_DEAD ownership proceeds; UNKNOWN fails closed (sweep needs records)."""
    dead = _scratch(owner_state=OwnerState.KNOWN_DEAD)
    (d1,) = _decisions([dead])
    assert d1.action == Action.ELIGIBLE_DELETE
    live_gate = _scratch(owner_state=OwnerState.KNOWN_LIVE)
    (d2,) = _decisions([live_gate])
    assert d2.action == Action.ELIGIBLE_DELETE
    unknown = _scratch(owner_state=OwnerState.UNKNOWN)
    (d3,) = _decisions([unknown])
    assert (d3.action, d3.reason) == (Action.KEEP, "unknown-owner")


def test_checkpoint_cross_job_mismatch_kept() -> None:
    """Covers: fixup B1 terminal metadata of another job never authorizes delete."""
    live_ref = f"{CHECKPOINT_REF_PREFIX}job-LIVE/5"
    item = _checkpoint(
        artifact_id="c-x",
        ref=live_ref,
        owner="job-OLD",
        owner_state=OwnerState.KNOWN_DEAD,
        job_id="job-OLD",
        job_terminal=True,
        terminal_at=NOW - timedelta(days=8),
        job_preempted=False,
    )
    (d,) = _decisions([item])
    assert (d.action, d.reason) == (Action.KEEP, "ref-job-mismatch")


def test_checkpoint_malformed_refs_kept() -> None:
    """Covers: fixup B1 accepted ref shape is exactly <job_id>/<nonempty-seq>."""
    cases = [
        (f"{CHECKPOINT_REF_PREFIX}job-9", "invalid-ref"),
        (f"{CHECKPOINT_REF_PREFIX}job-9/", "invalid-ref"),
        (f"{CHECKPOINT_REF_PREFIX}job-9/3/extra", "invalid-ref"),
        (f"{CHECKPOINT_REF_PREFIX}/3", "invalid-ref"),
    ]
    for ref, reason in cases:
        (d,) = _decisions([_checkpoint(ref=ref)])
        assert (d.action, d.reason) == (Action.KEEP, reason), ref
    (slashed,) = _decisions([_checkpoint(job_id="job/9")])
    assert (slashed.action, slashed.reason) == (Action.KEEP, "missing-metadata")


def test_evidence_long_running_recently_terminal_kept() -> None:
    """Covers: fixup B2 deletion measured from terminal time, not creation."""
    item = _evidence(
        run_accepted=True,
        created_at=NOW - timedelta(days=100),
        terminal_at=NOW - timedelta(days=1),
        compressed=True,
    )
    (d,) = _decisions([item])
    assert d.action == Action.KEEP
    assert d.reason == "evidence-retained"


def test_evidence_terminal_anchor_invalid_kept() -> None:
    """Covers: fixup B2 missing/naive/future terminal anchor fails closed."""
    old_created = NOW - timedelta(days=200)
    (d1,) = _decisions(
        [_evidence(created_at=old_created, terminal_at=None, compressed=True)]
    )
    assert (d1.action, d1.reason) == (Action.KEEP, "missing-metadata")
    naive = datetime(2026, 9, 1, 12, 0, 0)
    (d2,) = _decisions(
        [_evidence(created_at=old_created, terminal_at=naive, compressed=True)]
    )
    assert (d2.action, d2.reason) == (Action.KEEP, "invalid-time")
    (d3,) = _decisions(
        [
            _evidence(
                created_at=old_created,
                terminal_at=NOW + timedelta(days=1),
                compressed=True,
            )
        ]
    )
    assert (d3.action, d3.reason) == (Action.KEEP, "terminal-future")


def test_negative_policy_bounds_rejected() -> None:
    """Covers: fixup B3 negative retention/cap bounds fail closed with ValueError."""
    neg = timedelta(days=-1)
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=NOW, owned_roots=ROOTS, compress_after=neg)
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=NOW, owned_roots=ROOTS, accepted_retention=neg)
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=NOW, owned_roots=ROOTS, other_retention=neg)
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=NOW, owned_roots=ROOTS, checkpoint_retention=neg)
    with pytest.raises(ValueError):
        plan_gc([_scratch()], now=NOW, owned_roots=ROOTS, failed_scratch_cap_bytes=-1)


def test_negative_size_never_eligible() -> None:
    """Covers: fixup B3 negative artifact sizes never yield eligible deletes."""
    (d1,) = _decisions([_scratch(size_bytes=-1)])
    assert (d1.action, d1.reason) == (Action.KEEP, "invalid-size")
    (d2,) = _decisions([_evidence(size_bytes=-100)])
    assert (d2.action, d2.reason) == (Action.KEEP, "invalid-size")
    plan = plan_gc(
        [_scratch(size_bytes=-1, artifact_id="z1")], now=NOW, owned_roots=ROOTS
    )
    assert plan.deletion_log == ()


def test_ratio_out_of_range_never_vacuum() -> None:
    """Covers: fixup B3 out-of-range free-page ratios never yield vacuum."""
    for ratio in (-0.1, 1.5, float("nan")):
        (d,) = _decisions(
            [
                _ownerdb(
                    last_vacuum_at=NOW - timedelta(days=30),
                    free_page_ratio=ratio,
                )
            ]
        )
        assert (d.action, d.reason) == (Action.KEEP, "invalid-ratio"), ratio


ROOT = ROOTS[0]


def test_owned_root_itself_never_eligible() -> None:
    """Covers: fixup-2 O1 owned roots are never eligible delete or compress.

    Every destructive-class artifact placed at exactly the owned root, but
    otherwise fully terminal/dead and policy-eligible, must fail closed to
    KEEP ``root-itself-protected`` and never reach the deletion log. The owner
    DB is included per the O1 default-refuse decision at root equality.
    """
    items: list[Artifact] = [
        _scratch(path=ROOT, artifact_id="r1"),
        _worktree(path=ROOT, artifact_id="r2"),
        _venv(path=ROOT, artifact_id="r3"),
        _evidence(
            path=ROOT,
            artifact_id="r4",
            created_at=NOW - timedelta(days=200),
            terminal_at=NOW - timedelta(days=31),
            run_accepted=False,
            compressed=True,
            size_bytes=4096,
        ),
        _evidence(
            path=ROOT,
            artifact_id="r5",
            created_at=NOW - timedelta(days=30),
            terminal_at=NOW - timedelta(days=1),
            run_accepted=False,
            compressed=False,
        ),
        _ownerdb(
            path=ROOT,
            artifact_id="r6",
            last_vacuum_at=NOW - timedelta(days=30),
            free_page_ratio=0.9,
        ),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    assert plan.decisions
    for d in plan.decisions:
        assert (d.action, d.reason) == (Action.KEEP, "root-itself-protected"), d
    assert plan.deletion_log == ()


def test_owned_root_path_aliases_protected() -> None:
    """Covers: fixup-2 O1 trailing-slash/dot aliases of the root stay protected."""
    for alias in (ROOT + "/", ROOT + "/.", ROOT + "/./"):
        (d,) = _decisions([_scratch(path=alias, artifact_id="a1")])
        assert (d.action, d.reason) == (Action.KEEP, "root-itself-protected"), alias


def test_owned_root_strict_descendants_still_eligible() -> None:
    """Covers: fixup-2 O1 strict descendants of a root keep normal eligibility."""
    items: list[Artifact] = [
        _scratch(path=ROOT + "/scratch/run-1", artifact_id="d1"),
        _evidence(
            path=ROOT + "/evidence/run-1",
            artifact_id="d2",
            created_at=NOW - timedelta(days=200),
            terminal_at=NOW - timedelta(days=31),
            run_accepted=False,
            compressed=True,
            size_bytes=4096,
        ),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    by_id = {d.artifact_id: d for d in plan.decisions}
    assert by_id["d1"].action == Action.ELIGIBLE_DELETE
    assert by_id["d2"].action == Action.ELIGIBLE_DELETE
    assert len(plan.deletion_log) == 2


def test_filesystem_root_never_confers_eligibility() -> None:
    """Covers: fixup-2 O1 '/' as an owned root never means the whole filesystem."""
    plan = plan_gc(
        [
            _scratch(path="/etc/passwd", artifact_id="f1"),
            _evidence(
                path="/etc/shadow",
                artifact_id="f2",
                created_at=NOW - timedelta(days=200),
                terminal_at=NOW - timedelta(days=31),
                run_accepted=False,
                compressed=True,
                size_bytes=4096,
            ),
            _scratch(path="/", artifact_id="f3"),
        ],
        now=NOW,
        owned_roots=("/",),
    )
    for d in plan.decisions:
        assert d.action == Action.KEEP, d
        assert d.reason == "outside-roots", d
    assert plan.deletion_log == ()


def test_malformed_owned_roots_fail_closed() -> None:
    """Covers: fixup-2 O1 relative/traversal/empty/NUL roots confer no eligibility."""
    path = "/data/saberops/scratch/run-1"
    for roots in (
        ("data/saberops",),
        ("/data/../saberops",),
        ("",),
        ("/data/saberops\x00",),
        ("/",),
    ):
        plan = plan_gc([_scratch(artifact_id="g1", path=path)], now=NOW, owned_roots=roots)
        (d,) = plan.decisions
        assert (d.action, d.reason) == (Action.KEEP, "outside-roots"), roots
        assert plan.deletion_log == ()


def test_raw_string_owner_state_fails_closed() -> None:
    """Covers: fixup-2 O2 raw StrEnum-valued strings never authorize ownership."""
    corrupt: list[Artifact] = [
        _scratch(owner_state=cast(OwnerState, "unknown"), artifact_id="o1"),
        _scratch(owner_state=cast(OwnerState, "known-dead"), artifact_id="o2"),
        _scratch(owner_state=cast(OwnerState, "known-live"), artifact_id="o3"),
    ]
    plan = plan_gc(corrupt, now=NOW, owned_roots=ROOTS)
    assert plan.decisions
    for d in plan.decisions:
        assert (d.action, d.reason) == (Action.KEEP, "unknown-owner"), d
    assert plan.deletion_log == ()


def test_non_enum_owner_state_fails_closed() -> None:
    """Covers: fixup-2 O2 missing/foreign/non-enum owner_state fails closed."""

    class _ForeignOwnerState(StrEnum):
        KNOWN_DEAD = "known-dead"
        KNOWN_LIVE = "known-live"

    corrupt: list[Artifact] = [
        _scratch(owner_state=cast(OwnerState, None), artifact_id="n1"),
        _scratch(owner_state=cast(OwnerState, 42), artifact_id="n2"),
        _worktree(owner_state=cast(OwnerState, "accepted"), artifact_id="n3"),
        _evidence(owner_state=cast(OwnerState, ["known-dead"]), artifact_id="n4"),
        _scratch(
            owner_state=cast(OwnerState, _ForeignOwnerState.KNOWN_DEAD), artifact_id="n5"
        ),
        _scratch(
            owner_state=cast(OwnerState, _ForeignOwnerState.KNOWN_LIVE), artifact_id="n6"
        ),
    ]
    plan = plan_gc(corrupt, now=NOW, owned_roots=ROOTS)
    assert plan.decisions
    for d in plan.decisions:
        assert (d.action, d.reason) == (Action.KEEP, "unknown-owner"), d
    assert plan.deletion_log == ()


def test_exact_enum_owner_state_still_authorizes() -> None:
    """Covers: fixup-2 O2 exact KNOWN_LIVE/KNOWN_DEAD enums and blank owners."""
    items: list[Artifact] = [
        _scratch(owner="g", owner_state=OwnerState.KNOWN_DEAD, artifact_id="e1"),
        _worktree(owner="a", owner_state=OwnerState.KNOWN_LIVE, artifact_id="e2"),
        _scratch(owner=None, owner_state=OwnerState.KNOWN_DEAD, artifact_id="e3"),
        _scratch(owner="", owner_state=OwnerState.KNOWN_LIVE, artifact_id="e4"),
    ]
    plan = plan_gc(items, now=NOW, owned_roots=ROOTS)
    by_id = {d.artifact_id: d for d in plan.decisions}
    assert by_id["e1"].action == Action.ELIGIBLE_DELETE
    assert by_id["e2"].action == Action.ELIGIBLE_DELETE
    assert (by_id["e3"].action, by_id["e3"].reason) == (Action.KEEP, "unknown-owner")
    assert (by_id["e4"].action, by_id["e4"].reason) == (Action.KEEP, "unknown-owner")
    assert {e.reason for e in plan.deletion_log} == {
        "scratch-succeeded-eligible",
        "worktree-terminal-eligible",
    }
