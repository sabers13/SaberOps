"""The one durable save-and-resume record for interrupted work.

Every path that preserves work in progress records it the same way: a commit
in the attempt's own worktree named by a :class:`WorkerCheckpoint
<saberops.models.WorkerCheckpoint>` row (plus, for a parked job, the engine
checkpoint ref the preemption protocol anchors).  Every path that resumes work
reads that record -- either the run's latest checkpoint or a checkpoint staged
onto a successor run -- and starts the next attempt there.

The three triggers therefore share one mechanism:

* quota exhaustion / pause / redirect: the preemption protocol parks the job
  with a checkpoint and :func:`saberops.preemption.prepare_resume` stages it
  onto the re-dispatched run;
* a provider or runtime failure: the interrupted attempt is checkpointed and
  the failover decision starts the next attempt from it;
* an owner or planner resume (``resume_job`` / continue): the failed run's
  committed candidate is staged onto the successor as the same checkpoint
  record.

This module owns no policy.  It never decides whether a trigger applies and
never touches quota, routing, admission, review or acceptance; it only records
verified saved work and hands the record back.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from saberops.config import RunConfig, reconstruct_run_config
from saberops.db import current_iso_timestamp
from saberops.git import GitManager, assert_repository_at_revision
from saberops.models import (
    Attempt,
    Run,
    RunStatus,
    SideEffectOperation,
    SideEffectState,
    WorkerCheckpoint,
    WorkerInterruptionReason,
)
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.provenance import assert_run_repository
from saberops.session_journal import SessionJournal

if TYPE_CHECKING:
    from saberops.db import Database


class ResumeRefused(ValueError):
    """The saved work cannot be resumed safely."""


def record_checkpoint(
    db: Database,
    *,
    run_id: str,
    attempt_id: str,
    base_sha: str,
    checkpoint_sha: str,
    reason: str,
    changed_paths: tuple[str, ...] = (),
    handoff_id: str | None = None,
) -> WorkerCheckpoint:
    """Persist one checkpoint row for an already-committed saved-work SHA.

    Idempotent per run and SHA: a replay or a re-staged continuation returns
    the existing row instead of recording the same work twice.
    """
    existing = db.get_worker_checkpoint_by_sha(run_id, checkpoint_sha)
    if existing is not None:
        return existing
    checkpoint = WorkerCheckpoint(
        id=f"cp_{uuid.uuid4().hex[:10]}",
        run_id=run_id,
        source_attempt_id=attempt_id,
        source_base_sha=base_sha,
        checkpoint_sha=checkpoint_sha,
        reason=reason,
        changed_paths=tuple(changed_paths),
        created_at=current_iso_timestamp(),
        handoff_id=handoff_id,
    )
    db.create_worker_checkpoint(checkpoint)
    return checkpoint


def save_interrupted_work(
    db: Database,
    *,
    run_id: str,
    attempt_id: str,
    base_sha: str,
    worktree_path: str | Path,
    reason: str,
    changed_paths: tuple[str, ...] | None = None,
    journal: bool = False,
    message: str | None = None,
) -> WorkerCheckpoint | None:
    """Commit an attempt's uncommitted work and record it as the resume point.

    Returns ``None`` when the worktree holds no changes (nothing to preserve).
    ``journal`` makes the commit replay-safe through the side-effect journal,
    which the interruption and cold-recovery paths use; callers pass
    ``message`` when they need their historical commit label.
    """
    worktree = Path(worktree_path)
    changed = (
        tuple(changed_paths)
        if changed_paths is not None
        else tuple(GitManager.get_changed_paths(worktree))
    )
    key = f"checkpoint:{run_id}:{attempt_id}"
    if journal:
        # A crash between the commit and the row leaves a recorded completion
        # with a clean worktree: reuse the recorded checkpoint, never re-commit.
        existing = db.get_side_effect(key)
        if (
            existing is not None
            and existing.state == SideEffectState.COMPLETED
            and existing.result_json
        ):
            try:
                saved = json.loads(existing.result_json).get("checkpoint_sha")
            except (TypeError, ValueError):
                saved = None
            if isinstance(saved, str):
                found = db.get_worker_checkpoint_by_sha(run_id, saved)
                if found is not None:
                    return found
    if not changed:
        return None
    label = message or (
        f"orch checkpoint({run_id}/{attempt_id}): "
        f"partial worker state ({reason})"
    )
    if journal:
        db.journal_record_intent(
            key,
            run_id=run_id,
            attempt_id=attempt_id,
            operation=SideEffectOperation.CHECKPOINT_CREATION,
            payload={"reason": reason},
        )
    try:
        checkpoint_sha = GitManager.create_checkpoint_commit(worktree, label)
        if checkpoint_sha is None:
            if journal:
                db.journal_record_completed(key, {"no_changes": True})
            return None
        checkpoint = record_checkpoint(
            db,
            run_id=run_id,
            attempt_id=attempt_id,
            base_sha=base_sha,
            checkpoint_sha=checkpoint_sha,
            reason=reason,
            changed_paths=changed,
        )
    except Exception as exc:
        if journal:
            db.journal_record_failed(key, str(exc))
        raise
    if journal:
        db.journal_record_completed(
            key,
            {"checkpoint_sha": checkpoint_sha, "checkpoint_id": checkpoint.id},
        )
    return checkpoint


def stage_checkpoint(
    db: Database,
    *,
    new_run_id: str,
    attempt_id: str,
    base_sha: str,
    checkpoint_sha: str,
    reason: str,
    changed_paths: tuple[str, ...] = (),
    handoff_id: str | None = None,
) -> WorkerCheckpoint:
    """Stage verified saved work onto the successor run it resumes.

    The new run reads it through the ordinary checkpoint channel, so a resumed
    attempt starts exactly where the preserved work stopped.
    """
    return record_checkpoint(
        db,
        run_id=new_run_id,
        attempt_id=attempt_id,
        base_sha=base_sha,
        checkpoint_sha=checkpoint_sha,
        reason=reason,
        changed_paths=changed_paths,
        handoff_id=handoff_id,
    )


def stage_continuation(
    db: Database, parent: Run, attempt: Attempt, new_run_id: str
) -> WorkerCheckpoint:
    """Stage an owner/planner continuation's saved candidate as its resume point."""
    assert attempt.commit_sha
    changed: tuple[str, ...] = ()
    try:
        target = assert_run_repository(parent)
        changed = tuple(
            GitManager.get_changed_paths_between(
                target, parent.base_commit, attempt.commit_sha
            )
        )
    except Exception:
        changed = ()
    return stage_checkpoint(
        db,
        new_run_id=new_run_id,
        attempt_id=attempt.id,
        base_sha=parent.base_commit,
        checkpoint_sha=attempt.commit_sha,
        reason=WorkerInterruptionReason.GRACEFUL_HANDOFF.value,
        changed_paths=changed,
    )


