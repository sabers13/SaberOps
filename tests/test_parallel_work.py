"""Real Git/lifecycle tests; controlled workers never invoke paid providers."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.accept import AcceptEngine
from saberops.config import RunConfig, reconstruct_run_config
from saberops.continuation import prepare_continuation
from saberops.db import Database
from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    ReviewResult,
    ReviewVerdict,
    RunStatus,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.parallel_work import git_projection, scopes_overlap, team_children, validate_team_plan
from saberops.review import ReviewEngine
from saberops.supervisor import RunSupervisor
from saberops.workers.codex import CodexAdapter
from saberops.workers.registry import AdapterRegistry


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def plan_item(
    index: int, paths: list[str] | None = None, deps: list[int] | None = None
) -> dict[str, Any]:
    return {
        "title": f"Feature {index}",
        "goal": f"Implement feature {index} with unit checks.",
        "acceptance_criteria": [f"Feature {index} works"],
        "relevant_paths": paths or [f"feature{index}.txt"],
        "dependency_indexes": deps or [],
    }


@pytest.fixture
def team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]]:
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
    git(repo, "config", "user.name", "Team Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "README.md").write_text("Original project\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    db = Database(tmp_path / "project-state" / "runs.db")
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "is_available", lambda: True)
    registry = AdapterRegistry([adapter])
    threads: list[threading.Thread] = []

    def launch(supervisor: RunSupervisor, run_id: str, config: RunConfig) -> None:
        def execute() -> None:
            Orchestrator(db=db, registry=registry, worktree_base=tmp_path / "trees").run(
                config=config, run_id=run_id
            )

        worker = threading.Thread(target=execute)
        threads.append(worker)
        worker.start()

    monkeypatch.setattr(RunSupervisor, "_launch", launch)
    config = RunConfig(
        task="Build two useful features",
        target_repo=str(repo),
        provider_override="codex",
        model_override="test-model",
        max_attempts=1,
        gate_command="true",
        review_disabled=True,
        execution_mode="parallel",
        max_parallel_workers=2,
        parallel_plan=validate_team_plan([plan_item(0), plan_item(1)]),
    )
    return db, repo, config, registry, threads


def result(request: WorkerRequest) -> WorkerResult:
    return WorkerResult(
        "codex",
        request.model,
        AttemptStatus.SUCCESS,
        "Implemented",
        "Test evidence",
        "",
        has_changes=True,
        execution_quiescent=True,
    )


def run_team(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    config: RunConfig | None = None,
) -> str:
    db, repo, default, registry, threads = team
    run = Orchestrator(db=db, registry=registry, worktree_base=repo.parent / "trees").run(
        config=config or default
    )
    for worker in threads:
        worker.join(timeout=10)
        assert not worker.is_alive()
    return run.run_id


def test_workers_overlap_merge_in_plan_order_and_parent_has_full_candidate(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, repo, config, registry, _ = team
    original = GitManager.get_head_commit(repo)
    barrier = threading.Barrier(2, timeout=10)
    final_seen: list[str] = []

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task: Feature" in request.task:
            index = 0 if "Team task: Feature 0" in request.task else 1
            barrier.wait()
            if index == 0:
                time.sleep(0.2)  # Completion order must not become merge order.
            (request.worktree_path / f"feature{index}.txt").write_text(f"Feature {index}\n")
        else:
            assert (request.worktree_path / "feature0.txt").is_file()
            assert (request.worktree_path / "feature1.txt").is_file()
            final_seen.append(request.task)
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    run_id = run_team(team)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.COMPLETED, parent.result_summary if parent else ""
    assert len(final_seen) == 1
    children = team_children(db, run_id)
    assert len(children) == 2 and len(db.list_runs()) == 1
    merges = [
        e.payload
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "team_worker_integrated"
    ]
    assert [m["task_index"] for m in merges if m] == [0, 1]
    candidate = db.get_attempts_for_run(run_id)[-1]
    assert candidate.commit_sha
    assert set(git(repo, "diff", "--name-only", original, candidate.commit_sha).splitlines()) == {
        "feature0.txt",
        "feature1.txt",
    }
    assert GitManager.get_head_commit(repo) == original
    assert not (repo / "feature0.txt").exists()
    projection = git_projection(db, parent)
    assert [t["status"] for t in projection["threads"]] == ["Integrated", "Integrated"]
    assert projection["target"] == GitManager.get_current_branch(repo)
    assert projection["integration"]["branch"] == f"orch/{run_id}/integration"
    for child_id in children:
        child = db.get_run(child_id)
        assert child and child.config_json and parent.config_json
        frozen = reconstruct_run_config(json.loads(child.config_json))
        assert frozen.parallel_parent_run_id == run_id and frozen.execution_mode == "single"
        assert frozen.gate_command == config.gate_command and not frozen.publish_candidate
        assert child.control_plane_snapshot_json == parent.control_plane_snapshot_json
        assert child.tool_contract_json == parent.tool_contract_json
        assert not AcceptEngine(db=db).check_accept_eligibility(child_id).eligible
        with pytest.raises(RuntimeError, match="combined parent"):
            AcceptEngine(db=db).accept_run(child_id)


def test_dependency_waits_for_integrated_prerequisites(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _, config, registry, _ = team
    config = replace(
        config,
        parallel_plan=validate_team_plan([plan_item(0), plan_item(1), plan_item(2, deps=[0, 1])]),
    )

    def implement(request: WorkerRequest) -> WorkerResult:
        for index in range(3):
            if f"Team task: Feature {index}" in request.task:
                if index == 2:
                    assert (request.worktree_path / "feature0.txt").is_file()
                    assert (request.worktree_path / "feature1.txt").is_file()
                (request.worktree_path / f"feature{index}.txt").write_text(str(index))
                break
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    run_id = run_team(team, config)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.COMPLETED, parent.result_summary if parent else ""
    waves = [
        e.payload
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "team_wave_started"
    ]
    assert [w["tasks"] for w in waves if w] == [[0, 1], [2]]


def test_overlapping_file_ownership_is_serialized(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _, config, registry, _ = team
    config = replace(
        config,
        parallel_plan=validate_team_plan(
            [plan_item(0, ["shared.txt"]), plan_item(1, ["shared.txt"])]
        ),
    )

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task:" in request.task:
            file = request.worktree_path / "shared.txt"
            index = 0 if "Team task: Feature 0" in request.task else 1
            if index == 1:
                assert file.read_text() == "0"
            file.write_text(str(index))
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    run_id = run_team(team, config)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.COMPLETED, parent.result_summary if parent else ""
    waves = [
        e.payload
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "team_wave_started"
    ]
    assert [w["tasks"] for w in waves if w] == [[0], [1]]


def test_worker_outside_scope_is_preserved_but_never_integrated(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, repo, _, registry, _ = team
    original = GitManager.get_head_commit(repo)

    def implement(request: WorkerRequest) -> WorkerResult:
        (request.worktree_path / "unowned.txt").write_text("Wrong task\n")
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    run_id = run_team(team)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.FAILED
    assert "another task" in (parent.result_summary or "")
    assert not any(
        e.event_type == "team_worker_integrated" for e in db.get_complete_events_for_run(run_id)
    )
    assert GitManager.get_head_commit(repo) == original
    assert all(db.get_attempts_for_run(child)[-1].commit_sha for child in team_children(db, run_id))


@pytest.mark.parametrize("bad_path", ["/tmp/a", "../a", ".git/config", "src/*", "a//b", "a/../b"])
def test_reject_unsafe_planner_paths(bad_path: str) -> None:
    with pytest.raises(ValueError):
        validate_team_plan([plan_item(0, [bad_path]), plan_item(1)])


def test_reject_cycles_duplicate_objectives_and_invalid_dependencies() -> None:
    for raw in (
        [plan_item(0, deps=[1]), plan_item(1, deps=[0])],
        [plan_item(0), plan_item(0)],
        [plan_item(0, deps=[9]), plan_item(1)],
    ):
        with pytest.raises(ValueError):
            validate_team_plan(raw)
    assert scopes_overlap(["src/**"], ["src/api.py"])
    assert scopes_overlap(["src/**"], ["src/backend/**"])
    assert not scopes_overlap(["src/**"], ["docs/**"])


def test_final_review_receives_the_full_combined_delta(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, repo, config, registry, _ = team
    config = replace(config, review_disabled=False, review_enabled=True)
    reviewed: list[str] = []

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task:" in request.task:
            index = 0 if "Team task: Feature 0" in request.task else 1
            (request.worktree_path / f"feature{index}.txt").write_text(str(index))
        return result(request)

    def review(self: ReviewEngine, run_id: str, **kwargs: Any) -> ReviewResult:
        run = db.get_run(run_id)
        assert run and run.config_json
        candidate = db.get_attempts_for_run(run_id)[-1]
        assert db.get_checks_for_attempt(candidate.id)[-1].passed
        if json.loads(run.config_json)["execution_mode"] == "parallel":
            assert candidate.commit_sha
            paths = git(repo, "diff", "--name-only", run.base_commit, candidate.commit_sha)
            assert set(paths.splitlines()) == {"feature0.txt", "feature1.txt"}
        reviewed.append(run_id)
        verdict = ReviewResult(
            id="review-" + run_id,
            run_id=run_id,
            provider="test-reviewer",
            model="independent",
            verdict=ReviewVerdict.PASS,
            summary="Full delta reviewed",
            details="PASS",
        )
        db.create_review(verdict)
        return verdict

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    monkeypatch.setattr(ReviewEngine, "review_run", review)
    run_id = run_team(team, config)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.COMPLETED, parent.result_summary if parent else ""
    assert reviewed[-1] == run_id and len(reviewed) == 3
    eligibility = AcceptEngine(db=db).check_accept_eligibility(run_id)
    assert eligibility.eligible, eligibility.checks
    # Altered integration receipts cannot substitute a foreign candidate.
    import sqlite3

    integrated = next(
        e for e in db.get_complete_events_for_run(run_id) if e.event_type == "team_integrated"
    )
    assert integrated.payload
    forged = {**integrated.payload, "candidate_sha": parent.base_commit}
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE events SET payload_json=? WHERE id=?", (json.dumps(forged), integrated.id)
        )
    assert not AcceptEngine(db=db).check_accept_eligibility(run_id).eligible
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE events SET payload_json=? WHERE id=?",
            (json.dumps(integrated.payload), integrated.id),
        )
    # The real acceptance engine, against this disposable test repository only.
    candidate = db.get_attempts_for_run(run_id)[-1]
    AcceptEngine(db=db).accept_run(run_id)
    assert GitManager.get_head_commit(repo) == candidate.commit_sha
    assert (repo / "feature0.txt").is_file() and (repo / "feature1.txt").is_file()


def test_combined_gate_failure_blocks_parent_acceptance(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    db, repo, config, registry, _ = team
    gate = (
        f"{sys.executable} -c 'from pathlib import Path; "
        'assert not (Path("feature0.txt").exists() and Path("feature1.txt").exists())\''
    )
    config = replace(config, gate_command=gate)

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task:" in request.task:
            index = 0 if "Team task: Feature 0" in request.task else 1
            (request.worktree_path / f"feature{index}.txt").write_text(str(index))
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    base = GitManager.get_head_commit(repo)
    run_id = run_team(team, config)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.FAILED
    assert not db.get_checks_for_attempt(db.get_attempts_for_run(run_id)[-1].id)[-1].passed
    assert all(
        db.get_run(child).status == RunStatus.COMPLETED  # type: ignore[union-attr]
        for child in team_children(db, run_id)
    )
    assert not AcceptEngine(db=db).check_accept_eligibility(run_id).eligible
    assert GitManager.get_head_commit(repo) == base


def test_missing_gate_is_reported_from_worker_even_after_routing_blocker(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from saberops.models import PlanStepStatus
    from saberops.observability.run_report import (
        build_run_report,
        canonical_report_json,
        render_monitor_frame,
        render_report_text,
        run_explanation,
    )

    if not shutil.which("make"):
        pytest.skip("The real missing-Makefile reproduction requires make")
    db, repo, config, registry, _ = team
    config = replace(config, gate_command="make gate", parallel_plan=validate_team_plan([
        plan_item(0), plan_item(1, deps=[0]),
    ]))
    calls: list[str] = []

    def implement(request: WorkerRequest) -> WorkerResult:
        calls.append(request.task)
        (request.worktree_path / "feature0.txt").write_text("Shared API contract")
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    base = GitManager.get_head_commit(repo)
    run_id = run_team(team, config)
    assert len(calls) == 1
    steps = db.get_plan_steps(run_id)
    assert [step.status for step in steps] == [PlanStepStatus.FAILED, PlanStepStatus.BLOCKED]
    child_id = team_children(db, run_id)[0]
    saved = db.get_attempts_for_run(child_id)[-1].commit_sha
    assert saved
    # A later routing blocker must not erase the preceding implementation/check.
    db.record_event(child_id, "run_failed", payload={
        "outcome": "NO_ELIGIBLE_CANDIDATE",
        "summary": "Fallback readiness UNKNOWN (STALE_READINESS_EVIDENCE)",
        "skipped_candidates": [{"model": "fallback", "readiness_state": "UNKNOWN"}],
    })
    report = build_run_report(db, run_id)
    assert report["execution"]["worker_dispatch_count"] == 0
    assert report["gate"]["summary"]["check_total"] == 0
    assert report["team"]["execution"]["worker_dispatch_count"] == 1
    assert report["team"]["gate"]["check_failed"] == 1
    worker = report["team"]["workers"][0]
    assert worker["preserved_attempts"][0]["commit_sha"] == saved
    assert "No rule to make target" in worker["last_failed_check"]["stderr"]
    assert report["acceptance"]["acceptance_status"] == "NOT_ACCEPTED"
    assert report["identity"]["candidate_sha"] == "UNKNOWN"
    explanation = run_explanation(report)
    assert "No worker was dispatched" not in explanation
    assert explanation.index("No rule to make target") < explanation.index("STALE_READINESS")
    assert "Team (parent and linked workers)" in render_monitor_frame(report)
    assert "check_failed 1" in render_report_text(report)
    assert canonical_report_json(report) == canonical_report_json(build_run_report(db, run_id))
    assert GitManager.get_head_commit(repo) == base
    assert not (repo / "feature0.txt").exists()


def test_actual_git_conflict_preserves_worker_branches_and_stops_integration(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import saberops.parallel_work as module
    from saberops.process import ProcessResult, run_process

    db, repo, _, registry, _ = team
    real_process = run_process
    injected = False

    def implement(request: WorkerRequest) -> WorkerResult:
        index = 0 if "Team task: Feature 0" in request.task else 1
        (request.worktree_path / f"feature{index}.txt").write_text("Worker version")
        return result(request)

    def process(args: list[str], **kwargs: Any) -> ProcessResult:
        nonlocal injected
        if "--no-ff" in args and not injected:
            injected = True
            integration = Path(kwargs["cwd"])
            (integration / "feature0.txt").write_text("Concurrent external version")
            git(integration, "add", ".")
            git(integration, "commit", "-m", "Concurrent integration edit")
        return real_process(args, **kwargs)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    monkeypatch.setattr(module, "run_process", process)
    base = GitManager.get_head_commit(repo)
    run_id = run_team(team)
    parent = db.get_run(run_id)
    assert parent and parent.status == RunStatus.FAILED
    projection = git_projection(db, parent)
    assert projection["conflicts"][0]["files"] == ["feature0.txt"]
    assert not any(t["status"] == "Integrated" for t in projection["threads"])
    assert git(repo, "branch", "--list", f"orch/{run_id}/integration")
    assert all(db.get_attempts_for_run(child)[-1].commit_sha for child in team_children(db, run_id))
    assert GitManager.get_head_commit(repo) == base


@pytest.mark.parametrize(
    "change",
    [
        {"execution_mode": "unknown"},
        {"max_parallel_workers": 99},
        {"parallel_plan": ()},
        {"parallel_parent_run_id": "missing-parent", "parallel_task_index": 0},
    ],
)
def test_invalid_team_configuration_never_launches_workers(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    change: dict[str, Any],
) -> None:
    db, _, config, registry, threads = team
    with pytest.raises(ValueError):
        RunSupervisor(db=db, registry=registry).start_supervised_run(replace(config, **change))
    assert threads == [] and db.list_runs() == []


def test_failed_combined_candidate_continues_without_relaunching_team(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    db, repo, config, registry, threads = team
    gate = (
        f"{sys.executable} -c 'from pathlib import Path; "
        'assert not (Path("feature0.txt").exists() and '
        'Path("feature1.txt").exists() and Path("feature1.txt").read_text()=="1")\''
    )
    config = replace(config, gate_command=gate)

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task:" in request.task:
            index = 0 if "Team task: Feature 0" in request.task else 1
            (request.worktree_path / f"feature{index}.txt").write_text(str(index))
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    failed_id = run_team(team, config)
    failed = db.get_run(failed_id)
    assert failed and failed.status == RunStatus.FAILED
    continuation = prepare_continuation(db, failed_id)
    calls: list[str] = []

    def repair(request: WorkerRequest) -> WorkerResult:
        assert "Team task:" not in request.task
        assert (request.worktree_path / "feature0.txt").read_text() == "0"
        assert (request.worktree_path / "feature1.txt").read_text() == "1"
        calls.append(request.task)
        (request.worktree_path / "feature1.txt").write_text("1 corrected")
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", repair)
    child_id = RunSupervisor(db=db, registry=registry).start_supervised_run(continuation).run_id
    for worker in threads:
        worker.join(timeout=20)
        assert not worker.is_alive()
    continued = db.get_run(child_id)
    assert continued and continued.status == RunStatus.COMPLETED, (
        continued.result_summary if continued else ""
    )
    assert len(calls) == 1 and not team_children(db, child_id)
    eligibility = AcceptEngine(db=db).check_accept_eligibility(child_id)
    assert eligibility.eligible, eligibility.checks
    assert git_projection(db, continued)["source_run_id"] == failed_id
    assert GitManager.get_head_commit(repo) == failed.base_commit


def test_parent_stop_cancels_durable_children_before_parent(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from saberops.ownership import ProcessIdentity

    db, repo, config, registry, _ = team
    monkeypatch.setattr(RunSupervisor, "_launch", lambda *args: None)
    supervisor = RunSupervisor(db=db, registry=registry)
    parent_id = supervisor.start_supervised_run(config).run_id
    db.update_run(parent_id, RunStatus.RUNNING)
    integration = GitManager.create_worktree(
        repo,
        f"orch/{parent_id}/integration",
        repo.parent / "integration",
        GitManager.get_head_commit(repo),
    )
    children: list[str] = []
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for index, identity in enumerate([parent_id, None, None]):
            if identity is None:
                identity = supervisor.start_supervised_run(
                    replace(
                        config,
                        target_repo=str(integration),
                        task=f"Implement feature {index}",
                        execution_mode="single",
                        parallel_plan=(),
                        parallel_parent_run_id=parent_id,
                        parallel_task_index=index - 1,
                    )
                ).run_id
                children.append(identity)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            owner = ProcessIdentity.for_pid(process.pid)
            assert owner
            db.claim_execution_owner(identity, identity=owner)
        assert supervisor.cancel(parent_id)
        for process in processes:
            process.wait(timeout=5)
        assert all(
            db.get_run(identity).status == RunStatus.FAILED  # type: ignore[union-attr]
            for identity in [parent_id, *children]
        )
        assert all(
            db.get_execution_owner(identity).released_at  # type: ignore[union-attr]
            for identity in [parent_id, *children]
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_child_claim_refuses_parent_cancellation_atomically(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.ownership import OwnershipError, ProcessIdentity

    db, repo, config, registry, _ = team
    monkeypatch.setattr(RunSupervisor, "_launch", lambda *args: None)
    supervisor = RunSupervisor(db=db, registry=registry)
    parent_id = supervisor.start_supervised_run(config).run_id
    db.update_run(parent_id, RunStatus.RUNNING)
    integration = GitManager.create_worktree(
        repo,
        f"orch/{parent_id}/integration",
        repo.parent / "integration",
        GitManager.get_head_commit(repo),
    )
    child_id = supervisor.start_supervised_run(
        replace(
            config,
            target_repo=str(integration),
            task="Implement one feature",
            execution_mode="single",
            parallel_plan=(),
            parallel_parent_run_id=parent_id,
            parallel_task_index=0,
        )
    ).run_id
    owner = db.claim_execution_owner(parent_id, identity=ProcessIdentity.for_self())
    assert db.begin_execution_cancellation(
        parent_id, owner_id=owner.owner_id, generation=owner.generation, agent=owner.identity
    )
    with pytest.raises(OwnershipError, match="Parent team is stopping"):
        db.claim_execution_owner(child_id, identity=ProcessIdentity.for_self())
    assert db.get_execution_owner(child_id) is None


def test_team_children_link_from_minimal_status_pages(
    team: tuple[Database, Path, RunConfig, AdapterRegistry, list[threading.Thread]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from saberops.web import create_app

    db, repo, _, registry, _ = team

    def implement(request: WorkerRequest) -> WorkerResult:
        if "Team task:" in request.task:
            index = 0 if "Team task: Feature 0" in request.task else 1
            (request.worktree_path / f"feature{index}.txt").write_text(str(index))
        return result(request)

    monkeypatch.setattr(registry.get("codex"), "run", implement)
    run_id = run_team(team)
    children = team_children(db, run_id)
    assert len(children) == 2
    app = create_app(db_path=db.db_path, repo_path=repo, registry=registry)
    with TestClient(app) as client:
        assert client.get(f"/runs/{children[0]}").status_code == 200
        assert client.get("/").status_code == 200
        assert db.list_runs()[0].id == run_id and len(db.list_runs()) == 1
