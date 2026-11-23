"""H28: one save-and-resume record for quota, failure and owner/planner resume."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from saberops.config import RunConfig
from saberops.db import Database
from saberops.git import GitManager
from saberops.handoff import CAPSULE_KIND, create_resume_capsule
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
from saberops.resume import (
    prepare_continuation,
    record_checkpoint,
    save_interrupted_work,
    stage_continuation,
)

_CREATED = "2026-11-23T00:00:00+00:00"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_candidate(tmp_path: Path) -> tuple[Path, str, str]:
    """A clean repository at its base plus a saved candidate that descends from it."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "h28@example.test")
    _git(repo, "config", "user.name", "H28")
    (repo / "app.txt").write_text("base\n")
    (repo / "Makefile").write_text("gate:\n\t@true\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "candidate")
    (repo / "feature.txt").write_text("saved work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return repo, base, candidate


def _seed_run(
    db: Database,
    *,
    run_id: str,
    attempt_id: str,
    repo: Path,
    base: str,
    status: RunStatus = RunStatus.PENDING,
) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="Build a useful application",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=False,
            gate_command="make gate",
            status=status,
            created_at=_CREATED,
            config_json="{}",
        )
    )
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="test-model",
            worktree_path="",
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.RUNNING,
            created_at=_CREATED,
            base_sha=base,
        )
    )


def _db_with_failed_run(tmp_path: Path, repo: Path, base: str, candidate: str) -> Database:
    db = Database(tmp_path / "state" / "runs.db")
    config = RunConfig(
        task="Build a useful application",
        target_repo=str(repo),
        provider_override="codex",
        model_override="test-model",
        max_attempts=1,
        gate_command="make gate",
    )
    db.create_run(
        Run(
            id="run_parent",
            task=config.task,
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=False,
            gate_command="make gate",
            status=RunStatus.FAILED,
            created_at=_CREATED,
            config_json=json.dumps(config.as_dict()),
            routing_snapshot_json="{}",
            control_plane_snapshot_json="{}",
        )
    )
    db.create_attempt(
        Attempt(
            id="run_parent_a1",
            run_id="run_parent",
            attempt_number=1,
            provider="codex",
            model="test-model",
            worktree_path=str(tmp_path / "gone-worktree"),
            branch_name="orch/run_parent/a1",
            status=AttemptStatus.SUCCESS,
            created_at=_CREATED,
            base_sha=base,
            commit_sha=candidate,
        )
    )
    return db


def test_interrupted_work_is_committed_and_replayed_as_one_checkpoint(
    tmp_path: Path,
) -> None:
    repo, base, _ = _repo_with_candidate(tmp_path)
    db = Database(tmp_path / "state" / "runs.db")
    _seed_run(
        db, run_id="run_worker", attempt_id="run_worker_a1", repo=repo, base=base
    )
    (repo / "app.txt").write_text("interrupted edit\n")

    first = save_interrupted_work(
        db,
        run_id="run_worker",
        attempt_id="run_worker_a1",
        base_sha=base,
        worktree_path=repo,
        reason="QUOTA_EXHAUSTED",
        journal=True,
    )
    assert first is not None
    assert first.checkpoint_sha == _git(repo, "rev-parse", "HEAD")
    assert GitManager.is_clean(repo)

    replay = save_interrupted_work(
        db,
        run_id="run_worker",
        attempt_id="run_worker_a1",
        base_sha=base,
        worktree_path=repo,
        reason="QUOTA_EXHAUSTED",
        journal=True,
    )
    assert replay is not None and replay.id == first.id
    assert len(db.get_worker_checkpoints_for_run("run_worker")) == 1


def test_continuation_stages_its_candidate_as_the_successor_checkpoint(
    tmp_path: Path,
) -> None:
    repo, base, candidate = _repo_with_candidate(tmp_path)
    db = _db_with_failed_run(tmp_path, repo, base, candidate)
    db.create_run(
        Run(
            id="run_child",
            task="Build a useful application",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=False,
            gate_command="make gate",
            status=RunStatus.PENDING,
            created_at=_CREATED,
            config_json=json.dumps(prepare_continuation(db, "run_parent").as_dict()),
            routing_snapshot_json="{}",
            control_plane_snapshot_json="{}",
        )
    )

    parent = db.get_run("run_parent")
    assert parent is not None
    attempt = db.get_attempt("run_parent_a1")
    assert attempt is not None
    staged = stage_continuation(db, parent, attempt, "run_child")

    assert staged.checkpoint_sha == candidate
    assert staged.source_attempt_id == "run_parent_a1"
    assert staged.source_base_sha == base
    assert staged.changed_paths == ("feature.txt",)
    # Staging the same saved work again returns the one record.
    again = stage_continuation(db, parent, attempt, "run_child")
    assert again.id == staged.id
    assert len(db.get_worker_checkpoints_for_run("run_child")) == 1


def test_resume_capsule_is_the_only_handoff_artifact(tmp_path: Path) -> None:
    repo, base, candidate = _repo_with_candidate(tmp_path)
    db = _db_with_failed_run(tmp_path, repo, base, candidate)

    capsule = create_resume_capsule(db, "run_parent")

    stored = db.get_handoffs_for_run("run_parent")
    assert [item.kind for item in stored] == [CAPSULE_KIND]
    assert stored[0].id == capsule.id
    payload = json.loads(capsule.content)
    assert payload["run_id"] == "run_parent"
    assert payload["candidate_sha"] == candidate


def test_checkpoint_rows_are_idempotent_per_run_and_sha(tmp_path: Path) -> None:
    repo, base, candidate = _repo_with_candidate(tmp_path)
    db = Database(tmp_path / "state" / "runs.db")
    _seed_run(db, run_id="run_saved", attempt_id="run_saved_a1", repo=repo, base=base)

    first = record_checkpoint(
        db,
        run_id="run_saved",
        attempt_id="run_saved_a1",
        base_sha=base,
        checkpoint_sha=candidate,
        reason="ABORTED",
    )
    second = record_checkpoint(
        db,
        run_id="run_saved",
        attempt_id="run_saved_a1",
        base_sha=base,
        checkpoint_sha=candidate,
        reason="ABORTED",
    )

    assert first.id == second.id
    assert len(db.get_worker_checkpoints_for_run("run_saved")) == 1