def verify_resume_source(db: Database, parent_id: str) -> tuple[Run, Attempt]:
    """Verify a failed run's committed candidate may be resumed.

    Fail closed on anything that could make the saved commit ambiguous or
    unsafe to build on: an unresolved side effect, a missing frozen execution
    contract, a live owner/worker/session or a moved worktree.  Returns the
    parent run and the attempt that produced the candidate.
    """
    parent = db.get_run(parent_id)
    if parent is None or parent.status != RunStatus.FAILED:
        raise ResumeRefused("Continue is available only for failed runs.")
    if not parent.config_json:
        raise ResumeRefused("This historical run has no saved execution configuration.")
    frozen = reconstruct_run_config(json.loads(parent.config_json))
    if frozen.parallel_parent_run_id:
        raise ResumeRefused("Continue or retry the parent Work, not an internal team worker.")
    if any(
        effect.state in (SideEffectState.STARTED, SideEffectState.UNCERTAIN)
        for effect in db.get_side_effects_for_run(parent.id)
    ):
        raise ResumeRefused("The previous run has an unresolved execution effect.")
    if not (parent.routing_snapshot_json and parent.control_plane_snapshot_json):
        raise ResumeRefused(
            "This run lacks the frozen execution contracts needed to continue safely."
        )
    target = assert_run_repository(parent)
    assert_repository_at_revision(target, parent.base_commit, when="continuing a failed run")
    attempts = db.get_attempts_for_run(parent.id)
    attempt = attempts[-1] if attempts else None
    if attempt is None or not attempt.commit_sha:
        raise ResumeRefused("No committed candidate was saved. Use Retry to start again.")
    sha = attempt.commit_sha
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ResumeRefused("The saved candidate identity is invalid.")
    if GitManager.get_ref_commit(target, sha + "^{commit}") != sha:
        raise ResumeRefused("The saved candidate commit is no longer available.")
    if not GitManager.is_ancestor(target, parent.base_commit, sha):
        raise ResumeRefused("The saved candidate does not descend from this run's base.")
    worktree = Path(attempt.worktree_path)
    if worktree.exists():
        if not GitManager.is_clean(worktree) or GitManager.get_head_commit(worktree) != sha:
            raise ResumeRefused(
                "The saved worktree changed after submission. "
                "Preserve those edits before continuing."
            )
    owner = db.get_execution_owner(parent.id)
    if owner and owner.released_at is None and probe_liveness(owner.identity) != Liveness.DEAD:
        raise ResumeRefused("The previous execution owner has not stopped safely yet.")
    supervision = db.get_supervision(parent.id)
    if supervision and (supervision.worker_identity or supervision.worker_pid):
        if probe_liveness(ProcessIdentity.from_json(supervision.worker_identity)) != Liveness.DEAD:
            raise ResumeRefused("The previous worker is still running or its state is unknown.")
    journal = SessionJournal(Path(db.db_path).parent / "agent-sessions")
    if journal.path.is_file():
        with journal.connect() as conn:
            sessions = conn.execute(
                "SELECT id,process_identity FROM sessions WHERE owner=?", (parent.id,)
            ).fetchall()
        for session in sessions:
            outcome = journal.view(session["id"], limit=1)["outcome"] or {}
            live = probe_liveness(ProcessIdentity.from_json(session["process_identity"]))
            if live == Liveness.LIVE or (
                live == Liveness.UNKNOWN and outcome.get("quiescent") is not True
            ):
                raise ResumeRefused(
                    "An earlier provider session has not been confirmed stopped."
                )
    return parent, attempt


