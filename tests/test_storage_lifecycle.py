"""Synthetic regression tests for the early storage-lifecycle planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    young = _evidence(
        run_accepted=False, created_at=NOW - timedelta(days=6), compressed=False
    )
    (d1,) = _decisions([young])
    assert (d1.action, d1.reason) == (Action.KEEP, "evidence-retained")
    due = _evidence(
        run_accepted=False, created_at=NOW - timedelta(days=7), compressed=False
    )
    (d2,) = _decisions([due])
    assert (d2.action, d2.reason) == (
        Action.ELIGIBLE_COMPRESS,
        "evidence-compress-eligible",
    )
    done = _evidence(
        run_accepted=False, created_at=NOW - timedelta(days=8), compressed=True
    )
    (d3,) = _decisions([done])
    assert (d3.action, d3.reason) == (Action.KEEP, "evidence-retained")


def test_evidence_accepted_90d_boundary() -> None:
    """Covers: ADR-0003 Q4 accepted evidence retained 90 days."""
    for days, eligible in ((89, False), (90, True), (91, True)):
        item = _evidence(
            run_accepted=True,
            created_at=NOW - timedelta(days=days),
            compressed=True,
        )
        (d,) = _decisions([item])
        assert (d.action == Action.ELIGIBLE_DELETE) is eligible, days


def test_evidence_other_terminal_30d_boundary() -> None:
    """Covers: ADR-0003 Q4 other terminal evidence retained 30 days."""
    for days, eligible in ((29, False), (30, True), (31, True)):
        item = _evidence(
            run_accepted=False,
            created_at=NOW - timedelta(days=days),
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
