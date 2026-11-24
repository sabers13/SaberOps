"""Stale candidate refresh keeps acceptance and evidence separate."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig, create_toy_repo

from saberops.accept import AcceptEngine
from saberops.cli import build_parser
from saberops.cli import main as cli_main
from saberops.config import RunConfig
from saberops.git import GitManager
from saberops.mcp.errors import ToolError
from saberops.mcp.guide import GUIDE
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import (
    MUTATING_TOOLS,
    READ_TOOLS,
    TOOL_NAMES,
    ServerContext,
    build_default_registry,
)
from saberops.models import DispatchStage, ReviewResult, Run, RunStatus, Tier
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.service import OrchestratorService, RunOperationError
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
        ).returncode
        == 0
    )


def _run_job(
    rig: RigEnvironment,
    *,
    task: str,
    target_file: str,
    content: str,
) -> Run:
    rig.set_worker_mode("commit", target_file=target_file, content=content)
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            provider_override="codex",
            model_override="gpt-5.6-terra",
            max_attempts=1,
            # Test-only stall bounds (H29): the run is awaited at 90 s below;
            # 15 s per phase was sized for an idle host.
            worker_timeout=60,
            gate_timeout=60,
            review_enabled=True,
            review_timeout=60,
        )
    )
    started = RunSupervisor(db=rig.db()).start_supervised_run(config)
    run = rig.wait_for_run(started.run_id, timeout=90)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    return run


def _run_ids(rig: RigEnvironment) -> set[str]:
    return {run.id for run in rig.db().list_runs(limit=100)}


def _new_run_after(rig: RigEnvironment, before: set[str]) -> Run:
    added = [run for run in rig.db().list_runs(limit=100) if run.id not in before]
    assert len(added) == 1, added
    return added[0]


def _move_target(repo: Path, name: str = "owner.txt") -> str:
    (repo / name).write_text("owner moved the target\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", "owner moves target during acceptance")
    return git(repo, "rev-parse", "HEAD")


def test_refresh_sibling_and_accept_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(rig, task="Create first", target_file="first.txt", content="first\n")
    second = _run_job(rig, task="Create second", target_file="second.txt", content="second\n")

    old_attempt = db.get_attempts_for_run(second.id)[-1]
    old_reviews = db.get_reviews_for_run(second.id)
    old_check_ids = {check.id for check in db.get_checks_for_run(second.id)}
    old_worktree = Path(old_attempt.worktree_path)
    assert old_attempt.commit_sha is not None
    assert old_worktree.is_dir()
    assert GitManager.get_ref_commit(rig.repo, old_attempt.branch_name) == old_attempt.commit_sha

    AcceptEngine(db).accept_run(first.id)
    base = git(rig.repo, "rev-parse", "HEAD")
    before = _run_ids(rig)
    assert len(before) == 2

    # Refresh starts only on explicit request; nothing has run automatically.
    code = cli_main(
        ["refresh", second.id, "--db", str(db.db_path), "--json"]
    )
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert code == 0

    refreshed = db.get_run(payload["run_id"])
    assert refreshed is not None and refreshed.status == RunStatus.COMPLETED
    assert refreshed.id != second.id
    assert second.project_id
    assert refreshed.id.startswith(f"run_{second.project_id}_")
    assert refreshed.base_commit == base
    assert _new_run_after(rig, before).id == refreshed.id

    # Refresh never touches the target branch.
    assert git(rig.repo, "rev-parse", "HEAD") == base
    assert git(rig.repo, "status", "--porcelain") == ""

    # The old candidate commit, worktree and evidence survive unchanged.
    old_run = db.get_run(second.id)
    assert old_run is not None and old_run.status == RunStatus.COMPLETED
    assert db.get_attempts_for_run(second.id)[-1].commit_sha == old_attempt.commit_sha
    assert {check.id for check in db.get_checks_for_run(second.id)} == old_check_ids
    assert [review.id for review in db.get_reviews_for_run(second.id)] == [
        review.id for review in old_reviews
    ]
    assert old_worktree.is_dir()
    assert GitManager.get_ref_commit(rig.repo, old_attempt.branch_name) == old_attempt.commit_sha

    new_attempt = db.get_attempts_for_run(refreshed.id)[-1]
    assert new_attempt.commit_sha is not None
    assert new_attempt.commit_sha != old_attempt.commit_sha
    assert _is_ancestor(rig.repo, base, new_attempt.commit_sha)
    new_checks = db.get_checks_for_run(refreshed.id)
    assert new_checks and all(check.passed for check in new_checks)

    # C10 binding: the refreshed SHA has its own bound review; the old review
    # cannot qualify it.
    new_reviews = db.get_reviews_for_run(refreshed.id)
    assert new_reviews
    assert {review.id for review in new_reviews}.isdisjoint(
        {review.id for review in old_reviews}
    )
    assert any(
        dispatch.stage == DispatchStage.REVIEW
        and dispatch.candidate_sha == new_attempt.commit_sha
        for dispatch in db.get_dispatch_evidence_for_run(refreshed.id)
    )
    assert AcceptEngine(db).check_accept_eligibility(refreshed.id).eligible is True

    registry = build_default_registry(ServerContext(db_path=db.db_path))
    spec = registry.get("refresh_candidate")
    assert spec is not None and spec.mutating is True
    assert spec.input_schema["required"] == ["run_id"]

    accepted = AcceptEngine(db).accept_run(refreshed.id)
    assert accepted.accepted is True
    assert accepted.final_sha == new_attempt.commit_sha
    assert git(rig.repo, "rev-parse", "HEAD") == new_attempt.commit_sha
    assert (rig.repo / "first.txt").exists() and (rig.repo / "second.txt").exists()


def test_refresh_conflict_preserves_original_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(
        rig, task="Create shared first", target_file="shared.txt", content="first\n"
    )
    second = _run_job(
        rig, task="Create shared second", target_file="shared.txt", content="second\n"
    )

    old_attempt = db.get_attempts_for_run(second.id)[-1]
    old_worktree = Path(old_attempt.worktree_path)
    old_check_ids = {check.id for check in db.get_checks_for_run(second.id)}
    old_review_ids = {review.id for review in db.get_reviews_for_run(second.id)}
    AcceptEngine(db).accept_run(first.id)
    base = git(rig.repo, "rev-parse", "HEAD")
    before = _run_ids(rig)

    with pytest.raises(RunOperationError, match="conflict"):
        rig.service().refresh_run(second.id)

    # The target branch is untouched and still carries only the first sibling.
    assert git(rig.repo, "rev-parse", "HEAD") == base
    assert git(rig.repo, "status", "--porcelain") == ""
    assert (rig.repo / "shared.txt").read_text(encoding="utf-8") == "first\n"

    # The original work, candidate commit and evidence remain for repair.
    old_run = db.get_run(second.id)
    assert old_run is not None and old_run.status == RunStatus.COMPLETED
    assert db.get_attempts_for_run(second.id)[-1].commit_sha == old_attempt.commit_sha
    assert old_worktree.is_dir()
    assert (old_worktree / "shared.txt").read_text(encoding="utf-8") == "second\n"
    assert {check.id for check in db.get_checks_for_run(second.id)} == old_check_ids
    assert {review.id for review in db.get_reviews_for_run(second.id)} == old_review_ids

    failed = _new_run_after(rig, before)
    assert failed.status == RunStatus.FAILED
    assert failed.result_summary and "conflict" in failed.result_summary.lower()


def test_refresh_refuses_dirty_source_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(rig, task="Create first", target_file="first.txt", content="first\n")
    second = _run_job(rig, task="Create second", target_file="second.txt", content="second\n")

    old_attempt = db.get_attempts_for_run(second.id)[-1]
    old_worktree = Path(old_attempt.worktree_path)
    dirty = old_worktree / "uncommitted-notes.txt"
    dirty.write_text("work in progress\n", encoding="utf-8")
    AcceptEngine(db).accept_run(first.id)
    base = git(rig.repo, "rev-parse", "HEAD")
    before = _run_ids(rig)

    lease = OrchestratorLease(tmp_path / "lease.json", holder="mcp-test", pid=os.getpid())
    assert lease.acquire() is True
    with pytest.raises(ToolError, match="Source worktree has changed"):
        build_default_registry(ServerContext(db_path=db.db_path, lease=lease)).call(
            "refresh_candidate", {"run_id": second.id}
        )

    assert git(rig.repo, "rev-parse", "HEAD") == base
    assert git(rig.repo, "status", "--porcelain") == ""
    assert dirty.read_text(encoding="utf-8") == "work in progress\n"
    old_run = db.get_run(second.id)
    assert old_run is not None and old_run.status == RunStatus.COMPLETED
    assert db.get_attempts_for_run(second.id)[-1].commit_sha == old_attempt.commit_sha
    assert _run_ids(rig) == before


def test_refresh_refuses_repository_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(rig, task="Create first", target_file="first.txt", content="first\n")
    second = _run_job(rig, task="Create second", target_file="second.txt", content="second\n")
    AcceptEngine(db).accept_run(first.id)
    base = git(rig.repo, "rev-parse", "HEAD")

    other_repo, other_base = create_toy_repo(tmp_path, name="other-repo")
    with db.write_transaction() as conn:
        conn.execute(
            "UPDATE runs SET target_repo = ? WHERE id = ?",
            (str(other_repo), second.id),
        )
    before = _run_ids(rig)

    with pytest.raises(RunOperationError, match="repository validation failed"):
        rig.service().refresh_run(second.id)

    assert git(rig.repo, "rev-parse", "HEAD") == base
    assert git(rig.repo, "status", "--porcelain") == ""
    assert git(other_repo, "rev-parse", "HEAD") == other_base
    assert git(other_repo, "status", "--porcelain") == ""
    assert _run_ids(rig) == before


def test_refresh_detects_target_moving_during_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(rig, task="Create first", target_file="first.txt", content="first\n")
    second = _run_job(rig, task="Create second", target_file="second.txt", content="second\n")
    old_attempt = db.get_attempts_for_run(second.id)[-1]
    old_worktree = Path(old_attempt.worktree_path)
    AcceptEngine(db).accept_run(first.id)
    before = _run_ids(rig)

    original_review = OrchestratorService.review_run
    move: dict[str, str | None] = {"sha": None}

    def move_target_then_review(
        self: OrchestratorService,
        run_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> ReviewResult:
        if move["sha"] is None:
            move["sha"] = _move_target(rig.repo)
        return original_review(self, run_id, *args, **kwargs)

    monkeypatch.setattr(OrchestratorService, "review_run", move_target_then_review)

    with pytest.raises(RunOperationError, match="Target moved during refresh"):
        rig.service().refresh_run(second.id)

    assert move["sha"] is not None
    assert git(rig.repo, "rev-parse", "HEAD") == move["sha"]
    assert git(rig.repo, "status", "--porcelain") == ""
    assert (rig.repo / "owner.txt").exists()
    assert old_worktree.is_dir()
    assert db.get_attempts_for_run(second.id)[-1].commit_sha == old_attempt.commit_sha

    failed = _new_run_after(rig, before)
    assert failed.status == RunStatus.FAILED
    assert failed.result_summary and "Target moved" in failed.result_summary


def test_accept_refuses_refreshed_candidate_after_target_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    first = _run_job(rig, task="Create first", target_file="first.txt", content="first\n")
    second = _run_job(rig, task="Create second", target_file="second.txt", content="second\n")
    AcceptEngine(db).accept_run(first.id)
    base = git(rig.repo, "rev-parse", "HEAD")

    result = rig.service().refresh_run(second.id)
    refreshed = db.get_run(result["run_id"])
    assert refreshed is not None and refreshed.status == RunStatus.COMPLETED
    attempt = db.get_attempts_for_run(refreshed.id)[-1]
    assert attempt.commit_sha is not None
    assert git(rig.repo, "rev-parse", "HEAD") == base

    moved = _move_target(rig.repo)

    eligibility = AcceptEngine(db).check_accept_eligibility(refreshed.id)
    assert eligibility.eligible is False
    assert any(
        check.key == "target_head" and not check.passed() for check in eligibility.checks
    )
    with pytest.raises(RuntimeError, match="has moved from recorded base commit"):
        AcceptEngine(db).accept_run(refreshed.id)

    # Acceptance refused before mutating anything: the owner's new commit and
    # the refreshed candidate both survive.
    assert git(rig.repo, "rev-parse", "HEAD") == moved
    assert git(rig.repo, "status", "--porcelain") == ""
    assert (rig.repo / "first.txt").exists()
    assert not (rig.repo / "second.txt").exists()
    assert Path(attempt.worktree_path).is_dir()
    assert db.get_attempts_for_run(refreshed.id)[-1].commit_sha == attempt.commit_sha


def test_refresh_is_explicit_cli_and_mcp_surface() -> None:
    parser = build_parser()
    args = parser.parse_args(["refresh", "run_example", "--json"])
    assert args.command == "refresh"
    assert args.run_id == "run_example"
    assert args.json is True

    assert "refresh_candidate" in TOOL_NAMES
    assert "refresh_candidate" in MUTATING_TOOLS
    assert "refresh_candidate" not in READ_TOOLS
    assert "refresh_candidate" in GUIDE
    assert "resubmit" not in GUIDE


def test_mcp_refresh_resolves_project_local_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.db import Database
    from saberops.mcp.tools import _run_database

    project_db = Database(tmp_path / "project.db")
    run = Run(
        id="run_0123456789ab_abcdef1234",
        task="Project-local candidate",
        target_repo=str(tmp_path),
        base_commit="0" * 40,
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.COMPLETED,
        created_at="2026-11-19T00:00:00+00:00",
    )
    project_db.create_run(run)
    owner_db = Database(tmp_path / "owner.db")
    context = ServerContext(db_path=owner_db.db_path)

    monkeypatch.setattr(
        "saberops.runtime.db_path_for_run", lambda _run_id: project_db.db_path
    )
    resolved = _run_database(context, run.id)
    assert resolved.db_path == project_db.db_path
    assert resolved.get_run(run.id) is not None
