"""F4: ordinary quota-failover start authorization is verified, never trusted.

The scheduler-dispatched continuation after quota exhaustion starts its
replacement attempt at the interrupted attempt's checkpoint, not at the run
base.  Acceptance and publication accept such a base only through
``failover_start_authorized``; these tests pin that every gap fails closed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    Attempt,
    AttemptStatus,
    Run,
    RunStatus,
    Tier,
    WorkerCheckpoint,
)
from saberops.provenance import failover_authorized_bases

RUN_ID = "run-f4"
SOURCE_ATTEMPT_ID = f"{RUN_ID}_a1"
CHECKPOINT_ID = "cp_f4_1"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "f4@example.com")
    _git(path, "config", "user.name", "F4")
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return path


@pytest.fixture()
def db(tmp_path: Path, repo: Path) -> Database:
    database = Database(tmp_path / "saberops" / "orchestrator.db")
    base_sha = _git(repo, "rev-parse", "HEAD")
    database.create_run(
        Run(
            id=RUN_ID,
            task="Continue after quota",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=current_iso_timestamp(),
        )
    )
    database.create_attempt(
        Attempt(
            id=SOURCE_ATTEMPT_ID,
            run_id=RUN_ID,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(repo),
            branch_name=f"orch/{RUN_ID}/a1",
            status=AttemptStatus.FAILED,
            base_sha=base_sha,
            interruption_reason="QUOTA_EXHAUSTED",
            created_at=current_iso_timestamp(),
        )
    )
    return database


def _checkpoint(db: Database, repo: Path, *, sha: str | None = None) -> WorkerCheckpoint:
    """Commit partial work on a branch and record it as the source checkpoint."""
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "partial.txt").write_text("partial work\n", encoding="utf-8")
    _git(repo, "add", "partial.txt")
    _git(repo, "commit", "-m", "checkpoint")
    checkpoint_sha = sha or _git(repo, "rev-parse", "HEAD")
    checkpoint = WorkerCheckpoint(
        id=CHECKPOINT_ID,
        run_id=RUN_ID,
        source_attempt_id=SOURCE_ATTEMPT_ID,
        source_base_sha=base_sha,
        checkpoint_sha=checkpoint_sha,
        reason="QUOTA_EXHAUSTED",
        changed_paths=("partial.txt",),
        created_at=current_iso_timestamp(),
    )
    db.create_worker_checkpoint(checkpoint)
    return checkpoint


def _record(db: Database, *, anchor: str, start: str, attempt_id: str = SOURCE_ATTEMPT_ID) -> None:
    db.record_event(
        RUN_ID,
        "failover_start_authorized",
        attempt_id=attempt_id,
        payload={
            "failover_from_attempt_id": attempt_id,
            "required_base_sha": anchor,
            "start_sha": start,
            "checkpoint_sha": start,
            "path": "ordinary_continuation",
        },
    )


def test_verified_ordinary_continuation_authorizes_its_start(db: Database, repo: Path) -> None:
    checkpoint = _checkpoint(db, repo)
    _record(db, anchor=checkpoint.source_base_sha, start=checkpoint.checkpoint_sha)

    assert failover_authorized_bases(db, RUN_ID) == {checkpoint.checkpoint_sha}


def test_missing_checkpoint_row_never_authorizes(db: Database, repo: Path) -> None:
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "partial.txt").write_text("partial work\n", encoding="utf-8")
    _git(repo, "add", "partial.txt")
    _git(repo, "commit", "-m", "checkpoint")
    start = _git(repo, "rev-parse", "HEAD")
    _record(db, anchor=base_sha, start=start)

    assert failover_authorized_bases(db, RUN_ID) == set()


def test_source_base_disagreement_never_authorizes(db: Database, repo: Path) -> None:
    checkpoint = _checkpoint(db, repo)
    # The record claims a different anchor than the source attempt's real base.
    _record(db, anchor="f" * 40, start=checkpoint.checkpoint_sha)

    assert failover_authorized_bases(db, RUN_ID) == set()


def test_unrelated_start_never_authorizes(db: Database, repo: Path) -> None:
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--orphan", "unrelated")
    (repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "unrelated root")
    unrelated = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    db.create_worker_checkpoint(
        WorkerCheckpoint(
            id=CHECKPOINT_ID,
            run_id=RUN_ID,
            source_attempt_id=SOURCE_ATTEMPT_ID,
            source_base_sha=base_sha,
            checkpoint_sha=unrelated,
            reason="QUOTA_EXHAUSTED",
            changed_paths=("other.txt",),
            created_at=current_iso_timestamp(),
        )
    )
    _record(db, anchor=base_sha, start=unrelated)

    assert failover_authorized_bases(db, RUN_ID) == set()


def test_record_for_another_runs_attempt_never_authorizes(db: Database, repo: Path) -> None:
    checkpoint = _checkpoint(db, repo)
    db.record_event(
        RUN_ID,
        "failover_start_authorized",
        attempt_id="other-run_a9",
        payload={
            "failover_from_attempt_id": "other-run_a9",
            "required_base_sha": checkpoint.source_base_sha,
            "start_sha": checkpoint.checkpoint_sha,
            "checkpoint_sha": checkpoint.checkpoint_sha,
            "path": "ordinary_continuation",
        },
    )

    assert failover_authorized_bases(db, RUN_ID) == set()
