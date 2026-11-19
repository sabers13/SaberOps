"""Explicit, isolated refresh of a stale candidate."""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saberops.accept import _lineage_base_sha, _select_final_candidate
from saberops.config import reconstruct_run_config
from saberops.db import current_iso_timestamp
from saberops.gate_receipt import record_receipt
from saberops.git import GitManager
from saberops.jobs import store
from saberops.models import Attempt, AttemptStatus, CheckResult, ReviewVerdict, RunStatus
from saberops.orchestrator import _run_configured_gate
from saberops.process import split_command
from saberops.project import new_run_id
from saberops.provenance import (
    assert_candidate_provenance,
    assert_run_repository,
    execution_authorized_bases,
    run_project_identity,
)

if TYPE_CHECKING:
    from saberops.service import OrchestratorService


def refresh_candidate(service: OrchestratorService, run_id: str) -> dict[str, Any]:
    """Rebuild one stale candidate on the current target base, in isolation.

    The source run, its candidate commit and all of its evidence stay
    untouched.  The refreshed changes are applied to a fresh worktree pinned
    at the target's current commit, then checked and independently reviewed
    as a new run.  This function never moves the target branch; the owner
    accepts the refreshed candidate through the ordinary acceptance path.
    """
    db = service.db
    source = db.get_run(run_id)
    if source is None or source.status != RunStatus.COMPLETED:
        raise ValueError("Refresh requires a completed source run.")
    if not source.config_json:
        raise ValueError("Refresh requires the saved execution policy.")
    config = reconstruct_run_config(json.loads(source.config_json))
    if config.parallel_parent_run_id:
        raise ValueError("Refresh the parent work, not an internal worker.")
    if not config.gate_enabled:
        raise ValueError("Refresh requires an enabled gate.")
    repo = assert_run_repository(source)
    if not GitManager.is_clean(repo):
        raise ValueError("Target worktree has uncommitted changes.")
    base = GitManager.get_head_commit(repo)
    branch = GitManager.get_current_branch(repo)
    if not branch or base == source.base_commit:
        raise ValueError("Refresh requires a stale candidate and a checked-out target branch.")
    attempts = db.get_attempts_for_run(run_id)
    attempt = _select_final_candidate(attempts)
    if attempt is None:
        raise ValueError("Source run has no successful candidate.")
    assert_candidate_provenance(
        source,
        attempt,
        lineage_base_sha=_lineage_base_sha(attempts, attempt),
        extra_authorized_bases=execution_authorized_bases(db, run_id),
    )
    sha = attempt.commit_sha
    assert sha is not None
    if not GitManager.is_ancestor(repo, source.base_commit, sha):
        raise ValueError("Source candidate does not descend from its recorded base.")
    old_wt = Path(attempt.worktree_path)
    if old_wt.exists() and (
        not GitManager.is_clean(old_wt) or GitManager.get_head_commit(old_wt) != sha
    ):
        raise ValueError("Source worktree has changed; preserve its edits before refresh.")
    candidate_ref = GitManager.get_ref_commit(repo, attempt.branch_name)
    if candidate_ref is not None and candidate_ref != sha:
        raise ValueError("Source candidate branch has moved.")
    new_id = new_run_id(run_project_identity(source))
    now = current_iso_timestamp()
    run = replace(
        source,
        id=new_id,
        base_commit=base,
        status=RunStatus.RUNNING,
        created_at=now,
        completed_at=None,
        final_attempt_id=None,
        result_summary=None,
        review_state_json=None,
        review_risk_decision_json=None,
    )
    db.create_run(run)
    # P3: link the refreshed run into its job's chain so acceptance and the
    # rendered ledger follow the refreshed candidate, not the pre-refresh run.
    try:
        owner_job = store.find_job_for_run(db, run_id)
        if owner_job is not None:
            store.append_run_to_job(db, owner_job.job_id, new_id)
    except Exception:  # pragma: no cover - refresh keeps its own evidence either way
        logging.getLogger(__name__).exception(
            "could not link refreshed run %s to its job", new_id
        )
    db.record_event(
        new_id,
        "refresh_started",
        payload={
            "source_run_id": run_id,
            "source_candidate_sha": sha,
            "source_base_sha": source.base_commit,
            "target_base_sha": base,
        },
    )
    wt = (service.worktree_base or GitManager.get_default_worktree_base(repo)) / new_id / "a1"
    new_branch = f"orch/{new_id}/a1"
    attempt_id = f"{new_id}_a1"
    try:
        GitManager.create_worktree(repo, new_branch, wt, start_point=base)
        patch = subprocess.run(
            ["git", "diff", "--binary", source.base_commit, sha, "--"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
        applied = subprocess.run(
            ["git", "apply", "--index", "--3way", "-"], cwd=wt, input=patch, capture_output=True
        )
        if applied.returncode:
            raise RuntimeError(
                f"Refresh conflict in {wt}: {applied.stderr.decode(errors='replace')} "
                f"Original candidate {sha[:8]} and its evidence are preserved."
            )
        commit = subprocess.run(
            ["git", "commit", "-m", f"Refresh candidate {run_id}"],
            cwd=wt,
            capture_output=True,
            text=True,
        )
        if commit.returncode:
            raise RuntimeError(f"Cannot commit refreshed candidate in {wt}: {commit.stderr}")
        new_sha = GitManager.get_head_commit(wt)
        new_attempt = Attempt(
            id=attempt_id,
            run_id=new_id,
            attempt_number=1,
            provider=attempt.provider,
            model=attempt.model,
            worktree_path=str(wt),
            branch_name=new_branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=new_sha,
            created_at=now,
            completed_at=current_iso_timestamp(),
            base_sha=base,
        )
        db.create_attempt(new_attempt)
        gate = _run_configured_gate(
            config, split_command(config.gate_command), cwd=wt, timeout=config.gate_timeout
        )
        db.create_check(
            CheckResult(
                id=f"check_{uuid.uuid4().hex[:10]}",
                attempt_id=attempt_id,
                gate_command=config.gate_command,
                exit_code=gate.exit_code,
                stdout=gate.stdout,
                stderr=gate.stderr,
                passed=gate.passed,
                skipped=gate.skipped,
                created_at=current_iso_timestamp(),
            )
        )
        db.record_event(
            new_id,
            "gate_completed",
            attempt_id=attempt_id,
            payload={"candidate_sha": new_sha, "exit_code": gate.exit_code},
        )
        if gate.passed and not gate.skipped:
            finished = current_iso_timestamp()
            record_receipt(
                db,
                run_id=new_id,
                attempt_id=attempt_id,
                config=config,
                base_sha=base,
                candidate_sha=new_sha,
                started_at=now,
                finished_at=finished,
                passed=True,
            )
        if (
            not gate.passed
            or gate.skipped
            or not GitManager.is_clean(wt)
            or GitManager.get_head_commit(wt) != new_sha
        ):
            raise RuntimeError(
                "Refreshed candidate failed its gate or changed during verification."
            )
        review = service.review_run(new_id, timeout=config.review_timeout)
        if review.verdict not in (ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_BACKLOG):
            raise RuntimeError(f"Refreshed candidate review did not pass: {review.verdict.value}")
        if (
            GitManager.get_current_branch(repo) != branch
            or GitManager.get_head_commit(repo) != base
            or not GitManager.is_clean(repo)
        ):
            raise RuntimeError("Target moved during refresh; candidate remains preserved.")
        if not GitManager.is_clean(wt) or GitManager.get_head_commit(wt) != new_sha:
            raise RuntimeError("Refreshed worktree changed during review.")
        db.update_run(
            new_id,
            RunStatus.COMPLETED,
            current_iso_timestamp(),
            attempt_id,
            "Refreshed candidate is ready for separate owner acceptance.",
        )
        db.record_event(
            new_id,
            "refresh_completed",
            attempt_id=attempt_id,
            payload={"source_run_id": run_id, "candidate_sha": new_sha, "base_sha": base},
        )
        return {
            "operation": "refresh",
            "source_run_id": run_id,
            "run_id": new_id,
            "candidate_sha": new_sha,
            "base_sha": base,
        }
    except Exception as exc:
        db.update_run(new_id, RunStatus.FAILED, current_iso_timestamp(), result_summary=str(exc))
        db.record_event(new_id, "refresh_failed", payload={"error": str(exc), "worktree": str(wt)})
        raise RuntimeError(f"Refresh {new_id} failed: {exc}") from exc