class ContinuationError(ValueError):
    pass


class ContinuationExists(ContinuationError):
    def __init__(self, child_id: str) -> None:
        self.child_id = child_id
        super().__init__("This run already has a continuation: " + child_id)


def source_candidate(db: Database, parent_id: str) -> tuple[Run, Attempt]:
    """Verify the parent run's saved candidate may be continued."""
    try:
        return verify_resume_source(db, parent_id)
    except ResumeRefused as exc:
        raise ContinuationError(str(exc)) from None


def prepare_continuation(db: Database, parent_id: str) -> RunConfig:
    """Freeze a failed run's policy for an owner/planner continuation."""
    parent, attempt = source_candidate(db, parent_id)
    assert parent.config_json and attempt.commit_sha
    original = reconstruct_run_config(json.loads(parent.config_json))
    return replace(
        original,
        target_repo=str(assert_run_repository(parent)),
        review_enabled=not original.review_disabled,
        continuation={
            "parent_run_id": parent.id,
            "attempt_id": attempt.id,
            "candidate_sha": attempt.commit_sha,
            "base_sha": parent.base_commit,
        },
    )


def validate_continuation(db: Database, config: RunConfig) -> tuple[Run, Attempt] | None:
    if config.continuation is None:
        return None
    reference = config.continuation
    parent, attempt = source_candidate(db, reference["parent_run_id"])
    if reference != {
        "parent_run_id": parent.id,
        "attempt_id": attempt.id,
        "candidate_sha": attempt.commit_sha,
        "base_sha": parent.base_commit,
    }:
        raise ContinuationError("The saved continuation reference no longer matches its source.")
    if Path(config.target_repo).resolve() != assert_run_repository(parent).resolve():
        raise ContinuationError("A continuation cannot change its target project.")
    expected = prepare_continuation(db, parent.id)
    if config.as_dict() != expected.as_dict():
        raise ContinuationError("A continuation cannot replace the saved task or execution policy.")
    return parent, attempt


__all__ = [
    "ContinuationError",
    "ContinuationExists",
    "ResumeRefused",
    "prepare_continuation",
    "record_checkpoint",
    "save_interrupted_work",
    "stage_checkpoint",
    "stage_continuation",
    "validate_continuation",
    "verify_resume_source",
]
