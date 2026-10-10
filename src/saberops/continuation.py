"""Owner-requested repair from a failed run's immutable candidate and evidence."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from saberops.config import RunConfig, reconstruct_run_config
from saberops.git import GitManager, assert_repository_at_revision
from saberops.models import Attempt, Run, RunStatus, SideEffectState
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.provenance import assert_run_repository
from saberops.session_journal import SessionJournal

if TYPE_CHECKING:
    from saberops.db import Database


class ContinuationError(ValueError):
    pass


class ContinuationExists(ContinuationError):
    def __init__(self, child_id: str) -> None:
        self.child_id = child_id
        super().__init__("This run already has a continuation: " + child_id)


def source_candidate(db: Database, parent_id: str) -> tuple[Run, Attempt]:
    parent = db.get_run(parent_id)
    if parent is None or parent.status != RunStatus.FAILED:
        raise ContinuationError("Continue is available only for failed runs.")
    if not parent.config_json:
        raise ContinuationError("This historical run has no saved execution configuration.")
    frozen = reconstruct_run_config(json.loads(parent.config_json))
    if frozen.parallel_parent_run_id:
        raise ContinuationError("Continue or retry the parent Work, not an internal team worker.")
    if any(
        effect.state in (SideEffectState.STARTED, SideEffectState.UNCERTAIN)
        for effect in db.get_side_effects_for_run(parent.id)
    ):
        raise ContinuationError("The previous run has an unresolved execution effect.")
    if not (
        parent.routing_snapshot_json
        and parent.control_plane_snapshot_json
        and parent.tool_contract_json
    ):
        raise ContinuationError(
            "This run lacks the frozen execution contracts needed to continue safely."
        )
    target = assert_run_repository(parent)
    assert_repository_at_revision(target, parent.base_commit, when="continuing a failed run")
    attempts = db.get_attempts_for_run(parent.id)
    attempt = attempts[-1] if attempts else None
    if attempt is None or not attempt.commit_sha:
        raise ContinuationError("No committed candidate was saved. Use Retry to start again.")
    sha = attempt.commit_sha
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ContinuationError("The saved candidate identity is invalid.")
    if GitManager.get_ref_commit(target, sha + "^{commit}") != sha:
        raise ContinuationError("The saved candidate commit is no longer available.")
    if not GitManager.is_ancestor(target, parent.base_commit, sha):
        raise ContinuationError("The saved candidate does not descend from this run's base.")
    plans = db.list_run_plans(parent.id)
    if plans:
        from saberops.orchestrator import (
            _durable_plan_expected_package_count,
            _durable_plan_linkage_is_adoptable,
        )

        count = _durable_plan_expected_package_count(db, parent.id, plans[0])
        packages = db.get_work_packages(parent.id)
        if (
            len(plans) != 1
            or count is None
            or not _durable_plan_linkage_is_adoptable(
                plans[0], db.get_plan_steps(parent.id), packages, count
            )
        ):
            raise ContinuationError("The saved execution plan is incomplete or inconsistent.")
        for package in packages:
            if package.base_candidate_sha and not GitManager.is_ancestor(
                target, package.base_candidate_sha, sha
            ):
                raise ContinuationError(
                    "The saved candidate does not contain its plan prerequisites."
                )
    worktree = Path(attempt.worktree_path)
    if worktree.exists():
        if not GitManager.is_clean(worktree) or GitManager.get_head_commit(worktree) != sha:
            raise ContinuationError(
                "The saved worktree changed after submission. "
                "Preserve those edits before continuing."
            )
    owner = db.get_execution_owner(parent.id)
    if owner and owner.released_at is None and probe_liveness(owner.identity) != Liveness.DEAD:
        raise ContinuationError("The previous execution owner has not stopped safely yet.")
    supervision = db.get_supervision(parent.id)
    if supervision and (supervision.worker_identity or supervision.worker_pid):
        if probe_liveness(ProcessIdentity.from_json(supervision.worker_identity)) != Liveness.DEAD:
            raise ContinuationError("The previous worker is still running or its state is unknown.")
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
                raise ContinuationError(
                    "An earlier provider session has not been confirmed stopped."
                )
    return parent, attempt


def prepare_continuation(db: Database, parent_id: str) -> RunConfig:
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


def continuation_offer(db: Database, run: Run) -> tuple[bool, str, str | None]:
    child = db.get_continuation(run.id)
    if child:
        return False, "A continuation already exists.", child
    try:
        source_candidate(db, run.id)
        return True, "Repair from the saved candidate and rerun configured verification.", None
    except (OSError, ValueError, RuntimeError) as exc:
        return False, str(exc), None
