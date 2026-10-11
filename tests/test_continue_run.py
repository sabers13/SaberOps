"""Continue preserves failed candidates, frozen policy and single launch authority."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.config import RunConfig, reconstruct_run_config
from saberops.continuation import ContinuationError, prepare_continuation, validate_continuation
from saberops.db import Database
from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    PlanStepStatus,
    ReviewResult,
    ReviewVerdict,
    RunStatus,
    SideEffectOperation,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.ownership import ProcessIdentity
from saberops.planning import persist_plan
from saberops.review import ReviewEngine
from saberops.session_journal import SessionJournal
from saberops.slicing import ProposedPackage, TaskSizingAssessment
from saberops.supervisor import RunSupervisor
from saberops.workers.codex import CodexAdapter
from saberops.workers.registry import AdapterRegistry


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Database, Path, str, AdapterRegistry]:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Continue Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "README.md").write_text("Original project\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    db = Database(tmp_path / "project-state" / "runs.db")
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "is_available", lambda: True)

    def implement(request: WorkerRequest) -> WorkerResult:
        (request.worktree_path / "features.json").write_text('{"saved_work": true}\n')
        return WorkerResult(
            "codex",
            request.model,
            AttemptStatus.SUCCESS,
            "Implemented",
            "Saved work",
            "",
            has_changes=True,
            execution_quiescent=True,
        )

    monkeypatch.setattr(adapter, "run", implement)
    registry = AdapterRegistry([adapter])
    result = Orchestrator(db=db, registry=registry, worktree_base=tmp_path / "trees").run(
        config=RunConfig(
            task="Build a useful application",
            target_repo=str(repo),
            provider_override="codex",
            model_override="test-model",
            max_attempts=1,
            gate_command="make gate",
        )
    )
    assert result.status == RunStatus.FAILED
    assert result.attempts[-1].commit_sha
    return db, repo, result.run_id, registry


def launch_without_process(
    db: Database, registry: AdapterRegistry, monkeypatch: pytest.MonkeyPatch
) -> tuple[RunSupervisor, list[str]]:
    launches: list[str] = []
    monkeypatch.setattr(
        RunSupervisor, "_launch", lambda self, run_id, config: launches.append(run_id)
    )
    return RunSupervisor(db=db, registry=registry), launches


def test_continue_repairs_saved_candidate_and_verifies_full_delta(
    failed: tuple[Database, Path, str, AdapterRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, repo, parent_id, registry = failed
    parent = db.get_run(parent_id)
    assert parent and parent.config_json
    original = db.get_attempts_for_run(parent_id)[-1]
    config = prepare_continuation(db, parent_id)
    assert config.review_enabled and config.gate_command == parent.gate_command
    assert config.task == parent.task and config.worker_binding_registry == json.loads(
        parent.config_json
    ).get("worker_binding_registry")
    assert reconstruct_run_config(config.as_dict()).as_dict() == config.as_dict()
    supervisor, launches = launch_without_process(db, registry, monkeypatch)
    child_id = supervisor.start_supervised_run(config).run_id
    assert launches == [child_id]
    child = db.get_run(child_id)
    assert child and child.config_json
    assert child.base_commit == parent.base_commit
    for field in (
        "routing_snapshot_json",
        "control_plane_snapshot_json",
        "tool_contract_json",
        "gateway_snapshot_json",
    ):
        assert getattr(child, field) == getattr(parent, field)
    dispatched: list[str] = []

    def repair(request: WorkerRequest) -> WorkerResult:
        assert GitManager.get_head_commit(request.worktree_path) == original.commit_sha
        assert json.loads((request.worktree_path / "features.json").read_text())["saved_work"]
        assert "No rule to make target" in request.task
        assert (
            "Owner-requested continuation" in request.task and "[RUN VERIFICATION]" in request.task
        )
        assert "context_mode: REPAIR" in request.task
        dispatched.append(request.task)
        (request.worktree_path / "Makefile").write_text(".PHONY: gate\ngate:\n\t@true\n")
        return WorkerResult(
            "codex",
            request.model,
            AttemptStatus.SUCCESS,
            "Repaired",
            "Gate entrypoint fixed",
            "",
            has_changes=True,
            execution_quiescent=True,
        )

    monkeypatch.setattr(registry.get("codex"), "run", repair)
    reviewed: list[str] = []

    def review(self: ReviewEngine, run_id: str, **kwargs: Any) -> ReviewResult:
        current = db.get_run(run_id)
        assert current
        candidate = db.get_attempts_for_run(run_id)[-1]
        assert candidate and candidate.commit_sha
        assert db.get_checks_for_attempt(candidate.id)[-1].passed
        diff = GitManager.get_diff(candidate.worktree_path, base_commit=current.base_commit)
        assert "features.json" in diff and "Makefile" in diff
        reviewed.append(run_id)
        result = ReviewResult(
            id="continue-review",
            run_id=run_id,
            provider="fake-reviewer",
            model="independent",
            verdict=ReviewVerdict.PASS,
            summary="Reviewed full delta",
            details="PASS",
        )
        db.create_review(result)
        return result

    monkeypatch.setattr(ReviewEngine, "review_run", review)
    outcome = Orchestrator(db=db, registry=registry, worktree_base=repo.parent / "trees").run(
        config=reconstruct_run_config(json.loads(child.config_json)), run_id=child_id
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(dispatched) == 1 and reviewed == [child_id]
    assert db.get_run(parent_id).status == RunStatus.FAILED  # type: ignore[union-attr]
    assert GitManager.get_head_commit(repo) == parent.base_commit
    assert not (repo / "features.json").exists()
    assert GitManager.get_head_commit(original.worktree_path) == original.commit_sha
    stages = [
        e.payload["stage"] for e in db.get_events_by_type(child_id, "worker_started") if e.payload
    ]
    assert stages == ["repair"]


def test_concurrent_continue_is_one_durable_child_and_one_launch(
    failed: tuple[Database, Path, str, AdapterRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, parent_id, registry = failed
    supervisor, launches = launch_without_process(db, registry, monkeypatch)
    config = prepare_continuation(db, parent_id)
    barrier = threading.Barrier(2)
    children: list[str] = []
    failures: list[BaseException] = []

    def request() -> None:
        try:
            barrier.wait()
            children.append(supervisor.start_supervised_run(config).run_id)
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not failures and len(children) == 2
    assert children[0] == children[1] and launches == [children[0]]
    assert db.get_continuation(parent_id) == children[0]
    assert len(db.list_runs()) == 2


@pytest.mark.parametrize(
    "problem",
    [
        "no_candidate",
        "moved_project",
        "dirty_candidate",
        "wrong_candidate",
        "wrong_policy",
        "not_failed",
        "started_effect",
        "uncertain_effect",
    ],
)
def test_unsafe_continuation_is_refused_before_launch(
    failed: tuple[Database, Path, str, AdapterRegistry],
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    db, repo, parent_id, registry = failed
    supervisor, launches = launch_without_process(db, registry, monkeypatch)
    config = prepare_continuation(db, parent_id)
    attempt = db.get_attempts_for_run(parent_id)[-1]
    if problem == "no_candidate":
        db.update_attempt(attempt.id, AttemptStatus.FAILED, commit_sha=None)
    elif problem == "moved_project":
        (repo / "extra").write_text("new")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "moved")
    elif problem == "dirty_candidate":
        (Path(attempt.worktree_path) / "features.json").write_text("unsaved edit")
    elif problem == "wrong_candidate":
        assert config.continuation
        config = replace(config, continuation={**config.continuation, "candidate_sha": "a" * 40})
    elif problem == "wrong_policy":
        config = replace(config, gate_command="true", training_allowed=not config.training_allowed)
    elif problem in ("started_effect", "uncertain_effect"):
        db.journal_record_intent(
            "old-effect", parent_id, attempt.id, SideEffectOperation.TOOL_INVOCATION
        )
        if problem == "started_effect":
            db.journal_record_started("old-effect")
        else:
            db.journal_record_uncertain("old-effect", "Outcome unknown")
    else:
        db.update_run(parent_id, RunStatus.COMPLETED)
    with pytest.raises((ContinuationError, RuntimeError)):
        supervisor.start_supervised_run(config)
    assert not launches and len(db.list_runs()) == 1


def test_continue_honors_explicit_review_disable_and_preserves_work_after_cleanup(
    failed: tuple[Database, Path, str, AdapterRegistry],
) -> None:
    db, repo, parent_id, _ = failed
    parent = db.get_run(parent_id)
    assert parent and parent.config_json
    config = reconstruct_run_config(json.loads(parent.config_json))
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE runs SET config_json=? WHERE id=?",
            (json.dumps(replace(config, review_disabled=True).as_dict()), parent_id),
        )
    attempt = db.get_attempts_for_run(parent_id)[-1]
    git(repo, "worktree", "remove", attempt.worktree_path)
    continued = prepare_continuation(db, parent_id)
    assert continued.review_disabled and not continued.review_enabled
    assert validate_continuation(db, continued)


@pytest.mark.parametrize("live,quiescent", [(True, True), (True, False), (False, False)])
def test_continue_refuses_live_or_uncertain_provider_sessions(
    failed: tuple[Database, Path, str, AdapterRegistry], live: bool, quiescent: bool
) -> None:
    db, _, parent_id, _ = failed
    journal = SessionJournal(db.db_path.parent / "agent-sessions")
    journal.register("old-worker", parent_id, "worker", "last-attempt", None)
    if live:
        journal.process("old-worker", ProcessIdentity.for_self().to_dict())
    journal.complete("old-worker", {"quiescent": quiescent})
    with pytest.raises(ContinuationError, match="confirmed stopped"):
        prepare_continuation(db, parent_id)


def test_continue_keeps_completed_plan_dependencies_without_replanning(
    failed: tuple[Database, Path, str, AdapterRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, parent_id, registry = failed
    parent = db.get_run(parent_id)
    assert parent
    plan = persist_plan(
        db,
        parent_id,
        parent.task,
        TaskSizingAssessment(10, ("test",), 2, True),
        [
            ProposedPackage("Saved feature", "Build saved feature", ("Feature exists",)),
            ProposedPackage("Repair gate", "Fix gate", ("Gate passes",), (0,)),
        ],
        parent.base_commit,
    )
    db.update_plan_step(plan.steps[0].id, PlanStepStatus.COMPLETED)
    db.update_plan_step(plan.steps[1].id, PlanStepStatus.FAILED, error="Gate failed")
    original_steps = db.get_plan_steps(parent_id)
    supervisor, _ = launch_without_process(db, registry, monkeypatch)
    config = prepare_continuation(db, parent_id)
    assert config.continuation
    child_id = supervisor.start_supervised_run(config).run_id
    sha = config.continuation["candidate_sha"]
    db.inherit_continuation_plan(parent_id, child_id, sha)
    from saberops.orchestrator import _adopt_durable_plan_for_recovery

    steps = _adopt_durable_plan_for_recovery(db, child_id)
    assert steps and len(steps) == 2
    assert steps[0].status == PlanStepStatus.COMPLETED
    assert steps[1].status == PlanStepStatus.READY
    assert steps[1].dependency_ids == (steps[0].id,)
    assert steps[1].error is None and steps[1].attempt_id is None
    packages = db.get_work_packages(child_id)
    assert len(packages) == 2
    by_id = {p.id: p for p in packages}
    assert steps[0].work_package_id and steps[1].work_package_id
    assert by_id[steps[1].work_package_id].dependency_package_ids == (steps[0].work_package_id,)
    for package in packages:
        assert package.base_candidate_sha == sha
        assert json.loads(package.context_capsule_json or "{}")["base_candidate_sha"] == sha
    assert db.get_plan_steps(parent_id) == original_steps
    db.inherit_continuation_plan(parent_id, child_id, sha)
    assert db.get_plan_steps(child_id) == list(steps)
    assert len(db.get_events_by_type(child_id, "plan_created")) == 1

    repaired: list[str] = []

    def repair(request: WorkerRequest) -> WorkerResult:
        assert (request.worktree_path / "features.json").is_file()
        assert GitManager.get_head_commit(request.worktree_path) == sha
        assert "Fix gate" in request.task and "No rule to make target" in request.task
        repaired.append(request.task)
        (request.worktree_path / "Makefile").write_text(".PHONY: gate\ngate:\n\t@true\n")
        return WorkerResult(
            "codex",
            request.model,
            AttemptStatus.SUCCESS,
            "Repaired",
            "Gate fixed",
            "",
            has_changes=True,
            execution_quiescent=True,
        )

    def review(self: ReviewEngine, run_id: str, **kwargs: Any) -> ReviewResult:
        result = ReviewResult(
            id="plan-continue-review",
            run_id=run_id,
            provider="fake-reviewer",
            model="independent",
            verdict=ReviewVerdict.PASS,
            summary="PASS",
            details="PASS",
        )
        db.create_review(result)
        return result

    monkeypatch.setattr(registry.get("codex"), "run", repair)
    monkeypatch.setattr(ReviewEngine, "review_run", review)
    child = db.get_run(child_id)
    assert child and child.config_json
    outcome = Orchestrator(db=db, registry=registry).run(
        config=reconstruct_run_config(json.loads(child.config_json)), run_id=child_id
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(repaired) == 1
    assert all(s.status == PlanStepStatus.COMPLETED for s in db.get_plan_steps(child_id))
    assert db.get_plan_steps(parent_id) == original_steps
