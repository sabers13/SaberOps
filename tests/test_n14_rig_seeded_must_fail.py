"""N14-rig: must-fail rows review a seeded violation, never a worker's mood.

The confirming run (``/var/tmp/so-confirm2/report.md`` §5) showed the design
flaw directly: Codex and OpenCode workers read the contradictory goal and
chose the acceptance clause, so six of eight must-fail observations had no
violation to block.  The row then measured the worker's judgement, not the
reviewer's guard.  These tests seed the violating candidate deterministically
(as the acceptance-review replay already does) and drive only the reviewer, so
a block means the review caught a real violation and a pass means it missed
one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from benchmark_core import (
    TASKS_BY_ID,
    BenchmarkReport,
    BenchmarkTask,
    SeededCandidate,
    benchmark_gate_command,
    create_toy_repository,
    probe_must_fail_acceptance,
    run_seeded_must_fail_row,
    seed_must_fail_candidate,
)

from saberops.db import Database, current_iso_timestamp
from saberops.jobs.store import list_jobs_tolerant
from saberops.models import (
    AttemptStatus,
    ReviewResult,
    ReviewVerdict,
    RunStatus,
)


def _setup(
    tmp_path: Path, task_id: str = "review-block-b"
) -> tuple[Path, str, Database, BenchmarkTask]:
    task = TASKS_BY_ID[task_id]
    repo, base_sha = create_toy_repository(
        tmp_path, f"toy-{task_id}", task_id=task.task_id
    )
    db = Database(tmp_path / "project.db")
    return repo, base_sha, db, task


def test_seeded_candidate_is_a_real_violation_and_has_no_job(tmp_path: Path) -> None:
    """The row's candidate is deterministic and no worker was ever dispatched."""
    repo, base_sha, db, task = _setup(tmp_path)
    seeded = seed_must_fail_candidate(
        repo=repo,
        base_sha=base_sha,
        task=task,
        db=db,
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
    )

    assert probe_must_fail_acceptance(repo, seeded.candidate_sha) is True
    run = db.get_run(seeded.run_id)
    assert run is not None
    assert run.status is RunStatus.RUNNING
    assert "Acceptance:" in run.task
    attempts = db.get_attempts_for_run(seeded.run_id)
    assert len(attempts) == 1
    assert attempts[0].status is AttemptStatus.SUCCESS
    assert attempts[0].commit_sha == seeded.candidate_sha
    jobs, _ = list_jobs_tolerant(db, limit=10, quarantine=False)
    assert jobs == [], "a seeded must-fail row must never submit a job"


def test_seeded_review_block_passes_and_names_the_candidate(tmp_path: Path) -> None:
    repo, base_sha, db, task = _setup(tmp_path)
    seen: dict[str, str] = {}

    def block(seeded: SeededCandidate) -> None:
        run_id = seeded.run_id
        candidate = seeded.candidate_sha
        seen["sha"] = candidate
        db.create_review(
            ReviewResult(
                id="rev_seeded_block",
                run_id=run_id,
                provider="codex",
                model="gpt-6-luna",
                verdict=ReviewVerdict.BLOCK,
                summary="CLAUSE 1: FAIL",
                details="the empty-name guard is gone",
                created_at=current_iso_timestamp(),
            )
        )

    row = run_seeded_must_fail_row(
        db=db,
        readiness=None,
        repo=repo,
        base_sha=base_sha,
        task=task,
        batch="opencode",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
        reviewer="codex/gpt-6-luna",
        review=block,
    )

    assert row.measured is True
    assert row.passed is True
    assert row.acceptance_violation is True
    assert row.blocked_candidate_sha == seen["sha"]
    table = BenchmarkReport(
        mode="real", results=(row,), started_at=0.0, finished_at=1.0
    ).table()
    assert "must-fail probes" in table
    assert "acceptance_violation=violated" in table
    assert f"blocked_candidate={seen['sha']}" in table


def test_seeded_worktree_and_branch_are_removed_after_the_row(
    tmp_path: Path,
) -> None:
    """D4: a long-lived store must not accumulate seeded worktrees."""
    repo, base_sha, db, task = _setup(tmp_path)
    seen: dict[str, str] = {}

    def block(seeded: SeededCandidate) -> None:
        seen["worktree"] = str(seeded.worktree)
        seen["branch"] = seeded.branch
        db.create_review(
            ReviewResult(
                id="rev_seeded_cleanup",
                run_id=seeded.run_id,
                provider="codex",
                model="gpt-6-luna",
                verdict=ReviewVerdict.BLOCK,
                summary="CLAUSE 1: FAIL",
                details="the empty-name guard is gone",
                created_at=current_iso_timestamp(),
            )
        )

    row = run_seeded_must_fail_row(
        db=db,
        readiness=None,
        repo=repo,
        base_sha=base_sha,
        task=task,
        batch="opencode",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
        reviewer="codex/gpt-6-luna",
        review=block,
    )
    assert row.passed is True
    assert not Path(seen["worktree"]).exists()

    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert seen["worktree"] not in worktrees
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", seen["branch"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""


def test_seeded_review_pass_fails_the_row(tmp_path: Path) -> None:
    """A reviewer that misses the seeded violation fails the measured row."""
    repo, base_sha, db, task = _setup(tmp_path, "review-block-a")

    def allow(seeded: SeededCandidate) -> None:
        db.create_review(
            ReviewResult(
                id="rev_seeded_pass",
                run_id=seeded.run_id,
                provider="codex",
                model="gpt-6-luna",
                verdict=ReviewVerdict.PASS,
                summary="CLAUSE 1: PASS",
                details="approved",
                created_at=current_iso_timestamp(),
            )
        )

    row = run_seeded_must_fail_row(
        db=db,
        readiness=None,
        repo=repo,
        base_sha=base_sha,
        task=task,
        batch="opencode",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
        reviewer="codex/gpt-6-luna",
        review=allow,
    )

    assert row.acceptance_violation is True
    assert row.review_count == 1
    assert row.measured is True
    assert row.passed is False
    assert (
        BenchmarkReport(
            mode="real", results=(row,), started_at=0.0, finished_at=1.0
        ).passed
        is False
    )


def test_review_that_errors_cannot_pass_on_a_partial_verdict(tmp_path: Path) -> None:
    """An aborted review cycle is a failure, not a review-only success."""

    repo, base_sha, db, task = _setup(tmp_path)

    def crash(_seeded: SeededCandidate) -> None:
        raise RuntimeError("reviewer transport failed")

    row = run_seeded_must_fail_row(
        db=db,
        readiness=None,
        repo=repo,
        base_sha=base_sha,
        task=task,
        batch="opencode",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
        reviewer="codex/gpt-6-luna",
        review=crash,
    )

    assert row.review_failed is True
    assert row.final_state == "FAILED"
    assert row.passed is False
    assert row.blocked_candidate_sha is not None


def test_seeded_review_of_a_missing_repo_is_unmeasured_not_a_failure(
    tmp_path: Path,
) -> None:
    """A rig failure leaves the row unmeasured; it never fakes a reviewer fail."""
    repo, base_sha, db, task = _setup(tmp_path)
    row = run_seeded_must_fail_row(
        db=db,
        readiness=None,
        repo=repo / "does-not-exist",
        base_sha=base_sha,
        task=task,
        batch="opencode",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        gate_command=benchmark_gate_command(),
        reviewer="codex/gpt-6-luna",
        review=lambda _seeded: pytest.fail("must not review without a candidate"),
    )

    assert row.final_state == "FAILED"
    assert row.candidate_produced is False
    assert row.acceptance_violation is None
    assert row.measured is False
